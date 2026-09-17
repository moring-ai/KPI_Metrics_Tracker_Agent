"""Aggregation: zero-fill, unknown-fill, and attribution.

The distinction this module exists to protect: a person who published nothing
gets 0; a person whose source broke gets None, rendered as a dash. Collapsing
two into "0" would tell the CTO something false about a named colleague.
"""

from datetime import date

import pytest

from kpi_tracker import report
from kpi_tracker.models import UNKNOWN_MARKER, BlogPost, FetchResult, LinkedInPost, Member
from kpi_tracker.timewindow import last_week

WEEK = last_week("2026-09-03", "Asia/Kolkata", "thursday")  # Thu 08-27 .. Wed 09-02


def li(handle, day):
    from datetime import datetime, timezone

    return LinkedInPost(handle, f"https://x/{handle}/{day}", datetime(2026, 8, day, 9, tzinfo=timezone.utc), 1)


def blog(handle=None, slug=None, name="Someone", day=28):
    return BlogPost(
        url=f"https://blog/{name}-{day}",
        title="A post",
        author_name=name,
        author_handle=handle,
        author_slug=slug,
        published_on=date(2026, 8, day),
        date_source="editorial",
    )


def ok(source, items):
    return FetchResult(source=source, ok=True, items=items)


def test_everyone_appears_even_with_nothing_published(members):
    built = report.build(members, ok("li", []), ok("blog", []), WEEK)
    assert [row.name for row in built.rows] == [m.name for m in members]
    assert all(row.linkedin_posts == 0 and row.blogs == 0 for row in built.rows)


def test_counts_are_per_person(members):
    posts = [li("ashvath-narayanan", 28), li("ashvath-narayanan", 29), li("arya-sharan", 30)]
    built = report.build(members, ok("li", posts), ok("blog", []), WEEK)
    counts = {row.name: row.linkedin_posts for row in built.rows}
    assert counts["Ashvath Narayan"] == 2
    assert counts["Arya Sharan"] == 1
    assert counts["Ellakkiaa"] == 0


def test_a_failed_linkedin_fetch_gives_unknown_not_zero(members):
    built = report.build(members, FetchResult.failed("li", "vendor down"), ok("blog", []), WEEK)
    assert all(row.linkedin_posts is None for row in built.rows)
    assert all(row.blogs == 0 for row in built.rows)
    assert all(row.cell(row.linkedin_posts) == UNKNOWN_MARKER for row in built.rows)
    assert any("not 0" in note for note in built.notes)


def test_a_failed_blog_fetch_gives_unknown_not_zero(members):
    built = report.build(members, ok("li", []), FetchResult.failed("blog", "503"), WEEK)
    assert all(row.blogs is None for row in built.rows)
    assert all(row.linkedin_posts == 0 for row in built.rows)


def test_blogs_are_attributed_by_linkedin_url_first(members):
    built = report.build(
        members, ok("li", []), ok("blog", [blog(handle="ashvath-narayanan", name="A. Narayan")]), WEEK
    )
    assert {row.name: row.blogs for row in built.rows}["Ashvath Narayan"] == 1


def test_blogs_are_attributed_by_author_slug_when_no_linkedin_url(members):
    built = report.build(members, ok("li", []), ok("blog", [blog(slug="balaji-nagaraj")]), WEEK)
    assert {row.name: row.blogs for row in built.rows}["Balaji Nagaraj"] == 1


def test_blogs_are_attributed_by_byline_as_a_last_resort(members):
    built = report.build(members, ok("li", []), ok("blog", [blog(name="Ellakkiaa")]), WEEK)
    assert {row.name: row.blogs for row in built.rows}["Ellakkiaa"] == 1


def test_a_post_by_someone_not_in_members_is_reported_not_silently_dropped(members):
    built = report.build(members, ok("li", []), ok("blog", [blog(name="A Guest Writer")]), WEEK)
    assert all(row.blogs == 0 for row in built.rows)
    assert any("not in members.json" in note for note in built.notes)
    assert any("A Guest Writer" in note for note in built.notes)


def test_blog_posts_outside_the_window_are_not_counted(members):
    outside = blog(handle="arya-sharan", day=26)  # Wed 26 Aug, the day before the window
    built = report.build(members, ok("li", []), ok("blog", [outside]), WEEK)
    assert all(row.blogs == 0 for row in built.rows)


