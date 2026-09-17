"""Identity normalisation. Every join in this system runs through here, so a
bug in this file shows up as somebody's number being quietly wrong."""

import pytest

from kpi_tracker.matching import blog_key, first_linkedin_url, handle_from_post_url, normalise
from kpi_tracker.models import Member


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/ashvath-narayanan",  # the real site omits the slash here
        "https://www.linkedin.com/in/ashvath-narayanan/",
        "http://linkedin.com/in/ashvath-narayanan",
        "https://www.linkedin.com/in/Ashvath-Narayanan/?trk=public_profile",
        "linkedin.com/in/ashvath-narayanan#about",
        "ashvath-narayanan",
        "  ashvath-narayanan  ",
    ],
)
def test_every_way_of_writing_a_profile_reduces_to_one_key(raw):
    assert normalise(raw) == "ashvath-narayanan"


def test_inconsistent_trailing_slashes_do_not_split_a_person_in_two():
    """The live site has 4 authors with a trailing slash and 1 without.
    Joining raw strings would silently drop that one person."""
    with_slash = "https://www.linkedin.com/in/balajinagarajkumar/"
    without = "https://www.linkedin.com/in/ashvath-narayanan"
    assert normalise(with_slash) == "balajinagarajkumar"
    assert normalise(without) == "ashvath-narayanan"


def test_a_url_that_is_not_a_profile_yields_no_handle():
    """Returning the whole URL as a 'handle' would poison every join."""
    assert normalise("https://www.linkedin.com/posts/foo_bar-activity-1-x") is None
    assert normalise("https://example.com/") is None


def test_empty_input():
    assert normalise(None) is None
    assert normalise("") is None
    assert normalise("   ") is None


def test_a_display_name_in_the_url_field_is_rejected():
    """A handle never has spaces. Accepting one makes a key that matches
    nothing, so the person silently scores zero forever."""
    assert normalise("Ashvath Narayan") is None
    assert normalise("not a url") is None


def test_post_permalink_reveals_its_author():
    url = "https://www.linkedin.com/posts/ashvath-narayanan_ai-activity-7054425663348363266-iH3M"
    assert handle_from_post_url(url) == "ashvath-narayanan"


def test_post_permalink_without_an_author_segment():
    assert handle_from_post_url("https://www.linkedin.com/feed/update/urn:li:activity:1/") is None
    assert handle_from_post_url(None) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Balaji Nagaraj", "balaji-nagaraj"),
        ("/authors/balaji-nagaraj", "balaji-nagaraj"),
        ("authors/balaji-nagaraj", "balaji-nagaraj"),
        ("  Ellakkiaa  ", "ellakkiaa"),
        ("Arya  Sharan", "arya-sharan"),
        ("O'Brien-Smith", "o-brien-smith"),
    ],
)
def test_a_byline_and_an_author_slug_reduce_to_the_same_key(raw, expected):
    assert blog_key(raw) == expected


def test_blog_key_of_nothing():
    assert blog_key(None) is None
    assert blog_key("") is None
    assert blog_key("///") is None


def test_sameAs_picks_the_linkedin_url_and_ignores_the_empty_slot():
    """sameAs is a 2-element list whose second entry is '' on every real post."""
    assert first_linkedin_url(["https://www.linkedin.com/in/x/", ""]) == "https://www.linkedin.com/in/x/"
    assert first_linkedin_url(["", "https://www.linkedin.com/in/x/"]) == "https://www.linkedin.com/in/x/"
    assert first_linkedin_url(["https://twitter.com/x"]) is None
    assert first_linkedin_url("https://www.linkedin.com/in/x/") == "https://www.linkedin.com/in/x/"
    assert first_linkedin_url(None) is None
    assert first_linkedin_url([]) is None


def test_a_member_is_findable_by_byline_and_by_name():
    member = Member("Ashvath Narayan", "ashvath-narayanan", "/authors/ashvath-narayan")
    assert member.blog_keys == {"ashvath-narayan"}

    no_override = Member("Arya Sharan", "arya-sharan")
    assert no_override.blog_keys == {"arya-sharan"}


def test_profile_url_is_rebuilt_from_the_normalised_handle():
    member = Member("Ashvath Narayan", "ashvath-narayanan")
    assert member.profile_url == "https://www.linkedin.com/in/ashvath-narayanan"
