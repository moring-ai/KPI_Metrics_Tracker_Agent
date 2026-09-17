"""LinkedIn posts from a JSON file. Used by the tests and by --source fixture.

Having a source with no network in it means the whole pipeline -- matching,
aggregation, guardrails, Slack payload -- is testable end to end without a
vendor account, and a reviewer can run the thing on their laptop in a second.

    [{"handle": "ashvath-narayanan",
      "url": "https://www.linkedin.com/posts/...-activity-7054425663348363266-iH3M"}]
"""

from __future__ import annotations

import json
from pathlib import Path

from kpi_tracker import matching
from kpi_tracker.models import FetchResult, LinkedInPost, Member
from kpi_tracker.sources import linkedin_urn
from kpi_tracker.timewindow import Week


class FixtureSource:
    name = "fixture"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def fetch(self, members: list[Member], week: Week) -> FetchResult:
        if not self._path.exists():
            return FetchResult.failed(self.name, f"{self._path} does not exist")

        try:
            records = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return FetchResult.failed(self.name, f"{self._path} is not valid JSON: {exc}")

        known = {member.linkedin_handle for member in members}
        posts, warnings = [], []

        for record in records:
            url = record.get("url", "")
            try:
                stamp = linkedin_urn.posted_at_from_url(url)
            except linkedin_urn.UndecodablePost as exc:
                warnings.append(str(exc))
                continue

            handle = matching.normalise(record.get("handle")) or matching.handle_from_post_url(url)
            if handle not in known or not week.contains(stamp):
                continue

            posts.append(
                LinkedInPost(
                    handle=handle,
                    url=url,
                    posted_at=stamp,
                    activity_id=linkedin_urn.extract_activity_id(url),
                )
            )

        return FetchResult(source=self.name, ok=True, items=posts, warnings=warnings)
