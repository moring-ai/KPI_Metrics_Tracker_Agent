"""The contract between the KPI client and the MCP servers.

Deliberately stdlib-only. Both sides import this module, so it is the single
place the wire format is defined -- but the client must never pull `mcp`,
`pydantic`, `starlette` or `boto3` into its import path, or the offline test
suite stops being offline (and stops being fast).

WHY THE SHAPE IS WHAT IT IS
---------------------------
Two decisions here are load-bearing, and both exist because of bugs this
project already paid for once. See docs/ARCHITECTURE.md §8b and §8c.

1. NO DATES ON THE WIRE. The server returns post permalinks and nothing else
   time-related. The client derives every timestamp itself by decoding the
   activity id (kpi_tracker/sources/linkedin_urn.py). That is not duplication
   for its own sake: the vendor's own date handling silently discarded 11 of
   17 posts, and the decoder is pinned by 16 tests against LinkedIn's own
   published values. If the server sent dates, a regression in its decoding
   would ship without those tests ever running.

2. ONE ENTRY PER REQUESTED PROFILE -- "accounted for". The server must return a
   result for every profile it was asked about, each with an explicit status.
   A profile that is absent from the response is treated by the client as
   FAILED, not as "no posts".

   This inverts a real defect. The natural design is to send back a list of
   posts; then a profile the vendor could not scrape simply has no posts, which
   is byte-for-byte identical to a person who published nothing, and renders a
   confident `0` about a named colleague. A missing field or a dropped entry
   must degrade towards "we do not know", never towards zero.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

# Statuses a server may report for one profile.
STATUS_OK = "ok"  # the profile was read; post_urls is complete (possibly empty)
STATUS_FAILED = "failed"  # the profile could not be read; count is unknown
PROFILE_STATUSES = (STATUS_OK, STATUS_FAILED)

# Statuses the Slack server may report for a send.
SEND_SENT = "sent"
SEND_SKIPPED_DUPLICATE = "skipped_duplicate"
SEND_FAILED = "failed"
SEND_STATUSES = (SEND_SENT, SEND_SKIPPED_DUPLICATE, SEND_FAILED)

# Tool names, so a typo is a NameError here rather than a runtime "unknown tool".
TOOL_COLLECT_POSTS = "collect_linkedin_posts"
TOOL_POST_REPORT = "post_weekly_report"
TOOL_CHECK = "check_credentials"
TOOL_POST_ALERT = "post_alert"


class WireError(ValueError):
    """The payload did not match this contract. Always treat as 'unknown'."""


def parse_collection(payload: object, requested_urls: list[str]) -> dict[str, list[str]]:
    """Validate a collect_linkedin_posts result and return {profile_url: post_urls}.

    Only profiles the server explicitly reported as OK appear in the result.
    Everything else -- absent, unknown status, malformed -- is left out, and the
    caller renders those people as unknown. Raises WireError if the payload as
    a whole cannot be trusted, which the caller turns into a failed fetch.
    """
    if not isinstance(payload, dict):
        raise WireError(f"expected an object, got {type(payload).__name__}")

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise WireError(
            f"server speaks schema_version {version!r}, this client speaks "
            f"{SCHEMA_VERSION} -- refusing to guess at the difference"
        )

    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise WireError("payload has no 'profiles' list")

    by_url: dict[str, list[str]] = {}
    for entry in profiles:
        if not isinstance(entry, dict):
            continue
        url = entry.get("profile_url")
        # Only an explicit STATUS_OK yields a countable number. Every other
        # value -- STATUS_FAILED, a status this version does not know, or a
        # missing one -- leaves the person out, and the caller renders a dash.
        # That default is the whole safety property, so it is a single check
        # rather than a validation step plus a filter that could drift apart.
        if not isinstance(url, str) or entry.get("status") != STATUS_OK:
            continue
        post_urls = entry.get("post_urls")
        if not isinstance(post_urls, list):
            continue  # claimed ok but gave us nothing usable -> unknown
        by_url[url] = [u for u in post_urls if isinstance(u, str) and u]

    return by_url


def profile_errors(payload: object) -> dict[str, str]:
    """{profile_url: error text} for profiles the server reported as failed."""
    if not isinstance(payload, dict):
        return {}
    errors = {}
    for entry in payload.get("profiles") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == STATUS_FAILED and isinstance(entry.get("profile_url"), str):
            errors[entry["profile_url"]] = str(entry.get("error") or "no reason given")
    return errors
