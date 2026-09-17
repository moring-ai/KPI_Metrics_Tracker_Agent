"""Every shell script in the repo must parse.

Added after introducing the same class of bug twice. Both were invisible until
someone ran the script -- which, for a deployment script, means on the box,
during a deploy. `bash -n` parses without executing, so this costs nothing and
catches the whole class.

The one that prompted it: an apostrophe inside ${VAR:?word} opens a quote
context even within double quotes, so bash runs off the end of the file looking
for its pair. `${LAMBDA_ARN:?set the invoke lambda's ARN}` is a syntax error,
and reads as perfectly ordinary English.
"""

import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).parent.parent
SCRIPTS = sorted(
    p for p in REPO.rglob("*.sh")
    if ".venv" not in p.parts and "node_modules" not in p.parts
)


def test_there_are_scripts_to_check():
    """A glob that silently matches nothing would make every case below pass."""
    assert SCRIPTS, "no shell scripts found -- has the layout changed?"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash is not available")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_parses(script):
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"{script.relative_to(REPO)} is not valid bash:\n{result.stderr}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_is_executable_and_has_a_shebang(script):
    """A deploy script that is not executable fails at the worst moment."""
    assert script.read_text().startswith("#!"), f"{script.name} has no shebang"
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable (chmod +x)"
