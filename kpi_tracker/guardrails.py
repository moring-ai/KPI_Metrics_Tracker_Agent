"""Checks that run on the finished report, just before it would be sent.

Evals (in tests/) tell us how good this system is offline. Guardrails run on
the live report and can hold it back. The distinction matters: everything here
exists because the failure it catches produces a *plausible* report rather
than an error, and a plausible wrong report about named colleagues is worse
than no report.

Almost nothing here is AI safety. There is no user-supplied text, no retrieval
and no model in the control path, so prompt injection, jailbreak and
groundedness checks genuinely do not apply -- they are listed as N/A in
docs/ARCHITECTURE.md rather than faked. What can actually hurt us is a
scraper that breaks quietly, so these are data-integrity checks.

Every check names its trigger, its action, and what happens next.
"""

from __future__ import annotations

from dataclasses import dataclass

from kpi_tracker.models import Report

# One person publishing more than this in a week means we are counting
# something that is not a post (comments, reactions, a pagination bug).
MAX_PLAUSIBLE_POSTS_PER_WEEK = 25


@dataclass(frozen=True)
class Decision:
    name: str
    fired: bool
    action: str  # "block" | "note"
    reason: str

    @property
    def blocks(self) -> bool:
        return self.fired and self.action == "block"


def evaluate(report: Report, *, recent_linkedin_totals: list[int] | None = None) -> list[Decision]:
    """Run every guardrail. Returns one Decision per check, fired or not."""
    recent_linkedin_totals = recent_linkedin_totals or []
    return [
        _both_sources_down(report),
        _linkedin_collapse(report, recent_linkedin_totals),
        _implausible_count(report),
        _no_members(report),
        _empty_cells(report),
    ]


def blocked(decisions: list[Decision]) -> bool:
    return any(decision.blocks for decision in decisions)


def _both_sources_down(report: Report) -> Decision:
    """Trigger: neither source returned data. Action: block. Next: fix and re-run."""
    fired = not report.linkedin_ok and not report.blogs_ok
    return Decision(
        "both_sources_down",
        fired,
        "block",
        "Both LinkedIn and blog fetches failed; there is nothing to report."
        if fired
        else "At least one source returned data.",
    )


def _linkedin_collapse(report: Report, recent_totals: list[int]) -> Decision:
    """Trigger: LinkedIn 'succeeded' with zero posts for everyone, after weeks
    that were not zero. Action: block. Next: check the vendor, then re-run.

    This is the failure this whole system is most likely to hit: a scraper
    that starts returning empty results without erroring. Without history to
    compare against we cannot distinguish it from a quiet week, so on a fresh
    install this only notes rather than blocks.
    """
    if not report.linkedin_ok:
        return Decision("linkedin_collapse", False, "block", "LinkedIn already reported as failed.")

    # A column containing any unknown marker is partial, not collapsed -- the
    # signal is "we asked everyone and every answer was zero". The all-zero
    # test below would reach the same verdict on its own (None != 0), but it
    # would report it as "some posts were found", which is not what happened
    # and sends the operator looking in the wrong place.
    unknown = [row.name for row in report.rows if row.linkedin_posts is None]
    if unknown:
        return Decision(
            "linkedin_collapse",
            False,
            "block",
            f"Not a collapse: no count at all for {', '.join(unknown)}.",
        )

    all_zero = bool(report.rows) and all(row.linkedin_posts == 0 for row in report.rows)
    if not all_zero:
        return Decision("linkedin_collapse", False, "block", "Some LinkedIn posts were found.")

    had_activity = any(total > 0 for total in recent_totals)
    if had_activity:
        return Decision(
            "linkedin_collapse",
            True,
            "block",
            f"Zero LinkedIn posts for everyone, but recent weeks had {recent_totals}. "
            "The source has probably broken silently.",
        )
    return Decision(
        "linkedin_collapse",
        True,
        "note",
        "Zero LinkedIn posts for everyone, and no prior week to compare against. "
        "Sending, but verify one profile by hand.",
    )


def _implausible_count(report: Report) -> Decision:
    """Trigger: a count above MAX_PLAUSIBLE_POSTS_PER_WEEK. Action: block."""
    offenders = [
        f"{row.name}={row.linkedin_posts}"
        for row in report.rows
        if isinstance(row.linkedin_posts, int)
        and row.linkedin_posts > MAX_PLAUSIBLE_POSTS_PER_WEEK
    ]
    return Decision(
        "implausible_count",
        bool(offenders),
        "block",
        f"Counts above {MAX_PLAUSIBLE_POSTS_PER_WEEK}/week: {', '.join(offenders)}. "
        "Probably counting comments or reshares."
        if offenders
        else "All counts are within a plausible range.",
    )


def _no_members(report: Report) -> Decision:
    """Trigger: an empty table. Action: block. Sending one would be noise."""
    fired = not report.rows
    return Decision(
        "no_members",
        fired,
        "block",
        "No members to report on." if fired else f"{len(report.rows)} member(s) in the table.",
    )


def _empty_cells(report: Report) -> Decision:
    """Trigger: a cell that would render as an empty string. Action: block.

    Slack rejects an entire message with invalid_blocks if any table cell is
    zero-length, so one bad cell would take down the whole report rather than
    leaving a gap. Row.cell() already guarantees this, so a fire here means
    someone changed that and did not notice.
    """
    bad = [row.name for row in report.rows if not row.cell(row.linkedin_posts) or not row.cell(row.blogs)]
    return Decision(
        "empty_cells",
        bool(bad),
        "block",
        f"Empty table cells for {bad}; Slack would reject the whole message."
        if bad
        else "Every cell renders to a non-empty string.",
    )