def test_a_post_with_no_date_at_all_is_not_counted(members):
    dateless = BlogPost("https://blog/x", "T", "Ellakkiaa", None, None, None, "missing")
    built = report.build(members, ok("li", []), ok("blog", [dateless]), WEEK)
    assert all(row.blogs == 0 for row in built.rows)


def test_a_quiet_blog_week_says_so_explicitly(members):
    """Otherwise a legitimate all-zero week reads as a broken scraper."""
    built = report.build(members, ok("li", [li("arya-sharan", 28)]), ok("blog", []), WEEK)
    assert any("real zero" in note for note in built.notes)


def test_the_target_is_stated_in_the_notes(members):
    built = report.build(members, ok("li", []), ok("blog", []), WEEK, target_posts_per_week=5)
    assert any("Target: 5" in note for note in built.notes)


def test_the_reported_range_is_inclusive_of_the_last_day(members):
    built = report.build(members, ok("li", []), ok("blog", []), WEEK)
    assert built.week_start == "2026-08-27"
    assert built.week_end == "2026-09-02"  # Wednesday, not the next Thursday


def test_rendered_table_has_a_row_per_member_and_no_empty_cells(members):
    built = report.build(members, FetchResult.failed("li", "x"), ok("blog", []), WEEK)
    text = report.render_text(built)
    for member in members:
        assert member.name in text
    assert UNKNOWN_MARKER in text


def test_an_empty_member_list_produces_an_empty_table():
    built = report.build([], FetchResult("li", True, []), FetchResult("blog", True, []), WEEK)
    assert built.rows == []
    report.render_text(built)  # must not raise


# -- regressions found by an adversarial review ---------------------------


def warned(source, items, *warnings):
    return FetchResult(source=source, ok=True, items=list(items), warnings=list(warnings))


def test_a_source_warning_reaches_the_reader(members):
    """A source can succeed overall and still have lost data. If those
    warnings only ever hit the terminal, the CTO sees confident numbers with
    no marker that part of the week was missed."""
    blogs = warned("moring_blog", [], "listing page is now paginated; ... is undercounting")
    built = report.build(members, ok("li", []), blogs, WEEK)

    assert any("paginated" in note for note in built.notes)
    assert any("moring_blog reported 1 issue" in note for note in built.notes)


def test_many_warnings_are_summarised_not_dumped(members):
    blogs = warned("moring_blog", [], *[f"problem {i}" for i in range(5)])
    built = report.build(members, ok("li", []), blogs, WEEK)
    note = next(n for n in built.notes if "moring_blog reported" in n)

    assert "5 issue(s)" in note and "+3 more" in note


def test_warnings_are_redacted_before_they_are_shown(members):
    # Split for the same reason as SLACK_TOKEN in tests/test_redaction.py.
    token = "xoxb-" + "1111111111-2222222222-abcdefghijklmnopqrstuvwx"
    blogs = warned("moring_blog", [], f"auth failed with {token}")
    built = report.build(members, ok("li", []), blogs, WEEK)

    assert token not in " ".join(built.notes)


def test_a_zero_is_only_called_real_when_nothing_went_wrong(members):
    """The worst version of this bug: printing 'the scraper ran fine, this is
    a real zero' over a table of zeros produced by a scraper that had just
    reported losing data."""
    clean = report.build(members, ok("li", [li("arya-sharan", 28)]), ok("blog", []), WEEK)
    assert any("real zero" in note for note in clean.notes)

    degraded = report.build(
        members,
        ok("li", [li("arya-sharan", 28)]),
        warned("blog", [], "no usable publish date for /blogs/x"),
        WEEK,
    )
    assert not any("real zero" in note for note in degraded.notes)
    assert any("may be incomplete" in note for note in degraded.notes)


# -- per-person unknowns: one profile failing must not read as a zero ------


def partial(source, items, *unknown):
    return FetchResult(source=source, ok=True, items=list(items), unknown_keys=frozenset(unknown))


