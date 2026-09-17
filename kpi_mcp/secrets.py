"""Where the MCP servers get their credentials.

Two implementations behind one tiny interface:

  EnvSecrets  -- reads os.environ. For local development and the test suite.
  AwsSecrets  -- reads AWS Secrets Manager. For the EC2 deployment.

Which one runs is decided by one environment variable, KPI_SECRET_BACKEND, so
the production path is never accidentally active on a laptop and the dev path
is never accidentally active on the VM.

WHY SEPARATE SECRETS RATHER THAN ONE JSON BLOB
----------------------------------------------
Each server reads exactly one secret, so its IAM policy can name that one ARN.
The LinkedIn server physically cannot read the Slack token, and vice versa. A
single secret holding both would mean one compromised server yields both
credentials -- which throws away most of the benefit of splitting them.

ON CACHING
----------
The servers are long-lived and Secrets Manager charges per API call, so a value
is cached for CACHE_TTL_SECONDS. The TTL is what makes rotation work without a
restart: rotate the secret, and within an hour every server picks it up. A
cache that never expires would need a deploy to rotate; no cache at all would
mean a network round trip on every weekly run for no benefit.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Protocol

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600.0

BACKEND_ENV_VAR = "KPI_SECRET_BACKEND"
BACKEND_ENV = "env"
BACKEND_AWS = "aws"


class SecretUnavailable(RuntimeError):
    """The credential could not be fetched. Never contains the credential."""


class SecretProvider(Protocol):
    def get(self, name: str) -> str:
        """Return the secret's value, or raise SecretUnavailable."""
        ...


class EnvSecrets:
    """Read secrets from the environment. Development and tests only."""

    name = "env"

    def get(self, key: str) -> str:
        value = os.environ.get(key, "").strip()
        if not value:
            raise SecretUnavailable(
                f"{key} is not set in the environment (KPI_SECRET_BACKEND=env)"
            )
        return value


class AwsSecrets:
    """Read secrets from AWS Secrets Manager, with a TTL cache.

    On EC2 no credentials are configured anywhere: boto3 picks up the instance
    profile automatically. Nothing about AWS auth is this module's business,
    which is exactly why there is no key handling here to get wrong.
    """

    name = "aws"

    def __init__(self, *, region: str | None = None, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._region = region or os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION"
        )
        self._ttl = ttl
        self._client = None
        self._cache: dict[str, tuple[float, str]] = {}

    def _boto(self):
        if self._client is None:
            import boto3  # imported lazily so the test suite never needs it

            self._client = boto3.client("secretsmanager", region_name=self._region)
        return self._client

    def get(self, secret_id: str) -> str:
        cached = self._cache.get(secret_id)
        if cached and time.monotonic() < cached[0]:
            return cached[1]

        try:
            response = self._boto().get_secret_value(SecretId=secret_id)
        except Exception as exc:  # noqa: BLE001 - translated to one clear message below
            raise SecretUnavailable(_explain_aws_failure(secret_id, exc)) from None

        value = (response.get("SecretString") or "").strip()
        if not value:
            raise SecretUnavailable(
                f"secret {secret_id} has no SecretString -- a binary secret is not supported"
            )

        self._cache[secret_id] = (time.monotonic() + self._ttl, value)
        log.info("fetched secret %s from Secrets Manager (cached %.0fs)", secret_id, self._ttl)
        return value


def _explain_aws_failure(secret_id: str, exc: Exception) -> str:
    """Turn a boto3 exception into something an operator can act on.

    Deliberately does not include the exception's own text: botocore messages
    are long, and this string travels into logs. The error class is the useful
    part, and each of these has exactly one fix.
    """
    name = type(exc).__name__
    fixes = {
        "ResourceNotFoundException": (
            f"secret {secret_id!r} does not exist in this region -- check the name "
            "and AWS_REGION"
        ),
        "AccessDeniedException": (
            f"the instance role is not allowed secretsmanager:GetSecretValue on "
            f"{secret_id!r} -- see deploy/iam-policy.json"
        ),
        "DecryptionFailure": f"secret {secret_id!r} uses a KMS key this role cannot decrypt",
        "NoCredentialsError": (
            "no AWS credentials found -- on EC2 this means the instance has no IAM role "
            "attached, or IMDS is unreachable from this process"
        ),
        "EndpointConnectionError": "cannot reach Secrets Manager -- check egress and AWS_REGION",
    }
    return fixes.get(name, f"could not fetch secret {secret_id!r} ({name})")


def from_environment() -> SecretProvider:
    """Build the provider named by KPI_SECRET_BACKEND. Defaults to env."""
    backend = os.environ.get(BACKEND_ENV_VAR, BACKEND_ENV).strip().lower()
    if backend == BACKEND_AWS:
        return AwsSecrets()
    if backend == BACKEND_ENV:
        return EnvSecrets()
    raise SecretUnavailable(
        f"{BACKEND_ENV_VAR}={backend!r} is not understood; expected "
        f"{BACKEND_ENV!r} or {BACKEND_AWS!r}"
    )
