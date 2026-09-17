"""Derive a LinkedIn post's publish time from its own permalink.

Why bother, when the data vendor hands us a date? Because the vendor's date is
the part most likely to be wrong or reformatted, and because it is the only
field we cannot check. The activity id in the URL *contains* the timestamp, so
we can compute the date ourselves in six auditable lines and use the vendor's
date only as a cross-check. That also makes vendors swappable: a source only
has to return permalinks.

How it works: a LinkedIn activity id is a 63-bit integer whose top 41 bits are
the Unix epoch in milliseconds.

    ts_ms = activity_id >> 22          # 63 - 41 = 22

Two traps, both of which have bitten people publicly:

1. The formula circulated on the web is ">> 23", which is off by one and
   returns dates in 1998. Ours is pinned by a test against ids whose true
   timestamps LinkedIn publishes in its own API documentation.

2. It is tempting to write `>> (id.bit_length() - 41)` so the shift adapts.
   Do not. That expression always yields a 41-bit result, so *any* garbage
   input decodes to a plausible-looking date between 2004 and 2039 and sails
   straight past the sanity check below. A fixed shift makes a truncated id
   decode to 1975, which the sanity check catches. The fixed shift stays
   correct until ids reach 64 bits, in September 2039.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Matches all three permalink shapes LinkedIn emits:
#   /posts/<slug>-activity-7054425663348363266-iH3M
#   /feed/update/urn:li:activity:7092521914341978112/
#   urn:li:share:... and urn:li:ugcPost:...
ACTIVITY_ID_RE = re.compile(r"(?:activity[:\-]|ugcPost:|share:)(\d{17,20})")

EPOCH_SHIFT = 22
ID_BIT_LENGTH = 63

# Anything outside this is a decoding bug, not a real post. LinkedIn did not
# exist in 1975, and a post cannot be from next year.
EARLIEST_PLAUSIBLE = datetime(2010, 1, 1, tzinfo=timezone.utc)


class UndecodablePost(ValueError):
    """The permalink did not contain a decodable activity id."""


def extract_activity_id(url: str) -> int | None:
    """Pull the numeric activity id out of a post permalink."""
    match = ACTIVITY_ID_RE.search(url or "")
    return int(match.group(1)) if match else None


def posted_at(activity_id: int, *, now: datetime | None = None) -> datetime:
    """The instant the post was created, as aware UTC.

    Raises UndecodablePost if the id is not the shape we know how to decode.
    We would rather fail loudly than report a confident wrong week.
    """
    if activity_id.bit_length() != ID_BIT_LENGTH:
        raise UndecodablePost(
            f"activity id {activity_id} is {activity_id.bit_length()} bits, "
            f"expected {ID_BIT_LENGTH} -- refusing to guess the shift"
        )

    stamp = datetime.fromtimestamp((activity_id >> EPOCH_SHIFT) / 1000, tz=timezone.utc)

    ceiling = now or datetime.now(timezone.utc)
    if not (EARLIEST_PLAUSIBLE <= stamp <= ceiling):
        raise UndecodablePost(
            f"activity id {activity_id} decodes to {stamp.isoformat()}, "
            "which is outside the plausible range"
        )
    return stamp


def posted_at_from_url(url: str, *, now: datetime | None = None) -> datetime:
    """Convenience: permalink straight to timestamp."""
    activity_id = extract_activity_id(url)
    if activity_id is None:
        raise UndecodablePost(f"no activity id found in {url!r}")
    return posted_at(activity_id, now=now)
