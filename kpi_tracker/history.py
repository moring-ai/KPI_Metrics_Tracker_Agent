"""A one-line-per-run record of every report, in data/history.jsonl.

Two jobs. It is the baseline the collapse guardrail compares against -- you
cannot tell "the scraper broke" from "a quiet week" without knowing what
previous weeks looked like. And it makes the run idempotent: if this week has
already been reported, we skip rather than double-message the CTO.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from kpi_tracker import storage
from kpi_tracker.models import Report


def append(path: str | Path, report: Report, *, sent: bool) -> None:
    """Add one line. `path` may be a local path or an s3://bucket/key URI.

    S3 has no append, so the remote case is a read-modify-write. Safe here:
    one process writes this file, once a week.
    """
    record = json.dumps(asdict(report) | {"sent": sent}, sort_keys=True)
    existing = storage.read_text(path)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    storage.write_text(path, existing + record + "\n")


def read_all(path: str | Path) -> list[dict]:
    records = []
    for line in storage.read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a truncated line should not break the whole run
    return records


def already_sent(path: str | Path, week_start: str) -> bool:
    return any(
        record.get("week_start") == week_start and record.get("sent")
        for record in read_all(path)
    )


def recent_linkedin_totals(path: str | Path, *, limit: int = 4) -> list[int]:
    """LinkedIn post totals from the last few successful runs, newest last.

    One entry per week, not per run: a week re-run several times (dry runs
    while setting up, or a --force re-send) must not fill the baseline with
    copies of itself and drown out the weeks either side.
    """
    by_week: dict[str, int] = {}
    for record in read_all(path):
        if not record.get("linkedin_ok"):
            continue
        counts = [row.get("linkedin_posts") for row in record.get("rows", [])]
        by_week[record.get("week_start")] = sum(c for c in counts if isinstance(c, int))
    return [by_week[week] for week in sorted(by_week)][-limit:]
