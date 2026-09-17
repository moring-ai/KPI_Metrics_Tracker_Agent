"""Loading secrets from .env.

Small, but it sits directly in front of every credential, so the two things
that matter get tests: a real environment variable must always win over the
file (that is what makes CI secrets work), and no value may ever be logged.
"""

import logging

import pytest

from kpi_tracker import envfile


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("TEST_TOKEN", "TEST_OTHER", "TEST_QUOTED"):
        monkeypatch.delenv(key, raising=False)


def write(tmp_path, body):
    path = tmp_path / ".env"
    path.write_text(body)
    return path


def test_a_plain_assignment_is_loaded(tmp_path, monkeypatch):
    import os

    assert envfile.load(write(tmp_path, "TEST_TOKEN=abc123\n")) == ["TEST_TOKEN"]
    assert os.environ["TEST_TOKEN"] == "abc123"


def test_a_real_environment_variable_always_wins(tmp_path, monkeypatch):
    """CI injects secrets as env vars; a stray committed .env must not win."""
    import os

    monkeypatch.setenv("TEST_TOKEN", "from-the-environment")
    assert envfile.load(write(tmp_path, "TEST_TOKEN=from-the-file\n")) == []
    assert os.environ["TEST_TOKEN"] == "from-the-environment"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    body = "# a comment\n\n   \nTEST_TOKEN=abc\n#TEST_OTHER=nope\n"
    assert envfile.load(write(tmp_path, body)) == ["TEST_TOKEN"]


def test_an_export_prefix_is_accepted(tmp_path):
    import os

    envfile.load(write(tmp_path, "export TEST_TOKEN=abc\n"))
    assert os.environ["TEST_TOKEN"] == "abc"


@pytest.mark.parametrize("raw,expected", [
    ('TEST_QUOTED="abc"', "abc"),
    ("TEST_QUOTED='abc'", "abc"),
    ("TEST_QUOTED=abc", "abc"),
    ("TEST_QUOTED=  abc  ", "abc"),
])
def test_quotes_and_padding_are_stripped(tmp_path, raw, expected):
    import os

    envfile.load(write(tmp_path, raw + "\n"))
    assert os.environ["TEST_QUOTED"] == expected


def test_a_dollar_sign_in_a_token_survives(tmp_path):
    """No interpolation -- a token is an opaque string, not a shell fragment."""
    import os

    envfile.load(write(tmp_path, "TEST_TOKEN=abc$HOME$def\n"))
    assert os.environ["TEST_TOKEN"] == "abc$HOME$def"


def test_an_empty_value_is_not_set(tmp_path):
    """The shipped .env.example has blank values; loading '' would look set."""
    import os

    assert envfile.load(write(tmp_path, "TEST_TOKEN=\n")) == []
    assert "TEST_TOKEN" not in os.environ


def test_a_missing_file_is_fine(tmp_path):
    assert envfile.load(tmp_path / "nope") == []


def test_a_malformed_line_is_skipped_without_its_content_being_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert envfile.load(write(tmp_path, "this is not an assignment\nTEST_TOKEN=ok\n")) == [
            "TEST_TOKEN"
        ]
    assert "not an assignment" not in caplog.text  # never echo file content
    assert "not KEY=VALUE" in caplog.text


def test_no_value_is_ever_returned_or_logged(tmp_path, caplog):
    """load() returns names only. A caller cannot accidentally log a secret."""
    secret = "xoxb-super-secret-value-9876543210"
    with caplog.at_level(logging.DEBUG):
        names = envfile.load(write(tmp_path, f"TEST_TOKEN={secret}\n"))
    assert names == ["TEST_TOKEN"]
    assert secret not in caplog.text
    assert secret not in str(names)
