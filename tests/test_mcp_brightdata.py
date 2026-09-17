"""The Bright Data vendor protocol, now inside the LinkedIn MCP server.

These cases moved here from test_sources.py when the credential moved into the
server. Every one records a defect found by review or by running the thing for
real -- see docs/ARCHITECTURE.md §8b and §8c. The assertions are unchanged in
substance; only the return shape differs, because the server hands back
permalinks per profile rather than a FetchResult.
"""

import pytest
import responses

from kpi_mcp import brightdata
from kpi_mcp.brightdata import VendorFailure, collect_post_urls

TRIGGER = "https://api.brightdata.com/datasets/v3/trigger"
SNAPSHOT = "https://api.brightdata.com/datasets/v3/snapshot/snap-1"

ALICE = "https://www.linkedin.com/in/alice"
BOB = "https://www.linkedin.com/in/bob"
PROFILES = [ALICE, BOB]

POST_A = "https://www.linkedin.com/posts/alice_x-activity-7499027998312443453-aB3x"
POST_B = "https://www.linkedin.com/posts/bob_y-activity-7478009502107643453-aB3x"

# The exact shape the vendor returns for a profile it could not read.
CRAWL_ERROR = {
    "timestamp": "2026-09-07T11:58:17.409Z",
    "input": {"url": BOB, "only_authored_posts": True},
    "error": "Crawler error: Minimal layout detected, retrying",
    "error_code": "crawl_error",
}
# ...and for a profile it read fine that simply had nothing.
NO_POSTS = {
    "input": {"url": BOB},
    "error": (
        "Total posts: 17, with dates: 6. No posts found for the selected period: "
        "Wed Aug 19 2026 - Wed Aug 26 2026."
    ),
    "error_code": "crawl_error",
}


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(brightdata.time, "sleep", lambda _s: None)


def collect(records, profiles=PROFILES, *, trigger_status=None, trigger_body=None):
    with responses.RequestsMock() as mock:
        if trigger_status:
            mock.add(responses.POST, TRIGGER, status=trigger_status, body=trigger_body or "")
        else:
            mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, json=records)
        return collect_post_urls("token", profiles)


# -- the happy path -------------------------------------------------------


def test_triggers_then_polls_then_groups_posts_by_profile():
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, status=202)  # still running
        mock.add(responses.GET, SNAPSHOT, json=[{"url": POST_A}, {"url": POST_B}])
        out = collect_post_urls("token", PROFILES)

    assert out["ok"] == {ALICE: [POST_A], BOB: [POST_B]}
    assert out["failed"] == {}


def test_no_profiles_does_no_work():
    assert collect_post_urls("token", []) == {"ok": {}, "failed": {}}


def test_an_empty_result_is_a_quiet_week_for_everyone():
    out = collect([])
    assert out["ok"] == {ALICE: [], BOB: []}
    assert out["failed"] == {}


def test_a_dict_body_with_a_data_list_is_accepted():
    out = collect({"data": [{"url": POST_A}]})
    assert out["ok"][ALICE] == [POST_A]


# -- the request we send --------------------------------------------------


def test_we_ask_for_authored_posts_only_and_send_no_date_filter():
    """The vendor's own filter drops any post whose date it could not parse --
    it reported "Total posts: 17, with dates: 6" and returned nothing for every
    profile. We window locally instead."""
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, json=[])
        collect_post_urls("token", PROFILES)
        import json

        body = json.loads(mock.calls[0].request.body)
        url = mock.calls[0].request.url

    assert all(entry["only_authored_posts"] is True for entry in body)
    assert not any("start_date" in entry or "end_date" in entry for entry in body)
    # Without include_errors the vendor stays SILENT about failed profiles, and
    # the person renders as a confident zero.
    assert "include_errors=true" in url


# -- unknown vs zero ------------------------------------------------------


def test_a_crawl_error_marks_that_profile_failed():
    out = collect([{"url": POST_A}, CRAWL_ERROR])
    assert out["ok"] == {ALICE: [POST_A]}
    assert "Minimal layout detected" in out["failed"][BOB]
    assert BOB not in out["ok"], "a failed profile must not also appear as zero"


def test_no_posts_in_period_is_a_real_zero_not_a_failure():
    """The vendor reports an empty result as an error row. Treating it as a
    failure would put a dash in front of a number we actually know."""
    out = collect([NO_POSTS])
    assert out["failed"] == {}
    assert out["ok"][BOB] == []


