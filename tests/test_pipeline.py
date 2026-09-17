"""The whole job, wired together, with no network and no real Slack.

These are the tests that would catch a break in the seams between steps --
the failures that unit tests pass straight through.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import responses
from kpi_tracker import config as config_module
from kpi_tracker import history, pipeline, slack_delivery
from kpi_tracker.models import Report, Row
from tests.conftest import BLOG_BASE, FIXTURES


def activity_id(iso_utc: str) -> int:
    ms = int(datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return (ms << 22) | 0x1F2E3D


def post_url(handle: str, iso_utc: str) -> str:
    return f"https://www.linkedin.com/posts/{handle}_x-activity-{activity_id(iso_utc)}-aB3x"


@pytest.fixture
def project(tmp_path):
    """A complete, self-contained installation in a temp directory."""
    (tmp_path / "data").mkdir()

    members = [
        {"name": "Ashvath Narayan", "linkedin_url": "https://www.linkedin.com/in/ashvath-narayanan/"},
        {"name": "Ellakkiaa", "linkedin_url": "https://www.linkedin.com/in/ellakkiaa-s-278823200/"},
        {"name": "Balaji Nagaraj", "linkedin_url": "https://www.linkedin.com/in/balajinagarajkumar/"},
    ]
    (tmp_path / "data" / "members.json").write_text(json.dumps(members))

    posts = [
        {"url": post_url("ashvath-narayanan", "2026-08-28T09:00:00")},
        {"url": post_url("ashvath-narayanan", "2026-08-30T09:00:00")},
        {"url": post_url("ellakkiaa-s-278823200", "2026-08-29T09:00:00")},
        {"url": post_url("ashvath-narayanan", "2026-07-01T09:00:00")},  # outside the window
    ]
    (tmp_path / "data" / "linkedin.json").write_text(json.dumps(posts))

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "timezone": "Asia/Kolkata",
                "week_start_day": "thursday",
                "members_path": str(tmp_path / "data" / "members.json"),
                "history_path": str(tmp_path / "data" / "history.jsonl"),
                "linkedin": {
                    "source": "fixture",
                    "target_posts_per_week": 3,
                    "fixture_path": str(tmp_path / "data" / "linkedin.json"),
                },
                "blog": {"base_url": BLOG_BASE},
                # The credentials live in the MCP servers now, so the client's
                # config holds only their addresses.
                "mcp": {
                    "linkedin_url": "http://127.0.0.1:8801/mcp",
                    "slack_url": "http://127.0.0.1:8802/mcp",
                },
                "slack": {"destination_label": "#marketing-kpi"},
            }
        )
    )
    return config_module.load(tmp_path / "config.json", env={})


def test_the_whole_job_produces_the_three_column_table(project, blog_site):
    report, _ = pipeline.collect(project, run_at="2026-09-03")

    assert report.week_label == "Thu 2026-08-27 to Wed 2026-09-02"
    counts = {row.name: (row.linkedin_posts, row.blogs) for row in report.rows}
    assert counts == {
        "Ashvath Narayan": (2, 1),   # 2 LinkedIn posts + the enterprise-architecture blog
        "Ellakkiaa": (1, 1),         # 1 LinkedIn post + the orchestration blog
        "Balaji Nagaraj": (0, 0),    # published nothing: a real zero, not unknown
    }


def test_posts_outside_the_window_are_excluded(project, blog_site):
    report, _ = pipeline.collect(project, run_at="2026-09-03")
    assert {row.name: row.linkedin_posts for row in report.rows}["Ashvath Narayan"] == 2


def test_a_dry_run_sends_nothing(project, blog_site, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a dry run must never call Slack")

    monkeypatch.setattr(slack_delivery, "send", explode)
    report, _ = pipeline.run(project, run_at="2026-09-03", send=False)
    assert report.rows

    # A dry run IS recorded -- the collapse guardrail needs a baseline from
    # the shadow weeks -- but it is never marked as sent.
    records = history.read_all(project.history_path)
    assert len(records) == 1 and records[0]["sent"] is False
    assert not history.already_sent(project.history_path, "2026-08-27")


def test_sending_records_the_week_so_it_is_not_sent_twice(project, blog_site, monkeypatch):
    calls = []
    monkeypatch.setattr(slack_delivery, "send", lambda *a, **k: calls.append(a) or "1.0")

    pipeline.run(project, run_at="2026-09-03", send=True)
    assert len(calls) == 1
    assert history.already_sent(project.history_path, "2026-08-27")

    report, decisions = pipeline.run(project, run_at="2026-09-03", send=True)
    assert len(calls) == 1, "the second run must not re-send"
    assert report is None, "an already-sent week reports nothing to print"
    assert decisions == []

    pipeline.run(project, run_at="2026-09-03", send=True, force=True)
    assert len(calls) == 2, "--force must override the skip"


def test_a_blog_outage_still_reports_the_linkedin_column(project):
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, f"{BLOG_BASE}/rss.xml", status=503)
        report, _ = pipeline.collect(project, run_at="2026-09-03")

    assert report.blogs_ok is False
    assert all(row.blogs is None for row in report.rows)
    assert {row.name: row.linkedin_posts for row in report.rows}["Ashvath Narayan"] == 2
    assert not report.blocked


def test_both_sources_down_blocks_the_send(project):
    """Nothing to report is not a report. Better silence than a table of '?'."""
    broken = replace(project, fixture_path=project.fixture_path.parent / "gone.json")

    with responses.RequestsMock() as mock:
        mock.add(responses.GET, f"{BLOG_BASE}/rss.xml", status=503)
        report, decisions = pipeline.collect(broken, run_at="2026-09-03")

    assert report.blocked
    assert any(d.name == "both_sources_down" and d.fired for d in decisions)


def test_a_source_that_raises_unexpectedly_does_not_kill_the_other_column(project, blog_site, monkeypatch):
    class Exploding:
        name = "exploding"

        def fetch(self, members, week):
            raise RuntimeError("vendor changed their API")

    monkeypatch.setattr(pipeline, "build_linkedin_source", lambda config: Exploding())
    report, _ = pipeline.collect(project, run_at="2026-09-03")

    assert report.linkedin_ok is False
    assert all(row.linkedin_posts is None for row in report.rows)
    assert report.blogs_ok is True


def test_history_survives_a_truncated_line(project):
    project.history_path.parent.mkdir(parents=True, exist_ok=True)
    project.history_path.write_text('{"week_start": "2026-08-20", "sent": true}\n{"trunca\n')
    assert len(history.read_all(project.history_path)) == 1


def test_swapping_the_source_is_one_config_value(project):
    """The seam that makes a vendor disappearing a config edit, not a rewrite.
    manual_csv is now strictly better than before: it needs neither a
    credential nor the MCP server."""
    assert pipeline.build_linkedin_source(project).name == "fixture"
    assert pipeline.build_linkedin_source(replace(project, linkedin_source="manual_csv")).name == "manual_csv"
    assert (
        pipeline.build_linkedin_source(replace(project, linkedin_source="mcp")).name
        == "mcp_linkedin"
    )


def test_an_unknown_source_name_fails_loudly(project):
    with pytest.raises(config_module.ConfigError, match="unknown linkedin.source"):
        pipeline.build_linkedin_source(replace(project, linkedin_source="magic"))


# -- the send path ---------------------------------------------------------


def test_send_goes_through_the_mcp_server_and_names_no_destination():
    """The destination is the server's business. If it were a parameter, any
    process on the box could redirect the weekly report."""
    report = Report("W", "2026-08-27", "2026-09-02", "Asia/Kolkata", [Row("A", 1, 0)], True, True)
    captured = {}

    def caller(server_url, tool_name, arguments, **kwargs):
        captured.update({"url": server_url, "tool": tool_name, "args": arguments})
        return {"schema_version": 1, "status": "sent", "message_ts": "1725500000.000100"}

    ts = slack_delivery.send("http://127.0.0.1:8802/mcp", report, caller=caller)

    assert ts == "1725500000.000100"
    assert captured["tool"] == "post_weekly_report"
    assert set(captured["args"]) == {"week_key", "blocks", "fallback_text"}
    assert captured["args"]["week_key"] == "2026-08-27"
    assert any(block["type"] == "table" for block in captured["args"]["blocks"])


def test_a_server_side_slack_error_reaches_the_operator():
    from kpi_tracker.mcp_call import McpCallFailed

    def caller(*a, **k):
        raise McpCallFailed("slack rejected the message (missing_scope). add chat:write")

    report = Report("W", "2026-08-27", "2026-09-02", "Asia/Kolkata", [Row("A", 1, 0)], True, True)
    with pytest.raises(slack_delivery.SlackDeliveryError, match="chat:write"):
        slack_delivery.send("http://x", report, caller=caller)


def test_a_duplicate_send_reported_by_the_server_is_not_an_error():
    """The server enforces one report per week. A caller that tries twice gets
    the original timestamp back, not a failure."""
    report = Report("W", "2026-08-27", "2026-09-02", "Asia/Kolkata", [Row("A", 1, 0)], True, True)

    def caller(*a, **k):
        return {"schema_version": 1, "status": "skipped_duplicate", "message_ts": "111.222"}

    assert slack_delivery.send("http://x", report, caller=caller) == "111.222"


def test_version_skew_with_the_slack_server_is_refused():
    report = Report("W", "2026-08-27", "2026-09-02", "Asia/Kolkata", [Row("A", 1, 0)], True, True)

    def caller(*a, **k):
        return {"schema_version": 99, "status": "sent"}

    with pytest.raises(slack_delivery.SlackDeliveryError, match="schema_version"):
        slack_delivery.send("http://x", report, caller=caller)


def test_repeat_runs_of_one_week_do_not_flood_the_guardrail_baseline(project, blog_site):
    """Four dry runs of the same week must count as one week of history, or
    the collapse guardrail compares this week against copies of itself."""
    for _ in range(4):
        pipeline.run(project, run_at="2026-09-03", send=False)

    assert len(history.read_all(project.history_path)) == 4
    assert history.recent_linkedin_totals(project.history_path) == [3]


def test_the_baseline_is_ordered_oldest_to_newest_regardless_of_run_order(project, blog_site):
    """The guardrail reads this as a time series, so it must be in week order
    even when the runs happened out of order."""
    pipeline.run(project, run_at="2026-09-10", send=False)  # week 09-03..09-09: 0 posts
    pipeline.run(project, run_at="2026-09-03", send=False)  # week 08-27..09-02: 3 posts

    assert history.recent_linkedin_totals(project.history_path) == [3, 0]


def test_a_failed_week_does_not_enter_the_baseline(project):
    """An outage week must not teach the guardrail that zero is normal."""
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, f"{BLOG_BASE}/rss.xml", status=503)
        broken = replace(project, fixture_path=project.fixture_path.parent / "gone.json")
        pipeline.run(broken, run_at="2026-09-03", send=False)

    assert history.recent_linkedin_totals(project.history_path) == []


def test_sending_refuses_when_no_slack_server_is_configured(project, blog_site, monkeypatch):
    monkeypatch.setattr(
        slack_delivery, "send", lambda *a, **k: pytest.fail("must not reach the server")
    )
    with pytest.raises(config_module.ConfigError, match="mcp.slack_url"):
        pipeline.run(replace(project, slack_mcp_url=""), run_at="2026-09-03", send=True)


def test_an_already_sent_week_costs_no_vendor_credits(project, monkeypatch):
    """The skip must happen BEFORE fetching. Collecting first and discarding
    the result pays a vendor for data nobody reads."""
    fetched = []
    monkeypatch.setattr(slack_delivery, "send", lambda *a, **k: "1.0")

    class Counting:
        name = "counting"

        def fetch(self, members, week):
            fetched.append(week)
            from kpi_tracker.models import FetchResult

            return FetchResult(source="counting", ok=True, items=[])

    monkeypatch.setattr(pipeline, "build_linkedin_source", lambda config: Counting())

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, f"{BLOG_BASE}/rss.xml",
                 body=(FIXTURES / "blog" / "rss.xml").read_text())
        pipeline.run(project, run_at="2026-09-03", send=True)
        assert len(fetched) == 1

        pipeline.run(project, run_at="2026-09-03", send=True)
        assert len(fetched) == 1, "the skip must short-circuit before any fetch"


def test_a_dry_run_of_an_already_sent_week_still_shows_the_table(project, blog_site, monkeypatch):
    """The skip is about not re-sending, not about refusing to look."""
    monkeypatch.setattr(slack_delivery, "send", lambda *a, **k: "1.0")
    pipeline.run(project, run_at="2026-09-03", send=True)

    report, _ = pipeline.run(project, run_at="2026-09-03", send=False)
    assert report is not None and report.rows
