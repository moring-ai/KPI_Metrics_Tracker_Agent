"""The Lambda entry point.

Thin on purpose, so what is tested is the seam rather than the logic: does the
event shape do what it claims, and -- the part that matters -- does a run that
produced no report make the INVOCATION fail?

On EC2 a failed run trips a systemd OnFailure unit that posts an alert. Lambda
has no such thing: its only failure signal is the invocation's own Errors
metric. So anything meaning "no report went out" must raise, or the schedule
will keep firing into silence and silence looks exactly like success.
"""

import json
import types
from datetime import datetime, timezone

import pytest

import lambda_handler


def activity_url(handle: str, iso_utc: str) -> str:
    ms = int(datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return f"https://www.linkedin.com/posts/{handle}_x-activity-{(ms << 22) | 0x1F2E3D}-aB3x"


class FakeContext:
    """Lambda's context object, as far as this handler uses it."""

    def __init__(self, remaining_seconds: float = 900.0):
        self._remaining = remaining_seconds

    def get_remaining_time_in_millis(self):
        return int(self._remaining * 1000)


@pytest.fixture
def deployed(tmp_path, monkeypatch, blog_site):
    """A configured Lambda deployment with the vendor and Slack stubbed."""
    from kpi_mcp import linkedin_server, slack_server

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "members.json").write_text(json.dumps([
        {"name": "Alice", "linkedin_url": "https://www.linkedin.com/in/alice"},
        {"name": "Bob", "linkedin_url": "https://www.linkedin.com/in/bob"},
    ]))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "timezone": "Asia/Kolkata",
        "week_start_day": "thursday",
        "members_path": str(tmp_path / "data" / "members.json"),
        "history_path": str(tmp_path / "data" / "history.jsonl"),
        "linkedin": {"source": "fixture", "target_posts_per_week": 3,
                     "fixture_path": str(tmp_path / "unused.json")},
        "blog": {"base_url": "https://blog.test"},
        "mcp": {"linkedin_url": "unused", "slack_url": "unused"},
        "slack": {"destination_label": "#marketing-kpi"},
    }))
    monkeypatch.setenv("KPI_CONFIG_PATH", str(config))
    monkeypatch.setenv("KPI_SECRET_BACKEND", "env")
    monkeypatch.setenv("KPI_LINKEDIN_SECRET_ID", "TEST_BD")
    monkeypatch.setenv("TEST_BD", "vendor-token")
    monkeypatch.setenv("KPI_SLACK_SECRET_ID", "TEST_SLACK")
    monkeypatch.setenv("TEST_SLACK", "xoxb-1111-2222-abcdefghij")
    monkeypatch.setenv("KPI_SLACK_DESTINATION", "C0TEST")
    monkeypatch.setenv("KPI_SLACK_STATE_PATH", str(tmp_path / "sent.json"))

    monkeypatch.setattr(
        linkedin_server, "collect_post_urls",
        lambda token, urls, session=None: {
            "ok": {u: ([activity_url("alice", "2026-08-28T09:00:00")] if "alice" in u else [])
                   for u in urls},
            "failed": {},
        },
    )
    posted = []

    class FakeWebClient:
        def __init__(self, token=None):
            self.retry_handlers = []

        def chat_postMessage(self, **kwargs):
            posted.append(kwargs)
            return {"ts": "1788784986.507629"}

    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", FakeWebClient)
    return types.SimpleNamespace(config=config, posted=posted, tmp=tmp_path)


# -- the event contract ---------------------------------------------------


def test_a_dry_run_computes_everything_and_sends_nothing(deployed):
    result = lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext())

    assert result["status"] == "dry_run"
    assert result["sent"] is False
    assert deployed.posted == []
    assert {r["name"] for r in result["rows"]} == {"Alice", "Bob"}


def test_a_real_run_posts_once_and_reports_what_it_did(deployed):
    result = lambda_handler.handler({"week": "2026-09-03"}, FakeContext())

    assert result["status"] == "sent" and result["sent"] is True
    assert len(deployed.posted) == 1
    assert result["week"] == "Thu 2026-08-27 to Wed 2026-09-02"


def test_the_event_overrides_the_environment_so_a_dry_run_needs_no_redeploy(
    deployed, monkeypatch
):
    monkeypatch.setenv("AGENT_SEND", "true")
    assert lambda_handler.handler({"send": False, "week": "2026-09-03"}, FakeContext())["sent"] is False


def test_agent_send_false_in_the_environment_is_honoured(deployed, monkeypatch):
    monkeypatch.setenv("AGENT_SEND", "false")
    assert lambda_handler.handler({"week": "2026-09-03"}, FakeContext())["sent"] is False


def test_a_missing_event_is_treated_as_a_plain_run(deployed, monkeypatch):
    """EventBridge sends {} on a schedule."""
    monkeypatch.setenv("AGENT_SEND", "false")
    assert lambda_handler.handler({}, FakeContext())["status"] == "dry_run"
    assert lambda_handler.handler(None, FakeContext())["status"] == "dry_run"


def test_the_result_is_json_serialisable(deployed):
    """Lambda serialises the return value; a dataclass in there fails at the
    very end, after the report has already been sent."""
    json.dumps(lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext()))


# -- failure must fail the invocation -------------------------------------


def test_a_blocked_report_raises_so_the_errors_metric_moves(deployed, monkeypatch):
    """A guardrail block means nothing reached the channel. Returning quietly
    would leave the schedule firing into silence."""
    from kpi_mcp import linkedin_server

    def vendor_down(token, urls, session=None):
        from kpi_mcp.brightdata import VendorFailure

        raise VendorFailure("snapshot not ready")

    monkeypatch.setattr(linkedin_server, "collect_post_urls", vendor_down)
    import responses

    with responses.RequestsMock() as mock:
        mock.add(responses.GET, "https://blog.test/rss.xml", status=503)
        with pytest.raises(lambda_handler.ReportNotSent, match="blocked"):
            lambda_handler.handler({"week": "2026-09-03"}, FakeContext())

    assert deployed.posted == [], "nothing may be sent when the report is blocked"


