"""The reporting week window.

"Last week" is the most recently *completed* seven-day block, aligned to a
configurable anchor day and measured in local time. We return it as a pair of
timezone-aware UTC datetimes, because every scraped timestamp is normalised to
UTC before comparison and UTC is the only thing that compares safely.

The anchor day matters more than it looks. This job runs on a Thursday so the
CTO can talk to it at Friday standup. With a Monday anchor, Thursday's run
reports Mon-Sun -- already four days stale by the time anyone discusses it.
With a Thursday anchor it reports Thu-Wed: the seven days immediately before
the run. Same seven days of work, four days fresher at the meeting.

Set it in config.json as `week_start_day`.

Stdlib only: datetime + zoneinfo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"

# Monday is 0, matching date.weekday().
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_START_DAY = 3  # Thursday: the job runs Thursday, for Friday standup


def weekday_number(name: str | int) -> int:
    """Turn 'thursday' (or 3) into 3. Raises ValueError on anything else."""
    if isinstance(name, int):
        if not 0 <= name <= 6:
            raise ValueError(f"weekday number must be 0-6, got {name}")
        return name
    try:
        return WEEKDAY_NAMES.index(str(name).strip().lower())
    except ValueError:
        raise ValueError(
            f"unknown weekday {name!r}; expected one of {', '.join(WEEKDAY_NAMES)}"
        ) from None


def _start_of_local_day(day: date, tz: ZoneInfo) -> datetime:
    """First instant of `day` in `tz`, as aware UTC.

    `fold=0` (the default) is deliberate and is correct on both DST edges:

    * Spring-forward *over* midnight (e.g. America/Santiago 2026-09-06, where
      23:59 is followed by 01:00): local 00:00 does not exist. PEP 495 says a
      nonexistent time with fold=0 uses the offset *before* the gap, which maps
      to exactly the transition instant -- i.e. the true first instant of the
      day. Verified: 2026-09-06T04:00Z is that day's first instant, and one
      minute earlier is still 2026-09-05 local.
    * Fall-back *over* midnight (local 00:00 happens twice): fold=0 selects the
      earlier of the two, which is again the true first instant of the day.

    So no special-casing is needed. Do not "fix" this with fold=1.
    """
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


@dataclass(frozen=True)
class Week:
    """A half-open [start, end) reporting window."""

    start: datetime  # aware UTC, inclusive
    end: datetime  # aware UTC, exclusive
    tz_name: str
    local_start: date  # the anchor day this window opens on, in local time
    local_end: date  # the anchor day it closes on, in local time (exclusive)

    def contains(self, ts: datetime, *, assume_tz: str | None = None) -> bool:
        """Is `ts` inside the window? Naive `ts` is read in `assume_tz`."""
        return self.start <= to_utc(ts, assume_tz or self.tz_name) < self.end

    def contains_date(self, day: date) -> bool:
        """Is this local calendar date inside the window?

        Blog posts carry a date typed by a human ("July 14, 2026"), not an
        instant. Converting it to a timestamp would invent a time of day, so
        compare it against the window's local Monday..Monday bounds instead.
        """
        return day is not None and self.local_start <= day < self.local_end

    @property
    def local_last_day(self) -> date:
        """The inclusive final day, which is what humans expect to read."""
        return self.local_end - timedelta(days=1)

    @property
    def label(self) -> str:
        """Human label, e.g. 'Thu 2026-08-27 to Wed 2026-09-02'.

        The weekday names are here on purpose: with a configurable anchor it
        should be obvious at a glance which seven days were measured.
        """
        start_name = self.local_start.strftime("%a")
        end_name = self.local_last_day.strftime("%a")
        return f"{start_name} {self.local_start} to {end_name} {self.local_last_day}"


def to_utc(ts: datetime, assume_tz: str = DEFAULT_TZ) -> datetime:
    """Normalise a scraped timestamp to aware UTC.

    Aware input is converted. Naive input is *assumed* to be wall time in
    `assume_tz` -- which is a guess, so scrapers should prefer to hand us aware
    datetimes (LinkedIn and most blog feeds emit ISO-8601 with an offset, or a
    Unix epoch, both of which are unambiguous).
    """
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        ts = ts.replace(tzinfo=ZoneInfo(assume_tz))
    return ts.astimezone(timezone.utc)


def last_week(
    run_at: datetime | date | str,
    tz_name: str = DEFAULT_TZ,
    start_day: int | str = DEFAULT_START_DAY,
) -> Week:
    """The most recently *completed* seven-day block relative to `run_at`.

    The current, partial week is never included, so every run inside the same
    week reports the same numbers -- which is what makes re-running safe.

    With the default Thursday anchor, a run on Thursday 2026-09-03 reports
    Thu 2026-08-27 through Wed 2026-09-02.
    """
    tz = ZoneInfo(tz_name)

    # The CLI hands us a string; accept ISO-8601 date or datetime.
    if isinstance(run_at, str):
        run_at = (
            datetime.fromisoformat(run_at)
            if len(run_at) > 10
            else date.fromisoformat(run_at)
        )

    # NOTE: datetime is a subclass of date, so test datetime first.
    if isinstance(run_at, datetime):
        local = run_at.astimezone(tz) if run_at.tzinfo else run_at.replace(tzinfo=tz)
        run_day = local.date()
    else:
        run_day = run_at

    anchor = weekday_number(start_day)
    days_since_anchor = (run_day.weekday() - anchor) % 7
    current_anchor = run_day - timedelta(days=days_since_anchor)
    previous_anchor = current_anchor - timedelta(days=7)

    return Week(
        start=_start_of_local_day(previous_anchor, tz),
        end=_start_of_local_day(current_anchor, tz),
        tz_name=tz_name,
        local_start=previous_anchor,
        local_end=current_anchor,
    )
