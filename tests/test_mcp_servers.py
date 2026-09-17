"""The MCP servers, exercised through a real MCP client.

The SDK can connect a client directly to a server object in-process, which
means these are genuine protocol round trips -- tool discovery, argument
validation, structured results, error channels -- with no sockets and no
vendor. That is the only way to catch the class of bug the fake caller in
test_mcp_linkedin.py cannot: a mismatch between the schema the server publishes
and the keys the client actually reads.
"""

import asyncio
import json

import pytest

mcp_sdk = pytest.importorskip("mcp", reason="the MCP SDK is only needed for the servers")

from mcp import Client  # noqa: E402

from kpi_mcp import brightdata, linkedin_server, slack_server  # noqa: E402
from kpi_tracker import wire  # noqa: E402
from kpi_tracker.mcp_call import McpCallFailed, _text_of  # noqa: E402

ALICE = "https://www.linkedin.com/in/alice"
BOB = "https://www.linkedin.com/in/bob"
POST_A = "https://www.linkedin.com/posts/alice_x-activity-7499027998312443453-aB3x"


@pytest.fixture(autouse=True)
def clean_server_env(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_BACKEND", "env")
    monkeypatch.setenv("KPI_LINKEDIN_SECRET_ID", "TEST_BD_SECRET")
    monkeypatch.setenv("TEST_BD_SECRET", "vendor-token-value")
    monkeypatch.setenv("KPI_SLACK_SECRET_ID", "TEST_SLACK_SECRET")
    monkeypatch.setenv("TEST_SLACK_SECRET", "xoxb-1111-2222-abcdefghij")
    monkeypatch.setenv("KPI_SLACK_DESTINATION", "C0TEST")


def call(server, tool_name, arguments=None):
    """One real round trip. Mirrors kpi_tracker.mcp_call semantics exactly.

    Note the raise happens AFTER the `async with` exits, exactly as the real
    _call does. Raising inside the block gets the exception wrapped in anyio's
    BaseExceptionGroup ("unhandled errors in a TaskGroup"), which is precisely
    the shape mcp_call.call_tool has to flatten -- and is why it catches
    BaseException rather than Exception.
    """
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool_name, arguments or {})

    result = asyncio.run(go())
    if result.is_error:
        raise McpCallFailed(_text_of(result))
    assert result.structured_content is not None, (
        "the tool lost its typed return annotation -- a plain '-> dict' makes the "
        "SDK publish no output schema and return structured_content=None"
    )
    return result.structured_content


def schema_of(server, tool_name):
    async def go():
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}[tool_name]
    return asyncio.run(go())


# -- kpi-linkedin ---------------------------------------------------------


def test_the_linkedin_tool_returns_one_accounted_for_entry_per_profile(monkeypatch):
    monkeypatch.setattr(
        linkedin_server, "collect_post_urls",
        lambda token, urls, session=None: {"ok": {ALICE: [POST_A]}, "failed": {}},
    )
    payload = call(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS, {"profile_urls": [ALICE, BOB]})

    assert payload["schema_version"] == wire.SCHEMA_VERSION
    by_url = {p["profile_url"]: p for p in payload["profiles"]}
    assert by_url[ALICE]["status"] == wire.STATUS_OK
    # Bob was never mentioned by the vendor. The server must say so explicitly
    # rather than leaving the client to read an absence as zero.
    assert by_url[BOB]["status"] == wire.STATUS_FAILED
    assert "no result" in by_url[BOB]["error"]


def test_the_server_fetches_the_credential_itself(monkeypatch):
    """The client sends profile URLs and nothing else -- there is no token
    parameter to send."""
    seen = {}

    def fake(token, urls, session=None):
        seen["token"] = token
        return {"ok": {ALICE: []}, "failed": {}}

    monkeypatch.setattr(linkedin_server, "collect_post_urls", fake)
    call(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS, {"profile_urls": [ALICE]})

    assert seen["token"] == "vendor-token-value"
    assert "token" not in schema_of(
        linkedin_server.mcp, wire.TOOL_COLLECT_POSTS
    ).input_schema["properties"]


