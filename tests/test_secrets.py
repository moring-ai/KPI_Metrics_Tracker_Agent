"""The secret providers used by the MCP servers.

The property worth protecting is narrow and important: an error about a
credential must never contain the credential. These strings go to journald and
into `check` output.
"""

import pytest

from kpi_mcp import secrets


def test_env_secrets_reads_the_environment(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "abc123")
    assert secrets.EnvSecrets().get("TEST_TOKEN") == "abc123"


def test_a_missing_env_secret_names_the_variable_and_the_backend(monkeypatch):
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    with pytest.raises(secrets.SecretUnavailable, match="TEST_TOKEN"):
        secrets.EnvSecrets().get("TEST_TOKEN")


def test_a_blank_env_secret_counts_as_missing(monkeypatch):
    """The shipped .env.example has empty values; treating '' as set would
    hand the vendor an empty token and produce a confusing 401."""
    monkeypatch.setenv("TEST_TOKEN", "   ")
    with pytest.raises(secrets.SecretUnavailable):
        secrets.EnvSecrets().get("TEST_TOKEN")


@pytest.mark.parametrize("backend,expected", [("env", "env"), ("aws", "aws"), ("ENV", "env")])
def test_the_backend_is_chosen_by_one_environment_variable(monkeypatch, backend, expected):
    monkeypatch.setenv(secrets.BACKEND_ENV_VAR, backend)
    assert secrets.from_environment().name == expected


def test_the_default_backend_is_env_not_aws(monkeypatch):
    """So the production path is never accidentally live on a laptop."""
    monkeypatch.delenv(secrets.BACKEND_ENV_VAR, raising=False)
    assert secrets.from_environment().name == "env"


def test_an_unknown_backend_is_refused(monkeypatch):
    monkeypatch.setenv(secrets.BACKEND_ENV_VAR, "vault")
    with pytest.raises(secrets.SecretUnavailable, match="not understood"):
        secrets.from_environment()


# -- the AWS failure explainer -------------------------------------------


class AccessDeniedException(Exception):
    pass


class ResourceNotFoundException(Exception):
    pass


class NoCredentialsError(Exception):
    pass


def test_a_boto_exceptions_own_text_is_never_echoed():
    """botocore messages can quote the request that failed, headers included.
    The error class is the useful part; the text is a liability."""
    leaky = AccessDeniedException(
        "An error occurred: BRIGHTDATA_API_TOKEN=deadbeef-0000-4000-8000-000000000000"
    )
    explained = secrets._explain_aws_failure("kpi/brightdata", leaky)

    assert "deadbeef" not in explained
    assert "An error occurred" not in explained
    assert "iam-policy.json" in explained, "it should say how to fix it"


@pytest.mark.parametrize("exc,expected", [
    (AccessDeniedException("x"), "not allowed"),
    (ResourceNotFoundException("x"), "does not exist"),
    (NoCredentialsError("x"), "no IAM role"),
])
def test_each_aws_failure_gets_a_fix_not_a_stack_trace(exc, expected):
    assert expected in secrets._explain_aws_failure("kpi/brightdata", exc)


def test_an_unrecognised_aws_failure_still_says_nothing_secret():
    class SomethingNew(Exception):
        pass

    explained = secrets._explain_aws_failure(
        "kpi/brightdata", SomethingNew("token=abcdef1234567890 was rejected")
    )
    assert "abcdef1234567890" not in explained
    assert "SomethingNew" in explained


def test_the_secret_id_is_shown_because_it_is_not_secret():
    """The ARN or name is an identifier worth logging; only the value is secret."""
    assert "kpi/brightdata" in secrets._explain_aws_failure(
        "kpi/brightdata", ResourceNotFoundException("x")
    )