def test_a_broken_config_raises_rather_than_returning_a_success(deployed, monkeypatch):
    from kpi_tracker import config as config_module

    monkeypatch.setenv("KPI_CONFIG_PATH", "/nonexistent/config.json")
    with pytest.raises(config_module.ConfigError):
        lambda_handler.handler({}, FakeContext())


def test_an_already_reported_week_returns_quietly_without_fetching(deployed):
    lambda_handler.handler({"week": "2026-09-03"}, FakeContext())
    again = lambda_handler.handler({"week": "2026-09-03"}, FakeContext())

    assert again["status"] == "already_reported"
    assert len(deployed.posted) == 1, "the second invocation must not post again"


# -- the Lambda-specific timeout trap -------------------------------------


def test_it_warns_when_lambda_would_die_before_the_vendor_poll_gives_up(
    deployed, monkeypatch, caplog
):
    """The invariant everywhere else is that the inner timeout fires first. On
    Lambda the outer one is capped at 900s, below this project's 1800s default,
    and getting it wrong is silent until a slow week."""
    import logging

    monkeypatch.setattr("kpi_mcp.brightdata.POLL_TIMEOUT_SECONDS", 1800.0)
    with caplog.at_level(logging.ERROR):
        lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext(900))

    assert any("MISCONFIGURED" in r.message for r in caplog.records)
    assert any("KPI_VENDOR_POLL_TIMEOUT" in str(r.message) for r in caplog.records)


def test_it_stays_quiet_when_the_timeouts_are_ordered_correctly(deployed, monkeypatch, caplog):
    import logging

    monkeypatch.setattr("kpi_mcp.brightdata.POLL_TIMEOUT_SECONDS", 780.0)
    with caplog.at_level(logging.ERROR):
        lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext(900))

    assert not any("MISCONFIGURED" in r.message for r in caplog.records)


def test_no_context_is_tolerated(deployed):
    """Local invocation and some test harnesses pass none."""
    assert lambda_handler.handler({"week": "2026-09-03", "send": False}, None)["status"] == "dry_run"


def test_a_local_history_path_is_refused_on_lambda(deployed, monkeypatch):
    """/var/task is read-only and does not survive an invocation. A local path
    means the collapse guardrail gets no baseline and a retry double-posts --
    both silent. Caught before the vendor call, not after the message is sent."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "kpi-weekly")
    # Anchored at the start of the message, and deliberately so. Two looser
    # assertions both passed with this check DISABLED: "s3://" appears in the
    # Slack state check's message too, and "history_path" appears inside the
    # tmp_path that pytest names after this very test function.
    with pytest.raises(lambda_handler.ReportNotSent, match=r"^history_path is"):
        lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext())
    assert deployed.posted == []


def test_an_s3_history_path_is_accepted_on_lambda(deployed, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "kpi-weekly")
    monkeypatch.setenv("KPI_HISTORY_PATH", "s3://kpi-bucket/kpi-tracker/history.jsonl")
    monkeypatch.setenv("KPI_SLACK_STATE_PATH", "s3://kpi-bucket/kpi-tracker/slack-sent.json")

    import sys
    import types

    store = {}

    class FakeS3:
        def get_object(self, Bucket, Key):  # noqa: N803
            from botocore.exceptions import ClientError

            if (Bucket, Key) not in store:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": types.SimpleNamespace(read=lambda: store[(Bucket, Key)].encode())}

        def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
            store[(Bucket, Key)] = Body.decode()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: FakeS3()))
    result = lambda_handler.handler({"week": "2026-09-03"}, FakeContext())

    assert result["sent"] is True
    assert store, "the run must have written its history to S3"


def test_a_local_history_path_is_fine_off_lambda(deployed, monkeypatch):
    """The same code runs on EC2, where a local path is exactly right."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext())["sent"] is False


def test_a_local_slack_state_path_is_refused_on_lambda(deployed, monkeypatch):
    """`update-function-configuration --environment` REPLACES the environment,
    so flipping AGENT_SEND without re-listing every variable silently drops
    this one -- and it is the only thing stopping a retry double-posting."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "kpi-weekly")
    monkeypatch.setenv("KPI_HISTORY_PATH", "s3://kpi-bucket/kpi-tracker/history.jsonl")
    monkeypatch.delenv("KPI_SLACK_STATE_PATH", raising=False)

    with pytest.raises(lambda_handler.ReportNotSent, match="SLACK_STATE_PATH"):
        lambda_handler.handler({"week": "2026-09-03", "send": False}, FakeContext())
    assert deployed.posted == []


def test_a_failed_history_write_does_not_undo_a_successful_send(deployed, monkeypatch, caplog):
    """The message is already in the channel. Dying here would fail the
    invocation, fire the alarm for a week that WAS reported, and -- worse --
    let the retry send it again. On Lambda the failure is a botocore
    ClientError, which is not an OSError, so the narrower clause missed it."""
    import logging

    from kpi_tracker import history

    def write_fails(*args, **kwargs):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    monkeypatch.setattr(history, "append", write_fails)
    with caplog.at_level(logging.ERROR):
        result = lambda_handler.handler({"week": "2026-09-03"}, FakeContext())

    assert result["sent"] is True, "the send succeeded; the run must not be marked failed"
    assert len(deployed.posted) == 1
    assert any("could not write" in r.message or "record" in r.message.lower()
               for r in caplog.records), "the failure must still be loud in the log"
