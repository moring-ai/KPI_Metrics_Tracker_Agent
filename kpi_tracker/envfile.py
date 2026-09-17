"""Read secrets from a .env file into the environment.

Used by the MCP SERVERS for local development, so you can run them on a laptop
without an AWS account. On EC2 they read AWS Secrets Manager instead and
nothing is on disk.

The CLIENT deliberately does not use this. It holds no credential, so it never
reads a credential file -- one fewer way for the isolation to be undone by
accident. `kpi-tracker check` fails outright if it finds a token in its own
environment.

Fifteen lines of stdlib rather than a dependency. It handles what a .env file
actually contains: comments, blank lines, an optional `export` prefix, and
quoted values. It does NOT do variable interpolation -- a token containing a
literal `$` should survive unchanged.

A real environment variable always wins over the file, which is what makes CI
work: the GitHub Actions workflow injects secrets as env vars, and a stray
committed .env can never override them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load(path: str | Path = ".env") -> list[str]:
    """Set any variable named in `path` that is not already in the environment.

    Returns the names it set, so the caller can log what happened without
    logging any values.
    """
    path = Path(path)
    if not path.exists():
        return []

    applied = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            log.warning("%s:%d is not KEY=VALUE, ignoring", path, number)
            continue

        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if not key or not value:
            continue
        if key in os.environ:
            continue  # a real environment variable always wins

        os.environ[key] = value
        applied.append(key)

    return applied
