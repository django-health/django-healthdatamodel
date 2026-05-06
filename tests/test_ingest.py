"""
Tests for healthdatamodel.ingest and the in-memory query helper
``get_activity_by_day_from_records``.

All tests run against SQLite (:memory:).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import (
    aingest_compact_activity,
    aingest_records,
    expand_compact_activity,
    ingest_compact_activity,
    ingest_records,
)
from healthdatamodel.models import Record
from healthdatamodel.query import ActivityMetric, get_activity_by_day_from_records
from healthdatamodel.schemas import RecordInput

User = get_user_model()

pytestmark = pytest.mark.django_db

WEEK_START = datetime(2025, 6, 2, 0, 0, tzinfo=timezone.utc)  # Monday
MON = date(2025, 6, 2)
TUE = date(2025, 6, 3)


@pytest.fixture
def customer():
    return User.objects.create_user(username="ingest-test-user")


def _record(
    start: datetime,
    end: datetime,
    value: str,
    unit: str = "kcal",
    type: str = ActivityMetric.ACTIVE_CALORIES,
) -> RecordInput:
    return RecordInput(
        startDate=start,
        endDate=end,
        creationDate=datetime.now(timezone.utc),
        sourceName="apple",
        value=value,
        unit=unit,
        type=type,
    )


# ---------------------------------------------------------------------------
# ingest_records
# ---------------------------------------------------------------------------


class TestIngestRecords:
    def test_saves_records_to_db(self, customer):
        start = WEEK_START
        records = [_record(start, start + timedelta(days=1), "500")]
        ingest_records(customer, records, source=DataSource.APPLE_HEALTH)
        assert Record.objects.filter(customer=customer).count() == 1

    def test_admin_create_date_set(self, customer):
        ts = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        ingest_records(
            customer,
            [_record(WEEK_START, WEEK_START + timedelta(days=1), "100")],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=ts,
        )
        record = Record.objects.get(customer=customer)
        assert record.admin_create_date == ts

    def test_source_field_stored(self, customer):
        ingest_records(
            customer,
            [_record(WEEK_START, WEEK_START + timedelta(days=1), "100")],
            source=DataSource.FITBIT,
        )
        record = Record.objects.get(customer=customer)
        assert record.source == DataSource.FITBIT

    def test_empty_list_is_noop(self, customer):
        ingest_records(customer, [], source=DataSource.APPLE_HEALTH)
        assert Record.objects.filter(customer=customer).count() == 0


# ---------------------------------------------------------------------------
# expand_compact_activity
# ---------------------------------------------------------------------------


class TestExpandCompactActivity:
    def test_single_source_expands(self):
        values = [300.0, 250.0, 200.0, 150.0]
        records = expand_compact_activity(
            ActivityMetric.ACTIVE_CALORIES, WEEK_START, [(values, "apple")], 15, "kcal"
        )
        assert len(records) == 4
        assert records[0].startDate == WEEK_START
        assert records[0].endDate == WEEK_START + timedelta(minutes=15)
        assert records[0].value == "300.0"
        assert records[0].type == ActivityMetric.ACTIVE_CALORIES

    def test_two_sources_each_expanded(self):
        values_a = [100.0, 200.0]
        values_b = [50.0, 75.0]
        records = expand_compact_activity(
            ActivityMetric.STEPS,
            WEEK_START,
            [(values_a, "garmin"), (values_b, "apple")],
            15,
            "count",
        )
        assert len(records) == 4
        sources = [r.sourceName for r in records]
        assert sources.count("garmin") == 2
        assert sources.count("apple") == 2

    def test_intervals_are_contiguous(self):
        values = [1.0, 2.0, 3.0]
        records = expand_compact_activity(
            ActivityMetric.ACTIVE_CALORIES, WEEK_START, [(values, "apple")], 15, "kcal"
        )
        for i in range(len(records) - 1):
            assert records[i].endDate == records[i + 1].startDate

    def test_empty_values_returns_empty(self):
        records = expand_compact_activity(
            ActivityMetric.ACTIVE_CALORIES, WEEK_START, [([], "apple")], 15, "kcal"
        )
        assert records == []


# ---------------------------------------------------------------------------
# ingest_compact_activity
# ---------------------------------------------------------------------------


class TestIngestCompactActivity:
    def test_saves_one_row_per_source_per_interval(self, customer):
        values_a = [100.0, 200.0]
        values_b = [50.0, 75.0]
        ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [(values_a, "apple"), (values_b, "fitbit")],
            resolution_minutes=15,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
        )
        assert Record.objects.filter(customer=customer).count() == 4

    def test_return_results_false_returns_none(self, customer):
        result = ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([300.0, 200.0], "apple")],
            resolution_minutes=1440,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            return_results=False,
        )
        assert result is None

    def test_return_results_true_returns_daily_totals(self, customer):
        # Two 1440-min intervals: Mon=300 kcal, Tue=200 kcal
        result = ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([300.0, 200.0], "apple")],
            resolution_minutes=1440,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            return_results=True,
        )
        assert result is not None
        assert result[MON] == pytest.approx(300.0)
        assert result[TUE] == pytest.approx(200.0)

    def test_return_results_unit_cal_converted(self, customer):
        result = ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([300_000.0, 200_000.0], "apple")],
            resolution_minutes=1440,
            unit="cal",
            source=DataSource.APPLE_HEALTH,
            return_results=True,
        )
        assert result is not None
        assert result[MON] == pytest.approx(300.0)
        assert result[TUE] == pytest.approx(200.0)

    def test_return_results_zero_is_not_none(self, customer):
        result = ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([0.0, 500.0], "apple")],
            resolution_minutes=1440,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            return_results=True,
        )
        assert result is not None
        assert result[MON] == 0.0
        assert result[TUE] == pytest.approx(500.0)

    def test_return_results_missing_day_is_none(self, customer):
        # Only one interval: MON; TUE should be None
        result = ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([400.0], "apple")],
            resolution_minutes=1440,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            return_results=True,
        )
        assert result is not None
        assert result[MON] == pytest.approx(400.0)
        assert result.get(TUE) is None  # TUE is outside the 1-element range


# ---------------------------------------------------------------------------
# get_activity_by_day_from_records
# ---------------------------------------------------------------------------


class TestGetActivityByDayFromRecords:
    def _make_daily_record(
        self,
        day: date,
        value: float,
        metric: ActivityMetric = ActivityMetric.ACTIVE_CALORIES,
        unit: str = "kcal",
    ) -> RecordInput:
        start = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
        return RecordInput(
            startDate=start,
            endDate=start + timedelta(days=1),
            creationDate=datetime.now(timezone.utc),
            sourceName="apple",
            value=str(value),
            unit=unit,
            type=metric,
        )

    def test_sums_records_by_day(self):
        records = [
            self._make_daily_record(MON, 300.0),
            self._make_daily_record(TUE, 250.0),
        ]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, TUE
        )
        assert result[MON] == pytest.approx(300.0)
        assert result[TUE] == pytest.approx(250.0)

    def test_missing_day_returns_none(self):
        records = [self._make_daily_record(MON, 300.0)]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, TUE
        )
        assert result[MON] == pytest.approx(300.0)
        assert result[TUE] is None

    def test_zero_value_returns_zero_not_none(self):
        records = [self._make_daily_record(MON, 0.0)]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, MON
        )
        assert result[MON] == 0.0

    def test_cal_unit_converted_to_kcal(self):
        records = [self._make_daily_record(MON, 300_000.0, unit="cal")]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, MON
        )
        assert result[MON] == pytest.approx(300.0)

    def test_wrong_metric_excluded(self):
        records = [
            self._make_daily_record(MON, 300.0, metric=ActivityMetric.ACTIVE_CALORIES),
            self._make_daily_record(MON, 1000.0, metric=ActivityMetric.STEPS),
        ]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, MON
        )
        assert result[MON] == pytest.approx(300.0)

    def test_negative_value_clamped_to_zero(self):
        records = [self._make_daily_record(MON, -50.0)]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, MON
        )
        assert result[MON] == 0.0

    def test_sums_15min_records_to_daily(self):
        # 4 × 15-min records summing to 100 kcal
        base = datetime.combine(MON, datetime.min.time()).replace(tzinfo=timezone.utc)
        records = [
            RecordInput(
                startDate=base + timedelta(minutes=i * 15),
                endDate=base + timedelta(minutes=(i + 1) * 15),
                creationDate=datetime.now(timezone.utc),
                sourceName="apple",
                value="25.0",
                unit="kcal",
                type=ActivityMetric.ACTIVE_CALORIES,
            )
            for i in range(4)
        ]
        result = get_activity_by_day_from_records(
            records, ActivityMetric.ACTIVE_CALORIES, MON, MON
        )
        assert result[MON] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Async ingest (basic smoke tests — no event loop complexity needed here)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAsyncIngest:
    async def test_aingest_records_saves(self):
        customer = await User.objects.acreate_user(username="async-ingest-user-1")
        records = [_record(WEEK_START, WEEK_START + timedelta(days=1), "500")]
        await aingest_records(customer, records, source=DataSource.APPLE_HEALTH)
        count = await Record.objects.filter(customer=customer).acount()
        assert count == 1

    async def test_aingest_compact_activity_saves(self):
        customer = await User.objects.acreate_user(username="async-ingest-user-2")
        await aingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([300.0, 200.0], "apple")],
            resolution_minutes=1440,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
        )
        count = await Record.objects.filter(customer=customer).acount()
        assert count == 2

    async def test_aingest_compact_activity_return_results(self):
        customer = await User.objects.acreate_user(username="async-ingest-user-3")
        result = await aingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            WEEK_START,
            [([300.0, 200.0], "apple")],
            resolution_minutes=1440,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            return_results=True,
        )
        assert result is not None
        assert result[MON] == pytest.approx(300.0)
        assert result[TUE] == pytest.approx(200.0)
