"""The client/server contract.

Every case here answers one question: when the payload is wrong, does the
client degrade towards "we do not know" or towards zero? Towards zero is the
failure this system exists to prevent, so a malformed response must never
produce a countable number.
"""

import pytest

from kpi_tracker import wire

URL_A = "https://www.linkedin.com/in/alice"
URL_B = "https://www.linkedin.com/in/bob"


def payload(*profiles, version=wire.SCHEMA_VERSION):
    return {"schema_version": version, "profiles": list(profiles)}


def ok(url, posts):
    return {"profile_url": url, "status": wire.STATUS_OK, "post_urls": posts}


def failed(url, error="crawl_error"):
    return {"profile_url": url, "status": wire.STATUS_FAILED, "error": error}


def test_a_good_payload_parses():
    parsed = wire.parse_collection(payload(ok(URL_A, ["u1", "u2"])), [URL_A])
    assert parsed == {URL_A: ["u1", "u2"]}


def test_an_ok_profile_with_no_posts_is_a_real_zero():
    """Distinct from a failure: the server read the profile and it was empty."""
    assert wire.parse_collection(payload(ok(URL_A, [])), [URL_A]) == {URL_A: []}


def test_a_failed_profile_is_absent_from_the_parse_so_the_caller_marks_it_unknown():
    parsed = wire.parse_collection(payload(ok(URL_A, ["u1"]), failed(URL_B)), [URL_A, URL_B])
    assert URL_B not in parsed
    assert wire.profile_errors(payload(failed(URL_B, "Minimal layout"))) == {URL_B: "Minimal layout"}


def test_a_profile_the_server_never_mentions_is_absent_too():
    """The whole point of "accounted for": silence is not zero."""
    parsed = wire.parse_collection(payload(ok(URL_A, ["u1"])), [URL_A, URL_B])
    assert URL_B not in parsed


@pytest.mark.parametrize("bad,reason", [
    ({"profiles": []}, "no schema_version"),
    ({"schema_version": 2, "profiles": []}, "version skew"),
    ({"schema_version": wire.SCHEMA_VERSION}, "no profiles list"),
    ({"schema_version": wire.SCHEMA_VERSION, "profiles": "nope"}, "profiles not a list"),
    ("a string", "not an object"),
    (None, "null"),
])
def test_an_untrustworthy_payload_is_refused_outright(bad, reason):
    """Refusing means the caller reports a failed fetch -- every count '—'."""
    with pytest.raises(wire.WireError):
        wire.parse_collection(bad, [URL_A])


@pytest.mark.parametrize("entry", [
    {"profile_url": URL_A, "status": "weird"},              # unknown status
    {"profile_url": URL_A},                                  # no status
    {"status": wire.STATUS_OK, "post_urls": []},             # no profile_url
    {"profile_url": URL_A, "status": wire.STATUS_OK},        # ok but no post_urls
    {"profile_url": URL_A, "status": wire.STATUS_OK, "post_urls": "u1"},  # not a list
    "not even an object",
])
def test_a_malformed_entry_leaves_that_person_unknown_rather_than_zero(entry):
    assert wire.parse_collection(payload(entry), [URL_A]) == {}


def test_non_string_post_urls_are_dropped_not_counted():
    parsed = wire.parse_collection(payload(ok(URL_A, ["u1", None, 42, "", "u2"])), [URL_A])
    assert parsed == {URL_A: ["u1", "u2"]}


def test_version_skew_says_which_side_is_which():
    with pytest.raises(wire.WireError, match="server speaks schema_version 99"):
        wire.parse_collection(payload(version=99), [URL_A])


def test_profile_errors_is_safe_on_rubbish():
    assert wire.profile_errors(None) == {}
    assert wire.profile_errors({"profiles": ["x", 1, None]}) == {}


def test_a_failed_profile_with_no_reason_still_gets_one():
    assert wire.profile_errors(payload({"profile_url": URL_A, "status": wire.STATUS_FAILED})) == {
        URL_A: "no reason given"
    }


def test_the_tool_names_are_constants_so_a_typo_is_not_a_runtime_surprise():
    assert wire.TOOL_COLLECT_POSTS and wire.TOOL_POST_REPORT and wire.TOOL_CHECK
    assert wire.STATUS_OK in wire.PROFILE_STATUSES
    assert wire.SEND_SENT in wire.SEND_STATUSES


def test_only_an_explicit_ok_yields_a_countable_number():
    """Anything else -- failed, a status from a newer server, a missing one --
    must leave the person out so the caller renders a dash. This is the single
    check the whole safety property rests on."""
    for status in (wire.STATUS_FAILED, "partial", "OK", "", None, 1, True):
        entry = {"profile_url": URL_A, "status": status, "post_urls": ["u1"]}
        assert wire.parse_collection(payload(entry), [URL_A]) == {}, (
            f"status={status!r} produced a countable number"
        )
