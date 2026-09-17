"""AWS Lambda entry point for the weekly KPI report.

    EventBridge Scheduler  --Thu 11:00 IST-->  this function

Deliberately thin. Everything it does lives in `kpi_tracker`, which has its own
tests; this file is the AWS-shaped wrapper and nothing else. Keeping it that way
is what stops the three deployments (EC2, AgentCore, Lambda) from drifting.

TWO THINGS ARE DIFFERENT ON LAMBDA, AND BOTH ARE DELIBERATE
-----------------------------------------------------------
1. THE CREDENTIALS LIVE HERE. On EC2 the LinkedIn and Slack MCP servers are
   separate processes under a different Unix user, which the weekly job cannot
   read. A Lambda is one process, so the same servers run IN-PROCESS -- the
   identical code path, with no transport -- and this function holds both
   tokens. That is a real reduction in isolation, accepted because a Lambda
   cannot express the alternative. Secrets Manager still holds the values; the
   execution role fetches them.

2. FAILURE IS SIGNALLED BY RAISING. On EC2 a failed run triggers a systemd
   OnFailure unit that posts an alert, because silence must not look like
   success. Lambda's equivalent is the invocation's own failure metric, so
   anything that means "no report went out" raises here rather than returning
   quietly. Wire a CloudWatch alarm on Errors -- see deploy/lambda/README.md.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger()

SEND_ENV_VAR = "AGENT_SEND"
CONFIG_ENV_VAR = "KPI_CONFIG_PATH"


class ReportNotSent(RuntimeError):
    """No report reached the channel. Raised so the invocation is marked failed."""


def _should_send(event: dict) -> bool:
    """The event wins over the environment, so a manual dry run needs no redeploy."""
    if isinstance(event, dict) and "send" in event:
        return bool(event["send"])
    return os.environ.get(SEND_ENV_VAR, "true").strip().lower() not in ("0", "false", "no")


def _check_timeout_ordering(context) -> None:
    """Warn loudly if Lambda will kill us before the vendor poll gives up.

    The invariant everywhere else in this system is that the inner timeout
    fires first, so a slow vendor produces a readable "snapshot not ready after
    Ns" rather than an opaque kill. On Lambda the outermost timeout is the
    function's own, and it is capped at 900s -- lower than this project's
    default 1800s ceiling. Getting that wrong is silent until a slow week.
    """
    from kpi_mcp.brightdata import POLL_TIMEOUT_ENV_VAR, POLL_TIMEOUT_SECONDS

    if context is None or not hasattr(context, "get_remaining_time_in_millis"):
        return
    remaining = context.get_remaining_time_in_millis() / 1000
    if remaining <= POLL_TIMEOUT_SECONDS:
        log.error(
            "MISCONFIGURED: this function has %.0fs left but the vendor poll ceiling is "
            "%.0fs, so Lambda will kill the run before the poll gives up and the failure "
            "will have no reason attached. Set %s below the function timeout "
            "(e.g. 780 for a 900s function).",
            remaining, POLL_TIMEOUT_SECONDS, POLL_TIMEOUT_ENV_VAR,
        )


def _check_history_is_durable(config) -> None:
    """On Lambda both state files MUST be in S3.

    /var/task is read-only and nothing on the filesystem survives an
    invocation, so a local path means two things break silently: the collapse
    guardrail never learns what a normal week looks like, and the "already sent
    this week" check is always false, so a retry posts the report twice.

    Checked up front rather than at write time, which is after the vendor call
    has been made and the message has gone out.
    """
    from kpi_tracker import storage

    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return  # EC2 and local runs: a local path is exactly right there

    if not storage.is_remote(config.history_path):
        raise ReportNotSent(
            f"history_path is {config.history_path!r}, a local path, but this is Lambda: "
            "the filesystem is read-only and does not survive an invocation. Set "
            "KPI_HISTORY_PATH to an s3://bucket/key URI."
        )

    # The Slack server's own record needs the same treatment, and is easier to
    # lose: `update-function-configuration --environment` REPLACES the whole
    # environment, so flipping AGENT_SEND without re-listing every variable
    # silently drops it back to its local default. That record is the only
    # thing stopping a retry from posting the report to the CTO twice.
    #
    # Read through the server's own constants so the two cannot drift apart.
    from kpi_mcp import slack_server

    slack_state = os.environ.get(
        slack_server.STATE_PATH_ENV_VAR, slack_server.DEFAULT_STATE_PATH
    )
    if not storage.is_remote(slack_state):
        raise ReportNotSent(
            f"{slack_server.STATE_PATH_ENV_VAR} is {slack_state!r}, a local path, but this "
            "is Lambda: nothing on the filesystem survives an invocation, so a retry would "
            "post the report a second time. Set it to an s3://bucket/key URI."
        )


def handler(event, context=None):
    """Run the weekly report.

    event (all optional):
        {"week": "2026-08-28"}   run that week instead of the current one
        {"send": false}          dry run: compute everything, send nothing
    """
    from kpi_mcp import linkedin_server, slack_server

    from kpi_tracker import config as config_module
    from kpi_tracker import logging_setup, pipeline, report
    from kpi_tracker.sources.mcp_linkedin import McpLinkedInSource

    logging_setup.configure()
    _check_timeout_ordering(context)

    event = event if isinstance(event, dict) else {}
    send = _should_send(event)

    config = config_module.load(os.environ.get(CONFIG_ENV_VAR, "config.json"))
    _check_history_is_durable(config)
    built, decisions = pipeline.run(
        config,
        run_at=event.get("week"),
        send=send,
        force=bool(event.get("force")),
        # In-process MCP: same code, no transport. See the module docstring.
        linkedin_source=McpLinkedInSource(linkedin_server.mcp),
        slack_target=slack_server.mcp,
    )

    if built is None:
        log.info("this week was already reported; nothing fetched, nothing sent")
        return {"status": "already_reported", "sent": False}

    table = report.render_text(built)
    log.info("report for %s:\n%s", built.week_label, table)

    summary = {
        "status": "blocked" if built.blocked else ("sent" if send else "dry_run"),
        "week": built.week_label,
        "sent": send and not built.blocked,
        "rows": [
            {"name": row.name, "linkedin": row.linkedin_posts, "blogs": row.blogs}
            for row in built.rows
        ],
        "notes": built.notes,
        "guardrails_fired": [d.name for d in decisions if d.fired],
    }

    # A guardrail block means no report reached the channel. On EC2 that is a
    # non-zero exit and an OnFailure alert; here it must fail the invocation so
    # the Errors metric moves and somebody finds out.
    if built.blocked:
        raise ReportNotSent(
            f"guardrails blocked the report for {built.week_label}: "
            f"{[d.reason for d in decisions if d.blocks]}"
        )

    return summary
