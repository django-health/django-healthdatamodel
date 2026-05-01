"""
Tests for healthdatamodel.query.

Sleep tests run against SQLite (:memory:) and cover the full contract.

Activity tests require PostgreSQL (the source-ranking CTE uses
``EXTRACT(epoch FROM ...)::INTEGER``).  They are skipped here; the
consuming project's pytest suite (which runs against PostgreSQL) covers them.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.models import DataSourceRanking, Record, WearableConnection
from healthdatamodel.query import ActivityMetric, get_activity_by_day, get_sleep_hours_by_day

User = get_user_model()

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

TODAY = date(2025, 6, 10)
YESTERDAY = TODAY - timedelta(days=1)

# 2 pm UTC boundary — same as production default
_DAY_START = datetime.combine(TODAY, time(14)).replace(tzinfo=timezone.utc) - timedelta(days=1)
_DAY_END = datetime.combine(TODAY, time(14)).replace(tzinfo=timezone.utc)

NOW = datetime(2025, 6, 10, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def customer():
    return User.objects.create_user(username="query-test-user")


def _sleep_record(customer, start: datetime, end: datetime, value: str, sourceName: str = "apple", admin_create_date: datetime = NOW):
    return Record.objects.create(
        customer=customer,
        startDate=start,
        endDate=end,
        type="HKCategoryTypeIdentifierSleepAnalysis",
        value=value,
        source=DataSource.APPLE_HEALTH,
        sourceName=sourceName,
        creationDate=NOW,
        admin_create_date=admin_create_date,
    )


# ---------------------------------------------------------------------------
# get_sleep_hours_by_day — missing data
# ---------------------------------------------------------------------------


class TestSleepHoursNoData:
    def test_empty_returns_none(self, customer):
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result == {TODAY: None}

    def test_wrong_day_returns_none(self, customer):
        # Record is a full day BEFORE the window — should not be counted
        two_days_ago = YESTERDAY - timedelta(days=1)
        _sleep_record(
            customer,
            start=datetime.combine(two_days_ago, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(YESTERDAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] is None

    def test_aggregate_record_excluded(self, customer):
        # Numeric value in the value field is an aggregate — not a sleep interval
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="100",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] is None

    def test_awake_inbed_excluded(self, customer):
        for non_sleep_value in (
            "HKCategoryValueSleepAnalysisAwake",
            "HKCategoryValueSleepAnalysisInBed",
        ):
            _sleep_record(
                customer,
                start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
                end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                value=non_sleep_value,
            )
            result = get_sleep_hours_by_day(customer, TODAY, TODAY)
            assert result[TODAY] is None, f"expected None for {non_sleep_value}"
            Record.objects.filter(customer=customer).delete()


# ---------------------------------------------------------------------------
# get_sleep_hours_by_day — basic computations
# ---------------------------------------------------------------------------


class TestSleepHoursBasic:
    def test_single_source_8_hours(self, customer):
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == 8.0

    def test_multiple_records_same_upload_summed(self, customer):
        # 3 h + 4.5 h with a gap → 7.5 h
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        _sleep_record(
            customer,
            start=datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == pytest.approx(7.5)

    def test_most_recent_upload_wins(self, customer):
        # Older upload: 3 h record.  Newer upload: 4.5 h record.
        # Only the newer upload should be counted.
        older = NOW - timedelta(hours=2)
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            admin_create_date=older,
        )
        _sleep_record(
            customer,
            start=datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            admin_create_date=NOW,
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        # Only the newer record (4.5 h) should count
        assert result[TODAY] == pytest.approx(4.5)

    def test_all_asleep_subtypes_counted(self, customer):
        for subtype in (
            "HKCategoryValueSleepAnalysisAsleepUnspecified",
            "HKCategoryValueSleepAnalysisAsleepCore",
            "HKCategoryValueSleepAnalysisAsleepDeep",
            "HKCategoryValueSleepAnalysisAsleepREM",
        ):
            _sleep_record(
                customer,
                start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
                end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                value=subtype,
            )
            result = get_sleep_hours_by_day(customer, TODAY, TODAY)
            assert result[TODAY] == pytest.approx(8.0), f"failed for {subtype}"
            Record.objects.filter(customer=customer).delete()

    def test_record_crossing_day_boundary_clipped(self, customer):
        # Record from 1 pm previous day to 3 pm current day crosses both
        # boundaries.  Only the portion within the 2pm–2pm window counts:
        # 1 pm prev → 2 pm prev = 0h (before window start)
        # 2 pm prev → 2 pm today = 24 h (the whole window)
        # But the record only covers 1pm prev → 3pm today,
        # so clipped to 2pm prev → 2pm today = 24 h.
        # Practical case: short record straddling the 2pm boundary.
        # 1pm yesterday → 3pm today, clipped to 2pm yesterday → 2pm today = 24h.
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(13)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(15)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == pytest.approx(24.0)


# ---------------------------------------------------------------------------
# get_sleep_hours_by_day — date ranges
# ---------------------------------------------------------------------------


class TestSleepHoursDateRange:
    def test_single_day_range(self, customer):
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert set(result.keys()) == {TODAY}

    def test_multi_day_range_all_keys_present(self, customer):
        end = TODAY + timedelta(days=3)
        result = get_sleep_hours_by_day(customer, TODAY, end)
        expected = {TODAY + timedelta(days=i) for i in range(4)}
        assert set(result.keys()) == expected

    def test_multi_day_mix_of_none_and_values(self, customer):
        # Sleep record only for TODAY
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        tomorrow = TODAY + timedelta(days=1)
        result = get_sleep_hours_by_day(customer, TODAY, tomorrow)
        assert result[TODAY] == pytest.approx(8.0)
        assert result[tomorrow] is None

    def test_custom_boundary_hour(self, customer):
        # Using midnight boundary (hour=0): window is midnight-to-midnight
        _sleep_record(
            customer,
            start=datetime.combine(TODAY, time(1)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY, day_boundary_hour=0)
        # window is midnight prev day → midnight today; record starts at 1am today
        # which is AFTER the boundary end, so it falls outside the window
        assert result[TODAY] is None

        # Using boundary_hour=8: window is 8am prev → 8am today
        result2 = get_sleep_hours_by_day(customer, TODAY, TODAY, day_boundary_hour=8)
        # record (1am–7am today) is fully within 8am prev → 8am today
        assert result2[TODAY] == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# get_sleep_hours_by_day — multi-device selection
# ---------------------------------------------------------------------------


class TestSleepHoursMultiDevice:
    def test_default_sort_prefers_apple_over_garmin(self, customer):
        # apple: 3 h,  garmin: 4.5 h — same upload time
        # default order has apple before garmin, so apple should win
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            sourceName="apple",
        )
        _sleep_record(
            customer,
            start=datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            sourceName="garmin",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == pytest.approx(3.0)

    def test_preferred_sleep_device_from_wearable_connection(self, customer):
        # Same records as above, but WearableConnection marks garmin as preferred
        _sleep_record(
            customer,
            start=datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            sourceName="apple",
        )
        _sleep_record(
            customer,
            start=datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
            end=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            sourceName="garmin",
        )
        WearableConnection.objects.create(
            customer=customer,
            data_source=DataSource.APPLE_HEALTH,
            device_brand="garmin",
            preferred_for_sleep=True,
            status="active",
        )
        result = get_sleep_hours_by_day(customer, TODAY, TODAY)
        assert result[TODAY] == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# get_activity_by_day — skipped (requires PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="get_activity_by_day requires PostgreSQL; tested via consuming project")
class TestActivityByDay:
    def test_empty_returns_none(self, customer):
        result = get_activity_by_day(customer, ActivityMetric.ACTIVE_CALORIES, TODAY, TODAY)
        assert result == {TODAY: None}

    def test_single_day_value(self, customer):
        DataSourceRanking.objects.create(
            customer=customer, dataSource=DataSource.APPLE_HEALTH, rank=1
        )
        DataSourceRanking.objects.create(
            customer=customer, dataSource=DataSource.FITBIT, rank=2
        )
        DataSourceRanking.objects.create(
            customer=customer, dataSource=DataSource.HEALTH_CONNECT, rank=3
        )
        Record.objects.create(
            customer=customer,
            startDate=datetime.combine(TODAY, time(0)).replace(tzinfo=timezone.utc),
            endDate=datetime.combine(TODAY + timedelta(days=1), time(0)).replace(tzinfo=timezone.utc),
            type=ActivityMetric.ACTIVE_CALORIES.value,
            value="500",
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            sourceName="apple",
            creationDate=NOW,
            admin_create_date=NOW,
        )
        result = get_activity_by_day(customer, ActivityMetric.ACTIVE_CALORIES, TODAY, TODAY)
        assert result[TODAY] == pytest.approx(500.0)

    def test_multi_day_range(self, customer):
        end = TODAY + timedelta(days=2)
        result = get_activity_by_day(customer, ActivityMetric.ACTIVE_CALORIES, TODAY, end)
        assert set(result.keys()) == {TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=2)}
        assert all(v is None for v in result.values())
