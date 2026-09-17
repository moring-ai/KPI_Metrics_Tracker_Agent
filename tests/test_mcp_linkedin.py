"""The MCP-backed LinkedIn source: the client half of the migration.

A fake caller stands in for the server, so every semantic is tested without a
server, a network or the mcp package. The one real round trip through an actual
MCP server lives in test_mcp_servers.py.

What this file protects is the property the whole system rests on: a person
whose data we could not get renders as a dash, and only a person we genuinely
read renders as a number.
"""

from datetime import datetime, timezone

import pytest

from kpi_tracker import wire
from kpi_tracker.mcp_call import McpCallFailed
from kpi_tracker.models import Member
from kpi_tracker.sources.mcp_linkedin import McpLinkedInSource
from kpi_tracker.timewindow import last_week

WEEK = last_week("2026-09-03", "Asia/Kolkata", "thursday")  # Thu 08-27 .. Wed 09-02

MEMBERS = [
    Member("Alice", "alice"),
    Member("Bob", "bob"),
    Member("Carol", "carol"),
]


def activity_id(iso_utc: str) -> int:
    ms = int(datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return (ms << 22) | 0x1F2E3D


def post(handle: str, iso_utc: str) -> str:
    return f"https://www.linkedin.com/posts/{handle}_x-activity-{activity_id(iso_utc)}-aB3x"


IN_1 = post("alice", "2026-08-28T09:00:00")
IN_2 = post("alice", "2026-08-30T09:00:00")
OUT = post("alice", "2026-07-01T09:00:00")


def caller_returning(payload):
    def caller(server_url, tool_name, arguments, *, timeout_seconds=None):
        return payload
    return caller


def caller_raising(message):
    def caller(server_url, tool_name, arguments, *, timeout_seconds=None):
        raise McpCallFailed(message)
    return caller


def entry(handle, posts=None, status=wire.STATUS_OK, error=None):
    body = {"profile_url": f"https://www.linkedin.com/in/{handle}", "status": status}
    if posts is not None:
        body["post_urls"] = posts
    if error:
        body["error"] = error
    return body


def collection(*entries):
    return {"schema_version": wire.SCHEMA_VERSION, "profiles": list(entries)}


def counts(result, members=MEMBERS):
    """What the table would show: an int, or None for a dash."""
    return {
        m.name: None
        if result.is_unknown(m.linkedin_handle)
        else sum(p.handle == m.linkedin_handle for p in result.items)
        for m in members
    }


# -- the happy path -------------------------------------------------------


def test_posts_are_counted_per_person_and_windowed_locally():
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(
            entry("alice", [IN_1, IN_2, OUT]), entry("bob", []), entry("carol", []),
        ))
    ).fetch(MEMBERS, WEEK)

    assert result.ok
    assert counts(result) == {"Alice": 2, "Bob": 0, "Carol": 0}


def test_the_client_dates_posts_itself_and_the_server_sends_no_dates():
    """The decoder is pinned by 16 tests the server's deploy never runs, so it
    stays here. The payload carries permalinks only."""
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(entry("alice", [IN_1])))
    ).fetch([MEMBERS[0]], WEEK)

    assert result.items[0].posted_at == datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)


def test_no_members_asks_the_server_nothing():
    def explode(*a, **k):
        raise AssertionError("must not call the server for an empty roster")

    assert McpLinkedInSource("http://x", caller=explode).fetch([], WEEK).ok


# -- unknown, not zero ----------------------------------------------------


def test_a_failed_profile_is_a_dash_while_a_quiet_one_is_zero():
    """The distinction the migration most easily loses."""
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(
            entry("alice", [IN_1]),
            entry("bob", status=wire.STATUS_FAILED, error="Minimal layout detected"),
            entry("carol", []),
        ))
    ).fetch(MEMBERS, WEEK)

    assert counts(result) == {"Alice": 1, "Bob": None, "Carol": 0}
    assert result.unknown_keys == frozenset({"bob"})
    assert any("Minimal layout detected" in w for w in result.warnings)


