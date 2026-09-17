"""The contract every LinkedIn source implements.

A source's only job is to answer "which posts did these people publish in this
window". It returns a FetchResult, never a bare list, so that a failure is
reported as a failure rather than as an empty week.

Sources deliberately do NOT decode dates themselves -- see linkedin_urn for
why we derive timestamps from the permalink instead of trusting the vendor.
"""

from __future__ import annotations

from typing import Protocol

from kpi_tracker.models import FetchResult, Member
from kpi_tracker.timewindow import Week


class LinkedInSource(Protocol):
    """Swap implementations by changing one line in config.json.

    This exists because LinkedIn data acquisition is the part of this system
    most likely to break for reasons outside our control: LinkedIn has sued
    scraping vendors out of existence twice in the last two years. Keeping the
    boundary this narrow means replacing a vendor is a new file, not a rewrite.
    """

    name: str

    def fetch(self, members: list[Member], week: Week) -> FetchResult:
        """Return a FetchResult whose items are LinkedInPost objects."""
        ...
