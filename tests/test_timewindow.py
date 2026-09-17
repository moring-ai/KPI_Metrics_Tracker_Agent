from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kpi_tracker.timewindow import DEFAULT_TZ, to_utc
from kpi_tracker.timewindow import last_week as _last_week


def last_week(run_at, tz_name=DEFAULT_TZ, start_day="monday"):
    """Every case below was written against a Monday anchor, so pin it.

    The production default is Thursday (the job runs Thursday for Friday
    standup); Thursday-anchor behaviour is covered separately at the bottom
    of this file. Anchoring here keeps these DST and boundary cases about the
    thing they were written to test.
    """
    return _last_week(run_at, tz_name, start_day)

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# --------------------------------------------------------------------------
# Basic shape, in the default DST-less zone (Asia/Kolkata, always +05:30)
# --------------------------------------------------------------------------

def test_midweek_run_reports_previous_mon_to_mon():
    # Wed 2026-09-09 IST -> window is Mon 08-31 .. Mon 09-07 local.
    w = last_week(datetime(2026, 9, 9, 11, 0, tzinfo=IST))
    assert w.local_start == date(2026, 8, 31)
    assert w.local_end == date(2026, 9, 7)
    # 00:00 IST == 18:30 UTC the previous day.
    assert w.start == _utc(2026, 8, 30, 18, 30)
    assert w.end == _utc(2026, 9, 6, 18, 30)


def test_no_dst_zone_window_is_exactly_168_hours():
    w = last_week(date(2026, 9, 9))
    assert w.end - w.start == timedelta(hours=168)


def test_run_on_monday_reports_the_week_that_just_ended():
    # The tricky one: Monday must NOT report the week before last.
    w = last_week(datetime(2026, 9, 7, 9, 0, tzinfo=IST))
    assert (w.local_start, w.local_end) == (date(2026, 8, 31), date(2026, 9, 7))
    # And the window ends at "now-ish", not in the future.
    assert w.end <= datetime(2026, 9, 7, 9, 0, tzinfo=IST).astimezone(UTC)


def test_monday_at_one_minute_past_midnight_still_reports_prior_week():
    w = last_week(datetime(2026, 9, 7, 0, 1, tzinfo=IST))
    assert w.local_end == date(2026, 9, 7)


def test_sunday_late_and_monday_early_differ_by_exactly_one_week():
    sun = last_week(datetime(2026, 9, 6, 23, 59, tzinfo=IST))
    mon = last_week(datetime(2026, 9, 7, 0, 0, tzinfo=IST))
    assert mon.start - sun.start == timedelta(days=7)


@pytest.mark.parametrize("weekday_offset", range(7))
def test_every_day_of_a_week_yields_the_same_window(weekday_offset):
    monday = date(2026, 9, 7)
    base = last_week(monday)
    assert last_week(monday + timedelta(days=weekday_offset)) == base


def test_accepts_plain_date_and_naive_datetime_identically():
    assert last_week(date(2026, 9, 9)) == last_week(datetime(2026, 9, 9, 13, 45))


def test_aware_input_is_converted_to_local_before_picking_the_day():
    # 2026-09-06 20:00 UTC is already Mon 2026-09-07 01:30 IST.
    # So the run day is Monday, not Sunday, and we get the newer window.
    w = last_week(datetime(2026, 9, 6, 20, 0, tzinfo=UTC))
    assert w.local_end == date(2026, 9, 7)


# --------------------------------------------------------------------------
# DST: America/New_York
# --------------------------------------------------------------------------

def test_ny_spring_forward_week_is_167_hours():
    # DST starts Sun 2026-03-08. Week Mon 03-02 .. Mon 03-09 loses an hour.
    w = last_week(date(2026, 3, 10), tz_name="America/New_York")
    assert (w.local_start, w.local_end) == (date(2026, 3, 2), date(2026, 3, 9))
    assert w.start == _utc(2026, 3, 2, 5)  # 00:00 EST = 05:00Z
    assert w.end == _utc(2026, 3, 9, 4)  # 00:00 EDT = 04:00Z
    assert w.end - w.start == timedelta(hours=167)


def test_ny_fall_back_week_is_169_hours():
    # DST ends Sun 2026-11-01. Week Mon 10-26 .. Mon 11-02 gains an hour.
    w = last_week(date(2026, 11, 3), tz_name="America/New_York")
    assert w.start == _utc(2026, 10, 26, 4)  # 00:00 EDT = 04:00Z
    assert w.end == _utc(2026, 11, 2, 5)  # 00:00 EST = 05:00Z
    assert w.end - w.start == timedelta(hours=169)


def test_ny_boundaries_are_true_local_midnight():
    w = last_week(date(2026, 3, 10), tz_name="America/New_York")
    assert w.start.astimezone(NY).strftime("%H:%M") == "00:00"
    assert w.end.astimezone(NY).strftime("%H:%M") == "00:00"


# --------------------------------------------------------------------------
# The nasty case: DST transition that lands exactly ON midnight.
# --------------------------------------------------------------------------

def test_santiago_gap_at_midnight_uses_the_transition_instant():
    # Chile 2026-09-06: 23:59 -04:00 is followed by 01:00 -03:00.
    # Local midnight never happens, so the day starts at 04:00Z.
    w = last_week(date(2026, 9, 7), tz_name="America/Santiago")
    assert w.local_end == date(2026, 9, 6) + timedelta(days=1)
    scl = ZoneInfo("America/Santiago")
    assert w.end == _utc(2026, 9, 7, 3)
    # The *start* of that Monday-to-Monday span is the well-defined one; check
    # the gap day itself resolves to the transition instant, not to 00:00.
    from kpi_tracker.timewindow import _start_of_local_day

    first = _start_of_local_day(date(2026, 9, 6), scl)
    assert first == _utc(2026, 9, 6, 4)
    assert (first - timedelta(minutes=1)).astimezone(scl).date() == date(2026, 9, 5)
    assert first.astimezone(scl).date() == date(2026, 9, 6)


