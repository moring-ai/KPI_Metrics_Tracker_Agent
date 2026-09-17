"""Turn two FetchResults into the table the CTO sees.

The zero-fill rule is the whole point of this module, and it has two halves
that are easy to conflate:

  * A person the source succeeded for, who published nothing, gets 0.
  * A person whose source FAILED gets None, which renders as a dash.

Reporting "0" when we actually mean "we do not know" is the single worst thing
this system could do, because the CTO cannot tell the two apart and will read
the wrong one as fact about a named colleague.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from kpi_tracker.matching import blog_key
from kpi_tracker.models import UNKNOWN_MARKER, FetchResult, Member, Report, Row
from kpi_tracker.redaction import redact
from kpi_tracker.timewindow import Week


def build(
    members: list[Member],
    linkedin: FetchResult,
    blogs: FetchResult,
    week: Week,
    *,
    target_posts_per_week: int = 3,
) -> Report:
    linkedin_counts = Counter(post.handle for post in linkedin.items) if linkedin.ok else {}

    blog_counts: Counter = Counter()
    unattributed: list[str] = []
    if blogs.ok:
        for post in blogs.items:
            if not week.contains_date(post.published_on):
                continue
            member = _attribute(post, members)
            if member is not None:
                blog_counts[member.linkedin_handle] += 1
            else:
                unattributed.append(f"{post.author_name} ({post.url})")

    rows = [
        Row(
            name=member.name,
            linkedin_posts=(
                None
                if linkedin.is_unknown(member.linkedin_handle)
                else linkedin_counts.get(member.linkedin_handle, 0)
            ),
            blogs=(
                None
                if blogs.is_unknown(member.linkedin_handle)
                else blog_counts.get(member.linkedin_handle, 0)
            ),
        )
        for member in members
    ]

    return Report(
        week_label=week.label,
        week_start=week.local_start.isoformat(),
        # The window is half-open [Mon, Mon), but humans read an inclusive
        # Mon..Sun range, so report the Sunday.
        week_end=(week.local_end - timedelta(days=1)).isoformat(),
        tz_name=week.tz_name,
        rows=rows,
        linkedin_ok=linkedin.ok,
        blogs_ok=blogs.ok,
        notes=_notes(
            linkedin, blogs, rows, unattributed, target_posts_per_week, members
        ),
    )


def _reason_for(handles: list[str], *results: FetchResult) -> str:
    """A short parenthetical explaining why, taken from the source's warning."""
    for result in results:
        for warning in result.warnings:
            if any(handle in warning for handle in handles):
                # "vendor could not scrape x: Crawler error: Minimal layout ..."
                tail = warning.split(":", 2)[-1].strip().rstrip(".")
                if tail:
                    return f" ({redact(tail)})"
    return ""


def _explains_a_named_person(warning: str, unknown_keys: frozenset[str]) -> bool:
    """Is this warning just the raw version of the "could not retrieve" note?"""
    return any(handle in warning for handle in unknown_keys)


def _attribute(post, members: list[Member]) -> Member | None:
    """Whose blog post is this? First match wins; every step is exact.

    1. the LinkedIn URL in the post's JSON-LD, against members.json
    2. the author-page slug or byline, against `blog_author`
    3. the same, against `name`

    Returning None is a real answer -- the caller reports it as unattributed
    rather than quietly dropping the post, so a missing members.json entry
    shows up as a line in the Slack message instead of a number that is
    silently one too low.
    """
    if post.author_handle:
        for member in members:
            if member.linkedin_handle == post.author_handle:
                return member

    candidates = {key for key in (post.author_slug, blog_key(post.author_name)) if key}
    if candidates:
        for member in members:
            if member.blog_keys & candidates:
                return member
    return None


def _notes(
    linkedin: FetchResult,
    blogs: FetchResult,
    rows: list[Row],
    unattributed: list[str],
    target: int,
    members: list[Member],
) -> list[str]:
    """Short lines of context that go under the table.

    These exist so the reader can tell a quiet week from a broken scraper
    without opening a log.
    """
    notes = [f"Target: {target} LinkedIn posts per person per week."]

    if not linkedin.ok:
        notes.append(
            f"LinkedIn data unavailable ({linkedin.error}) - "
            f"shown as '{UNKNOWN_MARKER}', not 0."
        )
    if not blogs.ok:
        notes.append(
            f"Blog data unavailable ({blogs.error}) - shown as '{UNKNOWN_MARKER}', not 0."
        )

    # Only claim a zero is real when nothing went wrong. A source can succeed
    # overall and still have lost data -- a paginated listing it only read page
    # one of, a post it could not date. Saying "the scraper ran fine" over a
    # table of zeros turns an unknown into a confident assertion, which is
    # worse than the zero itself.
    if blogs.ok and all(row.blogs == 0 for row in rows):
        if blogs.warnings:
            notes.append(
                "No blogs counted this week, but the scraper reported problems - "
                "this zero may be incomplete."
            )
        else:
            notes.append(
                "No blogs were published this week. The scraper ran fine; this is a real zero."
            )

    if linkedin.ok and all(row.linkedin_posts == 0 for row in rows):
        notes.append("No LinkedIn posts found for anyone this week - worth a sanity check.")

    # Surface partial failures. Without this, a source that declared itself
    # "undercounting" still renders as a table of confident numbers.
    #
    # Warnings that merely restate an already-named unknown person are dropped:
    # the note above says it in plain language, and repeating it as raw vendor
    # text makes a report the CTO reads look like a log file.
    for result in (linkedin, blogs):
        if not result.ok:
            continue
        fresh = [w for w in result.warnings if not _explains_a_named_person(w, result.unknown_keys)]
        if fresh:
            shown = "; ".join(redact(w) for w in fresh[:2])
            more = f" (+{len(fresh) - 2} more)" if len(fresh) > 2 else ""
            notes.append(f"{result.source} reported {len(fresh)} issue(s): {shown}{more}")

    # Name the people whose number we could not establish. A bare marker with no
    # explanation invites the reader to assume it means zero.
    partial = sorted(linkedin.unknown_keys | blogs.unknown_keys)
    if partial:
        by_handle = {m.linkedin_handle: m.name for m in members}
        names = [by_handle.get(handle, handle) for handle in partial]
        reason = _reason_for(partial, linkedin, blogs)
        notes.append(
            f"Could not retrieve data for {len(names)} person(s): {', '.join(names)}"
            f"{reason} - shown as '{UNKNOWN_MARKER}', not 0."
        )

    if unattributed:
        notes.append(
            f"{len(unattributed)} blog post(s) by people not in members.json were not counted: "
            + "; ".join(unattributed[:3])
        )

    return notes


def render_text(report: Report) -> str:
    """Plain-text table, for --dry-run and for the Slack fallback text."""
    headers = ("Name", "LinkedIn posts", "Blogs")
    body = [(row.name, row.cell(row.linkedin_posts), row.cell(row.blogs)) for row in report.rows]

    widths = [
        max(len(headers[i]), *(len(line[i]) for line in body)) if body else len(headers[i])
        for i in range(3)
    ]
    line = lambda cells: "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    out = [
        f"Weekly KPI - {report.week_label} ({report.tz_name})",
        "",
        line(headers),
        "  ".join("-" * width for width in widths),
    ]
    out.extend(line(cells) for cells in body)
    out.append("")
    out.extend(f"- {note}" for note in report.notes)
    return "\n".join(out)