def test_a_person_the_server_never_mentions_is_a_dash_not_a_zero():
    """The defect a naive port reintroduces: absence read as zero."""
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(entry("alice", [IN_1])))
    ).fetch(MEMBERS, WEEK)

    assert counts(result) == {"Alice": 1, "Bob": None, "Carol": None}
    assert any("did not report on this profile" in w for w in result.warnings)


def test_a_dropped_post_urls_field_makes_that_person_unknown_not_zero():
    """The exact schema-drift case: 'ok' with the list missing."""
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(entry("alice", None)))
    ).fetch([MEMBERS[0]], WEEK)

    assert result.ok is False or counts(result, [MEMBERS[0]])["Alice"] is None


def test_every_profile_failing_is_a_failed_fetch_not_a_table_of_dashes():
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(
            *(entry(m.linkedin_handle, status=wire.STATUS_FAILED) for m in MEMBERS)
        ))
    ).fetch(MEMBERS, WEEK)

    assert result.ok is False
    assert "could not read any" in result.error


# -- transport and protocol failures --------------------------------------


def test_a_dead_server_is_a_failed_fetch():
    result = McpLinkedInSource(
        "http://x", caller=caller_raising("ConnectError: connection refused")
    ).fetch(MEMBERS, WEEK)

    assert result.ok is False and result.items == []
    assert "connection refused" in result.error


@pytest.mark.parametrize("payload", [
    {"schema_version": 99, "profiles": []},
    {"profiles": []},
    {"schema_version": wire.SCHEMA_VERSION},
    "not an object",
])
def test_an_unusable_response_is_a_failed_fetch(payload):
    result = McpLinkedInSource("http://x", caller=caller_returning(payload)).fetch(MEMBERS, WEEK)
    assert result.ok is False
    assert "unusable response" in result.error


# -- the same hygiene the direct vendor client had ------------------------


def test_a_duplicate_post_is_counted_once():
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(entry("alice", [IN_1, IN_1])))
    ).fetch([MEMBERS[0]], WEEK)

    assert len(result.items) == 1
    assert any("duplicate" in w for w in result.warnings)


def test_a_repeated_out_of_window_post_is_silent():
    """Windowing runs before dedup, so a repeat of something nobody counts
    produces no warning noise."""
    result = McpLinkedInSource(
        "http://x", caller=caller_returning(collection(entry("alice", [OUT, OUT, IN_1])))
    ).fetch([MEMBERS[0]], WEEK)

    assert len(result.items) == 1
    assert result.warnings == []


def test_an_undecodable_permalink_is_skipped_with_a_warning():
    result = McpLinkedInSource(
        "http://x",
        caller=caller_returning(collection(entry("alice", ["https://linkedin.com/in/alice"]))),
    ).fetch([MEMBERS[0]], WEEK)

    assert result.ok and result.items == []
    assert any("undecodable" in w for w in result.warnings)


def test_the_request_sends_profile_urls_and_nothing_else():
    """No dates on the wire, and no credential."""
    captured = {}

    def caller(server_url, tool_name, arguments, *, timeout_seconds=None):
        captured.update({"tool": tool_name, "args": arguments})
        return collection(entry("alice", []))

    McpLinkedInSource("http://x", caller=caller).fetch([MEMBERS[0]], WEEK)

    assert captured["tool"] == wire.TOOL_COLLECT_POSTS
    assert set(captured["args"]) == {"profile_urls"}
    assert captured["args"]["profile_urls"] == ["https://www.linkedin.com/in/alice"]


# -- check() --------------------------------------------------------------


def test_check_reports_what_the_server_says():
    ok, detail = McpLinkedInSource(
        "http://x",
        caller=caller_returning(
            {"schema_version": wire.SCHEMA_VERSION, "ok": True, "detail": "secret readable"}
        ),
    ).check()
    assert ok and detail == "secret readable"


def test_check_fails_loudly_on_version_skew():
    ok, detail = McpLinkedInSource(
        "http://x", caller=caller_returning({"schema_version": 99, "ok": True})
    ).check()
    assert ok is False and "schema_version" in detail


def test_check_fails_when_the_server_is_unreachable():
    ok, detail = McpLinkedInSource("http://x", caller=caller_raising("refused")).check()
    assert ok is False and "refused" in detail
