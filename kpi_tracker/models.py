"""The data contracts every step of the pipeline hands to the next.

These types are the interface between steps. Each step validates what it
receives, so a break shows up at the step that caused it rather than three
steps later in a wrong number on the CTO's screen.

The single most important idea in this file is FetchResult: a source either
succeeds or it does not, and "it did not" is NOT the same as "the count is
zero". A broken scraper that reports 0 posts for everybody is indistinguishable
from a quiet week unless we keep the two apart all the way to the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from kpi_tracker.redaction import redact


# How an unknown count is shown. An em dash is the usual table convention for
# "no value available": it cannot be misread as a number, and unlike "?" it
# does not look like a data-entry mistake. Defined once so the cell and the
# footnote explaining it can never disagree.
UNKNOWN_MARKER = "\u2014"


@dataclass(frozen=True)
class Member:
    """One person we track. Loaded from data/members.json.

    Two identities, because the two sources name people differently: LinkedIn
    knows them by profile handle, the blog by byline. `blog_author` is only
    needed when the byline does not match `name` -- see data/members.json.
    """

    name: str
    linkedin_handle: str  # normalised, e.g. "ashvath-narayanan"
    blog_author: str | None = None  # byline or /authors/ slug on moring.ai

    @property
    def profile_url(self) -> str:
        return f"https://www.linkedin.com/in/{self.linkedin_handle}"

    @property
    def blog_keys(self) -> set[str]:
        """Every string that identifies this person on the blog."""
        from kpi_tracker.matching import blog_key

        return {key for key in (blog_key(self.blog_author), blog_key(self.name)) if key}


@dataclass(frozen=True)
class LinkedInPost:
    """One LinkedIn post, with a timestamp we derived ourselves."""

    handle: str  # whose post, normalised
    url: str
    posted_at: datetime  # aware UTC
    activity_id: int


@dataclass(frozen=True)
class BlogPost:
    """One post on the Moring blog."""

    url: str
    title: str
    author_name: str
    author_handle: str | None  # LinkedIn handle from JSON-LD author.sameAs
    author_slug: str | None  # author-page slug from JSON-LD author.url
    published_on: date | None
    date_source: str  # "editorial" | "cms-stamp" | "missing"


@dataclass(frozen=True)
class FetchResult:
    """What every source returns. Never a bare list.

    ok=False means "we do not know", and every count derived from it must be
    None (rendered as UNKNOWN_MARKER), never 0.
    """

    source: str
    ok: bool
    items: list = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    # Keys (LinkedIn handles) this source could not determine an answer for,
    # even though the fetch as a whole succeeded.
    #
    # Needed because the vendor returns a flat list of posts: if it fails to
    # scrape one profile out of five, that profile simply has no records,
    # which is indistinguishable from "this person published nothing". Without
    # this, one person's outage renders as a confident 0 about their week.
    unknown_keys: frozenset[str] = frozenset()

    def is_unknown(self, key: str) -> bool:
        """Should this key render the unknown marker rather than a number?"""
        return not self.ok or key in self.unknown_keys

    @classmethod
    def failed(cls, source: str, error: str) -> "FetchResult":
        """Build a failure, with the error text redacted.

        Redacted here rather than at each call site, because this string does
        not stay in the logs: it is quoted in a note under the table in Slack
        and written to data/history.jsonl. A vendor's exception is entitled to
        quote the request it attempted, headers and all.
        """
        return cls(source=source, ok=False, items=[], error=redact(error))


@dataclass(frozen=True)
class Row:
    """One line of the table the CTO sees."""

    name: str
    linkedin_posts: int | None  # None => unknown, renders UNKNOWN_MARKER
    blogs: int | None

    def cell(self, value: int | None) -> str:
        """Slack rejects the whole message on an empty cell, so never return ''."""
        return UNKNOWN_MARKER if value is None else str(value)


@dataclass(frozen=True)
class Report:
    """The finished artefact: what gets rendered, sent, and written to history."""

    week_label: str
    week_start: str  # ISO date, local
    week_end: str  # ISO date, local, inclusive (the Sunday)
    tz_name: str
    rows: list[Row]
    linkedin_ok: bool
    blogs_ok: bool
    notes: list[str] = field(default_factory=list)
    blocked: bool = False  # a guardrail held this back from sending