def test_a_missing_credential_is_reported_as_a_tool_error(monkeypatch):
    monkeypatch.delenv("TEST_BD_SECRET")
    with pytest.raises(McpCallFailed, match="credential unavailable"):
        call(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS, {"profile_urls": [ALICE]})


def test_a_vendor_failure_reaches_the_client_as_a_readable_reason(monkeypatch):
    def boom(token, urls, session=None):
        raise brightdata.VendorFailure("snapshot not ready after 1800s")

    monkeypatch.setattr(linkedin_server, "collect_post_urls", boom)
    with pytest.raises(McpCallFailed, match="snapshot not ready after 1800s"):
        call(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS, {"profile_urls": [ALICE]})


def test_an_unexpected_server_crash_does_not_leak_its_message(monkeypatch):
    """A bare exception's text is withheld by the SDK. That is why every
    expected failure is raised as ToolError instead."""
    def boom(token, urls, session=None):
        raise RuntimeError("BRIGHTDATA_API_TOKEN=deadbeef-0000-4000-8000 in a crash")

    monkeypatch.setattr(linkedin_server, "collect_post_urls", boom)
    with pytest.raises(McpCallFailed) as caught:
        call(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS, {"profile_urls": [ALICE]})
    assert "deadbeef" not in str(caught.value)


def test_an_empty_roster_needs_no_credential(monkeypatch):
    monkeypatch.delenv("TEST_BD_SECRET")
    payload = call(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS, {"profile_urls": []})
    assert payload["profiles"] == []


def test_check_credentials_confirms_access_without_returning_the_secret():
    payload = call(linkedin_server.mcp, wire.TOOL_CHECK)
    assert payload["ok"] is True
    assert "vendor-token-value" not in json.dumps(payload)


def test_check_credentials_reports_a_missing_secret(monkeypatch):
    monkeypatch.delenv("TEST_BD_SECRET")
    payload = call(linkedin_server.mcp, wire.TOOL_CHECK)
    assert payload["ok"] is False and "TEST_BD_SECRET" in payload["detail"]


# -- the schema contract --------------------------------------------------


def test_the_published_schema_contains_every_key_the_client_reads():
    """The bug a fake caller cannot catch: the server renames a field and the
    client silently stops finding it."""
    tool = schema_of(linkedin_server.mcp, wire.TOOL_COLLECT_POSTS)
    published = json.dumps(tool.output_schema)

    for key in ("schema_version", "profiles", "profile_url", "status", "post_urls", "error"):
        assert f'"{key}"' in published, f"the client reads {key!r}; the server no longer publishes it"
    assert list(tool.input_schema["properties"]) == ["profile_urls"]


def test_the_slack_schema_contains_every_key_the_client_reads():
    tool = schema_of(slack_server.mcp, wire.TOOL_POST_REPORT)
    published = json.dumps(tool.output_schema)
    for key in ("schema_version", "status", "message_ts"):
        assert f'"{key}"' in published


# -- kpi-slack ------------------------------------------------------------


class FakeWebClient:
    posted: list = []

    def __init__(self, token=None):
        self.retry_handlers = []

    def chat_postMessage(self, **kwargs):
        FakeWebClient.posted.append(kwargs)
        return {"ts": "1788784986.507629"}


@pytest.fixture
def fake_slack(monkeypatch, tmp_path):
    FakeWebClient.posted = []
    monkeypatch.setenv("KPI_SLACK_STATE_PATH", str(tmp_path / "sent.json"))
    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", FakeWebClient)
    return FakeWebClient


BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]


def test_the_destination_is_not_a_parameter_and_comes_from_the_server(fake_slack):
    """If it were a parameter, any process on the box could redirect the
    weekly report at somebody else."""
    assert "channel" not in schema_of(
        slack_server.mcp, wire.TOOL_POST_REPORT
    ).input_schema["properties"]

    payload = call(
        slack_server.mcp, wire.TOOL_POST_REPORT,
        {"week_key": "2026-08-27", "blocks": BLOCKS, "fallback_text": "weekly"},
    )
    assert payload["status"] == wire.SEND_SENT
    assert fake_slack.posted[0]["channel"] == "C0TEST"


