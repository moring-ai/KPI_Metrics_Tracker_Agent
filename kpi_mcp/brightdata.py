"""The Bright Data vendor client. Runs inside the LinkedIn MCP server.

Moved here from kpi_tracker/sources/brightdata.py so the credential lives only
in the server process. The behaviour is unchanged -- every comment below
records a defect this code already cost us once, so read before simplifying.

WHAT THIS RETURNS, AND WHAT IT DELIBERATELY DOES NOT
----------------------------------------------------
It returns, per requested profile, either the post permalinks it read or the
reason it could not read them. It does NOT return dates, does not window, does
not deduplicate and does not attribute posts to people. All of that stays on
the client, where it is covered by tests that this server's deploys do not run.
See kpi_tracker/wire.py for the reasoning.
"""

from __future__ import annotations

import logging
import os
import time

import requests

from kpi_tracker.matching import handle_from_post_url, normalise

log = logging.getLogger(__name__)

TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"
SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}"
DATASET_ID = "gd_lyy3tktm25m4avu764"  # LinkedIn posts, discover by profile URL

POLL_INTERVAL_SECONDS = 10

# How long to wait for the vendor before giving up.
#
# The default is 30 minutes: measured collections for six profiles ranged 1m35s
# to 9m39s, and a 10-minute ceiling once finished 21 seconds inside itself. A
# tighter ceiling silently loses a week rather than reporting late.
#
# It is configurable because AWS Lambda caps a function at 900s, which is less
# than this default -- so on Lambda the ceiling MUST come down or Lambda kills
# the function mid-poll and you get an opaque timeout instead of an honest
# "snapshot not ready after Ns". Set KPI_VENDOR_POLL_TIMEOUT below the Lambda
# timeout; kpi_tracker.mcp_call reads the same variable and keeps the client's
# own ceiling above this one automatically.
POLL_TIMEOUT_ENV_VAR = "KPI_VENDOR_POLL_TIMEOUT"
DEFAULT_POLL_TIMEOUT_SECONDS = 1800.0


def configured_poll_timeout() -> float:
    raw = os.environ.get(POLL_TIMEOUT_ENV_VAR)
    if not raw:
        return DEFAULT_POLL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %.0fs",
                    POLL_TIMEOUT_ENV_VAR, raw, DEFAULT_POLL_TIMEOUT_SECONDS)
        return DEFAULT_POLL_TIMEOUT_SECONDS
    if value <= 0:
        log.warning("%s must be positive; using %.0fs",
                    POLL_TIMEOUT_ENV_VAR, DEFAULT_POLL_TIMEOUT_SECONDS)
        return DEFAULT_POLL_TIMEOUT_SECONDS
    return value


POLL_TIMEOUT_SECONDS = configured_poll_timeout()
PROGRESS_LOG_EVERY_SECONDS = 60

# A transient 400 was observed in practice: a byte-identical request replayed
# minutes later succeeded. Auth failures are excluded -- retrying a rejected
# token only delays a clear error.
TRIGGER_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3
NO_RETRY_STATUSES = frozenset({401, 403})

# The vendor phrases "this profile had nothing in that period" as an error row.
# That is a real zero, not a failure, and must not be reported as unknown.
_NO_POSTS_MARKERS = ("no posts found", "no posts for", "0 posts found")


class VendorFailure(RuntimeError):
    """The whole collection failed. Never contains the credential."""


def collect_post_urls(token: str, profile_urls: list[str], *, session=None) -> dict:
    """Read every authored post permalink for each profile.

    Returns {"ok": {profile_url: [post_url, ...]}, "failed": {profile_url: reason}}.
    Raises VendorFailure if the collection as a whole could not be run.
    """
    if not profile_urls:
        return {"ok": {}, "failed": {}}

    session = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    snapshot_id = _trigger(session, headers, profile_urls)
    records = _await_snapshot(session, headers, snapshot_id)
    return _sort_records(records, profile_urls)


# -- vendor calls ---------------------------------------------------------


def _trigger(session, headers: dict, profile_urls: list[str]) -> str:
    # No start_date/end_date, deliberately. The vendor's own message explains
    # why: "Total posts: 17, with dates: 6 ... not all posts contain dates, so
    # they will be filtered out if the date is specified in the input. For a
    # better experience, we recommend leaving input.start_date, input.end_date
    # empty." Server-side filtering discarded two thirds of the posts and made
    # every profile return nothing.
    payload = [{"url": url, "only_authored_posts": True} for url in profile_urls]
    params = {
        "dataset_id": DATASET_ID,
        "type": "discover_new",
        "discover_by": "profile_url",
        # Without this the vendor stays SILENT about a profile it failed to
        # scrape: that person simply has no rows, which is identical to having
        # published nothing, and renders as a confident 0.
        "include_errors": "true",
    }

    last_error = ""
    for attempt in range(1, TRIGGER_ATTEMPTS + 1):
        response = session.post(
            TRIGGER_URL, headers=headers, params=params, json=payload, timeout=60
        )
        if response.ok:
            snapshot_id = response.json().get("snapshot_id")
            if not snapshot_id:
                raise VendorFailure(f"trigger returned no snapshot_id: {response.text[:300]}")
            log.info("snapshot %s triggered for %d profiles", snapshot_id, len(profile_urls))
            return snapshot_id

        # Always carry the response body: without it a 400 says nothing about
        # what was wrong, which is how a transient failure becomes an
        # afternoon of guessing.
        last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code in NO_RETRY_STATUSES:
            raise VendorFailure(f"bright data rejected the credentials -- {last_error}")
        if attempt < TRIGGER_ATTEMPTS:
            delay = _retry_after(response) or RETRY_BACKOFF_SECONDS * attempt
            log.warning(
                "trigger attempt %d/%d failed (%s); retrying in %ss",
                attempt, TRIGGER_ATTEMPTS, last_error, delay,
            )
            time.sleep(delay)

    raise VendorFailure(f"trigger failed after {TRIGGER_ATTEMPTS} attempts -- {last_error}")


