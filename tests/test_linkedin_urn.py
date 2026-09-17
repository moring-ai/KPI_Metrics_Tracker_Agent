"""Timestamp decoding is the one piece of cleverness in this system, so it is
the piece with the most tests.

The ground-truth pairs below come from LinkedIn's own API documentation, which
publishes both the post id and its createdAt. Decoding lands 50-250ms before
the documented value because the id is minted just before the publish commits.
"""

from datetime import datetime, timezone

import pytest

from kpi_tracker.sources.linkedin_urn import (
    UndecodablePost,
    extract_activity_id,
    posted_at,
    posted_at_from_url,
)

# (activity id, LinkedIn's own documented createdAt in epoch ms)
GROUND_TRUTH = [
    (6856921137721544704, 1634817394721),
    (6844785523593134080, 1631924038940),
    (6856810298419044352, 1634790968743),
    (6864691044148133888, 1636669884769),
    (6864768486615375872, 1636688348505),
    (6856904634360066048, 1634813460041),
]


@pytest.mark.parametrize("activity_id,documented_ms", GROUND_TRUTH)
def test_decodes_within_250ms_of_linkedins_own_timestamp(activity_id, documented_ms):
    derived_ms = posted_at(activity_id).timestamp() * 1000
    assert 0 <= documented_ms - derived_ms <= 250


def test_pinned_known_value():
    assert posted_at(7054425663348363266) == datetime(
        2023, 4, 19, 12, 9, 3, 33000, tzinfo=timezone.utc
    )


def test_the_widely_published_shift_of_23_is_wrong():
    """The formula circulating online is '>> 23'. It yields 1998 dates.

    This test exists so nobody "corrects" our shift back to the popular one.
    """
    activity_id = 7501765110059237376
    wrong = datetime.fromtimestamp((activity_id >> 23) / 1000, tz=timezone.utc)
    assert wrong.year == 1998
    correct = posted_at(activity_id, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert correct == datetime(2026, 9, 4, 22, 16, 18, 294000, tzinfo=timezone.utc)


def test_a_truncated_id_is_rejected_rather_than_guessed():
    """A self-adjusting shift like `>> (bit_length() - 41)` would turn this
    into a confident, plausible 2010s date. A fixed shift lets us catch it."""
    with pytest.raises(UndecodablePost, match="bits"):
        posted_at(705442566334836)


def test_an_implausible_date_is_rejected():
    with pytest.raises(UndecodablePost, match="plausible"):
        posted_at(7054425663348363266, now=datetime(2020, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.linkedin.com/posts/x_y-activity-7054425663348363266-iH3M", 7054425663348363266),
        ("https://www.linkedin.com/feed/update/urn:li:activity:7092521914341978112/", 7092521914341978112),
        ("urn:li:share:6856921137721544704", 6856921137721544704),
        ("urn:li:ugcPost:6856810298419044352", 6856810298419044352),
    ],
)
def test_extracts_the_id_from_every_permalink_shape(url, expected):
    assert extract_activity_id(url) == expected


def test_a_url_with_no_activity_id_is_rejected():
    assert extract_activity_id("https://www.linkedin.com/in/someone/") is None
    with pytest.raises(UndecodablePost, match="no activity id"):
        posted_at_from_url("https://www.linkedin.com/in/someone/")


def test_empty_input_does_not_crash():
    assert extract_activity_id("") is None
    assert extract_activity_id(None) is None
