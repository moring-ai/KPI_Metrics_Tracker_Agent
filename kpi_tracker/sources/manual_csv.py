"""LinkedIn posts from a hand-maintained CSV.

The escape hatch. If the vendor is down, priced out, or shut down by a lawsuit,
someone pastes post URLs into a CSV and the weekly report still goes out. It is
also the cheapest way to sanity-check the vendor: run both, compare the table.

The CSV needs one column, `url`. A `handle` column is optional -- when it is
absent the author is read out of the permalink, which is where LinkedIn puts it
anyway. Blank lines and a leading '#' comment are ignored.

    url
    https://www.linkedin.com/posts/ashvath-narayanan_x-activity-7054425663348363266-iH3M
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from kpi_tracker import matching
from kpi_tracker.models import FetchResult, LinkedInPost, Member
from kpi_tracker.sources import linkedin_urn
from kpi_tracker.timewindow import Week

log = logging.getLogger(__name__)


class ManualCsvSource:
    name = "manual_csv"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def fetch(self, members: list[Member], week: Week) -> FetchResult:
        if not self._path.exists():
            return FetchResult.failed(self.name, f"{self._path} does not exist")

        known = {member.linkedin_handle for member in members}
        posts: list[LinkedInPost] = []
        warnings: list[str] = []
        seen: set[int] = set()

        with self._path.open(newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(_strip_comments(handle)) if r]

        for line_no, row in enumerate(rows, start=2):
            url = (row.get("url") or "").strip()
            if not url:
                continue

            try:
                stamp = linkedin_urn.posted_at_from_url(url)
            except linkedin_urn.UndecodablePost as exc:
                warnings.append(f"line {line_no}: {exc}")
                continue

            activity_id = linkedin_urn.extract_activity_id(url)
            if activity_id in seen:
                warnings.append(f"line {line_no}: duplicate post {activity_id}, counted once")
                continue
            seen.add(activity_id)

            author = matching.normalise(row.get("handle")) or matching.handle_from_post_url(url)
            if author not in known:
                warnings.append(f"line {line_no}: handle {author!r} is not in members.json")
                continue

            if not week.contains(stamp):
                continue  # outside the window is normal, not worth a warning

            posts.append(
                LinkedInPost(handle=author, url=url, posted_at=stamp, activity_id=activity_id)
            )

        return FetchResult(source=self.name, ok=True, items=posts, warnings=warnings)


def _strip_comments(lines):
    for line in lines:
        if not line.lstrip().startswith("#"):
            yield line
