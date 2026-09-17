"""MCP server: posts the weekly report. Holds the Slack bot token.

Run it:
    KPI_SECRET_BACKEND=aws KPI_SLACK_SECRET_ID=kpi/slack-bot-token \
        KPI_SLACK_DESTINATION=C0BV21PC28P \
        python -m kpi_mcp.slack_server --port 8802

TWO DELIBERATE RESTRICTIONS
---------------------------
1. THE DESTINATION IS NOT A TOOL ARGUMENT. It comes from this server's own
   environment. Without that, any process on the VM could call this tool on
   localhost and message an arbitrary person or channel -- which would make the
   migration an authorisation DOWNGRADE, since posting today at least requires
   reading a chmod-600 file.

2. ONE REPORT PER WEEK, ENFORCED HERE. The client also tracks this, but the
   client's history file is not the thing holding the token. A server-side
   check means a buggy or duplicated caller cannot spam the channel.

Message *formatting* stays on the client: the block layout is pure, has 13
tests, and moving it here would put untested rendering in front of the CTO.
This server takes finished blocks and posts them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from kpi_mcp import secrets
from kpi_tracker import envfile, storage, wire
from kpi_tracker.logging_setup import configure

log = logging.getLogger(__name__)

SECRET_ID_ENV_VAR = "KPI_SLACK_SECRET_ID"
DEFAULT_SECRET_ID = "kpi/slack-bot-token"
DESTINATION_ENV_VAR = "KPI_SLACK_DESTINATION"
STATE_PATH_ENV_VAR = "KPI_SLACK_STATE_PATH"
DEFAULT_STATE_PATH = "/var/lib/kpi-mcp/slack-sent.json"
DEFAULT_PORT = 8802

mcp = MCPServer(
    name="kpi-slack",
    version="1.0.0",
    instructions="Posts the weekly KPI report to one preconfigured Slack destination.",
)


class SendResult(BaseModel):
    schema_version: int
    status: str = Field(description=f"one of {list(wire.SEND_STATUSES)}")
    message_ts: str | None = None
    error: str | None = None


class CredentialCheck(BaseModel):
    schema_version: int
    ok: bool
    detail: str


def _secret_id() -> str:
    return os.environ.get(SECRET_ID_ENV_VAR, DEFAULT_SECRET_ID)


def _destination() -> str:
    destination = os.environ.get(DESTINATION_ENV_VAR, "").strip()
    if not destination:
        raise ToolError(
            f"{DESTINATION_ENV_VAR} is not set on the server -- it is configured here, "
            "not passed by the caller"
        )
    return destination


def _state_path() -> str:
    """Where the one-report-per-week record lives.

    A local path on EC2; an s3://bucket/key URI on Lambda, where the filesystem
    does not survive an invocation and this record is the only thing stopping a
    retry from posting the report twice.
    """
    return os.environ.get(STATE_PATH_ENV_VAR, DEFAULT_STATE_PATH)


def _already_sent(week_key: str) -> str | None:
    """The message ts for this week, if we have already posted it."""
    try:
        raw = storage.read_text(_state_path())
    except Exception:  # noqa: BLE001 - S3 down must not block the weekly report
        log.exception("could not read the sent-weeks record; assuming not sent")
        return None
    if not raw:
        return None
    try:
        sent = json.loads(raw)
    except json.JSONDecodeError:
        return None  # an unreadable state file must not block the weekly report
    value = sent.get(week_key) if isinstance(sent, dict) else None
    return value if isinstance(value, str) else None


def _record_sent(week_key: str, message_ts: str) -> None:
    location = _state_path()
    try:
        sent = {}
        raw = storage.read_text(location)
        if raw:
            try:
                loaded = json.loads(raw)
                sent = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                sent = {}
        sent[week_key] = message_ts
        storage.write_text(location, json.dumps(sent, indent=2, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        # The message went out. Losing the record only risks a duplicate next
        # run, which is far better than failing after a successful send.
        log.error("posted but could not record it at %s: %s", location, exc)


@mcp.tool(
    description=(
        "Post the weekly KPI report to this server's configured Slack destination. "
        "The destination is NOT a parameter. Sending the same week_key twice is a no-op."
    )
)
def post_weekly_report(week_key: str, blocks: list[dict], fallback_text: str) -> SendResult:
    if not week_key.strip():
        raise ToolError("week_key is required; it is what makes a repeat send a no-op")
    if not blocks:
        raise ToolError("blocks is empty -- refusing to post an empty report")
    if not fallback_text.strip():
        raise ToolError("fallback_text is required; it is the notification text")

    existing = _already_sent(week_key)
    if existing:
        log.info("week %s already posted (ts=%s); not sending again", week_key, existing)
        return SendResult(
            schema_version=wire.SCHEMA_VERSION,
            status=wire.SEND_SKIPPED_DUPLICATE,
            message_ts=existing,
        )

    destination = _destination()
    try:
        token = secrets.from_environment().get(_secret_id())
    except secrets.SecretUnavailable as exc:
        raise ToolError(f"credential unavailable: {exc}") from None

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

    client = WebClient(token=token)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))

    try:
        response = client.chat_postMessage(
            channel=destination, blocks=blocks, text=fallback_text
        )
    except SlackApiError as exc:
        raise ToolError(_explain_slack(exc)) from None

    message_ts = str(response["ts"])
    _record_sent(week_key, message_ts)
    log.info("posted week %s to %s (ts=%s)", week_key, destination, message_ts)
    return SendResult(
        schema_version=wire.SCHEMA_VERSION, status=wire.SEND_SENT, message_ts=message_ts
    )


@mcp.tool(
    description=(
        "Post a short operational alert to the configured destination. For the "
        "dead-man's check: use post_weekly_report for the actual report."
    )
)
def post_alert(text: str) -> SendResult:
    """Deliberately separate from post_weekly_report.

    The report is idempotent per week, which is exactly wrong for an alert --
    if the run fails three weeks running, that should be three messages. And
    keeping them apart means an alert can never be mistaken for a report, or
    consume the week's idempotency slot.
    """
    message = text.strip()
    if not message:
        raise ToolError("text is required")

    destination = _destination()
    try:
        token = secrets.from_environment().get(_secret_id())
    except secrets.SecretUnavailable as exc:
        raise ToolError(f"credential unavailable: {exc}") from None

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        response = WebClient(token=token).chat_postMessage(
            channel=destination, text=message[:3000]
        )
    except SlackApiError as exc:
        raise ToolError(_explain_slack(exc)) from None

    log.info("posted alert to %s", destination)
    return SendResult(
        schema_version=wire.SCHEMA_VERSION,
        status=wire.SEND_SENT,
        message_ts=str(response["ts"]),
    )


@mcp.tool(description="Confirm this server has a credential and a destination. Returns neither.")
def check_credentials() -> CredentialCheck:
    problems = []
    try:
        token = secrets.from_environment().get(_secret_id())
        complaint = describe_slack_token(token)
        if complaint:
            problems.append(complaint)
    except secrets.SecretUnavailable as exc:
        problems.append(str(exc))
    if not os.environ.get(DESTINATION_ENV_VAR, "").strip():
        problems.append(f"{DESTINATION_ENV_VAR} is not set")

    if problems:
        return CredentialCheck(
            schema_version=wire.SCHEMA_VERSION, ok=False, detail="; ".join(problems)
        )
    return CredentialCheck(
        schema_version=wire.SCHEMA_VERSION,
        ok=True,
        detail=f"credential readable; destination {os.environ[DESTINATION_ENV_VAR]}",
    )


# chat.postMessage needs a BOT token. Slack issues several token types and the
# wrong one fails only at send time, with a bare "invalid_auth":
#   xoxb- bot token          <- the one we need
#   xapp- app-level token    <- Socket Mode only; cannot post messages
#   xoxp- user token         <- posts as a person, not the app
#   xoxe- refresh token
#
# This check lives here, next to the token, rather than on the client -- the
# client no longer sees a token to validate. Observed for real: an xapp- token
# was configured and `check` reported it as fine.
SLACK_BOT_TOKEN_PREFIX = "xoxb-"
SLACK_TOKEN_HINTS = {
    "xapp-": "an app-level token (Socket Mode). Use the Bot User OAuth Token from "
             "OAuth & Permissions instead",
    "xoxp-": "a user token. Use the Bot User OAuth Token from OAuth & Permissions instead",
    "xoxe-": "a refresh token, not an access token",
    "xoxa-": "a legacy workspace token",
}


def describe_slack_token(token: str | None) -> str | None:
    """Why this token will not work, or None if it looks usable."""
    if not token:
        return None
    if token.startswith(SLACK_BOT_TOKEN_PREFIX):
        return None
    for prefix, description in SLACK_TOKEN_HINTS.items():
        if token.startswith(prefix):
            return f"the Slack token looks like {description}"
    return (
        f"the Slack token does not start with {SLACK_BOT_TOKEN_PREFIX!r} -- "
        "it should be the Bot User OAuth Token from OAuth & Permissions"
    )


def _explain_slack(exc) -> str:
    """Turn a Slack error code into something an operator can act on."""
    code = exc.response.get("error", "unknown_error")
    hints = {
        "invalid_auth": "the bot token is wrong or revoked; no retry will help",
        "token_revoked": "the bot token has been revoked; reinstall the app",
        "channel_not_found": (
            f"{DESTINATION_ENV_VAR} is not a channel this bot can post to -- invite the "
            "bot to the channel, or check the ID"
        ),
        "not_in_channel": "invite the bot to that channel",
        "missing_scope": f"add the scope {exc.response.get('needed')!r} and reinstall",
        "invalid_blocks": "Slack rejected the payload; an empty table cell is the usual cause",
    }
    return f"slack rejected the message ({code}). {hints.get(code, 'see the Slack API docs')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kpi-slack-mcp", description=__doc__.split("\n")[0])
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
    log.info(
        "kpi-slack listening on %s:%d, secret %s, destination %s",
        args.host, args.port, _secret_id(),
        os.environ.get(DESTINATION_ENV_VAR, "<UNSET>"),
    )
    mcp.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
