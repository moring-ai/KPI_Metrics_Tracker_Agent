"""The blog source, reading the site's RSS feed.

REWRITTEN alongside the source, September 2026. The previous suite tested four
traps in the Webflow markup -- repeated cards across category tabs, a stale
sitemap, a CMS batch timestamp masquerading as a publish date, and phantom
JSON-LD. None of them exist in a feed, so none of those cases survive.

What replaces them is the same question asked of a different source: when the
feed is wrong, missing or unreadable, does this degrade towards "we do not
know" or towards zero? Towards zero is the failure this system exists to
prevent.
"""

from datetime import date

import pytest
import responses

from kpi_tracker.sources.moring_blog import MoringBlogSource, _published_date
from kpi_tracker.timewindow import last_week
from tests.conftest import BLOG_BASE, FIXTURES

# The fixture feed holds three posts: 2026-09-01, 2026-08-28 and 2026-07-14.
WEEK = last_week("2026-09-03", "Asia/Kolkata", "thursday")  # Thu 08-27 .. Wed 09-02

FEED = (FIXTURES / "blog" / "rss.xml").read_text()


def fetch(**kwargs):
    return MoringBlogSource(base_url=BLOG_BASE).fetch(WEEK, **kwargs)


def serve(body=FEED, status=200, content_type="application/rss+xml"):
    mock = responses.RequestsMock()
    mock.start()
    mock.add(responses.GET, f"{BLOG_BASE}/rss.xml", body=body, status=status,
             content_type=content_type)
    return mock


# -- the happy path -------------------------------------------------------


def test_every_post_in_the_feed_is_returned(blog_site):
    result = fetch()
    assert result.ok
    assert len(result.items) == 3
    assert result.warnings == []


def test_the_whole_feed_costs_exactly_one_request(blog_site):
    """The old implementation made up to twelve: a listing page plus one per
    post to recover the author. The feed carries the author itself."""
    fetch()
    assert len(blog_site.calls) == 1


def test_title_author_and_date_are_read(blog_site):
    posts = {p.url.rstrip("/").rsplit("/", 1)[-1]: p for p in fetch().items}
    enterprise = posts["enterprise-architecture"]
    assert enterprise.title == "Enterprise AI Architecture"
    assert enterprise.author_name == "Ashvath Narayan"
    assert enterprise.published_on == date(2026, 9, 1)
    assert enterprise.date_source == "rss"


def test_windowing_is_left_to_the_report_layer(blog_site):
    """The source returns everything; report.build decides what counts. Keeping
    the window out of here means the source has no notion of 'this week' to get
    wrong."""
    dates = {p.published_on for p in fetch().items}
    assert date(2026, 7, 14) in dates, "an out-of-window post is still returned"
    assert sum(WEEK.contains_date(d) for d in dates) == 2


def test_resolve_all_is_accepted_and_changes_nothing(blog_site):
    """The `blogs` CLI command still passes it."""
    assert len(fetch(resolve_all=True).items) == 3


# -- unknown, not zero ----------------------------------------------------


def test_an_empty_feed_is_a_failure_not_a_quiet_week():
    """The guard that caught the real site rebuild. A feed with no items and a
    feed we cannot parse are the same thing downstream, and neither is a fact
    about anybody's week."""
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    with serve(empty):
        result = fetch()
    assert result.ok is False
    assert "no items" in result.error


def test_an_http_error_is_a_failure():
    with serve(status=503):
        result = fetch()
    assert result.ok is False and result.items == []


def test_malformed_xml_is_a_failure_not_an_empty_week():
    with serve("<rss><channel><item>unclosed"):
        result = fetch()
    assert result.ok is False and "not valid XML" in result.error


def test_html_served_where_a_feed_was_expected_is_a_failure():
    """What the site rebuild actually looked like: a 200 with the wrong body."""
    with serve("<!doctype html><html><body>Not a feed</body></html>"):
        result = fetch()
    assert result.ok is False


# -- individual items that are wrong --------------------------------------


def item(link="https://blog.test/blogs/x/", pub="Tue, 01 Sep 2026 00:00:00 GMT",
         author="Ashvath Narayan", title="A post"):
    parts = [f"<title>{title}</title>" if title else "",
             f"<link>{link}</link>" if link else "",
             f"<pubDate>{pub}</pubDate>" if pub else "",
             f"<author>{author}</author>" if author else ""]
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>'
            f'<item>{"".join(parts)}</item></channel></rss>')


def test_an_item_with_no_link_is_skipped_with_a_warning():
    with serve(item(link="")):
        result = fetch()
    assert result.ok and result.items == []
    assert any("no <link>" in w for w in result.warnings)


def test_an_item_with_no_date_is_kept_but_cannot_be_counted():
    """report.build excludes a post with no date, so it must be visible here
    rather than silently absent."""
    with serve(item(pub="")):
        result = fetch()
    assert len(result.items) == 1
    assert result.items[0].published_on is None
    assert any("no usable pubDate" in w for w in result.warnings)


def test_an_unparseable_date_is_warned_about_not_guessed():
    with serve(item(pub="last Tuesday-ish")):
        result = fetch()
    assert result.items[0].published_on is None
    assert any("pubDate" in w for w in result.warnings)


def test_an_item_with_no_author_is_reported_not_dropped():
    """It becomes unattributed in the report, which is named under the table."""
    with serve(item(author="")):
        result = fetch()
    assert len(result.items) == 1
    assert result.items[0].author_name == "(unknown)"
    assert any("no <author>" in w for w in result.warnings)


def test_an_item_with_no_title_still_counts():
    """A missing title is cosmetic; the count is what matters."""
    with serve(item(title="")):
        result = fetch()
    assert result.items[0].title == "(untitled)"
    assert result.items[0].published_on == date(2026, 9, 1)


# -- the date, which is the subtle part -----------------------------------


def test_the_date_is_taken_as_written_and_never_shifted_by_a_timezone():
    """Every pubDate in this feed is exactly 00:00:00 GMT -- a DATE rendered as
    an instant, not a real timestamp.

    Converting it into the report timezone, which the old CMS timestamp
    correctly needed, would move every post back a day for any timezone behind
    UTC. This is the inverse case and must not be 'fixed' to match the other.
    """
    assert _published_date("Wed, 02 Sep 2026 00:00:00 GMT") == date(2026, 9, 2)
    assert _published_date("Wed, 02 Sep 2026 00:00:00 +0000") == date(2026, 9, 2)


def test_a_real_timestamp_with_an_offset_is_still_read_correctly():
    assert _published_date("Wed, 02 Sep 2026 14:30:00 +0530") == date(2026, 9, 2)


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "2026-09-02"])
def test_a_date_we_cannot_read_is_none_rather_than_a_guess(raw):
    assert _published_date(raw) is None


# -- what the site rebuild cost us ---------------------------------------


def test_posts_no_longer_carry_a_linkedin_handle_or_author_slug(blog_site):
    """Documenting a real regression. The old site published the author's
    LinkedIn URL in per-post JSON-LD, which made the blog-to-person join an
    EXACT key. The rebuilt site publishes only a display name, so attribution
    now depends on name matching -- previously only the fallback.

    If a future redesign restores an author URL, this test should fail and the
    stronger join should be wired back up.
    """
    for post in fetch().items:
        assert post.author_handle is None
        assert post.author_slug is None
