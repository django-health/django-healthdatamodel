"""
DB-backed tests for the sleep summary / regularity queries.

Like tests/test_query.py, every test runs in both read modes (compact
SleepDay and legacy Record) via the autouse ``read_mode`` fixture, with data
created through ``ingest_records`` dual-writes, so these double as
compact/legacy parity tests.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_records
from healthdatamodel.query import (
    DailySleepSummary,
    SleepInterval,
    SleepRegularity,
    get_sleep_by_day,
    get_sleep_intervals_by_day,
    get_sleep_regularity_index,
    get_sleep_summary_by_day,
)
from healthdatamodel.schemas import RecordInput

User = get_user_model()

pytestmark = pytest.mark.django_db

TODAY = date(2025, 6, 10)
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)
NOW = datetime(2025, 6, 10, 8, 0, tzinfo=timezone.utc)

ASLEEP = "HKCategoryValueSleepAnalysisAsleepUnspecified"
CORE = "HKCategoryValueSleepAnalysisAsleepCore"
DEEP = "HKCategoryValueSleepAnalysisAsleepDeep"
REM = "HKCategoryValueSleepAnalysisAsleepREM"
AWAKE = "HKCategoryValueSleepAnalysisAwake"
IN_BED = "HKCategoryValueSleepAnalysisInBed"
ANDROID_ASLEEP = "HKCategoryValueSleepAnalysisAsleep"
OUT_OF_BED = "HKCategoryValueSleepAnalysisOutOfBed"


@pytest.fixture(params=["compact", "legacy"], autouse=True)
def read_mode(request, settings):
    """Run every test against both the compact and legacy read paths."""
    settings.HEALTHDATAMODEL_READ_COMPACT = request.param == "compact"
    return request.param


@pytest.fixture
def customer():
    return User.objects.create_user(username="sleep-query-test-user")


def at(hour: int, minute: int = 0, night_of: date = TODAY) -> datetime:
    """UTC datetime in the night ending on *night_of* (hours >= 14 fall on
    the previous calendar day)."""
    day = night_of - timedelta(days=1) if hour >= 14 else night_of
    return datetime.combine(day, time(hour, minute)).replace(tzinfo=timezone.utc)


def ingest_night(
    customer,
    night: list[tuple[datetime, datetime, str]],
    sourceName: str = "apple",
    source: str = DataSource.APPLE_HEALTH,
    admin_create_date: datetime = NOW,
):
    ingest_records(
        customer,
        [
            RecordInput(
                startDate=start,
                endDate=end,
                creationDate=NOW,
                sourceName=sourceName,
                value=value,
                type="HKCategoryTypeIdentifierSleepAnalysis",
            )
            for start, end, value in night
        ],
        source=source,
        admin_create_date=admin_create_date,
    )


# ---------------------------------------------------------------------------
# get_sleep_intervals_by_day
# ---------------------------------------------------------------------------


class TestSleepIntervalsByDay:
    def test_no_data_is_empty_list(self, customer):
        assert get_sleep_intervals_by_day(customer, TODAY, TOMORROW) == {
            TODAY: [],
            TOMORROW: [],
        }

    def test_returns_all_stages_sorted(self, customer):
        ingest_night(
            customer,
            [
                (at(23), at(2), CORE),
                (at(22, 30), at(7), IN_BED),
                (at(2), at(2, 20), AWAKE),
                (at(2, 20), at(6), DEEP),
            ],
            sourceName="pixel",
            source=DataSource.GOOGLE_HEALTH,
        )
        result = get_sleep_intervals_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == [
            SleepInterval(at(22, 30), at(7), "in_bed", "pixel"),
            SleepInterval(at(23), at(2), "core", "pixel"),
            SleepInterval(at(2), at(2, 20), "awake", "pixel"),
            SleepInterval(at(2, 20), at(6), "deep", "pixel"),
        ]
        assert all(isinstance(item, SleepInterval) for item in result[TODAY])

    def test_out_of_bed_is_awake(self, customer):
        ingest_night(
            customer,
            [
                (at(23), at(3), ANDROID_ASLEEP),
                (at(3), at(3, 30), OUT_OF_BED),
                (at(3, 30), at(7), ANDROID_ASLEEP),
            ],
            sourceName="android",
            source=DataSource.HEALTH_CONNECT,
        )
        stages = [
            item.stage
            for item in get_sleep_intervals_by_day(customer, TODAY, TODAY)[TODAY]
        ]
        assert stages == ["unspecified", "awake", "unspecified"]

    def test_non_stage_values_dropped(self, customer):
        ingest_night(customer, [(at(23), at(7), ASLEEP), (at(23), at(7), "100")])
        result = get_sleep_intervals_by_day(customer, TODAY, TODAY)
        assert [item.stage for item in result[TODAY]] == ["unspecified"]

    def test_only_awake_records_is_no_data(self, customer):
        # Selection requires asleep data, exactly like get_sleep_by_day.
        ingest_night(customer, [(at(22), at(7), IN_BED)])
        assert get_sleep_intervals_by_day(customer, TODAY, TODAY)[TODAY] == []
        assert get_sleep_by_day(customer, TODAY, TODAY)[TODAY].hours is None

    def test_same_device_choice_as_get_sleep_by_day(self, customer):
        # Same upload, two devices: default order prefers apple over garmin.
        ingest_night(
            customer,
            [(at(23), at(2), ASLEEP), (at(2), at(2, 30), AWAKE)],
            sourceName="apple",
        )
        ingest_night(
            customer,
            [(at(22), at(6), ASLEEP), (at(21), at(7), IN_BED)],
            sourceName="garmin",
        )
        intervals = get_sleep_intervals_by_day(customer, TODAY, TODAY)[TODAY]
        assert {item.device for item in intervals} == {"apple"}
        assert [item.stage for item in intervals] == ["unspecified", "awake"]
        asleep_minutes = sum(
            (item.end - item.start).total_seconds() // 60
            for item in intervals
            if item.stage == "unspecified"
        )
        assert get_sleep_by_day(customer, TODAY, TODAY)[TODAY].hours == pytest.approx(
            asleep_minutes / 60
        )

    def test_latest_upload_only(self, customer):
        ingest_night(
            customer,
            [(at(22), at(6), ASLEEP), (at(6), at(7), AWAKE)],
            admin_create_date=NOW - timedelta(hours=2),
        )
        ingest_night(customer, [(at(23), at(7), ASLEEP)], admin_create_date=NOW)
        result = get_sleep_intervals_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == [SleepInterval(at(23), at(7), "unspecified", "apple")]

    def test_interval_spanning_boundary_split(self, customer):
        # 13:00 → 15:00 UTC on TODAY spans the 14:00 boundary.
        ingest_night(customer, [(at(13), at(15, night_of=TOMORROW), ASLEEP)])
        result = get_sleep_intervals_by_day(customer, TODAY, TOMORROW)
        boundary = datetime.combine(TODAY, time(14)).replace(tzinfo=timezone.utc)
        assert result[TODAY] == [
            SleepInterval(at(13), boundary, "unspecified", "apple")
        ]
        assert result[TOMORROW] == [
            SleepInterval(boundary, at(15, night_of=TOMORROW), "unspecified", "apple")
        ]

    def test_custom_boundary_hour(self, customer):
        ingest_night(customer, [(at(23), at(3), ASLEEP), (at(3), at(4), AWAKE)])
        result = get_sleep_intervals_by_day(customer, TODAY, TODAY, day_boundary_hour=2)
        boundary = datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc)
        assert result[TODAY] == [
            SleepInterval(at(23), boundary, "unspecified", "apple")
        ]


# ---------------------------------------------------------------------------
# get_sleep_summary_by_day
# ---------------------------------------------------------------------------


class TestSleepSummaryByDay:
    def test_no_data_all_none(self, customer):
        result = get_sleep_summary_by_day(customer, TODAY, TODAY)
        summary = result[TODAY]
        assert isinstance(summary, DailySleepSummary)
        assert summary.sleep_start is None
        assert summary.sleep_end is None
        assert summary.asleep_minutes is None
        assert summary.in_bed_minutes is None
        assert summary.awake_minutes is None
        assert summary.efficiency is None
        assert summary.awakenings is None
        assert summary.nap_minutes is None
        assert summary.has_in_bed_data is False

    def test_apple_night(self, customer):
        ingest_night(
            customer,
            [
                (at(23), at(1), CORE),
                (at(1, 10), at(3), DEEP),
                (at(3), at(4), REM),
                (at(4, 30), at(7), CORE),
            ],
        )
        summary = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert summary.sleep_start == at(23)
        assert summary.sleep_end == at(7)
        assert summary.asleep_minutes == 120 + 110 + 60 + 150
        assert summary.in_bed_minutes == 480
        assert summary.awake_minutes == 40
        assert summary.efficiency == pytest.approx(440 / 480)
        assert summary.awakenings == 2
        assert summary.has_in_bed_data is False
        assert summary.device == "apple"

    def test_google_night_uses_in_bed(self, customer):
        ingest_night(
            customer,
            [
                (at(22, 30), at(7), IN_BED),
                (at(23), at(2), CORE),
                (at(2), at(2, 20), AWAKE),
                (at(2, 20), at(6, 40), DEEP),
            ],
            sourceName="pixel",
            source=DataSource.GOOGLE_HEALTH,
        )
        summary = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert summary.has_in_bed_data is True
        assert summary.in_bed_minutes == 510
        assert summary.asleep_minutes == 440
        assert summary.efficiency == pytest.approx(440 / 510)
        assert summary.awakenings == 1

    def test_android_single_block(self, customer):
        ingest_night(
            customer,
            [(at(23), at(7), ANDROID_ASLEEP)],
            sourceName="android",
            source=DataSource.HEALTH_CONNECT,
        )
        summary = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert summary.efficiency == 1.0
        assert summary.awakenings == 0
        assert summary.asleep_minutes == 480

    def test_android_out_of_bed_counts_as_awakening(self, customer):
        ingest_night(
            customer,
            [
                (at(23), at(3), ANDROID_ASLEEP),
                (at(3), at(3, 30), OUT_OF_BED),
                (at(3, 30), at(7), ANDROID_ASLEEP),
            ],
            sourceName="android",
            source=DataSource.HEALTH_CONNECT,
        )
        summary = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert summary.asleep_minutes == 450
        assert summary.in_bed_minutes == 480
        assert summary.awake_minutes == 30
        assert summary.awakenings == 1

    def test_nap_separate_from_main_sleep(self, customer):
        ingest_night(customer, [(at(15), at(16), ASLEEP), (at(23), at(6), ASLEEP)])
        summary = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert summary.sleep_start == at(23)
        assert summary.asleep_minutes == 420
        assert summary.nap_minutes == 60
        # get_sleep_by_day still counts the nap in its total.
        assert get_sleep_by_day(customer, TODAY, TODAY)[TODAY].hours == pytest.approx(
            8.0
        )

    def test_tz_conversion(self, customer):
        ingest_night(customer, [(at(3), at(11), ASLEEP)])
        eastern = timezone(timedelta(hours=-4))
        summary = get_sleep_summary_by_day(customer, TODAY, TODAY, tz=eastern)[TODAY]
        assert summary.sleep_start == at(3)
        assert summary.sleep_start.utcoffset() == timedelta(hours=-4)
        assert (summary.sleep_start.hour, summary.sleep_end.hour) == (23, 7)
        utc = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert utc.sleep_start.utcoffset() == timedelta(0)

    def test_main_sleep_gap_parameter(self, customer):
        ingest_night(customer, [(at(22), at(1), ASLEEP), (at(3), at(7), ASLEEP)])
        joined = get_sleep_summary_by_day(customer, TODAY, TODAY)[TODAY]
        assert (joined.nap_minutes, joined.awakenings) == (0, 1)
        split = get_sleep_summary_by_day(
            customer, TODAY, TODAY, main_sleep_gap_minutes=60
        )[TODAY]
        assert (split.asleep_minutes, split.nap_minutes) == (240, 180)

    def test_multi_day_mix(self, customer):
        ingest_night(customer, [(at(23), at(7), ASLEEP)])
        result = get_sleep_summary_by_day(customer, YESTERDAY, TOMORROW)
        assert set(result) == {YESTERDAY, TODAY, TOMORROW}
        assert result[TODAY].asleep_minutes == 480
        assert result[YESTERDAY].asleep_minutes is None
        assert result[TOMORROW].asleep_minutes is None


# ---------------------------------------------------------------------------
# get_sleep_regularity_index
# ---------------------------------------------------------------------------


DAYS = [TODAY - timedelta(days=3 - i) for i in range(4)]


class TestSleepRegularityIndex:
    def _nights(self, customer, schedule):
        for day, (start_hour, hours) in schedule.items():
            start = at(start_hour, night_of=day)
            ingest_night(
                customer,
                [
                    (start, start + timedelta(hours=hours), CORE),
                    (start - timedelta(minutes=30), start, AWAKE),
                ],
            )

    def test_identical_schedule_is_100(self, customer):
        self._nights(customer, {day: (23, 8) for day in DAYS})
        result = get_sleep_regularity_index(customer, TODAY)
        assert result == SleepRegularity(
            sri=pytest.approx(100.0),
            consistency_pct=pytest.approx(100.0),
            days_with_data=4,
            minutes_compared=3 * 1440,
        )

    def test_shifted_night_lowers_score(self, customer):
        schedule = {day: (23, 8) for day in DAYS}
        schedule[DAYS[2]] = (1, 8)
        self._nights(customer, schedule)
        result = get_sleep_regularity_index(customer, TODAY)
        matches = 3 * 1440 - 2 * 240
        assert result.sri == pytest.approx(-100 + 200 * matches / (3 * 1440))
        assert 0 < result.consistency_pct < 100

    def test_one_night_is_none(self, customer):
        self._nights(customer, {TODAY: (23, 8)})
        result = get_sleep_regularity_index(customer, TODAY)
        assert result.sri is None
        assert result.consistency_pct is None
        assert result.days_with_data == 1

    def test_missing_night_excluded(self, customer):
        self._nights(customer, {day: (23, 8) for day in DAYS if day != DAYS[2]})
        result = get_sleep_regularity_index(customer, TODAY)
        assert result.days_with_data == 3
        assert result.minutes_compared == 1440
        assert result.sri == pytest.approx(100.0)

    def test_days_must_be_at_least_two(self, customer):
        with pytest.raises(ValueError):
            get_sleep_regularity_index(customer, TODAY, days=1)