def _await_snapshot(session, headers: dict, snapshot_id: str) -> list[dict]:
    """Poll until the snapshot is ready. HTTP 202 means 'still running'."""
    started = time.monotonic()
    deadline = started + POLL_TIMEOUT_SECONDS
    next_log = started + PROGRESS_LOG_EVERY_SECONDS

    while True:
        response = session.get(
            SNAPSHOT_URL.format(snapshot_id=snapshot_id),
            headers=headers,
            params={"format": "json"},
            timeout=60,
        )
        if response.status_code == 202:
            now = time.monotonic()
            if now >= deadline:
                raise VendorFailure(
                    f"snapshot {snapshot_id} not ready after {POLL_TIMEOUT_SECONDS}s"
                )
            if now >= next_log:
                log.info(
                    "still collecting snapshot %s (%.0fs elapsed, giving up at %ds)",
                    snapshot_id, now - started, POLL_TIMEOUT_SECONDS,
                )
                next_log = now + PROGRESS_LOG_EVERY_SECONDS
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if not response.ok:
            raise VendorFailure(
                f"snapshot {snapshot_id} fetch failed -- "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        log.info("snapshot %s ready after %.0fs", snapshot_id, time.monotonic() - started)
        return _records_from(response.json())


def _records_from(body: object) -> list[dict]:
    """The rows in a snapshot response, or an exception.

    Bright Data answers 200 with a JSON *object* in several non-success states
    -- {"status": "running"} and {"status": "failed"} among them -- and does not
    always use 202. Reading those as "no rows" would report zero posts for the
    whole team as though it were a fact.
    """
    if isinstance(body, list):
        return body  # an empty result legitimately returns an empty list
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    raise VendorFailure(f"unexpected snapshot body: {str(body)[:200]}")


# -- turning vendor rows into per-profile outcomes ------------------------


def _sort_records(records: list[dict], profile_urls: list[str]) -> dict:
    """Group post permalinks by profile, and record which profiles failed.

    Every requested profile ends up in exactly one of the two buckets. That is
    the property the wire contract depends on: a profile the vendor never
    mentioned must not silently become "zero posts".
    """
    wanted = {normalise(url): url for url in profile_urls}
    found: dict[str, list[str]] = {url: [] for url in profile_urls}
    failed: dict[str, str] = {}
    unreadable = 0
    post_rows = 0

    for record in records:
        if record.get("error") or record.get("error_code"):
            message = str(record.get("error") or record.get("error_code"))
            handle = normalise((record.get("input") or {}).get("url"))
            # "No posts found for the selected period" means the profile was
            # read fine and had nothing. Calling that unknown would put a dash
            # in front of a number we actually know.
            if _means_no_posts(message):
                continue
            # Anything else is "we could not read this profile". Biased on
            # purpose: a failure mistaken for zero is a false claim about
            # someone's week; a zero mistaken for a failure is a dash we can
            # re-run.
            if handle in wanted:
                failed[wanted[handle]] = message
            else:
                log.warning("vendor error for a profile we did not ask about: %s", message)
            continue

        post_rows += 1
        url = record.get("url") or record.get("post_url") or ""
        handle = handle_from_post_url(url) or normalise(
            record.get("user_url") or record.get("user_id")
        )
        if handle is None:
            unreadable += 1
            continue
        if handle in wanted:
            found[wanted[handle]].append(url)

    # If nothing could be read at all, the vendor's URL format has probably
    # changed -- that is a failure, not an empty week for everyone.
    if post_rows and unreadable == post_rows:
        raise VendorFailure(
            f"all {post_rows} records had unreadable permalinks -- "
            "the vendor's URL format has probably changed"
        )

    for url in list(found):
        if url in failed:
            found.pop(url)
    return {"ok": found, "failed": failed}


def _means_no_posts(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _NO_POSTS_MARKERS)


def _retry_after(response) -> float | None:
    """Honour the server's own backoff instruction when it gives one."""
    raw = response.headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw else None
    except (TypeError, ValueError):
        return None