def test_no_local_day_is_ever_skipped_or_double_counted_across_a_year():
    """Consecutive weekly windows must tile the timeline with no gap/overlap."""
    tz = "America/New_York"
    cur = last_week(date(2026, 1, 5), tz_name=tz)
    for i in range(1, 60):
        nxt = last_week(date(2026, 1, 5) + timedelta(days=7 * i), tz_name=tz)
        assert nxt.start == cur.end, f"week {i} does not abut the previous one"
        cur = nxt


# --------------------------------------------------------------------------
# Comparing against scraped timestamps (naive vs aware)
# --------------------------------------------------------------------------

def test_contains_is_half_open():
    w = last_week(date(2026, 9, 9))
    assert w.contains(w.start)  # inclusive lower bound
    assert not w.contains(w.end)  # exclusive upper bound
    assert w.contains(w.end - timedelta(microseconds=1))


def test_contains_naive_timestamp_assumes_the_window_timezone():
    w = last_week(date(2026, 9, 9))  # Mon 08-31 .. Mon 09-07 IST
    assert w.contains(datetime(2026, 8, 31, 0, 0))  # naive == IST midnight
    assert not w.contains(datetime(2026, 8, 30, 23, 59))


def test_contains_naive_timestamp_can_override_the_assumed_zone():
    w = last_week(date(2026, 9, 9))
    # A blog that stamps naive UTC: 2026-08-30 19:00 UTC is inside the window
    # (it is 2026-08-31 00:30 IST) but would fall outside if read as IST.
    ts = datetime(2026, 8, 30, 19, 0)
    assert w.contains(ts, assume_tz="UTC")
    assert not w.contains(ts)


def test_contains_aware_timestamp_in_any_zone():
    w = last_week(date(2026, 9, 9))
    assert w.contains(datetime(2026, 9, 3, 12, 0, tzinfo=UTC))
    assert w.contains(datetime(2026, 9, 3, 8, 0, tzinfo=NY))
    assert not w.contains(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))


def test_to_utc_epoch_style_roundtrip():
    ts = datetime.fromtimestamp(1756_000_000, tz=UTC)
    assert to_utc(ts) is not None and to_utc(ts).tzinfo is UTC


def test_label_is_human_readable_inclusive_range():
    w = last_week(date(2026, 9, 9))
    assert w.label == "Mon 2026-08-31 to Sun 2026-09-06"


def test_default_tz_is_kolkata():
    assert DEFAULT_TZ == "Asia/Kolkata"
    assert last_week(date(2026, 9, 9)).tz_name == "Asia/Kolkata"


def test_accepts_iso_strings_from_the_cli():
    assert last_week("2026-09-09") == last_week(date(2026, 9, 9))
    assert last_week("2026-09-09T13:45:00") == last_week(date(2026, 9, 9))
    assert last_week("2026-09-06T20:00:00+00:00").local_end == date(2026, 9, 7)


# --------------------------------------------------------------------------
# The configurable anchor day. Production default is Thursday: the job runs on
# a Thursday so the CTO can talk to the numbers at Friday standup, and a
# Thursday anchor makes the window the seven days immediately before the run
# rather than a Mon-Sun week that is already four days stale.
# --------------------------------------------------------------------------

def test_thursday_anchor_reports_the_seven_days_before_a_thursday_run():
    w = _last_week(date(2026, 9, 3), "Asia/Kolkata", "thursday")  # a Thursday
    assert (w.local_start, w.local_last_day) == (date(2026, 8, 27), date(2026, 9, 2))
    assert w.label == "Thu 2026-08-27 to Wed 2026-09-02"


def test_thursday_is_the_production_default():
    assert _last_week(date(2026, 9, 3)) == _last_week(date(2026, 9, 3), start_day="thursday")


@pytest.mark.parametrize("offset", range(7))
def test_any_day_from_thursday_to_wednesday_reports_the_same_window(offset):
    """Re-running mid-week must not change the numbers."""
    thursday = date(2026, 9, 3)
    base = _last_week(thursday, "Asia/Kolkata", "thursday")
    assert _last_week(thursday + timedelta(days=offset), "Asia/Kolkata", "thursday") == base


def test_the_window_advances_on_the_next_thursday():
    this_week = _last_week(date(2026, 9, 3), "Asia/Kolkata", "thursday")
    next_week = _last_week(date(2026, 9, 10), "Asia/Kolkata", "thursday")
    assert next_week.local_start == this_week.local_start + timedelta(days=7)


def test_every_anchor_produces_a_seven_day_window():
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        w = _last_week(date(2026, 9, 3), "Asia/Kolkata", day)
        assert w.local_end - w.local_start == timedelta(days=7)
        assert w.local_start.strftime("%A").lower() == day


def test_anchor_accepts_a_weekday_number():
    assert _last_week(date(2026, 9, 3), "Asia/Kolkata", 3) == _last_week(
        date(2026, 9, 3), "Asia/Kolkata", "thursday"
    )


def test_unknown_anchor_day_is_rejected():
    with pytest.raises(ValueError, match="unknown weekday"):
        _last_week(date(2026, 9, 3), "Asia/Kolkata", "thorsday")
