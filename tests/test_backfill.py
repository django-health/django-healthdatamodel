"""
Tests for healthdatamodel.compat — backfilling the compact tables from Record.

Strategy: ingest with the compact write disabled (legacy-only state, as on an
install upgrading from 0.x), run the backfill, and assert the compact state
matches what live dual-writing would have produced.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from healthdatamodel.compat import backfill_from_records
from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_records
from healthdatamodel.models import ActivityDay, Record, SleepDay
from healthdatamodel.query import ActivityMetric, get_sleep_hours_by_day
from healthdatamodel.schemas import RecordInput

User = get_user_model()

pytestmark = pytest.mark.django_db

TODAY = date(2025, 6, 10)
YESTERDAY = TODAY - timedelta(days=1)
NOW = datetime(2025, 6, 10, 8, 0, tzinfo=timezone.utc)
MIDNIGHT = datetime.combine(TODAY, time(0)).replace(tzinfo=timezone.utc)

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP = "HKCategoryValueSleepAnalysisAsleepUnspecified"


@pytest.fixture
def customer():
    return User.objects.create_user(username="backfill-user")


@pytest.fixture
def legacy_only(settings):
    """Simulate a pre-1.0 install: ingest writes only to Record."""
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    yield
    settings.HEALTHDATAMODEL_SAVE_COMPACT = True


def _ingest_night(customer, start, end, admin_create_date, sourceName="apple"):
    ingest_records(
        customer,
        [
            RecordInput(
                startDate=start,
                endDate=end,
                creationDate=NOW,
                sourceName=sourceName,
                value=ASLEEP,
                type=SLEEP_TYPE,
            )
        ],
        source=DataSource.APPLE_HEALTH,
        admin_create_date=admin_create_date,
    )


def _ingest_slot(
    customer, index, value, admin_create_date, source=DataSource.APPLE_HEALTH
):
    start = MIDNIGHT + timedelta(minutes=index * 15)
    ingest_records(
        customer,
        [
            RecordInput(
                startDate=start,
                endDate=start + timedelta(minutes=15),
                creationDate=NOW,
                sourceName="apple",
                value=str(value),
                unit="kcal",
                type=ActivityMetric.ACTIVE_CALORIES.value,
            )
        ],
        source=source,
        admin_create_date=admin_create_date,
    )


class TestBackfill:
    def test_seeds_compact_tables(self, customer, legacy_only):
        _ingest_night(
            customer,
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            admin_create_date=NOW,
        )
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW)
        assert SleepDay.objects.count() == 0 and ActivityDay.objects.count() == 0

        seen, uploads = backfill_from_records()
        assert seen == 2
        assert SleepDay.objects.count() == 1
        assert ActivityDay.objects.count() == 1

    def test_replay_order_newest_upload_wins(self, customer, legacy_only):
        # Older upload has the longer night; newer upload replaces it.
        _ingest_night(
            customer,
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            admin_create_date=NOW - timedelta(hours=2),
        )
        _ingest_night(
            customer,
            datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            admin_create_date=NOW,
        )
        backfill_from_records()
        assert get_sleep_hours_by_day(customer, TODAY, TODAY)[TODAY] == pytest.approx(
            4.5
        )

    def test_matches_live_dual_write_state(self, customer, settings):
        # Same upload sequence, once via backfill and once via live dual-write,
        # must produce identical compact rows.
        def run_uploads():
            _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(hours=2))
            _ingest_slot(customer, 1, 20.0, admin_create_date=NOW - timedelta(hours=1))
            _ingest_slot(customer, 0, 8.0, admin_create_date=NOW)

        settings.HEALTHDATAMODEL_SAVE_COMPACT = True
        run_uploads()
        live = list(
            ActivityDay.objects.values(
                "source", "device", "metric", "day", "resolution_minutes", "values"
            )
        )
        ActivityDay.objects.all().delete()
        Record.objects.all().delete()

        settings.HEALTHDATAMODEL_SAVE_COMPACT = False
        run_uploads()
        settings.HEALTHDATAMODEL_SAVE_COMPACT = True
        backfill_from_records()
        backfilled = list(
            ActivityDay.objects.values(
                "source", "device", "metric", "day", "resolution_minutes", "values"
            )
        )
        assert backfilled == live

    def test_idempotent(self, customer, legacy_only):
        _ingest_night(
            customer,
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            admin_create_date=NOW,
        )
        backfill_from_records()
        first = list(SleepDay.objects.values("device", "day", "intervals"))
        backfill_from_records()
        assert list(SleepDay.objects.values("device", "day", "intervals")) == first
        assert SleepDay.objects.count() == 1

    def test_customers_filter(self, customer, legacy_only):
        other = User.objects.create_user(username="backfill-other")
        for c in (customer, other):
            _ingest_slot(c, 0, 10.0, admin_create_date=NOW)
        backfill_from_records(customers=[customer.pk])
        assert ActivityDay.objects.filter(customer=customer).exists()
        assert not ActivityDay.objects.filter(customer=other).exists()

    def test_since_filter(self, customer, legacy_only):
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(days=10))
        _ingest_slot(customer, 1, 20.0, admin_create_date=NOW)
        backfill_from_records(since=NOW - timedelta(days=1))
        row = ActivityDay.objects.get(customer=customer)
        assert row.values[0] is None
        assert row.values[1] == pytest.approx(20.0)

    def test_management_command(self, customer, legacy_only):
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW)
        call_command("backfill_compact")
        assert ActivityDay.objects.count() == 1
