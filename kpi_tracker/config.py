"""Configuration for the KPI client.

Settings live in config.json so they can be reviewed in a diff. There are NO
credentials here and none in this process's environment: the MCP servers hold
the tokens and fetch them from AWS Secrets Manager themselves. What this file
holds is the addresses of those servers.

Everything is validated on load. A weekly job that only runs 52 times a year
should not discover a typo at the moment it tries to message the CTO.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kpi_tracker.timewindow import weekday_number

VALID_SOURCES = ("mcp", "manual_csv", "fixture")


class ConfigError(ValueError):
    """config.json is missing something, or says something impossible."""


def _storage_location(value: str) -> str | Path:
    """A local path becomes a Path; an s3:// URI stays a string.

    Path("s3://bucket/key") silently normalises to "s3:/bucket/key" -- one
    slash -- which is not a valid URI and does not fail until something tries
    to write, long after the vendor call has been paid for.
    """
    from kpi_tracker import storage

    return value if storage.is_remote(value) else Path(value)


@dataclass(frozen=True)
class Config:
    timezone: str
    week_start_day: str
    linkedin_source: str
    target_posts_per_week: int
    manual_csv_path: Path
    fixture_path: Path
    blog_base_url: str
    slack_destination_label: str
    members_path: Path
    # str when it is an s3:// URI, Path when it is a local file. NOT always a
    # Path: pathlib collapses the double slash, turning s3://bucket/key into
    # s3:/bucket/key, which is not a valid URI and fails only at write time.
    history_path: str | Path

    # MCP server endpoints. This process holds NO credentials: the servers do,
    # and they fetch them from AWS Secrets Manager themselves. See kpi_mcp/.
    linkedin_mcp_url: str = ""
    slack_mcp_url: str = ""


def load(path: str | Path = "config.json", *, env: dict | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found -- copy config.example.json to config.json")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    env = os.environ if env is None else env
    linkedin = raw.get("linkedin", {})
    slack = raw.get("slack", {})
    mcp_config = raw.get("mcp", {})

    timezone = raw.get("timezone", "Asia/Kolkata")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone {timezone!r} is not a valid IANA zone") from exc

    week_start_day = raw.get("week_start_day", "thursday")
    try:
        weekday_number(week_start_day)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    source = linkedin.get("source", "manual_csv")
    if source not in VALID_SOURCES:
        raise ConfigError(f"linkedin.source must be one of {VALID_SOURCES}, got {source!r}")

    target = linkedin.get("target_posts_per_week", 3)
    if not isinstance(target, int) or target < 1:
        raise ConfigError(f"linkedin.target_posts_per_week must be a positive int, got {target!r}")

    # No slack.destination here on purpose. It is configured on the kpi-slack
    # server (KPI_SLACK_DESTINATION), so nothing on this machine can redirect
    # the weekly report. `destination_label` is kept purely for the dry-run
    # message, and is cosmetic.

    config = Config(
        timezone=timezone,
        week_start_day=week_start_day,
        linkedin_source=source,
        target_posts_per_week=target,
        manual_csv_path=Path(linkedin.get("manual_csv_path", "data/linkedin_posts.csv")),
        fixture_path=Path(linkedin.get("fixture_path", "tests/fixtures/linkedin_posts.json")),
        blog_base_url=raw.get("blog", {}).get("base_url", "https://www.moring.ai"),
        slack_destination_label=slack.get("destination_label", "the configured Slack channel"),
        members_path=Path(raw.get("members_path", "data/members.json")),
        # KPI_HISTORY_PATH wins over the file so one bundled config.json works
        # on EC2 (a local path) and on Lambda (an s3://bucket/key URI, because
        # the filesystem does not survive an invocation).
        history_path=_storage_location(
            env.get("KPI_HISTORY_PATH") or raw.get("history_path", "data/history.jsonl")
        ),
        linkedin_mcp_url=mcp_config.get("linkedin_url", ""),
        slack_mcp_url=mcp_config.get("slack_url", ""),
    )

    if config.linkedin_source == "mcp" and not config.linkedin_mcp_url:
        raise ConfigError("linkedin.source is 'mcp' but mcp.linkedin_url is not set")
    return config


def load_members(path: str | Path):
    """Read the team list -- the one file you edit to add or remove a person.

    Each entry needs a name and a LinkedIn profile URL. `blog_author` is
    optional and only matters when someone's byline on moring.ai differs from
    their `name` here; it accepts either the byline or the /authors/ slug.

        {"name": "Ashvath Narayan",
         "linkedin_url": "https://www.linkedin.com/in/ashvath-narayanan/",
         "blog_author": "Ashvath Narayan"}

    Handles are normalised on load so the join key is canonical no matter how
    the URL was pasted -- with or without the trailing slash, www, or ?trk=
    tracking parameters.
    """
    from kpi_tracker.matching import normalise
    from kpi_tracker.models import Member

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found -- it lists the people to track")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path} must be a non-empty list of members")

    members, seen = [], set()
    for index, entry in enumerate(raw):
        name = (entry.get("name") or "").strip()
        # linkedin_url is the documented field; linkedin_handle is accepted
        # too, since a bare handle is a reasonable thing to paste.
        handle = normalise(entry.get("linkedin_url") or entry.get("linkedin_handle"))

        if not name:
            raise ConfigError(f"{path}[{index}] has no name")
        if not handle:
            raise ConfigError(
                f"{path}[{index}] ({name}) has no usable linkedin_url -- expected something "
                "like https://www.linkedin.com/in/ashvath-narayanan/"
            )
        if handle in seen:
            raise ConfigError(f"{path} lists the LinkedIn profile {handle!r} twice")
        seen.add(handle)

        members.append(
            Member(
                name=name,
                linkedin_handle=handle,
                blog_author=(entry.get("blog_author") or "").strip() or None,
            )
        )

    return members
