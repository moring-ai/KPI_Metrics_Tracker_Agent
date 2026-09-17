"""The architecture, enforced.

Two rules, and a test each, because both are the kind of thing that decays the
first time someone adds a convenient import:

  1. kpi_tracker (the client) must never import kpi_mcp, mcp or boto3 at module
     scope. If it does, the client process gains the ability to talk to Secrets
     Manager -- and "the agent does not hold credentials" stops being a
     structural property and becomes a habit.

  2. The offline test suite must stay offline and fast. mcp drags in pydantic,
     starlette, uvicorn, httpx2 and opentelemetry; boto3 drags in botocore.
     Paying for those on every test run is how a 0.2s suite becomes a 3s one
     that people stop running.
"""

import ast
import pathlib
import subprocess
import sys

CLIENT = pathlib.Path(__file__).parent.parent / "kpi_tracker"
FORBIDDEN_ON_THE_CLIENT_PATH = {"kpi_mcp", "mcp", "boto3", "botocore", "slack_sdk"}


def module_level_imports(path: pathlib.Path) -> set[str]:
    """Top-level imports only. A lazy import inside a function is fine, and is
    exactly how mcp_call.py keeps mcp off this path."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:  # module scope only, deliberately
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_client_module_imports_the_server_or_its_dependencies():
    offenders = {}
    for path in sorted(CLIENT.rglob("*.py")):
        bad = module_level_imports(path) & FORBIDDEN_ON_THE_CLIENT_PATH
        if bad:
            offenders[path.name] = sorted(bad)
    assert offenders == {}, (
        f"client modules importing server-side packages at module scope: {offenders}. "
        "Move the import inside the function that needs it, as mcp_call.py does."
    )


def test_importing_the_whole_client_does_not_load_mcp_or_boto3():
    """The proof that matters: import everything and check sys.modules."""
    code = (
        "import importlib, sys, pathlib\n"
        "root = pathlib.Path('kpi_tracker')\n"
        "for p in sorted(root.rglob('*.py')):\n"
        "    if p.name == '__main__.py':\n"
        "        continue\n"
        "    name = '.'.join(p.with_suffix('').parts)\n"
        "    importlib.import_module(name)\n"
        "leaked = sorted({'mcp', 'boto3', 'botocore'} & set(sys.modules))\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=CLIENT.parent
    )
    assert out.returncode == 0, out.stderr
    assert "LEAKED:" in out.stdout
    leaked = out.stdout.split("LEAKED:")[1].strip()
    assert leaked == "", f"importing the client pulled in {leaked}"


def test_the_server_may_import_the_client():
    """The dependency points one way. The servers reuse the client's tested
    identity helpers rather than duplicating them."""
    server = pathlib.Path(__file__).parent.parent / "kpi_mcp" / "brightdata.py"
    assert "kpi_tracker" in module_level_imports(server)
