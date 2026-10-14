"""
The v0.11 consumer helpers are compact-aware in 1.0.

Covers ``customer_data.delete_records`` / ``delete_customer_health_data`` /
``reassign_customer_health_data``, ``ingest.restore_records`` and the
``testing`` fixtures against the compact tables (``SleepDay`` /
``ActivityDay``).

Tests that read through the query API run in both read modes via the
``read_mode`` fixture, like ``test_query.py``; the legacy *activity* read
needs PostgreSQL, so :func:`_activity` skips it on SQLite.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from healthdatamodel.constants import DataSource
from healthdatamodel.customer_data import (
    delete_customer_health_data,
    delete_records,
    has_records,
    reassign_customer_health_data,
)
from healthdatamodel.ingest import ingest_records, restore_records
from healthdatamodel.models import ActivityDay, Record, SleepDay
from healthdatamodel.query import (
    ActivityMetric,
    get_activity_records,
    get_sleep_hours_by_day,
)
from healthdatamodel.schemas import RecordInput, StoredRecord
from healthdatamodel.testing import create_record, create_records

User = get_user_model()

pytestmark = pytest.mark.django_db

POSTGRES = connection.vendor == "postgresql"

DAY = date(2025, 6, 10)
MIDNIGHT = datetime.combine(DAY, time(0)).replace(tzinfo=timezone.utc)
T1 = datetime(2025, 6, 10, 8, 0, tzinfo=timezone.utc)
T2 = T1 + timedelta(hours=1)
T3 = T1 + timedelta(hours=2)

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP = "HKCategoryValueSleepAnalysisAsleepUnspecified"
KCAL = ActivityMetric.ACTIVE_CALORIES.value


def _at(hour: int, day: date = DAY) -> datetime:
    return datetime.combine(day, time(0)).replace(tzinfo=timezone.utc) + timedelta(
        hours=hour
    )


def _night(start_hour: int, end_hour: int, device: str = "apple") -> RecordInput:
    """Sleep from *start_hour* (relative to midnight of DAY, may be negative)."""
    return RecordInput(
        startDate=_at(start_hour),
        endDate=_at(end_hour),
        creationDate=T1,
        sourceName=device,
        value=ASLEEP,
        type=SLEEP_TYPE,
    )


def _slot(index: int, value: float, device: str = "apple") -> RecordInput:
    start = MIDNIGHT + timedelta(minutes=15 * index)
    return RecordInput(
        startDate=start,
        endDate=start + timedelta(minutes=15),
        creationDate=T1,
        sourceName=device,
        value=str(value),
        unit="kcal",
        type=KCAL,
    )


def _ingest(customer, records, acd, source=DataSource.APPLE_HEALTH):
    ingest_records(customer, records, source=source, admin_create_date=acd)


def _stored(customer, record: RecordInput, acd, source=DataSource.APPLE_HEALTH):
    return StoredRecord(
        customer_id=customer.pk,
        startDate=record.startDate,
        endDate=record.endDate,
        creationDate=record.creationDate,
        sourceName=record.sourceName,
        source=str(source),
        value=record.value,
        type=record.type,
        admin_create_date=acd,
        unit=record.unit,
    )


@pytest.fixture(params=["compact", "legacy"])
def read_mode(request, settings):
    settings.HEALTHDATAMODEL_READ_COMPACT = request.param == "compact"
    return request.param


@pytest.fixture
def alice():
    return User.objects.create_user(username="alice")


@pytest.fixture
def bob():
    return User.objects.create_user(username="bob")


def _sleep(customer) -> float | None:
    return get_sleep_hours_by_day(customer, DAY, DAY)[DAY]


def _activity(customer, read_mode) -> dict[int, float]:
    """``{slot index: value}`` of the customer's 15-min active energy on DAY."""
    if read_mode == "legacy" and not POSTGRES:
        pytest.skip("legacy activity read path requires PostgreSQL")
    rows = get_activity_records(
        customer, ActivityMetric.ACTIVE_CALORIES, MIDNIGHT, MIDNIGHT + timedelta(1)
    )
    return {
        int((start - MIDNIGHT).total_seconds() // 900): value
        for start, _end, value in rows
    }


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_records_removes_compact_rows(self, alice, bob, read_mode):
        for customer in (alice, bob):
            _ingest(customer, [_night(-1, 7), _slot(0, 10)], T1)
        assert delete_records(alice) == 2
        assert not SleepDay.objects.filter(customer=alice).exists()
        assert not ActivityDay.objects.filter(customer=alice).exists()
        assert _sleep(alice) is None
        assert _activity(alice, read_mode) == {}
        # other customer untouched
        assert SleepDay.objects.filter(customer=bob).count() == 1
        assert ActivityDay.objects.filter(customer=bob).count() == 1
        assert _sleep(bob) == 8.0

    def test_delete_customer_health_data_counts_compact_rows(self, alice):
        _ingest(alice, [_night(-1, 7), _night(-1, 7, device="oura"), _slot(0, 10)], T1)
        counts = delete_customer_health_data(alice, workouts=False, rankings=False)
        assert counts == {
            "records": 3,
            "sleep_days": 2,
            "activity_days": 1,
            "workouts": 0,
            "workout_metadata": 0,
            "rankings": 0,
        }
        assert not SleepDay.objects.filter(customer=alice).exists()
        assert not ActivityDay.objects.filter(customer=alice).exists()

    def test_records_false_keeps_compact_rows(self, alice):
        _ingest(alice, [_night(-1, 7), _slot(0, 10)], T1)
        counts = delete_customer_health_data(alice, records=False)
        assert counts["sleep_days"] == counts["activity_days"] == 0
        assert SleepDay.objects.filter(customer=alice).count() == 1
        assert ActivityDay.objects.filter(customer=alice).count() == 1

    def test_deletes_compact_rows_without_record_backing(self, alice):
        # e.g. history whose Record rows were archived already
        _ingest(alice, [_night(-1, 7), _slot(0, 10)], T1)
        Record.objects.filter(customer=alice).delete()
        assert delete_records(alice) == 0
        assert not SleepDay.objects.filter(customer=alice).exists()
        assert not ActivityDay.objects.filter(customer=alice).exists()

    @pytest.mark.parametrize("helper", ["delete_records", "delete_all"])
    def test_atomic_across_tables(self, alice, monkeypatch, helper):
        _ingest(alice, [_night(-1, 7), _slot(0, 10)], T1)

        def boom(*args, **kwargs):
            raise RuntimeError("activity delete failed")

        monkeypatch.setattr(
            "healthdatamodel.customer_data.ActivityDay.objects.filter", boom
        )
        with pytest.raises(RuntimeError):
            if helper == "delete_records":
                delete_records(alice)
            else:
                delete_customer_health_data(alice)
        assert Record.objects.filter(customer=alice).count() == 2
        assert SleepDay.objects.filter(customer=alice).count() == 1


# ---------------------------------------------------------------------------
# reassign
# ---------------------------------------------------------------------------


class TestReassignCompact:
    def test_non_conflicting_rows_move(self, alice, bob, read_mode):
        _ingest(alice, [_night(-1, 7), _slot(0, 10)], T1)
        counts = reassign_customer_health_data(alice, bob)
        assert counts == {"records": 2, "workouts": 0}
        assert not SleepDay.objects.filter(customer=alice).exists()
        assert not ActivityDay.objects.filter(customer=alice).exists()
        assert _sleep(bob) == 8.0
        assert _sleep(alice) is None
        assert _activity(bob, read_mode) == {0: 10.0}

    @pytest.mark.parametrize("source_newer", [True, False])
    def test_conflicting_sleep_day(self, alice, bob, read_mode, source_newer):
        # Same (device, day) on both sides: the newer upload owns the day,
        # exactly as if both uploads had been ingested for bob.
        alice_acd, bob_acd = (T2, T1) if source_newer else (T1, T2)
        _ingest(alice, [_night(-1, 7)], alice_acd)  # 8 h
        _ingest(bob, [_night(0, 6)], bob_acd)  # 6 h
        reassign_customer_health_data(alice, bob)
        assert SleepDay.objects.filter(customer=bob).count() == 1
        assert not SleepDay.objects.filter(customer=alice).exists()
        assert Record.objects.filter(customer=bob).count() == 2
        assert _sleep(bob) == (8.0 if source_newer else 6.0)
        row = SleepDay.objects.get(customer=bob)
        assert row.admin_create_date == T2

    def test_conflicting_activity_day(self, alice, bob, read_mode):
        # Same (source, device, metric, day, resolution) on both sides:
        # newer upload wins per slot, older fills the slots it alone has.
        _ingest(alice, [_slot(0, 10), _slot(1, 20)], T1)
        _ingest(bob, [_slot(1, 5), _slot(2, 7)], T2)
        reassign_customer_health_data(alice, bob)
        assert ActivityDay.objects.filter(customer=bob).count() == 1
        assert not ActivityDay.objects.filter(customer=alice).exists()
        row = ActivityDay.objects.get(customer=bob)
        assert row.values[:3] == [10.0, 5.0, 7.0]
        assert row.admin_create_date == T2
        assert _activity(bob, read_mode) == {0: 10.0, 1: 5.0, 2: 7.0}

    def test_conflicting_activity_source_newer(self, alice, bob, read_mode):
        _ingest(alice, [_slot(1, 5), _slot(2, 7)], T2)
        _ingest(bob, [_slot(0, 10), _slot(1, 20)], T1)
        reassign_customer_health_data(alice, bob)
        assert _activity(bob, read_mode) == {0: 10.0, 1: 5.0, 2: 7.0}

    def test_rerun_target_state_is_stable(self, alice, bob):
        # A second (empty) reassign must not change the rebuilt target rows.
        _ingest(alice, [_night(-1, 7), _slot(0, 10), _slot(1, 20)], T1)
        _ingest(bob, [_night(0, 6), _slot(1, 5)], T2)
        reassign_customer_health_data(alice, bob)
        before = {
            m.__name__: sorted(
                m.objects.filter(customer=bob).values_list(
                    "day", "admin_create_date", "device"
                )
            )
            for m in (SleepDay, ActivityDay)
        }
        values = list(ActivityDay.objects.filter(customer=bob).values_list("values"))
        reassign_customer_health_data(alice, bob)
        after = {
            m.__name__: sorted(
                m.objects.filter(customer=bob).values_list(
                    "day", "admin_create_date", "device"
                )
            )
            for m in (SleepDay, ActivityDay)
        }
        assert before == after
        assert (
            list(ActivityDay.objects.filter(customer=bob).values_list("values"))
            == values
        )

    def test_compact_history_without_record_backing_is_kept(self, alice, bob):
        # Alice's older day is only in the compact tables (its Record rows
        # were archived); bob has a conflicting newer upload for the same
        # activity day.  The merge keeps alice's slots that bob lacks.
        old_day = DAY - timedelta(days=40)
        old_night = RecordInput(
            startDate=_at(-1, old_day),
            endDate=_at(7, old_day),
            creationDate=T1,
            sourceName="apple",
            value=ASLEEP,
            type=SLEEP_TYPE,
        )
        _ingest(alice, [old_night, _slot(0, 10)], T1)
        Record.objects.filter(customer=alice).delete()  # archived
        _ingest(bob, [_slot(1, 5)], T2)
        reassign_customer_health_data(alice, bob)
        assert SleepDay.objects.filter(customer=bob, day=old_day).exists()
        assert ActivityDay.objects.get(customer=bob).values[:2] == [10.0, 5.0]

    def test_save_compact_false_moves_rows_without_rebuild(self, alice, bob, settings):
        _ingest(alice, [_slot(0, 10)], T1)
        _ingest(bob, [_slot(1, 5)], T2)
        settings.HEALTHDATAMODEL_SAVE_COMPACT = False
        reassign_customer_health_data(alice, bob)
        assert not ActivityDay.objects.filter(customer=alice).exists()
        assert ActivityDay.objects.get(customer=bob).values[:2] == [10.0, 5.0]

    def test_requires_save_records(self, alice, bob, settings):
        _ingest(alice, [_slot(0, 10)], T1)
        settings.HEALTHDATAMODEL_SAVE_RECORDS = False
        with pytest.raises(ImproperlyConfigured, match="SAVE_RECORDS"):
            reassign_customer_health_data(alice, bob)
        # nothing moved
        assert Record.objects.filter(customer=alice).count() == 1
        assert ActivityDay.objects.filter(customer=alice).count() == 1
        # workouts-only reassign does not touch Record and is allowed
        assert reassign_customer_health_data(alice, bob, records=False) == {
            "records": 0,
            "workouts": 0,
        }

    def test_same_customer_rejected(self, alice):
        _ingest(alice, [_slot(0, 10)], T1)
        with pytest.raises(ValueError, match="same customer"):
            reassign_customer_health_data(alice, alice)
        assert ActivityDay.objects.filter(customer=alice).count() == 1

    def test_atomic(self, alice, bob, monkeypatch):
        _ingest(alice, [_night(-1, 7), _slot(0, 10)], T1)
        _ingest(bob, [_slot(0, 99)], T2)

        def boom(*args, **kwargs):
            raise RuntimeError("replay failed")

        monkeypatch.setattr("healthdatamodel.compat.backfill_from_records", boom)
        with pytest.raises(RuntimeError):
            reassign_customer_health_data(alice, bob)
        assert Record.objects.filter(customer=alice).count() == 2
        assert SleepDay.objects.filter(customer=alice).count() == 1
        assert ActivityDay.objects.get(customer=alice).values[0] == 10.0
        assert ActivityDay.objects.get(customer=bob).values[0] == 99.0


# ---------------------------------------------------------------------------
# restore_records
# ---------------------------------------------------------------------------


class TestRestoreCompact:
    def test_restore_into_empty_tables(self, alice, read_mode):
        rows = [
            _stored(alice, _night(-1, 7), T1),
            _stored(alice, _slot(0, 10), T1),
            _stored(alice, _slot(1, 20), T1),
        ]
        assert restore_records(rows) == 3
        assert SleepDay.objects.filter(customer=alice).count() == 1
        assert ActivityDay.objects.filter(customer=alice).count() == 1
        assert _sleep(alice) == 8.0
        assert _activity(alice, read_mode) == {0: 10.0, 1: 20.0}

    def test_restore_older_than_live_never_overwrites(self, alice, read_mode):
        _ingest(alice, [_night(0, 6), _slot(1, 5)], T2)  # live, newer
        restore_records(
            [
                _stored(alice, _night(-1, 7), T1),
                _stored(alice, _slot(0, 10), T1),
                _stored(alice, _slot(1, 99), T1),
            ]
        )
        # sleep day kept from the newer upload; activity: older fills only
        # the empty slot.  Legacy reads (newest upload wins) agree.
        assert _sleep(alice) == 6.0
        assert _activity(alice, read_mode) == {0: 10.0, 1: 5.0}
        assert SleepDay.objects.get(customer=alice).admin_create_date == T2

    def test_restore_newer_than_stored_replaces(self, alice, read_mode):
        _ingest(alice, [_night(0, 6), _slot(1, 5)], T1)
        restore_records(
            [_stored(alice, _night(-1, 7), T2), _stored(alice, _slot(1, 99), T2)]
        )
        assert _sleep(alice) == 8.0
        assert _activity(alice, read_mode) == {1: 99.0}

    def test_same_timestamp_rerun_is_idempotent(self, alice):
        rows = [
            _stored(alice, _night(-1, 3), T1),
            _stored(alice, _night(3, 7), T1),
            _stored(alice, _slot(0, 10), T1),
            _stored(alice, _slot(1, 20), T1),
        ]
        restore_records(rows)
        snapshot = (
            list(SleepDay.objects.values_list("intervals", "admin_create_date")),
            list(ActivityDay.objects.values_list("values", "admin_create_date")),
        )
        restore_records(rows)
        assert (
            list(SleepDay.objects.values_list("intervals", "admin_create_date")),
            list(ActivityDay.objects.values_list("values", "admin_create_date")),
        ) == snapshot
        # Record has no unique key: a repeated restore stores rows twice.
        assert Record.objects.filter(customer=alice).count() == 8

    def test_same_timestamp_as_live_write_overwrites_not_sums(self, alice):
        _ingest(alice, [_slot(0, 10), _slot(1, 20)], T1)
        restore_records([_stored(alice, _slot(0, 10), T1)])
        assert ActivityDay.objects.get(customer=alice).values[:2] == [10.0, 20.0]

    def test_upload_split_across_batches(self, alice, read_mode):
        rows = [_stored(alice, _slot(i, 10 + i), T1) for i in range(5)]
        rows.append(_stored(alice, _night(-1, 7), T1))
        restore_records(iter(rows), batch_size=2)
        assert _sleep(alice) == 8.0
        assert _activity(alice, read_mode) == {i: 10.0 + i for i in range(5)}

    def test_unsorted_input_replayed_oldest_first(self, alice):
        restore_records(
            [
                _stored(alice, _slot(0, 30), T3),
                _stored(alice, _slot(0, 10), T1),
                _stored(alice, _slot(0, 20), T2),
            ]
        )
        row = ActivityDay.objects.get(customer=alice)
        assert row.values[0] == 30.0
        assert row.admin_create_date == T3

    def test_multiple_customers_and_sources(self, alice, bob):
        restore_records(
            [
                _stored(alice, _slot(0, 10), T1),
                _stored(bob, _slot(0, 20), T1, source=DataSource.FITBIT),
            ]
        )
        assert ActivityDay.objects.get(customer=alice).source == "apple_health"
        assert ActivityDay.objects.get(customer=bob).source == "fitbit"

    def test_uncompactable_rows_only_in_record(self, alice):
        hr = RecordInput(
            startDate=MIDNIGHT,
            endDate=MIDNIGHT,
            creationDate=T1,
            sourceName="apple",
            value="60",
            unit="count/min",
            type="HKQuantityTypeIdentifierHeartRate",
        )
        assert restore_records([_stored(alice, hr, T1)]) == 1
        assert Record.objects.filter(customer=alice).count() == 1
        assert not SleepDay.objects.exists() and not ActivityDay.objects.exists()

    def test_respects_save_compact(self, alice, settings):
        settings.HEALTHDATAMODEL_SAVE_COMPACT = False
        restore_records([_stored(alice, _slot(0, 10), T1)])
        assert Record.objects.filter(customer=alice).count() == 1
        assert not ActivityDay.objects.exists()

    def test_atomic(self, alice):
        bad = {"customer_id": alice.pk, "startDate": MIDNIGHT}  # missing fields
        rows = [_stored(alice, _slot(0, 10), T1), _stored(alice, _slot(1, 10), T1)]
        with pytest.raises(ValueError):
            restore_records([*rows, bad], batch_size=2)
        assert not Record.objects.exists()
        assert not ActivityDay.objects.exists()

    def test_mapping_rows_with_string_datetimes(self, alice):
        restore_records(
            [
                {
                    "customer_id": alice.pk,
                    "startDate": MIDNIGHT.isoformat(),
                    "endDate": (MIDNIGHT + timedelta(minutes=15)).isoformat(),
                    "creationDate": T1.isoformat(),
                    "sourceName": "apple",
                    "source": "apple_health",
                    "value": 10,
                    "unit": "kcal",
                    "type": KCAL,
                    "admin_create_date": T1.isoformat(),
                }
            ]
        )
        row = ActivityDay.objects.get(customer=alice)
        assert row.values[0] == 10.0
        assert row.admin_create_date == T1


# ---------------------------------------------------------------------------
# testing fixtures + read helpers
# ---------------------------------------------------------------------------


class TestFixturesAndReaders:
    def test_create_record_visible_in_both_read_modes(self, alice, read_mode):
        create_record(
            alice,
            startDate=_at(-1),
            endDate=_at(7),
            type=SLEEP_TYPE,
            value=ASLEEP,
            sourceName="apple",
            admin_create_date=T1,
        )
        create_records(
            alice,
            [{"startDate": MIDNIGHT, "type": KCAL, "value": 12, "unit": "kcal"}],
            admin_create_date=T1,
        )
        assert SleepDay.objects.filter(customer=alice).count() == 1
        assert ActivityDay.objects.filter(customer=alice).count() == 1
        assert _sleep(alice) == 8.0
        assert _activity(alice, read_mode) == {0: 12.0}

    def test_has_records_reads_record(self, alice):
        _ingest(alice, [_slot(0, 10)], T1)
        assert has_records(alice)
        Record.objects.filter(customer=alice).delete()
        # documented: Record-only, regardless of compact rows
        assert ActivityDay.objects.filter(customer=alice).exists()
        assert not has_records(alice)
