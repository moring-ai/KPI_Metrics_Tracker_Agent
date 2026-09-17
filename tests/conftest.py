"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import pathlib

import pytest
import responses as responses_lib

from kpi_tracker.models import Member

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BLOG_BASE = "https://blog.test"


@pytest.fixture
def members() -> list[Member]:
    return [
        Member("Balaji Nagaraj", "balajinagarajkumar", "Balaji Nagaraj"),
        Member("Vignesh Nagarajan", "vignesh-nagarajan-63793b1a4", "Vignesh Nagarajan"),
        Member("Ashvath Narayan", "ashvath-narayanan", "Ashvath Narayan"),
        Member("Ellakkiaa", "ellakkiaa-s-278823200", "Ellakkiaa"),
        Member("Arya Sharan", "arya-sharan", "Arya Sharan"),
    ]


@pytest.fixture
def blog_site():
    """Serve the saved blog feed over mocked HTTP.

    The site was rebuilt in September 2026 and now publishes /rss.xml, so this
    serves a feed rather than the old Webflow listing page plus a page per post.
    One request replaces twelve.
    """
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(
            responses_lib.GET,
            f"{BLOG_BASE}/rss.xml",
            body=(FIXTURES / "blog" / "rss.xml").read_text(),
            content_type="application/rss+xml",
        )
        yield mock
