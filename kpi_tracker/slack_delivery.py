"""Render the report as Slack blocks and send it.

Deliberately template-driven with no model involved. Every field here is
derived mechanically from the data, so the message is byte-identical for
identical input, diffable, and unit-testable -- and there is no chance of the
CTO's weekly report inventing a number.

We use Slack's native `table` block (shipped 2025-08-14) rather than the old
padded-monospace trick: it aligns columns properly and reads the same on
mobile, which is where a Monday-morning DM actually gets read.

Delivery goes through the kpi-slack MCP server, which holds the bot token. This
process has no Slack credential at all.

Note what does NOT cross that boundary: the destination. It is configured on
the server, not passed as an argument, so nothing running on this machine can
redirect the weekly report at somebody else. Block *formatting* stays here,
because it is pure, deterministic and covered by tests that a server deploy
would not run.
"""

from __future__ import annotations

import logging

from kpi_tracker import wire
from kpi_tracker.mcp_call import McpCallFailed, call_tool
from kpi_tracker.models import Report

log = logging.getLogger(__name__)

HEADER_MAX_CHARS = 150  # Slack's documented limit on a header block


class SlackDeliveryError(RuntimeError):
    """Sending failed in a way a human needs to fix."""


def build_blocks(report: Report, *, target_posts_per_week: int = 3) -> list[dict]:
    """The message payload. Pure function -- no network, no clock, no config."""
    header = f"Weekly KPI - {report.week_label}"

    rows = [
        [
            _cell("Name"),
            _cell(f"LinkedIn posts (target {target_posts_per_week})"),
            _cell("Blogs"),
        ]
    ]
    rows.extend(
        [_cell(row.name), _cell(row.cell(row.linkedin_posts)), _cell(row.cell(row.blogs))]
        for row in report.rows
    )

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:HEADER_MAX_CHARS]}},
        {
            "type": "context",
            "elements": [
                {
                    # week_label already carries the real weekday names, which
                    # track the configured anchor day. Hardcoding "Mon-Sun"
                    # here would contradict the header for 6 of the 7 anchors.
                    "type": "mrkdwn",
                    "text": f"Seven days, *{report.week_label}*, {report.tz_name}",
                }
            ],
        },
        {
            "type": "table",
            "block_id": f"kpi-{report.week_start}",
            "column_settings": [{"is_wrapped": False}, {"align": "right"}, {"align": "right"}],
            "rows": rows,
        },
    ]

    if report.notes:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "\n".join(f"• {n}" for n in report.notes)}],
            }
        )
    return blocks


def fallback_text(report: Report) -> str:
    """Notification and desktop-preview text, and what any surface that cannot
    render a table falls back to. Never leave this unset."""
    summary = ", ".join(
        f"{row.name} {row.cell(row.linkedin_posts)}/{row.cell(row.blogs)}" for row in report.rows
    )
    return f"Weekly KPI ({report.week_label}) - LinkedIn/blogs per person: {summary}"


def _cell(text: str) -> dict:
    """A table cell. Slack rejects the whole message on a zero-length cell."""
    return {"type": "raw_text", "text": text if text else "-"}


def send(
    server_url: str,
    report: Report,
    *,
    target_posts_per_week: int = 3,
    caller=call_tool,
) -> str:
    """Post the report via the kpi-slack MCP server. Returns the message ts.

    `report.week_start` is the idempotency key. The server refuses to post the
    same week twice, so a duplicated or retried run cannot spam the channel --
    a check that lives with the token rather than with the caller.
    """
    blocks = build_blocks(report, target_posts_per_week=target_posts_per_week)
    try:
        payload = caller(
            server_url,
            wire.TOOL_POST_REPORT,
            {
                "week_key": report.week_start,
                "blocks": blocks,
                "fallback_text": fallback_text(report),
            },
        )
    except McpCallFailed as exc:
        raise SlackDeliveryError(str(exc)) from None

    if payload.get("schema_version") != wire.SCHEMA_VERSION:
        raise SlackDeliveryError(
            f"kpi-slack speaks schema_version {payload.get('schema_version')!r}, "
            f"this client speaks {wire.SCHEMA_VERSION}"
        )

    status = payload.get("status")
    if status == wire.SEND_SKIPPED_DUPLICATE:
        log.info("kpi-slack had already posted week %s; not sent again", report.week_start)
        return str(payload.get("message_ts") or "")
    if status != wire.SEND_SENT:
        raise SlackDeliveryError(
            f"kpi-slack reported {status!r}: {payload.get('error') or 'no reason given'}"
        )

    message_ts = str(payload.get("message_ts") or "")
    log.info("posted weekly KPI via %s (ts=%s)", server_url, message_ts)
    return message_ts
