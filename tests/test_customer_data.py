"""Tests for healthdatamodel.customer_data (SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.customer_data import (
    DataSourceSummary,
    delete_customer_health_data,
    delete_records,
    get_data_source_summary,
    has_records,
    reassign_customer_health_data,
)
from healthdatamodel.ingest import ingest_workouts
from healthdatamodel.models import (
    DataSourceRanking,
    Record,
    Workout,
    WorkoutMetadataEntry,
)
from healthdatamodel.query import ensure_ranks
from healthdatamodel.schemas import MetadataEntry, WorkoutInput
from healthdatamodel.testing import create_records, set_customer_device

User = get_user_model()

pytestmark = pytest.mark.django_db

T0 = datetime(2025, 6, 2, tzinfo=timezone.utc)


@pytest.fixture
def alice():
    return User.objects.create_user(username="alice")


@pytest.fixture
def bob():
    return User.objects.create_user(username="bob")


def _workout(start: datetime, n_meta: int = 0) -> WorkoutInput:
    return WorkoutInput(
        startDate=start,
        endDate=start + timedelta(minutes=30),
        creationDate=start,
        sourceName="watch",
        durationUnit="min",
        duration=30,
        workoutActivityType="RUNNING",
        metadataEntry=[MetadataEntry(key=f"k{i}", value="v") for i in range(n_meta)],
    )


def _seed(customer, n_records: int = 3, n_workouts: int = 2, n_meta: int = 2):
    create_records(
        customer,
        [{"startDate": T0 + timedelta(minutes=15 * i)} for i in range(n_records)],
    )
    ingest_workouts(
        customer,
        [_workout(T0 + timedelta(hours=i), n_meta) for i in range(n_workouts)],
        source=DataSource.APPLE_HEALTH,
    )
    ensure_ranks(customer)


class TestHasRecords:
    def test_false_then_true(self, alice):
        assert has_records(alice) is False
        create_records(alice, [{}])
        assert has_records(alice) is True

    def test_scoped_to_customer(self, alice, bob):
        create_records(alice, [{}])
        assert has_records(bob) is False


class TestDeleteRecords:
    def test_deletes_only_records_of_customer(self, alice, bob):
        _seed(alice)
        _seed(bob)
        assert delete_records(alice) == 3
        assert not Record.objects.filter(customer=alice).exists()
        # workouts and ranks untouched; other customer untouched
        assert Workout.objects.filter(customer=alice).count() == 2
        assert DataSourceRanking.objects.filter(customer=alice).exists()
        assert Record.objects.filter(customer=bob).count() == 3

    def test_zero_when_nothing(self, alice):
        assert delete_records(alice) == 0


class TestDeleteCustomerHealthData:
    def test_counts_per_model(self, alice, bob):
        _seed(alice, n_records=3, n_workouts=2, n_meta=2)
        _seed(bob)
        counts = delete_customer_health_data(alice)
        assert counts == {
            "records": 3,
            "workouts": 2,
            "workout_metadata": 4,
            "rankings": len(DataSource.values),
        }
        assert not Record.objects.filter(customer=alice).exists()
        assert not Workout.objects.filter(customer=alice).exists()
        assert not WorkoutMetadataEntry.objects.filter(workout__customer=alice).exists()
        assert not DataSourceRanking.objects.filter(customer=alice).exists()
        assert Record.objects.filter(customer=bob).count() == 3
        assert Workout.objects.filter(customer=bob).count() == 2

    def test_flags_skip_categories(self, alice):
        _seed(alice)
        counts = delete_customer_health_data(alice, records=False, rankings=False)
        assert counts["records"] == 0
        assert counts["rankings"] == 0
        assert counts["workouts"] == 2
        assert Record.objects.filter(customer=alice).count() == 3
        assert DataSourceRanking.objects.filter(customer=alice).exists()

    def test_empty_customer(self, alice):
        assert delete_customer_health_data(alice) == {
            "records": 0,
            "workouts": 0,
            "workout_metadata": 0,
            "rankings": 0,
        }

    def test_leaves_wearable_connections(self, alice):
        set_customer_device(alice, DataSource.FITBIT)
        _seed(alice)
        delete_customer_health_data(alice)
        assert alice.wearable_connections.count() == 1

    def test_atomic(self, alice, monkeypatch):
        _seed(alice)

        def boom(*a, **k):
            raise RuntimeError("ranking delete failed")

        monkeypatch.setattr(
            "healthdatamodel.customer_data.DataSourceRanking.objects.filter", boom
        )
        with pytest.raises(RuntimeError):
            delete_customer_health_data(alice)
        assert Record.objects.filter(customer=alice).count() == 3
        assert Workout.objects.filter(customer=alice).count() == 2


class TestReassign:
    def test_moves_records_and_workouts(self, alice, bob):
        _seed(alice, n_records=4, n_workouts=2, n_meta=1)
        assert reassign_customer_health_data(alice, bob) == {
            "records": 4,
            "workouts": 2,
        }
        assert not has_records(alice)
        assert Record.objects.filter(customer=bob).count() == 4
        assert Workout.objects.filter(customer=bob).count() == 2
        assert WorkoutMetadataEntry.objects.filter(workout__customer=bob).count() == 2
        # rankings stay put
        assert DataSourceRanking.objects.filter(customer=alice).exists()
        assert not DataSourceRanking.objects.filter(customer=bob).exists()

    def test_records_only(self, alice, bob):
        _seed(alice)
        counts = reassign_customer_health_data(alice, bob, workouts=False)
        assert counts == {"records": 3, "workouts": 0}
        assert Workout.objects.filter(customer=alice).count() == 2

    def test_keeps_target_data(self, alice, bob):
        _seed(alice, n_records=2)
        _seed(bob, n_records=1)
        reassign_customer_health_data(alice, bob)
        assert Record.objects.filter(customer=bob).count() == 3


class TestDataSourceSummary:
    def test_full_outer_join_semantics(self, alice):
        early = datetime(2025, 1, 1, tzinfo=timezone.utc)
        late = datetime(2025, 3, 1, tzinfo=timezone.utc)
        create_records(
            alice,
            [
                {"startDate": T0, "endDate": T0 + timedelta(hours=1)},
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=early,
        )
        create_records(
            alice,
            [{"startDate": T0 + timedelta(days=2), "endDate": T0 + timedelta(days=3)}],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=late,
        )
        # a source with records but no ranking row
        create_records(alice, [{}], source="legacy_gf", admin_create_date=early)
        DataSourceRanking.objects.create(
            customer=alice, dataSource=DataSource.APPLE_HEALTH, rank=1
        )
        # a ranked source with no records
        DataSourceRanking.objects.create(
            customer=alice, dataSource=DataSource.FITBIT, rank=2
        )
        summary = get_data_source_summary(alice)
        assert set(summary) == {"apple_health", "fitbit", "legacy_gf"}
        apple = summary["apple_health"]
        assert apple == DataSourceSummary(
            last_sync_date=late,
            connected_date=early,
            most_recent_data_date=T0 + timedelta(days=3),
            rank=1,
        )
        assert apple.has_data
        fitbit = summary["fitbit"]
        assert fitbit.rank == 2
        assert not fitbit.has_data
        assert fitbit.last_sync_date is None
        assert fitbit.most_recent_data_date is None
        assert summary["legacy_gf"].rank is None
        assert summary["legacy_gf"].has_data

    def test_blank_and_null_sources_omitted(self, alice):
        create_records(alice, [{}], source="")
        DataSourceRanking.objects.create(customer=alice, dataSource=None, rank=1)
        assert get_data_source_summary(alice) == {}

    def test_duplicate_ranking_rows_report_lowest(self, alice):
        DataSourceRanking.objects.create(customer=alice, dataSource="fitbit", rank=3)
        DataSourceRanking.objects.create(customer=alice, dataSource="fitbit", rank=2)
        assert get_data_source_summary(alice)["fitbit"].rank == 2

    def test_scoped_to_customer(self, alice, bob):
        create_records(bob, [{}], source=DataSource.FITBIT)
        assert get_data_source_summary(alice) == {}

    def test_after_ensure_ranks_every_source_present(self, alice):
        create_records(alice, [{}], source=DataSource.GARMIN)
        ensure_ranks(alice)
        summary = get_data_source_summary(alice)
        assert set(summary) == set(DataSource.values)
        assert summary["garmin"].has_data
        assert sum(s.has_data for s in summary.values()) == 1
