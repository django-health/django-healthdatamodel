"""Tests for the record-level readers in healthdatamodel.query (SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_workouts
from healthdatamodel.query import (
    SLEEP_TYPE,
    ActivityMetric,
    SleepValue,
    get_sleep_records,
    infer_resolution_minutes,
    iter_records,
    iter_workouts,
)
from healthdatamodel.schemas import StoredRecord, StoredWorkout, WorkoutInput
from healthdatamodel.testing import create_record, create_records

User = get_user_model()

pytestmark = pytest.mark.django_db

WK_START = datetime(2025, 6, 2, tzinfo=timezone.utc)  # Monday
WK_END = WK_START + timedelta(days=7) - timedelta(seconds=1)
EARLY = datetime(2025, 6, 3, tzinfo=timezone.utc)
LATE = datetime(2025, 6, 5, tzinfo=timezone.utc)


@pytest.fixture
def customer():
    return User.objects.create_user(username="reader")


# ---------------------------------------------------------------------------
# infer_resolution_minutes
# ---------------------------------------------------------------------------


class TestInferResolution:
    def test_none_without_records(self, customer):
        assert infer_resolution_minutes(customer, WK_START, WK_END) is None

    def test_most_recent_upload_wins(self, customer):
        # daily records uploaded first, then 15-minute records later
        create_record(
            customer,
            startDate=WK_START,
            endDate=WK_START + timedelta(days=1),
            type=ActivityMetric.STEPS,
            admin_create_date=EARLY,
        )
        create_record(
            customer,
            startDate=WK_START,
            type=ActivityMetric.ACTIVE_CALORIES,
            admin_create_date=LATE,
        )
        assert infer_resolution_minutes(customer, WK_START, WK_END) == 15

    def test_source_filter(self, customer):
        create_record(
            customer,
            startDate=WK_START,
            endDate=WK_START + timedelta(days=1),
            source=DataSource.FITBIT,
            admin_create_date=EARLY,
        )
        create_record(
            customer,
            startDate=WK_START,
            source=DataSource.APPLE_HEALTH,
            admin_create_date=LATE,
        )
        assert (
            infer_resolution_minutes(
                customer, WK_START, WK_END, source=DataSource.FITBIT
            )
            == 1440
        )
        assert (
            infer_resolution_minutes(customer, WK_START, WK_END, source="garmin")
            is None
        )

    def test_ignores_other_types_by_default(self, customer):
        create_record(
            customer,
            startDate=WK_START,
            endDate=WK_START + timedelta(hours=8),
            type=SLEEP_TYPE,
            value=SleepValue.ASLEEP_CORE,
            admin_create_date=LATE,
        )
        create_record(
            customer,
            startDate=WK_START,
            endDate=WK_START + timedelta(minutes=60),
            admin_create_date=EARLY,
        )
        assert infer_resolution_minutes(customer, WK_START, WK_END) == 60
        assert (
            infer_resolution_minutes(customer, WK_START, WK_END, types=[SLEEP_TYPE])
            == 480
        )

    def test_record_must_lie_inside_window(self, customer):
        create_record(
            customer,
            startDate=WK_END - timedelta(minutes=5),
            endDate=WK_END + timedelta(minutes=10),
        )
        assert infer_resolution_minutes(customer, WK_START, WK_END) is None

    def test_truncates_partial_minutes(self, customer):
        create_record(
            customer,
            startDate=WK_START,
            endDate=WK_START + timedelta(minutes=14, seconds=59),
        )
        assert infer_resolution_minutes(customer, WK_START, WK_END) == 14


# ---------------------------------------------------------------------------
# get_sleep_records
# ---------------------------------------------------------------------------


def _sleep(start: datetime, hours: float, value: str = SleepValue.ASLEEP_CORE):
    return {
        "startDate": start,
        "endDate": start + timedelta(hours=hours),
        "type": SLEEP_TYPE,
        "value": value,
        "sourceName": "Apple Watch",
    }


class TestGetSleepRecords:
    def test_inclusive_startdate_window_and_order(self, customer):
        a = WK_START + timedelta(hours=22)
        create_records(
            customer,
            [
                _sleep(WK_END, 8),  # exactly at end: included
                _sleep(a, 1),
                _sleep(WK_START, 2, SleepValue.AWAKE),  # exactly at start
                _sleep(WK_START - timedelta(seconds=1), 3),  # before: excluded
                _sleep(WK_END + timedelta(seconds=1), 3),  # after: excluded
            ],
        )
        create_record(customer, startDate=a)  # non-sleep: excluded
        rows = get_sleep_records(customer, WK_START, WK_END)
        assert [r.startDate for r in rows] == [WK_START, a, WK_END]
        assert all(isinstance(r, StoredRecord) for r in rows)
        assert rows[0].value == SleepValue.AWAKE
        assert rows[1].endDate == a + timedelta(hours=1)
        assert rows[1].sourceName == "Apple Watch"
        assert rows[1].source == DataSource.APPLE_HEALTH
        assert rows[1].customer_id == customer.pk

    def test_asleep_only(self, customer):
        create_records(
            customer,
            [
                _sleep(WK_START, 1, SleepValue.IN_BED),
                _sleep(WK_START + timedelta(hours=1), 1, SleepValue.ASLEEP_DEEP),
            ],
        )
        rows = get_sleep_records(customer, WK_START, WK_END, asleep_only=True)
        assert [r.value for r in rows] == [SleepValue.ASLEEP_DEEP]

    def test_scoped_to_customer(self, customer):
        other = User.objects.create_user(username="other-sleeper")
        create_records(other, [_sleep(WK_START, 8)])
        assert get_sleep_records(customer, WK_START, WK_END) == []


# ---------------------------------------------------------------------------
# iter_records / iter_workouts
# ---------------------------------------------------------------------------


class TestIterRecords:
    def test_order_and_limit(self, customer):
        create_records(
            customer,
            [{"startDate": WK_START + timedelta(hours=i)} for i in range(5)],
        )
        newest = list(iter_records(customer))
        assert [r.startDate for r in newest] == [
            WK_START + timedelta(hours=i) for i in range(4, -1, -1)
        ]
        oldest_two = list(iter_records(customer, limit=2, order="startDate"))
        assert [r.startDate for r in oldest_two] == [
            WK_START,
            WK_START + timedelta(hours=1),
        ]

    def test_bad_order(self, customer):
        with pytest.raises(ValueError):
            list(iter_records(customer, order="value"))  # type: ignore[arg-type]


class TestIterWorkouts:
    def test_yields_stored_workouts(self, customer):
        ingest_workouts(
            customer,
            [
                WorkoutInput(
                    startDate=WK_START + timedelta(hours=i),
                    endDate=WK_START + timedelta(hours=i, minutes=30),
                    creationDate=WK_START,
                    sourceName="watch",
                    durationUnit="min",
                    duration=30,
                    workoutActivityType=f"T{i}",
                )
                for i in range(3)
            ],
            source=DataSource.APPLE_HEALTH,
        )
        rows = list(iter_workouts(customer, limit=2))
        assert all(isinstance(w, StoredWorkout) for w in rows)
        assert [w.workoutActivityType for w in rows] == ["T2", "T1"]
        assert rows[0].duration == 30
        assert rows[0].source == DataSource.APPLE_HEALTH

    def test_bad_order(self, customer):
        with pytest.raises(ValueError):
            list(iter_workouts(customer, order="duration"))  # type: ignore[arg-type]
