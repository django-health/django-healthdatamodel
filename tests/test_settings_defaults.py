"""The 1.0 defaults: a consumer who sets nothing gets DDL only, dual writes,
legacy reads (see docs/UPGRADING-1.0.md)."""

from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel import compact
from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_records
from healthdatamodel.models import Record, SleepDay
from healthdatamodel.query import get_sleep_hours_by_day
from healthdatamodel.schemas import RecordInput

User = get_user_model()

TODAY = date(2025, 6, 10)
NOW = datetime(2025, 6, 10, 8, 0, tzinfo=timezone.utc)
SETTING_NAMES = (
    "HEALTHDATAMODEL_SAVE_RECORDS",
    "HEALTHDATAMODEL_SAVE_COMPACT",
    "HEALTHDATAMODEL_READ_COMPACT",
    "HEALTHDATAMODEL_BACKFILL_IN_MIGRATION",
)

backfill_migration = importlib.import_module(
    "healthdatamodel.migrations.0012_backfill_compact"
)


@pytest.fixture
def unset(settings):
    """Remove every compact-storage setting so the package defaults apply."""
    for name in SETTING_NAMES:
        if hasattr(settings, name):
            delattr(settings, name)


def _ingest_night(customer):
    ingest_records(
        customer,
        [
            RecordInput(
                startDate=datetime.combine(TODAY - timedelta(days=1), time(23)).replace(
                    tzinfo=timezone.utc
                ),
                endDate=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                creationDate=NOW,
                sourceName="apple",
                value="HKCategoryValueSleepAnalysisAsleepUnspecified",
                type="HKCategoryTypeIdentifierSleepAnalysis",
            )
        ],
        source=DataSource.APPLE_HEALTH,
        admin_create_date=NOW,
    )


def test_effective_defaults(unset):
    assert compact.effective_settings() == {
        "HEALTHDATAMODEL_SAVE_RECORDS": True,
        "HEALTHDATAMODEL_SAVE_COMPACT": True,
        "HEALTHDATAMODEL_READ_COMPACT": False,
        "HEALTHDATAMODEL_BACKFILL_IN_MIGRATION": False,
    }


def test_settings_override_defaults(settings):
    settings.HEALTHDATAMODEL_READ_COMPACT = True
    settings.HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = True
    assert compact.read_compact_enabled() is True
    assert compact.backfill_in_migration_enabled() is True


@pytest.mark.django_db
def test_default_dual_writes_and_reads_legacy(unset):
    customer = User.objects.create_user(username="defaults-user")
    _ingest_night(customer)
    assert Record.objects.count() == 1
    assert SleepDay.objects.count() == 1
    # Prove the read went to Record: drop the compact row, the answer stays.
    SleepDay.objects.all().delete()
    assert get_sleep_hours_by_day(customer, TODAY, TODAY)[TODAY] == 8.0


@pytest.mark.django_db
def test_backfill_migration_is_noop_by_default(unset, settings):
    customer = User.objects.create_user(username="defaults-migration-user")
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _ingest_night(customer)
    assert SleepDay.objects.count() == 0

    backfill_migration.forwards(None, None)
    assert SleepDay.objects.count() == 0


@pytest.mark.django_db
def test_backfill_migration_runs_when_opted_in(settings):
    customer = User.objects.create_user(username="optin-migration-user")
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _ingest_night(customer)

    settings.HEALTHDATAMODEL_SAVE_COMPACT = True
    settings.HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = True
    backfill_migration.forwards(None, None)
    assert SleepDay.objects.count() == 1
