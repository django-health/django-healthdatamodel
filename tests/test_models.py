from datetime import datetime, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.models import (
    DataSourceRanking,
    Record,
    Workout,
    WorkoutMetadataEntry,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="test-user-1")


NOW = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)


class TestDataSourceRanking:
    def test_create(self, user):
        ranking = DataSourceRanking.objects.create(
            customer=user,
            dataSource=DataSource.APPLE_HEALTH,
            rank=1,
        )
        assert ranking.pk is not None
        assert ranking.dataSource == "apple_health"

    def test_str(self, user):
        ranking = DataSourceRanking.objects.create(
            customer=user,
            dataSource=DataSource.FITBIT,
            rank=2,
        )
        text = str(ranking)
        assert "fitbit" in text
        assert "2" in text


class TestWorkout:
    def test_create(self, user):
        workout = Workout.objects.create(
            customer=user,
            startDate=NOW,
            endDate=NOW,
            creationDate=NOW,
            sourceName="HealthKit",
            source="Apple HealthKit",
            durationUnit="min",
            duration=30,
            workoutActivityType="HKWorkoutActivityTypeRunning",
        )
        assert workout.pk is not None
        assert workout.duration == 30

    def test_metadata_entry(self, user):
        workout = Workout.objects.create(
            customer=user,
            startDate=NOW,
            endDate=NOW,
            creationDate=NOW,
            sourceName="HealthKit",
            source="Apple HealthKit",
            durationUnit="min",
            duration=45,
            workoutActivityType="HKWorkoutActivityTypeCycling",
        )
        entry = WorkoutMetadataEntry.objects.create(
            workout=workout,
            key="HKIndoorWorkout",
            value="1",
        )
        assert entry.pk is not None
        assert entry.key == "HKIndoorWorkout"


class TestRecord:
    def test_create(self, user):
        record = Record.objects.create(
            customer=user,
            startDate=NOW,
            endDate=NOW,
            creationDate=NOW,
            sourceName="HealthKit",
            source="Apple HealthKit",
            value="150",
            unit="kcal",
            type="ActiveCalories",
            admin_create_date=NOW,
        )
        assert record.pk is not None
        assert record.type == "ActiveCalories"

    def test_str(self, user):
        record = Record.objects.create(
            customer=user,
            startDate=NOW,
            endDate=NOW,
            creationDate=NOW,
            sourceName="HealthKit",
            source="Apple HealthKit",
            value="42",
            unit="count",
            type="StepCount",
            admin_create_date=NOW,
        )
        text = str(record)
        assert "StepCount" in text
        assert "42" in text