def test_the_same_week_is_only_posted_once(fake_slack):
    """Enforced next to the token, not only in the caller's history file."""
    args = {"week_key": "2026-08-27", "blocks": BLOCKS, "fallback_text": "weekly"}
    first = call(slack_server.mcp, wire.TOOL_POST_REPORT, args)
    second = call(slack_server.mcp, wire.TOOL_POST_REPORT, args)

    assert first["status"] == wire.SEND_SENT
    assert second["status"] == wire.SEND_SKIPPED_DUPLICATE
    assert second["message_ts"] == first["message_ts"]
    assert len(fake_slack.posted) == 1, "the channel must not be posted to twice"


def test_a_different_week_still_posts(fake_slack):
    for week in ("2026-08-27", "2026-09-03"):
        call(slack_server.mcp, wire.TOOL_POST_REPORT,
             {"week_key": week, "blocks": BLOCKS, "fallback_text": "weekly"})
    assert len(fake_slack.posted) == 2


@pytest.mark.parametrize("bad,match", [
    ({"week_key": "", "blocks": BLOCKS, "fallback_text": "x"}, "week_key is required"),
    ({"week_key": "w", "blocks": [], "fallback_text": "x"}, "blocks is empty"),
    ({"week_key": "w", "blocks": BLOCKS, "fallback_text": " "}, "fallback_text is required"),
])
def test_the_server_refuses_a_malformed_report(fake_slack, bad, match):
    with pytest.raises(McpCallFailed, match=match):
        call(slack_server.mcp, wire.TOOL_POST_REPORT, bad)


def test_a_missing_destination_is_refused_rather_than_guessed(fake_slack, monkeypatch):
    monkeypatch.delenv("KPI_SLACK_DESTINATION")
    with pytest.raises(McpCallFailed, match="KPI_SLACK_DESTINATION"):
        call(slack_server.mcp, wire.TOOL_POST_REPORT,
             {"week_key": "w", "blocks": BLOCKS, "fallback_text": "x"})


def test_the_wrong_slack_token_type_fails_the_check(monkeypatch):
    """Observed for real: an xapp- token was configured and the old client-side
    check reported it as fine. It cannot call chat.postMessage."""
    monkeypatch.setenv("TEST_SLACK_SECRET", "xapp-1-A00EXAMPLE00-1200-82fe40")
    payload = call(slack_server.mcp, wire.TOOL_CHECK)
    assert payload["ok"] is False and "app-level" in payload["detail"]


def test_the_slack_check_reports_a_missing_destination(monkeypatch):
    monkeypatch.delenv("KPI_SLACK_DESTINATION")
    payload = call(slack_server.mcp, wire.TOOL_CHECK)
    assert payload["ok"] is False and "KPI_SLACK_DESTINATION" in payload["detail"]


def test_the_slack_check_never_returns_the_token():
    payload = call(slack_server.mcp, wire.TOOL_CHECK)
    assert payload["ok"] is True
    assert "xoxb-1111" not in json.dumps(payload)


# -- the whole chain, no fakes -------------------------------------------
#
# Client() accepts a server object as well as a URL, so the PRODUCTION
# call_tool can talk to a real server in-process. These are the only tests
# where nothing is stubbed except the vendor's HTTP: real source, real
# asyncio bridge, real protocol, real server, real wire validation.


def end_to_end(members, week, vendor_result=None, vendor_raises=None, monkeypatch=None):
    from kpi_tracker.sources.mcp_linkedin import McpLinkedInSource

    def fake_vendor(token, urls, session=None):
        if vendor_raises:
            raise vendor_raises
        return vendor_result

    monkeypatch.setattr(linkedin_server, "collect_post_urls", fake_vendor)
    # server_url is the server object itself; every other layer is production.
    return McpLinkedInSource(linkedin_server.mcp).fetch(members, week)


