"""MCP server: LinkedIn post permalinks. Holds the Bright Data credential.

Run it:
    KPI_SECRET_BACKEND=aws KPI_LINKEDIN_SECRET_ID=kpi/brightdata \
        python -m kpi_mcp.linkedin_server --port 8801

Two tools:
    collect_linkedin_posts(profile_urls) -> CollectionResult
    check_credentials()                  -> CredentialCheck

The return types are pydantic models on purpose. A plain `-> dict` annotation
makes the SDK publish no output schema and return structured_content=None, so
the client would have to parse text. A model gives the client a real dict and
gives `kpi-tracker check` a published schema it can assert against before the
weekly run depends on it.
"""

from __future__ import annotations

import argparse
import logging
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from kpi_mcp import secrets
from kpi_mcp.brightdata import VendorFailure, collect_post_urls
from kpi_tracker import envfile, wire
from kpi_tracker.logging_setup import configure

log = logging.getLogger(__name__)

SECRET_ID_ENV_VAR = "KPI_LINKEDIN_SECRET_ID"
DEFAULT_SECRET_ID = "kpi/brightdata-api-token"
DEFAULT_PORT = 8801

mcp = MCPServer(
    name="kpi-linkedin",
    version="1.0.0",
    instructions="Reads LinkedIn post permalinks for named profiles. Returns no dates.",
)


class ProfileResult(BaseModel):
    """One requested profile. Exactly one of these per profile, always."""

    profile_url: str
    status: str = Field(description=f"one of {list(wire.PROFILE_STATUSES)}")
    post_urls: list[str] = Field(default_factory=list, description="permalinks; no dates")
    error: str | None = Field(default=None, description="why, when status is 'failed'")


class CollectionResult(BaseModel):
    schema_version: int
    profiles: list[ProfileResult]


class CredentialCheck(BaseModel):
    schema_version: int
    ok: bool
    detail: str


def _secret_id() -> str:
    return os.environ.get(SECRET_ID_ENV_VAR, DEFAULT_SECRET_ID)


@mcp.tool(
    description=(
        "Read every authored post permalink for each LinkedIn profile URL. "
        "Returns one entry per requested profile, with an explicit status. "
        "Returns permalinks only -- the caller derives dates itself."
    )
)
def collect_linkedin_posts(profile_urls: list[str]) -> CollectionResult:
    if not profile_urls:
        return CollectionResult(schema_version=wire.SCHEMA_VERSION, profiles=[])

    try:
        token = secrets.from_environment().get(_secret_id())
    except secrets.SecretUnavailable as exc:
        # ToolError text reaches the client; a bare exception's does not. This
        # is an operator problem, so the client needs to be told what it is.
        raise ToolError(f"credential unavailable: {exc}") from None

    try:
        sorted_records = collect_post_urls(token, profile_urls)
    except VendorFailure as exc:
        raise ToolError(f"vendor failure: {exc}") from None

    profiles = [
        ProfileResult(profile_url=url, status=wire.STATUS_OK, post_urls=posts)
        for url, posts in sorted_records["ok"].items()
    ]
    profiles += [
        ProfileResult(profile_url=url, status=wire.STATUS_FAILED, error=reason)
        for url, reason in sorted_records["failed"].items()
    ]

    # The contract says one entry per requested profile. Anything the vendor
    # never mentioned is unknown, not zero -- so say so explicitly rather than
    # leaving the client to infer it from an absence.
    accounted = {p.profile_url for p in profiles}
    profiles += [
        ProfileResult(
            profile_url=url,
            status=wire.STATUS_FAILED,
            error="the vendor returned no result for this profile",
        )
        for url in profile_urls
        if url not in accounted
    ]

    log.info(
        "collected %d profile(s): %d ok, %d failed",
        len(profiles),
        sum(p.status == wire.STATUS_OK for p in profiles),
        sum(p.status == wire.STATUS_FAILED for p in profiles),
    )
    return CollectionResult(schema_version=wire.SCHEMA_VERSION, profiles=profiles)


@mcp.tool(description="Confirm this server can reach its credential. Never returns it.")
def check_credentials() -> CredentialCheck:
    secret_id = _secret_id()
    try:
        token = secrets.from_environment().get(secret_id)
    except secrets.SecretUnavailable as exc:
        return CredentialCheck(schema_version=wire.SCHEMA_VERSION, ok=False, detail=str(exc))
    return CredentialCheck(
        schema_version=wire.SCHEMA_VERSION,
        ok=True,
        detail=f"{secret_id} readable ({len(token)} chars)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kpi-linkedin-mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address (keep it loopback)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure(args.verbose)
    # Local development convenience: on EC2 there is no .env and this is a
    # no-op, because secrets come from Secrets Manager.
    loaded = envfile.load()
    if loaded:
        log.debug("loaded %d variable(s) from .env", len(loaded))
    log.info("kpi-linkedin listening on %s:%d, secret %s", args.host, args.port, _secret_id())
    mcp.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