def test_one_persons_failed_profile_shows_as_unknown_not_zero(members):
    """The vendor returns a flat list of posts. If it fails to scrape one
    profile, that person has no records -- identical to having published
    nothing. Reporting 0 there is a false statement about their week."""
    result = partial("li", [li("arya-sharan", 28)], "balajinagarajkumar")
    built = report.build(members, result, ok("blog", []), WEEK)
    counts = {row.name: row.linkedin_posts for row in built.rows}

    assert counts["Balaji Nagaraj"] is None, "a failed profile must be '?'"
    assert counts["Arya Sharan"] == 1
    assert counts["Ellakkiaa"] == 0, "a genuinely quiet person is still 0"


def test_the_people_we_could_not_retrieve_are_named(members):
    """A bare '?' invites the reader to assume it means zero."""
    result = partial("li", [], "balajinagarajkumar", "arya-sharan")
    built = report.build(members, result, ok("blog", []), WEEK)
    note = next(n for n in built.notes if "Could not retrieve" in n)

    assert "2 person(s)" in note
    assert "Balaji Nagaraj" in note and "Arya Sharan" in note
    assert "not 0" in note


def test_a_partial_column_is_never_called_a_real_zero(members):
    """'The scraper ran fine; this is a real zero' must not appear over a
    column that contains a '?'."""
    built = report.build(
        members, ok("li", []), partial("blog", [], "ellakkiaa-s-278823200"), WEEK
    )
    assert not any("real zero" in note for note in built.notes)


def test_a_fully_known_quiet_week_is_still_called_a_real_zero(members):
    built = report.build(members, ok("li", [li("arya-sharan", 28)]), ok("blog", []), WEEK)
    assert any("real zero" in note for note in built.notes)


def test_unknown_keys_default_to_empty_so_existing_sources_are_unaffected():
    result = FetchResult(source="x", ok=True, items=[])
    assert result.unknown_keys == frozenset()
    assert result.is_unknown("anyone") is False


def test_a_wholly_failed_fetch_makes_every_key_unknown():
    result = FetchResult.failed("x", "boom")
    assert result.is_unknown("anyone") is True


def test_a_vendor_failure_on_someone_not_on_the_roster_still_leaves_a_real_zero(members):
    """Every member's number is known, so the zero is a fact. Suppressing the
    note because the vendor tripped over an unrelated profile would be
    over-cautious -- the row-level check is the exact one."""
    stranger = FetchResult(source="li", ok=True, items=[], unknown_keys=frozenset({"someone-else"}))
    built = report.build(members, stranger, ok("blog", []), WEEK)

    assert all(row.linkedin_posts == 0 for row in built.rows)
    assert any("real zero" in note for note in built.notes)


def test_the_unknown_person_note_carries_the_reason(members):
    """The CTO should see why, not just that. Taken from the source's own
    warning so it stays accurate without being hardcoded."""
    result = FetchResult(
        source="brightdata",
        ok=True,
        items=[],
        warnings=["vendor could not scrape balajinagarajkumar: Crawler error: Minimal layout detected"],
        unknown_keys=frozenset({"balajinagarajkumar"}),
    )
    built = report.build(members, result, ok("blog", []), WEEK)
    note = next(n for n in built.notes if "Could not retrieve" in n)

    assert "Balaji Nagaraj" in note
    assert "Minimal layout detected" in note
    assert "not 0" in note


def test_the_raw_vendor_warning_is_not_repeated_underneath(members):
    """Saying it twice, once in plain language and once as raw vendor text,
    makes a report someone reads look like a log file."""
    result = FetchResult(
        source="brightdata",
        ok=True,
        items=[],
        warnings=["vendor could not scrape balajinagarajkumar: Crawler error: Minimal layout"],
        unknown_keys=frozenset({"balajinagarajkumar"}),
    )
    built = report.build(members, result, ok("blog", []), WEEK)

    assert not any("reported 1 issue" in note for note in built.notes)
    assert sum("Minimal layout" in note for note in built.notes) == 1


def test_an_unrelated_warning_is_still_surfaced(members):
    """Only warnings that restate a named person are dropped."""
    result = FetchResult(
        source="brightdata",
        ok=True,
        items=[],
        warnings=[
            "vendor could not scrape balajinagarajkumar: Crawler error",
            "duplicate post 123, counted once",
        ],
        unknown_keys=frozenset({"balajinagarajkumar"}),
    )
    built = report.build(members, result, ok("blog", []), WEEK)

    assert any("duplicate post 123" in note for note in built.notes)
    assert any("reported 1 issue" in note for note in built.notes)
