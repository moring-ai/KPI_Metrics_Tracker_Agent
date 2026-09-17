"""Every guardrail gets a case it must catch and a case it must let through.

A guardrail without tests rots silently -- you find out it stopped working
from an incident, which is exactly the incident it existed to prevent.
"""

from kpi_tracker import guardrails
from kpi_tracker.models import Report, Row


def make(rows, *, linkedin_ok=True, blogs_ok=True):
    return Report(
        week_label="Thu 2026-08-27 to Wed 2026-09-02",
        week_start="2026-08-27",
        week_end="2026-09-02",
        tz_name="Asia/Kolkata",
        rows=rows,
        linkedin_ok=linkedin_ok,
        blogs_ok=blogs_ok,
    )


def fired(report, **kwargs):
    return {d.name for d in guardrails.evaluate(report, **kwargs) if d.fired}


HEALTHY = [Row("A", 3, 1), Row("B", 1, 0)]


def test_a_healthy_report_fires_nothing():
    assert fired(make(HEALTHY)) == set()
    assert not guardrails.blocked(guardrails.evaluate(make(HEALTHY)))


def test_both_sources_down_blocks_the_send():
    report = make([Row("A", None, None)], linkedin_ok=False, blogs_ok=False)
    assert "both_sources_down" in fired(report)
    assert guardrails.blocked(guardrails.evaluate(report))


def test_one_source_down_still_sends():
    """Half a report is useful; the broken column shows '?'."""
    report = make([Row("A", None, 1)], linkedin_ok=False)
    assert "both_sources_down" not in fired(report)
    assert not guardrails.blocked(guardrails.evaluate(report))


def test_all_zero_linkedin_after_active_weeks_is_treated_as_a_broken_scraper():
    """The failure mode this system is most likely to hit: a source that
    starts returning empty results without erroring."""
    report = make([Row("A", 0, 0), Row("B", 0, 0)])
    decisions = guardrails.evaluate(report, recent_linkedin_totals=[12, 9, 14])
    assert guardrails.blocked(decisions)
    assert "collapse" in " ".join(d.name for d in decisions if d.fired)


def test_all_zero_linkedin_with_no_history_notes_rather_than_blocks():
    """On a fresh install there is no baseline, so we cannot tell a broken
    scraper from a quiet week. Send, but say so."""
    report = make([Row("A", 0, 0)])
    decisions = guardrails.evaluate(report, recent_linkedin_totals=[])
    assert not guardrails.blocked(decisions)
    assert any(d.fired and d.action == "note" for d in decisions)


def test_all_zero_linkedin_after_genuinely_quiet_weeks_does_not_block():
    report = make([Row("A", 0, 0)])
    assert not guardrails.blocked(guardrails.evaluate(report, recent_linkedin_totals=[0, 0]))


def test_an_implausible_count_blocks():
    """A count this high means we are counting comments or reshares."""
    report = make([Row("A", 400, 0)])
    assert "implausible_count" in fired(report)
    assert guardrails.blocked(guardrails.evaluate(report))


def test_a_high_but_believable_count_passes():
    assert "implausible_count" not in fired(make([Row("A", 20, 0)]))


def test_an_unknown_count_is_not_treated_as_implausible():
    assert "implausible_count" not in fired(make([Row("A", None, 0)], linkedin_ok=False))


def test_an_empty_table_blocks():
    assert "no_members" in fired(make([]))


def test_no_cell_ever_renders_empty():
    """Slack rejects the entire message with invalid_blocks on a zero-length
    cell, so one bad cell would take down the whole report."""
    report = make([Row("A", None, None), Row("B", 0, 0)])
    assert "empty_cells" not in fired(report)
    for row in report.rows:
        assert row.cell(row.linkedin_posts) and row.cell(row.blogs)


def test_every_check_reports_a_reason_whether_or_not_it_fired():
    for decision in guardrails.evaluate(make(HEALTHY)):
        assert decision.reason
        assert decision.action in ("block", "note")


def test_a_partial_column_is_not_read_as_a_collapse():
    """The collapse signal is 'we asked everyone and every answer was zero'.
    A column containing a '?' has not answered everyone.

    The verdict is also asserted, not just the outcome: the all-zero test
    would reach 'not a collapse' by itself, but would explain it as 'some
    posts were found', which is false and misdirects whoever reads the log.
    """
    report = make([Row("A", 0, 0), Row("B", None, 0)])
    decisions = guardrails.evaluate(report, recent_linkedin_totals=[12, 9])
    collapse = next(d for d in decisions if d.name == "linkedin_collapse")

    assert collapse.fired is False
    assert not guardrails.blocked(decisions)
    assert "no count at all for B" in collapse.reason


def test_a_genuine_collapse_still_blocks_when_every_value_is_known():
    report = make([Row("A", 0, 0), Row("B", 0, 0)])
    assert guardrails.blocked(guardrails.evaluate(report, recent_linkedin_totals=[12, 9]))
