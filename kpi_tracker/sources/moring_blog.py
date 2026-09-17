"""Blog posts from the Moring website, via its RSS feed.

REWRITTEN 2026-09-13, because the site was rebuilt.
----------------------------------------------------
The previous version scraped the Webflow listing page with CSS selectors like
`div.w-dyn-item` and `div.blog7_author-text`. The site moved to Astro, every one
of those class names vanished, and the scraper returned zero cards.

It failed correctly -- reporting "we could not read this" rather than a table of
zeros -- which is exactly what its guard existed for. But the lesson is the
guard was the second-best answer: the best one is not to depend on
design-tool-generated class names at all.

The rebuilt site publishes /rss.xml, which it did not before. That is a machine
contract rather than a rendering artefact, and it is strictly better here:

  * ONE request for every post, with no pagination to get wrong
  * `pubDate` IS the editorial date -- so the old trap, where JSON-LD carried a
    CMS batch timestamp that disagreed with the visible date on 5 of 10 posts,
    simply does not exist any more
  * nothing to dedupe: no repeated cards across category tabs
  * an `<author>` per item

WHAT WAS LOST, AND IT MATTERS
-----------------------------
The old per-post JSON-LD carried `author.sameAs` -- the author's LinkedIn
profile URL -- which let a blog post be joined to a person by an EXACT key. The
rebuilt site publishes only the author's display name (its JSON-LD `author` is
`{name, jobTitle}`, and the only LinkedIn URL anywhere on the page is the
company's).

So attribution now relies on name matching (`blog_key`), which was previously
only the fallback. All five current authors match the roster, but this is more
fragile than it was: someone changing how they are credited on the site will
start showing up as unattributed. That is reported, never silently dropped --
see report._attribute -- but it is worth knowing.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ElementTree
from datetime import date
from email.utils import parsedate_to_datetime

import requests

from kpi_tracker.models import BlogPost, FetchResult
from kpi_tracker.timewindow import Week

log = logging.getLogger(__name__)

BASE_URL = "https://www.moring.ai"  # the apex 301-redirects; use www
FEED_PATH = "/rss.xml"
USER_AGENT = "MoringKPIBot/1.0 (+internal weekly KPI report)"


class MoringBlogSource:
    name = "moring_blog"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        timeout: int = 30,
    ) -> None:
        self._session = session or requests.Session()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def fetch(self, week: Week, *, resolve_all: bool = False) -> FetchResult:
        """Every post in the feed. Windowing happens in report.build, not here.

        `resolve_all` is accepted and ignored: the old implementation used it to
        decide which posts needed a second HTTP request for their author. One
        request now returns everything, so there is nothing to defer. The
        parameter stays because the `blogs` CLI command passes it.
        """
        url = f"{self._base_url}{FEED_PATH}"
        try:
            response = self._session.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=self._timeout
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.error("blog feed fetch failed: %s", exc)
            return FetchResult.failed(self.name, f"could not fetch {url}: {exc}")

        try:
            channel = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            return FetchResult.failed(self.name, f"{url} is not valid XML: {exc}")

        items = channel.findall("./channel/item")
        # The same structural guard as before, for the same reason: an empty
        # feed and a feed we cannot read look identical downstream, and only one
        # of them is a fact about anybody's week. If the site changes again,
        # this is what turns it into a visible failure instead of six zeros.
        if not items:
            return FetchResult.failed(
                self.name, f"{url} contained no items -- the feed format has probably changed"
            )

        posts: list[BlogPost] = []
        warnings: list[str] = []
        for item in items:
            link = (item.findtext("link") or "").strip()
            if not link:
                warnings.append("an item in the feed has no <link>; skipped")
                continue

            published_on = _published_date(item.findtext("pubDate"))
            if published_on is None:
                warnings.append(f"no usable pubDate for {link}; excluded from the counts")

            author = (item.findtext("author") or "").strip()
            if not author:
                warnings.append(f"no <author> for {link}; it cannot be attributed")

            posts.append(
                BlogPost(
                    url=link,
                    title=(item.findtext("title") or "").strip() or "(untitled)",
                    author_name=author or "(unknown)",
                    # The rebuilt site publishes neither a per-author LinkedIn
                    # URL nor an author-page slug, so the exact join keys are
                    # gone and matching falls back to the display name.
                    author_handle=None,
                    author_slug=None,
                    published_on=published_on,
                    date_source="rss",
                )
            )

        log.info("read %d post(s) from %s", len(posts), url)
        return FetchResult(source=self.name, ok=True, items=posts, warnings=warnings)


def _published_date(raw: str | None) -> date | None:
    """The editorial date from an RFC 822 pubDate.

    Every entry in this feed is published at exactly 00:00:00 UTC, which means
    the value is a DATE rendered as an instant, not a real timestamp. So the
    date is taken as-is rather than converted into the report timezone.

    That is the opposite of what the old CMS timestamp needed, and deliberately
    so: that one was a genuine instant, where midnight UTC really is the
    previous evening in the Americas. Converting this one the same way would
    move every post back a day for any timezone behind UTC.
    """
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw.strip()).date()
    except (TypeError, ValueError):
        log.warning("unparseable pubDate %r", raw)
        return None
