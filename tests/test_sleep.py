"""Tests for the pure sleep calculations in healthdatamodel.sleep (no DB)."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from healthdatamodel.sleep import (
    DailySleepSummary,
    SleepInterval,
    sleep_day_window,
    sleep_regularity_from_intervals,
    summarize_sleep_intervals,
)

try:
    NEW_YORK = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover - no tz database here
    NEW_YORK = None

needs_tzdata = pytest.mark.skipif(NEW_YORK is None, reason="no IANA tz database")

TODAY = date(2025, 6, 10)
YESTERDAY = TODAY - timedelta(days=1)
WINDOW = sleep_day_window(TODAY)  # 2025-06-09 14:00 → 2025-06-10 14:00 UTC


def at(hour: int, minute: int = 0, second: int = 0, day: date | None = None):
    """UTC datetime on the night of TODAY: hours >= 14 fall on YESTERDAY."""
    if day is None:
        day = YESTERDAY if hour >= 14 else TODAY
    return datetime.combine(day, time(hour, minute, second)).replace(
        tzinfo=timezone.utc
    )


def iv(start, end, stage, device="apple"):
    return SleepInterval(start=start, end=end, stage=stage, device=device)


def summarize(intervals, **kwargs):
    return summarize_sleep_intervals(intervals, *WINDOW, **kwargs)


# ---------------------------------------------------------------------------
# summarize_sleep_intervals
# ---------------------------------------------------------------------------


class TestSummary:
    def test_no_intervals_all_none(self):
        result = summarize([])
        assert isinstance(result, DailySleepSummary)
        assert result == DailySleepSummary(
            sleep_start=None,
            sleep_end=None,
            asleep_minutes=None,
            in_bed_minutes=None,
            awake_minutes=None,
            efficiency=None,
            awakenings=None,
            nap_minutes=None,
            device=None,
            has_in_bed_data=False,
        )

    def test_only_non_asleep_stages_is_no_sleep(self):
        result = summarize([iv(at(22), at(7), "in_bed")])
        assert result.asleep_minutes is None
        assert result.sleep_start is None
        assert result.device == "apple"

    def test_apple_asleep_only_with_gaps(self):
        # Apple sends only asleep stages; gaps between them are awake time.
        result = summarize(
            [
                iv(at(23), at(1), "core"),
                iv(at(1, 5), at(2), "deep"),  # 5-minute gap before
                iv(at(2), at(3), "rem"),  # abutting: not a gap
                iv(at(3, 30), at(6, 30), "core"),  # 30-minute gap before
            ]
        )
        assert result.sleep_start == at(23)
        assert result.sleep_end == at(6, 30)
        assert result.asleep_minutes == 120 + 115 + 180
        assert result.in_bed_minutes == 450  # inferred: sleep_start → sleep_end
        assert result.awake_minutes == 35
        assert result.efficiency == pytest.approx(415 / 450)
        assert result.efficiency < 1
        assert result.awakenings == 2
        assert result.nap_minutes == 0
        assert result.has_in_bed_data is False
        assert result.device == "apple"

    def test_sub_minute_gap_is_not_an_awakening(self):
        result = summarize(
            [
                iv(at(23), at(3), "core"),
                iv(at(3, 0, 30), at(7), "core"),
            ]
        )
        assert result.awakenings == 0

    def test_google_explicit_awake_and_in_bed(self):
        result = summarize(
            [
                iv(at(22, 30), at(7), "in_bed", "pixel"),
                iv(at(23), at(2), "core", "pixel"),
                iv(at(2), at(2, 20), "awake", "pixel"),
                iv(at(2, 20), at(6, 40), "deep", "pixel"),
                iv(at(6, 40), at(7), "awake", "pixel"),
            ]
        )
        assert result.has_in_bed_data is True
        assert result.sleep_start == at(23)
        assert result.sleep_end == at(6, 40)
        assert result.in_bed_minutes == 510  # from in_bed: 22:30 → 07:00
        assert result.asleep_minutes == 180 + 260
        assert result.awake_minutes == 70
        assert result.efficiency == pytest.approx(440 / 510)
        # The trailing awake interval is after sleep_end: not an awakening.
        assert result.awakenings == 1
        assert result.device == "pixel"

    def test_in_bed_extended_to_cover_asleep(self):
        # in_bed stops before the last asleep interval ends.
        result = summarize(
            [
                iv(at(22), at(5), "in_bed"),
                iv(at(23), at(6), "core"),
            ]
        )
        assert result.in_bed_minutes == 8 * 60  # 22:00 → 06:00
        assert result.efficiency == pytest.approx(420 / 480)

    def test_android_single_asleep_block(self):
        result = summarize([iv(at(23), at(7), "unspecified", "android")])
        assert result.asleep_minutes == 480
        assert result.in_bed_minutes == 480
        assert result.awake_minutes == 0
        assert result.efficiency == 1.0
        assert result.awakenings == 0

    def test_afternoon_nap_separate_from_main_sleep(self):
        result = summarize(
            [
                iv(at(15), at(16), "core"),  # nap on the previous afternoon
                iv(at(23), at(6), "core"),
            ]
        )
        assert result.sleep_start == at(23)
        assert result.sleep_end == at(6)
        assert result.asleep_minutes == 420
        assert result.nap_minutes == 60
        assert result.awakenings == 0
        assert result.efficiency == 1.0

    def test_main_sleep_is_session_with_most_asleep_minutes(self):
        # The earlier session has more sleep, so it is the main sleep even
        # though the later one is "at night".
        result = summarize(
            [
                iv(at(15), at(20), "core"),
                iv(at(2), at(4), "core"),
            ]
        )
        assert result.sleep_start == at(15)
        assert result.sleep_end == at(20)
        assert result.asleep_minutes == 300
        assert result.nap_minutes == 120

    def test_main_sleep_tie_goes_to_later_session(self):
        result = summarize([iv(at(15), at(17), "core"), iv(at(2), at(4), "core")])
        assert result.sleep_start == at(2)
        assert result.nap_minutes == 120

    def test_gap_threshold_controls_sessions(self):
        intervals = [iv(at(22), at(1), "core"), iv(at(3), at(7), "core")]
        # 2 h gap is not > 120 min: one session with one awakening.
        joined = summarize(intervals)
        assert joined.nap_minutes == 0
        assert joined.awakenings == 1
        assert joined.in_bed_minutes == 9 * 60
        # With a 60-minute threshold the first block becomes a nap.
        split = summarize(intervals, main_sleep_gap_minutes=60)
        assert split.sleep_start == at(3)
        assert split.asleep_minutes == 240
        assert split.nap_minutes == 180

    def test_awake_interval_bridges_sessions(self):
        # Gaps are measured between intervals of any stage.
        result = summarize(
            [
                iv(at(22), at(23), "core"),
                iv(at(23), at(2), "awake"),
                iv(at(2), at(6), "core"),
            ]
        )
        assert result.nap_minutes == 0
        assert result.asleep_minutes == 300
        assert result.awakenings == 1

    def test_interval_spanning_boundary_is_clipped(self):
        result = summarize([iv(at(12, day=YESTERDAY), at(16), "core")])
        assert result.sleep_start == at(14)
        assert result.sleep_end == at(16)
        assert result.asleep_minutes == 120

    def test_overlapping_asleep_intervals_not_double_counted(self):
        result = summarize([iv(at(23), at(3), "core"), iv(at(2), at(6), "deep")])
        assert result.asleep_minutes == 420

    def test_tz_conversion(self):
        eastern = timezone(timedelta(hours=-4))
        result = summarize([iv(at(3), at(11), "core")], tz=eastern)
        assert result.sleep_start == at(3)
        assert result.sleep_start.utcoffset() == timedelta(hours=-4)
        assert result.sleep_start.hour == 23
        assert result.sleep_end.hour == 7

    def test_default_tz_is_utc(self):
        local = timezone(timedelta(hours=2))
        result = summarize([iv(at(1).astimezone(local), at(7), "core")])
        assert result.sleep_start.utcoffset() == timedelta(0)

    def test_plain_tuples_accepted(self):
        result = summarize([(at(23), at(7), "core")])
        assert result.asleep_minutes == 480
        assert result.device is None

    def test_nap_at_end_of_window_clipped_at_boundary(self):
        # A 13:00-15:00 UTC nap: only the hour before the 14:00 boundary
        # belongs to this night; it is a separate session from the night.
        result = summarize(
            [iv(at(23), at(7), "core"), iv(at(13), at(15, day=TODAY), "core")]
        )
        assert (result.sleep_start, result.sleep_end) == (at(23), at(7))
        assert result.asleep_minutes == 480
        assert result.nap_minutes == 60

    @needs_tzdata
    def test_dst_fall_back_night(self):
        # US DST ends 2025-11-02 02:00 local: 22:00 EDT -> 07:00 EST is
        # 03:00 -> 13:00 UTC, ten real hours.
        window = sleep_day_window(date(2025, 11, 2))
        start = datetime(2025, 11, 2, 2, 0, tzinfo=timezone.utc)
        end = datetime(2025, 11, 2, 12, 0, tzinfo=timezone.utc)
        result = summarize_sleep_intervals(
            [iv(start, end, "core")], *window, tz=NEW_YORK
        )
        assert result.asleep_minutes == 600
        assert (result.sleep_start.hour, result.sleep_start.utcoffset()) == (
            22,
            timedelta(hours=-4),
        )
        assert (result.sleep_end.hour, result.sleep_end.utcoffset()) == (
            7,
            timedelta(hours=-5),
        )

    def test_aware_non_utc_inputs(self):
        # Inputs in any time zone are compared as instants.
        local = timezone(timedelta(hours=-7))
        result = summarize(
            [iv(at(23).astimezone(local), at(7).astimezone(local), "core")]
        )
        assert result.asleep_minutes == 480
        assert result.sleep_start == at(23)


# ---------------------------------------------------------------------------
# sleep_regularity_from_intervals
# ---------------------------------------------------------------------------


def night(day: date, start_hour: int, hours: int):
    """Asleep (start, end) pair for sleep day *day* starting at start_hour."""
    window_start, _ = sleep_day_window(day)
    start = window_start + timedelta(hours=(start_hour - 14) % 24)
    return [(start, start + timedelta(hours=hours))]


DAYS = [TODAY - timedelta(days=3 - i) for i in range(4)]


class TestRegularity:
    def test_identical_schedule_is_100(self):
        by_day = {day: night(day, 23, 8) for day in DAYS}
        result = sleep_regularity_from_intervals(by_day, TODAY)
        assert result.sri == pytest.approx(100.0)
        assert result.consistency_pct == pytest.approx(100.0)
        assert result.days_with_data == 4
        assert result.minutes_compared == 3 * 1440

    def test_shifted_night_lowers_sri(self):
        by_day = {day: night(day, 23, 8) for day in DAYS}
        by_day[DAYS[2]] = night(DAYS[2], 1, 8)  # 01:00–09:00 instead of 23–07
        result = sleep_regularity_from_intervals(by_day, TODAY)
        # Two transitions each mismatch 2 h + 2 h = 240 minutes.
        matches = 3 * 1440 - 2 * 240
        assert result.sri == pytest.approx(-100 + 200 * matches / (3 * 1440))
        assert result.sri < 100
        assert result.consistency_pct == pytest.approx((result.sri + 100) / 2)

    def test_single_night_is_none(self):
        result = sleep_regularity_from_intervals({TODAY: night(TODAY, 23, 8)}, TODAY)
        assert result.sri is None
        assert result.consistency_pct is None
        assert result.days_with_data == 1
        assert result.minutes_compared == 0

    def test_day_without_data_is_excluded_not_awake(self):
        by_day = {day: night(day, 23, 8) for day in DAYS}
        by_day[DAYS[2]] = []  # no records that night
        result = sleep_regularity_from_intervals(by_day, TODAY)
        # Only the DAYS[0]→DAYS[1] pair remains; it is perfectly regular.
        assert result.sri == pytest.approx(100.0)
        assert result.days_with_data == 3
        assert result.minutes_compared == 1440

    def test_non_consecutive_days_give_no_pairs(self):
        by_day = {DAYS[0]: night(DAYS[0], 23, 8), DAYS[2]: night(DAYS[2], 23, 8)}
        result = sleep_regularity_from_intervals(by_day, TODAY)
        assert result.sri is None
        assert result.days_with_data == 2
        assert result.minutes_compared == 0

    def test_completely_opposite_schedules(self):
        # 12 h asleep vs. the other 12 h of the window: every minute differs.
        by_day = {
            DAYS[0]: night(DAYS[0], 14, 12),
            DAYS[1]: night(DAYS[1], 2, 12),
        }
        result = sleep_regularity_from_intervals(by_day, DAYS[1], days=2)
        assert result.sri == pytest.approx(-100.0)
        assert result.consistency_pct == pytest.approx(0.0)

    def test_sleep_interval_items_ignore_non_asleep_stages(self):
        by_day = {}
        for day in DAYS:
            ((start, end),) = night(day, 23, 8)
            by_day[day] = [
                SleepInterval(start - timedelta(hours=1), end, "in_bed", "pixel"),
                SleepInterval(start, end, "core", "pixel"),
            ]
        result = sleep_regularity_from_intervals(by_day, TODAY)
        assert result.sri == pytest.approx(100.0)

    def test_days_outside_range_ignored(self):
        by_day = {day: night(day, 23, 8) for day in DAYS}
        by_day[TODAY + timedelta(days=1)] = night(TODAY, 3, 1)
        result = sleep_regularity_from_intervals(by_day, TODAY, days=2)
        assert result.days_with_data == 2
        assert result.minutes_compared == 1440

    def test_days_must_be_at_least_two(self):
        with pytest.raises(ValueError):
            sleep_regularity_from_intervals({}, TODAY, days=1)

    def test_matches_phillips_definition_by_brute_force(self):
        # Oracle: Phillips et al. (2017), SRI = -100 + 200/(M(N-1)) * sum of
        # delta(s(t), s(t + 24 h)), evaluated directly on minute instants
        # (a minute is asleep when its start lies in an asleep interval),
        # skipping pairs that touch a day without data.  Random intervals
        # with second offsets exercise the minute rounding.
        rng = random.Random(20261014)
        for trial in range(25):
            days = rng.randint(2, 6)
            end_day = TODAY + timedelta(days=trial % 3)
            first = end_day - timedelta(days=days - 1)
            by_day = {}
            for offset in range(days):
                day = first + timedelta(days=offset)
                if rng.random() < 0.2:
                    continue  # no data that night
                window_start, _ = sleep_day_window(day)
                spans = []
                for _ in range(rng.randint(1, 4)):
                    start = window_start + timedelta(seconds=rng.randint(0, 86399))
                    length = timedelta(seconds=rng.randint(1, 6 * 3600))
                    # Some intervals spill outside the window: must be clipped.
                    spans.append((start, start + length))
                if rng.random() < 0.3:
                    spans.append((window_start - timedelta(hours=1), window_start))
                by_day[day] = spans

            def asleep(t, by_day=by_day, first=first, days=days):
                for offset in range(days):
                    day = first + timedelta(days=offset)
                    window_start, window_end = sleep_day_window(day)
                    if window_start <= t < window_end:
                        if not by_day.get(day):
                            return None
                        return int(any(s <= t < e for s, e in by_day[day]))
                raise AssertionError(t)

            origin = sleep_day_window(first)[0]
            pairs = matches = 0
            for minute in range((days - 1) * 1440):
                t = origin + timedelta(minutes=minute)
                a, b = asleep(t), asleep(t + timedelta(hours=24))
                if a is None or b is None:
                    continue
                pairs += 1
                matches += a == b

            result = sleep_regularity_from_intervals(by_day, end_day, days=days)
            assert result.minutes_compared == pairs
            assert result.days_with_data == sum(1 for v in by_day.values() if v)
            if pairs == 0 or result.days_with_data < 2:
                assert result.sri is None
            else:
                assert result.sri == pytest.approx(-100 + 200 * matches / pairs)
                assert result.consistency_pct == pytest.approx(100 * matches / pairs)

    def test_sub_minute_interval_marks_no_minute_unless_it_covers_a_start(self):
        ((start0, _),) = night(DAYS[0], 23, 8)
        ((start1, _),) = night(DAYS[1], 23, 8)
        by_day = {
            DAYS[0]: [(start0 + timedelta(seconds=10), start0 + timedelta(seconds=50))],
            DAYS[1]: [(start1, start1 + timedelta(seconds=1))],
        }
        result = sleep_regularity_from_intervals(by_day, DAYS[1], days=2)
        # DAYS[0] marks nothing; DAYS[1] marks one minute 24 h later.
        assert result.sri == pytest.approx(-100 + 200 * 1439 / 1440)
