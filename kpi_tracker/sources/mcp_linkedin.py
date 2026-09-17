"""LinkedIn posts via the kpi-linkedin MCP server.

One more implementation of the LinkedInSource protocol in base.py, alongside
fixture, manual_csv and (previously) brightdata. Nothing downstream changes:
pipeline.py, report.py and guardrails.py cannot tell which source produced a
FetchResult.

This client holds NO credential. It asks the server for permalinks, and does
everything else itself -- deliberately:

  * DATES. Every timestamp is decoded here from the activity id in the
    permalink (linkedin_urn), never taken from the server. That decoder is
    pinned by 16 tests against LinkedIn's own published values, and those tests
    do not run when the server is deployed. A server-side regression would
    otherwise ship unnoticed.

  * WINDOWING and DEDUPLICATION. Cheap, already tested, and keeping them here
    means the server has no notion of "the reporting week" to get wrong.

  * WHO IS UNKNOWN. A profile the server reports as failed, or does not mention
    at all, becomes an entry in unknown_keys -- so that person renders as a
    dash rather than a confident zero.
"""

from __future__ import annotations

import logging

from kpi_tracker import wire
from kpi_tracker.matching import normalise
from kpi_tracker.mcp_call import DEFAULT_TIMEOUT_SECONDS, McpCallFailed, call_tool
from kpi_tracker.models import FetchResult, LinkedInPost, Member
from kpi_tracker.sources import linkedin_urn
from kpi_tracker.timewindow import Week

log = logging.getLogger(__name__)


class McpLinkedInSource:
    name = "mcp_linkedin"

    def __init__(
        self,
        server_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        caller=call_tool,
    ) -> None:
        self._server_url = server_url
        self._timeout = timeout_seconds
        # Injected so the semantics below can be tested without a server.
        self._caller = caller

    def fetch(self, members: list[Member], week: Week) -> FetchResult:
        if not members:
            return FetchResult(source=self.name, ok=True, items=[])

        by_url = {member.profile_url: member for member in members}

        try:
            payload = self._caller(
                self._server_url,
                wire.TOOL_COLLECT_POSTS,
                {"profile_urls": list(by_url)},
                timeout_seconds=self._timeout,
            )
        except McpCallFailed as exc:
            # The whole call failed, so nobody's count is known.
            return FetchResult.failed(self.name, str(exc))

        try:
            ok_profiles = wire.parse_collection(payload, list(by_url))
        except wire.WireError as exc:
            return FetchResult.failed(self.name, f"unusable response: {exc}")

        errors = wire.profile_errors(payload)
        posts: list[LinkedInPost] = []
        warnings: list[str] = []
        unknown: set[str] = set()
        seen: set[int] = set()

        for profile_url, member in by_url.items():
            if profile_url not in ok_profiles:
                # Failed, or absent from the response. Either way: unknown.
                reason = errors.get(profile_url, "the server did not report on this profile")
                warnings.append(f"no data for {member.linkedin_handle}: {reason}")
                unknown.add(member.linkedin_handle)
                continue

            for url in ok_profiles[profile_url]:
                try:
                    stamp = linkedin_urn.posted_at_from_url(url)
                except linkedin_urn.UndecodablePost as exc:
                    warnings.append(f"skipped undecodable post: {exc}")
                    continue

                if not week.contains(stamp):
                    continue  # expected: we ask for the whole profile, window here

                activity_id = linkedin_urn.extract_activity_id(url)
                if activity_id in seen:
                    warnings.append(f"duplicate post {activity_id}, counted once")
                    continue
                seen.add(activity_id)

                posts.append(
                    LinkedInPost(
                        handle=member.linkedin_handle,
                        url=url,
                        posted_at=stamp,
                        activity_id=activity_id,
                    )
                )

        # Everyone failing is not a week of zeros; it is nothing to report.
        if unknown and len(unknown) == len(members):
            return FetchResult.failed(
                self.name, f"the server could not read any of the {len(members)} profiles"
            )

        return FetchResult(
            source=self.name,
            ok=True,
            items=posts,
            warnings=warnings,
            unknown_keys=frozenset(unknown),
        )

    def check(self) -> tuple[bool, str]:
        """Ask the server whether it can reach its credential. Used by `check`."""
        try:
            payload = self._caller(
                self._server_url, wire.TOOL_CHECK, {}, timeout_seconds=30.0
            )
        except McpCallFailed as exc:
            return False, str(exc)
        if payload.get("schema_version") != wire.SCHEMA_VERSION:
            return False, f"server speaks schema_version {payload.get('schema_version')!r}"
        return bool(payload.get("ok")), str(payload.get("detail") or "")
