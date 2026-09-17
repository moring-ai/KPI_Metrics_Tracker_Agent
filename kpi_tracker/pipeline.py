"""The weekly job, start to finish.

Six steps, always in this order, each one validated before the next runs:

    1. load config and the member list
    2. work out the reporting window
    3. fetch LinkedIn posts        -- FetchResult
    4. fetch blog posts            -- FetchResult
    5. aggregate into the table    -- Report
    6. run guardrails, then send

Steps 3 and 4 are independent, so a failure in one still leaves the other
column reportable. Nothing here is model-driven: the shape of the work is
known in advance, so it is written down as code rather than rediscovered by an
agent on every run. See docs/ARCHITECTURE.md for why.
"""

from __future__ import annotations

import logging

from kpi_tracker import config as config_module
from kpi_tracker import guardrails, history, report, slack_delivery
from kpi_tracker.models import FetchResult, Report
from kpi_tracker.sources.fixture import FixtureSource
from kpi_tracker.sources.manual_csv import ManualCsvSource
from kpi_tracker.sources.mcp_linkedin import McpLinkedInSource
from kpi_tracker.sources.moring_blog import MoringBlogSource
from kpi_tracker.timewindow import last_week

log = logging.getLogger(__name__)


def build_linkedin_source(config: config_module.Config):
    """One line to swap vendors. See sources/base.py for why this seam exists."""
    if config.linkedin_source == "mcp":
        return McpLinkedInSource(config.linkedin_mcp_url)
    if config.linkedin_source == "manual_csv":
        return ManualCsvSource(config.manual_csv_path)
    if config.linkedin_source == "fixture":
        return FixtureSource(config.fixture_path)
    raise config_module.ConfigError(f"unknown linkedin.source {config.linkedin_source!r}")


def collect(
    config: config_module.Config,
    run_at: str | None = None,
    *,
    linkedin_source=None,
) -> tuple[Report, list]:
    """Steps 1-5, plus the guardrail decisions. Does not send anything.

    `linkedin_source` overrides the one config would build. It exists for the
    AgentCore deployment, where there is no MCP transport available and the
    graph wires the server object in-process instead -- same code path, same
    tests, different place the credential lives. See src/agent/graph.py.
    """
    members = config_module.load_members(config.members_path)
    week = last_week(
        run_at or _today(config.timezone), config.timezone, config.week_start_day
    )
    log.info("reporting week %s (%s) for %d members", week.label, week.tz_name, len(members))

    source = linkedin_source or build_linkedin_source(config)
    linkedin = _safely(lambda: source.fetch(members, week), "linkedin")
    blogs = _safely(lambda: MoringBlogSource(base_url=config.blog_base_url).fetch(week), "blogs")

    for result in (linkedin, blogs):
        for warning in result.warnings:
            log.warning("[%s] %s", result.source, warning)
        if not result.ok:
            log.error("[%s] fetch failed: %s", result.source, result.error)

    built = report.build(
        members, linkedin, blogs, week, target_posts_per_week=config.target_posts_per_week
    )
    decisions = guardrails.evaluate(
        built, recent_linkedin_totals=history.recent_linkedin_totals(config.history_path)
    )
    for decision in decisions:
        if decision.fired:
            log.warning("guardrail %s fired (%s): %s", decision.name, decision.action, decision.reason)

    if guardrails.blocked(decisions):
        built = _mark_blocked(built)
    return built, decisions


def run(
    config: config_module.Config,
    *,
    run_at: str | None = None,
    send: bool,
    force: bool = False,
    linkedin_source=None,
    slack_target: str | None = None,
) -> tuple[Report, list]:
    """The whole job. `send=False` (the default everywhere) posts nothing.

    Returns (None, []) when the week has already been reported: fetching is a
    side effect that costs vendor credits, so a run whose only job is to do
    nothing should not pay for data first.
    """
    if send and not force:
        week = last_week(
            run_at or _today(config.timezone), config.timezone, config.week_start_day
        )
        if history.already_sent(config.history_path, week.local_start.isoformat()):
            log.info(
                "week %s has already been sent; skipping without fetching. "
                "Use --force to send again.",
                week.label,
            )
            return None, []

    built, decisions = collect(config, run_at, linkedin_source=linkedin_source)

    if built.blocked:
        log.error("guardrails blocked this report; nothing was sent")
        return built, decisions

    if not send:
        # Record dry runs too. The collapse guardrail needs a baseline of what
        # normal weeks look like, and the launch bar asks for a couple of
        # shadow weeks before the first real send -- without this, those weeks
        # would build no baseline and the guardrail would still be blind on
        # the day it matters most. sent=False keeps idempotency unaffected.
        history.append(config.history_path, built, sent=False)
        log.info("dry run: not sending. Pass --send to post to Slack.")
        return built, decisions

    target = slack_target or config.slack_mcp_url
    if not target:
        raise config_module.ConfigError("mcp.slack_url is not set, so --send cannot work")

    slack_delivery.send(
        target,
        built,
        target_posts_per_week=config.target_posts_per_week,
    )
    # Only after Slack confirms. If this write fails the message still went
    # out, so log loudly rather than dying: the operator needs to know the
    # next run will not know it was already sent.
    #
    # `except Exception`, not `except OSError`: on Lambda this file is in S3
    # and a failure arrives as a botocore ClientError, which is not an OSError.
    # Letting it through would fail the invocation AFTER the CTO already has
    # the report -- and then the retry would send it again.
    try:
        history.append(config.history_path, built, sent=True)
    except Exception:  # noqa: BLE001 - see below; the message has already gone
        log.exception(
            "sent to Slack but could not write %s -- next run may send this week again",
            config.history_path,
        )
    return built, decisions


def _safely(fetch, label: str) -> FetchResult:
    """A source raising an unexpected exception must not kill the other column."""
    try:
        return fetch()
    except Exception as exc:  # noqa: BLE001 - last line of defence around third-party code
        log.exception("%s source raised", label)
        return FetchResult.failed(label, f"{type(exc).__name__}: {exc}")


def _mark_blocked(built: Report) -> Report:
    from dataclasses import replace

    return replace(built, blocked=True)


def _today(tz_name: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name)).date().isoformat()