def test_an_unrecognised_error_is_treated_as_a_failure():
    """Biased on purpose: a failure read as zero is a false claim about
    someone's week; a zero read as a failure is a dash we can re-run."""
    mystery = {"input": {"url": BOB}, "error": "Something entirely new", "error_code": "weird"}
    out = collect([mystery])
    assert BOB in out["failed"]


def test_an_error_for_a_profile_we_did_not_ask_about_is_harmless():
    stranger = {"input": {"url": "https://www.linkedin.com/in/nobody"},
                "error": "Crawler error", "error_code": "crawl_error"}
    out = collect([stranger, {"url": POST_A}])
    assert out["failed"] == {}
    assert out["ok"][ALICE] == [POST_A]


def test_records_we_cannot_read_at_all_are_a_failure_not_a_zero():
    """If the vendor changes its permalink format, every row becomes
    unreadable. That is 'we could not read the answer', not 'nobody posted'."""
    with pytest.raises(VendorFailure, match="unreadable permalinks"):
        collect([{"url": "https://www.linkedin.com/newformat/abc"} for _ in range(3)])


@pytest.mark.parametrize("body", [
    {"status": "running", "message": "Snapshot is not ready yet"},
    {"status": "failed", "error": "snapshot expired"},
    {"unexpected": "shape"},
])
def test_a_non_list_snapshot_body_is_a_failure_not_an_empty_week(body):
    """Bright Data answers 200 with an object in several non-success states and
    does not always use 202."""
    with pytest.raises(VendorFailure, match="unexpected snapshot body"):
        collect(body)


# -- reliability ----------------------------------------------------------


def test_a_transient_trigger_failure_is_retried():
    """Observed for real: a 400 that succeeded on a byte-identical replay."""
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, status=400, body='{"error":"try again"}')
        mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, json=[{"url": POST_A}])
        out = collect_post_urls("token", PROFILES)

    assert out["ok"][ALICE] == [POST_A]


def test_the_vendors_own_error_body_is_always_reported():
    """A bare 'HTTP 400' is undiagnosable."""
    with responses.RequestsMock() as mock:
        for _ in range(brightdata.TRIGGER_ATTEMPTS):
            mock.add(responses.POST, TRIGGER, status=400, body='{"error":"bad dataset_id"}')
        with pytest.raises(VendorFailure, match="bad dataset_id"):
            collect_post_urls("token", PROFILES)


def test_bad_credentials_fail_immediately_without_retrying():
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, status=401, body='{"error":"invalid token"}')
        with pytest.raises(VendorFailure, match="credentials"):
            collect_post_urls("token", PROFILES)
        assert len(mock.calls) == 1, "a 401 must not be retried"


def test_a_retry_after_header_is_honoured(monkeypatch):
    slept = []
    monkeypatch.setattr(brightdata.time, "sleep", lambda s: slept.append(s))
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, status=429, headers={"Retry-After": "7"})
        mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, json=[])
        collect_post_urls("token", PROFILES)
    assert 7 in slept


def test_a_snapshot_http_error_reports_the_body():
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, status=500, body='{"error":"snapshot exploded"}')
        with pytest.raises(VendorFailure, match="snapshot exploded"):
            collect_post_urls("token", PROFILES)


def test_a_trigger_with_no_snapshot_id_is_an_error():
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, json={"unexpected": "shape"})
        with pytest.raises(VendorFailure, match="no snapshot_id"):
            collect_post_urls("token", PROFILES)


def test_it_gives_up_rather_than_hanging_the_weekly_run(monkeypatch):
    monkeypatch.setattr(brightdata, "POLL_TIMEOUT_SECONDS", 0)
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, TRIGGER, json={"snapshot_id": "snap-1"})
        mock.add(responses.GET, SNAPSHOT, status=202)
        with pytest.raises(VendorFailure, match="not ready"):
            collect_post_urls("token", PROFILES)


def test_the_poll_ceiling_is_generous_enough_for_observed_collection_times():
    """Measured runs for six profiles ranged 1m35s to 9m39s. A 10-minute
    ceiling once finished 21 seconds inside itself."""
    assert brightdata.POLL_TIMEOUT_SECONDS >= 1800


def test_the_client_timeout_is_larger_than_this_servers_poll_ceiling():
    """If the client gives up first we get a bare timeout with no information
    and the server keeps working for nothing."""
    from kpi_tracker.mcp_call import DEFAULT_TIMEOUT_SECONDS

    assert DEFAULT_TIMEOUT_SECONDS > brightdata.POLL_TIMEOUT_SECONDS