def test_end_to_end_a_mixed_week_renders_correctly(monkeypatch):
    from datetime import datetime, timezone

    from kpi_tracker.models import Member
    from kpi_tracker.timewindow import last_week

    def post(handle, iso):
        ms = int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)
        return f"https://www.linkedin.com/posts/{handle}_x-activity-{(ms << 22) | 0x1F2E3D}-aB3x"

    members = [Member("Alice", "alice"), Member("Bob", "bob"),
               Member("Carol", "carol"), Member("Dave", "dave")]
    week = last_week("2026-09-03", "Asia/Kolkata", "thursday")  # Thu 08-27 .. Wed 09-02

    result = end_to_end(
        members, week, monkeypatch=monkeypatch,
        vendor_result={
            "ok": {
                "https://www.linkedin.com/in/alice": [
                    post("alice", "2026-08-28T09:00:00"),
                    post("alice", "2026-08-30T09:00:00"),
                    post("alice", "2026-07-01T09:00:00"),   # outside the window
                ],
                "https://www.linkedin.com/in/carol": [],     # read fine, published nothing
            },
            "failed": {"https://www.linkedin.com/in/bob": "Crawler error: Minimal layout"},
            # Dave is absent entirely -- the vendor never mentioned him.
        },
    )

    counts = {
        m.name: None if result.is_unknown(m.linkedin_handle)
        else sum(p.handle == m.linkedin_handle for p in result.items)
        for m in members
    }
    assert counts == {"Alice": 2, "Bob": None, "Carol": 0, "Dave": None}, (
        "Carol published nothing (0); Bob failed and Dave was never mentioned (both unknown)"
    )
    assert result.ok is True


def test_end_to_end_a_vendor_failure_becomes_a_failed_fetch(monkeypatch):
    from kpi_tracker.models import Member
    from kpi_tracker.timewindow import last_week

    result = end_to_end(
        [Member("Alice", "alice")],
        last_week("2026-09-03", "Asia/Kolkata", "thursday"),
        vendor_raises=brightdata.VendorFailure("snapshot not ready after 1800s"),
        monkeypatch=monkeypatch,
    )
    assert result.ok is False
    assert "snapshot not ready" in result.error


def test_end_to_end_a_crash_is_a_failed_fetch_and_leaks_nothing(monkeypatch):
    from kpi_tracker.models import Member
    from kpi_tracker.timewindow import last_week

    result = end_to_end(
        [Member("Alice", "alice")],
        last_week("2026-09-03", "Asia/Kolkata", "thursday"),
        vendor_raises=RuntimeError("BRIGHTDATA_API_TOKEN=deadbeef-0000-4000-8000 boom"),
        monkeypatch=monkeypatch,
    )
    assert result.ok is False
    assert "deadbeef" not in (result.error or "")


def test_end_to_end_check_reaches_the_real_server():
    from kpi_tracker.sources.mcp_linkedin import McpLinkedInSource

    ok, detail = McpLinkedInSource(linkedin_server.mcp).check()
    assert ok is True and "readable" in detail


# -- the dead-man's alert -------------------------------------------------


def test_an_alert_is_not_deduplicated_like_a_report(fake_slack):
    """If the run fails three weeks running, that must be three messages. The
    report is idempotent per week; an alert must not be."""
    for _ in range(3):
        call(slack_server.mcp, wire.TOOL_POST_ALERT, {"text": "the weekly run failed"})
    assert len(fake_slack.posted) == 3


def test_an_alert_goes_to_the_same_fixed_destination(fake_slack):
    call(slack_server.mcp, wire.TOOL_POST_ALERT, {"text": "the weekly run failed"})
    assert fake_slack.posted[0]["channel"] == "C0TEST"
    assert "channel" not in schema_of(
        slack_server.mcp, wire.TOOL_POST_ALERT
    ).input_schema["properties"]


def test_an_empty_alert_is_refused(fake_slack):
    with pytest.raises(McpCallFailed, match="text is required"):
        call(slack_server.mcp, wire.TOOL_POST_ALERT, {"text": "   "})


def test_an_alert_does_not_consume_the_weeks_idempotency_slot(fake_slack):
    """An alert must not make the real report look already-sent."""
    call(slack_server.mcp, wire.TOOL_POST_ALERT, {"text": "run failed"})
    payload = call(
        slack_server.mcp, wire.TOOL_POST_REPORT,
        {"week_key": "2026-08-27", "blocks": BLOCKS, "fallback_text": "weekly"},
    )
    assert payload["status"] == wire.SEND_SENT
