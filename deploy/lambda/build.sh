#!/bin/bash
# Build the Lambda deployment zip.
#
#   bash deploy/lambda/build.sh            -> dist/kpi-tracker-lambda.zip
#
# THE ONE REAL GOTCHA: pydantic-core is a compiled Rust extension, so `pip
# install` on a Mac produces macOS wheels that cannot run on Lambda. This
# script forces Linux aarch64 wheels explicitly, which is why it works from a
# laptop without Docker.
#
# Target: Python 3.12 on arm64 (Graviton -- cheaper, and the same architecture
# family as the t4g host).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
BUILD_DIR="${BUILD_DIR:-build/lambda}"
OUT="${OUT:-dist/kpi-tracker-lambda.zip}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PLATFORM="${PLATFORM:-manylinux2014_aarch64}"

rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR" "$(dirname "$OUT")"

echo "== Dependencies ($PLATFORM, py$PYTHON_VERSION)"
# --only-binary=:all: makes a source-only package a hard error rather than a
# silent local build that produces the wrong architecture.
# uv resolves and downloads far faster than pip and understands the same
# platform flags; fall back to pip when uv is not installed.
if command -v uv >/dev/null; then
  uv pip install \
    --python-platform aarch64-manylinux2014 \
    --python-version "$PYTHON_VERSION" \
    --only-binary=:all: \
    --target "$BUILD_DIR" \
    --quiet \
    -r deploy/lambda/requirements-lambda.txt
else
  python3 -m pip install \
    --platform "$PLATFORM" \
    --python-version "$PYTHON_VERSION" \
    --implementation cp \
    --only-binary=:all: \
    --target "$BUILD_DIR" \
    --quiet \
    -r deploy/lambda/requirements-lambda.txt
fi

echo "== Application code"
cp lambda_handler.py "$BUILD_DIR/"
cp -R kpi_tracker kpi_mcp "$BUILD_DIR/"
# The Lambda config, not the EC2 one: /var/task is read-only, so the
# history path must be an S3 URI and the MCP servers run in-process.
cp deploy/lambda/config.lambda.json "$BUILD_DIR/config.json"
mkdir -p "$BUILD_DIR/data"
cp data/members.json "$BUILD_DIR/data/"

# Trim what is genuinely dead weight.
#
# *.dist-info is NOT dead weight and must stay. httpx2/__version__.py runs
# `importlib.metadata.version("httpx2")` at import time, so deleting its
# metadata makes every invocation fail with PackageNotFoundError before any
# config is read -- to save about a megabyte against a 50 MB limit. Several
# other packages do the same thing.
find "$BUILD_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name 'tests' -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" \( -name '*.pyc' -o -name '*.pyi' \) -exec rm -rf {} + 2>/dev/null || true

echo "== Zipping"
rm -f "$OUT"
(cd "$BUILD_DIR" && zip -qr "$OLDPWD/$OUT" .)

SIZE_MB=$(( $(wc -c < "$OUT") / 1024 / 1024 ))
UNZIPPED_MB=$(( $(du -sk "$BUILD_DIR" | cut -f1) / 1024 ))
echo
echo "  $OUT"
echo "  ${SIZE_MB} MB zipped · ${UNZIPPED_MB} MB unzipped   (Lambda limits: 50 MB / 250 MB)"

# A zip over 50 MB cannot be uploaded directly and must go via S3. Say so now
# rather than letting the upload fail.
if [ "$SIZE_MB" -ge 50 ]; then
  echo "  NOTE: over 50 MB, so upload via S3 (--code S3Bucket=...,S3Key=...)"
fi

# --- prove it imports ------------------------------------------------------
# The whole class of bug this catches -- a missing dist-info, a pruned module,
# a wrong-architecture wheel -- shows up only as Runtime.ImportModuleError on
# the first invocation, which for a weekly job means next Thursday. Importing
# the entry point here costs a second.
#
# Runs on THIS machine, so it proves the tree is complete and importable; it
# cannot prove the aarch64 binaries load (that needs Lambda or an emulator).
# The compiled ones are checked separately below.
echo "== Smoke test: is the built tree actually importable?"
# Importing lambda_handler alone proves almost nothing -- it imports only
# stdlib at module scope and defers the rest into handler(). So this checks the
# two things that genuinely broke a build:
#
#   1. package METADATA resolves. Several dependencies (httpx2 among them) call
#      importlib.metadata.version() at import time, so a stripped *.dist-info
#      turns every invocation into PackageNotFoundError. Metadata is plain text
#      and platform-independent, so this is checkable from any machine.
#   2. every pure-Python module the handler reaches can be found.
#
# It cannot prove the aarch64 binaries LOAD -- that needs Lambda or an
# emulator. Their architecture is checked separately below.
if python3 - "$BUILD_DIR" <<'SMOKE' 2>/tmp/kpi-smoke.err
import importlib.metadata as md, importlib.util, sys
build = sys.argv[1]
sys.path.insert(0, build)

import lambda_handler
assert callable(lambda_handler.handler), "lambda_handler.handler is not callable"

# Every distribution in the tree must have resolvable metadata.
# The floor matters: with *.dist-info stripped, distributions() returns NOTHING
# and a loop over nothing would pass while shipping a zip that cannot import.
dists = list(md.distributions(path=[build]))
if len(dists) < 10:
    raise SystemExit(
        f"only {len(dists)} distributions have metadata -- expected ~30. "
        "Something stripped *.dist-info; the zip will fail at import."
    )
missing = []
for dist in dists:
    name = dist.metadata["Name"]
    try:
        md.version(name)
    except md.PackageNotFoundError:
        missing.append(name)
if missing:
    raise SystemExit(f"metadata missing for: {missing} -- do not strip *.dist-info")

# And the deferred imports must at least be locatable.
for module in ("mcp", "kpi_mcp.linkedin_server", "kpi_mcp.slack_server",
               "kpi_tracker.pipeline", "kpi_tracker.storage", "slack_sdk", "requests"):
    if importlib.util.find_spec(module) is None:
        raise SystemExit(f"{module} is not in the built tree")
print(f"  metadata resolves for {len(dists)} distributions")
SMOKE
then
  sed 's/^/  /' /tmp/kpi-smoke.err 2>/dev/null || true
  echo "  handler imports and every deferred module is present"
else
  echo "  *** the built tree is not usable:"
  sed 's/^/    /' /tmp/kpi-smoke.err
  echo "  refusing to ship a zip that cannot start"
  exit 1
fi

echo "== Architecture of every compiled extension"
WRONG=$(find "$BUILD_DIR" -name '*.so' -exec file {} \; | grep -vc 'ARM aarch64' || true)
TOTAL=$(find "$BUILD_DIR" -name '*.so' | wc -l | tr -d ' ')
if [ "$WRONG" -gt 0 ]; then
  echo "  *** $WRONG of $TOTAL .so files are not ARM aarch64 -- they will not load in Lambda"
  find "$BUILD_DIR" -name '*.so' -exec file {} \; | grep -v 'ARM aarch64' | sed 's/^/    /'
  exit 1
fi
echo "  all $TOTAL compiled extensions are ARM aarch64"
