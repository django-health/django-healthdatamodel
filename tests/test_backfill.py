"""
Tests for healthdatamodel.compat — backfilling the compact tables from Record.

Strategy: ingest with the compact write disabled (legacy-only state, as on an
install upgrading from 0.x), run the backfill, and assert the compact state
matches what live dual-writing would have produced.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from healthdatamodel.compat import backfill_from_records, replay_records
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


@pytest.fixture
def compact_reads(settings):
    """Read through the compact tables (the 1.0 default is the legacy path)."""
    settings.HEALTHDATAMODEL_READ_COMPACT = True


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

        result = backfill_from_records()
        assert result.records == 2
        assert result.uploads == 1  # same (customer, timestamp, source)
        assert result.customers == 1
        assert SleepDay.objects.count() == 1
        assert ActivityDay.objects.count() == 1

    def test_android_sleep_values_backfilled(
        self, customer, legacy_only, compact_reads
    ):
        ingest_records(
            customer,
            [
                RecordInput(
                    startDate=start,
                    endDate=end,
                    creationDate=NOW,
                    sourceName="android",
                    value=value,
                    type=SLEEP_TYPE,
                )
                for start, end, value in (
                    (
                        datetime.combine(YESTERDAY, time(23)).replace(
                            tzinfo=timezone.utc
                        ),
                        datetime.combine(TODAY, time(3)).replace(tzinfo=timezone.utc),
                        "HKCategoryValueSleepAnalysisAsleep",
                    ),
                    (
                        datetime.combine(TODAY, time(3)).replace(tzinfo=timezone.utc),
                        datetime.combine(TODAY, time(4)).replace(tzinfo=timezone.utc),
                        "HKCategoryValueSleepAnalysisOutOfBed",
                    ),
                )
            ],
            source=DataSource.HEALTH_CONNECT,
            admin_create_date=NOW,
        )
        backfill_from_records()
        row = SleepDay.objects.get(customer=customer)
        assert len(row.intervals) == 2
        assert get_sleep_hours_by_day(customer, TODAY, TODAY)[TODAY] == 4.0

    def test_replay_order_newest_upload_wins(
        self, customer, legacy_only, compact_reads
    ):
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


def _row_values(customer):
    return ActivityDay.objects.get(customer=customer).values


class TestReplayIdempotency:
    """Replaying an upload the compact tables already hold must not change them."""

    def test_activity_rerun_after_dual_write_does_not_double(self, customer):
        # Dual-written live (the 1.0 default), then backfilled twice: the
        # replayed upload has the same admin_create_date as the stored row and
        # must overwrite, not sum.
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW)
        backfill_from_records()
        backfill_from_records()
        assert _row_values(customer)[0] == pytest.approx(10.0)

    def test_activity_rerun_legacy_only_is_stable(self, customer, legacy_only):
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(hours=1))
        _ingest_slot(customer, 1, 20.0, admin_create_date=NOW)
        backfill_from_records()
        first = list(_row_values(customer))
        backfill_from_records()
        backfill_from_records()
        assert _row_values(customer) == first
        assert first[:2] == [pytest.approx(10.0), pytest.approx(20.0)]

    def test_split_upload_matches_live_sum(self, customer):
        # One logical upload delivered in two ingest calls (same timestamp)
        # sums live; the replay group holds both rows and must reproduce it.
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW)
        _ingest_slot(customer, 0, 5.0, admin_create_date=NOW)
        assert _row_values(customer)[0] == pytest.approx(15.0)
        backfill_from_records()
        backfill_from_records()
        assert _row_values(customer)[0] == pytest.approx(15.0)

    def test_sleep_rerun_after_dual_write_is_stable(self, customer, compact_reads):
        _ingest_night(
            customer,
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            admin_create_date=NOW,
        )
        backfill_from_records()
        backfill_from_records()
        assert get_sleep_hours_by_day(customer, TODAY, TODAY)[TODAY] == 8.0

    def test_sleep_same_timestamp_two_sources_unioned(self, customer, legacy_only):
        # Two pipelines wrote the same device's night at the same timestamp.
        # Live ingest merges same-timestamp writes and the legacy read counts
        # every record at the winning timestamp, so the replay (one group per
        # source) must union them, and re-runs must stay stable.
        night_start = datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc)
        for source, start, end in (
            (DataSource.APPLE_HEALTH, night_start, night_start + timedelta(hours=4)),
            (
                DataSource.FITBIT,
                night_start + timedelta(hours=4),
                night_start + timedelta(hours=8),
            ),
        ):
            ingest_records(
                customer,
                [
                    RecordInput(
                        startDate=start,
                        endDate=end,
                        creationDate=NOW,
                        sourceName="apple",
                        value=ASLEEP,
                        type=SLEEP_TYPE,
                    )
                ],
                source=source,
                admin_create_date=NOW,
            )
        from django.test.utils import override_settings

        with override_settings(HEALTHDATAMODEL_READ_COMPACT=False):
            legacy = get_sleep_hours_by_day(customer, TODAY, TODAY)[TODAY]
        assert legacy == 8.0
        backfill_from_records()
        first = SleepDay.objects.get(customer=customer).intervals
        backfill_from_records()
        assert SleepDay.objects.get(customer=customer).intervals == first
        with override_settings(HEALTHDATAMODEL_READ_COMPACT=True):
            assert get_sleep_hours_by_day(customer, TODAY, TODAY)[TODAY] == legacy


class TestReplayNeverRegresses:
    """A replayed upload older than the stored row (e.g. a live upload landed
    after the backfill read its snapshot) must not overwrite newer data."""

    def test_sleep_older_replay_keeps_newer_day(self, customer, legacy_only):
        _ingest_night(
            customer,
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
            admin_create_date=NOW - timedelta(hours=2),
        )
        old_records = list(Record.objects.order_by("pk"))
        # Newer upload written live (compact only for this simulation).
        from django.test.utils import override_settings

        with override_settings(
            HEALTHDATAMODEL_SAVE_COMPACT=True, HEALTHDATAMODEL_SAVE_RECORDS=False
        ):
            _ingest_night(
                customer,
                datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
                datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                admin_create_date=NOW,
            )
        before = SleepDay.objects.get(customer=customer)
        replay_records(old_records)
        after = SleepDay.objects.get(customer=customer)
        assert after.intervals == before.intervals
        assert after.admin_create_date == NOW

    def test_activity_older_replay_only_fills_empty_slots(self, customer, settings):
        settings.HEALTHDATAMODEL_SAVE_COMPACT = False
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(hours=2))
        _ingest_slot(customer, 1, 30.0, admin_create_date=NOW - timedelta(hours=2))
        old_records = list(Record.objects.order_by("pk"))
        settings.HEALTHDATAMODEL_SAVE_COMPACT = True
        _ingest_slot(customer, 0, 8.0, admin_create_date=NOW)

        replay_records(old_records)
        row = ActivityDay.objects.get(customer=customer)
        assert row.values[0] == pytest.approx(8.0)  # newer slot kept
        assert row.values[1] == pytest.approx(30.0)  # empty slot filled
        assert row.admin_create_date == NOW


class TestBackfillBatching:
    @pytest.fixture
    def customers(self, legacy_only):
        users = [User.objects.create_user(username=f"batch-{i}") for i in range(5)]
        for i, user in enumerate(users):
            _ingest_slot(user, i, 10.0 + i, admin_create_date=NOW)
        return users

    def test_customer_range(self, customers):
        lo, hi = customers[1].pk, customers[3].pk
        result = backfill_from_records(customer_range=(lo, hi))
        covered = set(ActivityDay.objects.values_list("customer_id", flat=True))
        assert covered == {c.pk for c in customers[1:4]}
        assert result.customers == 3
        assert result.last_customer == hi

    def test_small_batches_cover_everyone(self, customers):
        batches = []
        result = backfill_from_records(
            customer_batch_size=2, on_batch=lambda p: batches.append(p.last_customer)
        )
        assert ActivityDay.objects.count() == len(customers)
        assert batches == [customers[i].pk for i in (1, 3, 4)]
        assert result.uploads == len(customers)

    def test_progress_callback(self, customers):
        seen = []
        backfill_from_records(
            progress_every=2, on_progress=lambda p: seen.append(p.uploads)
        )
        assert seen == [2, 4]

    def test_uncompactable_types_not_replayed(self, customer, legacy_only):
        ingest_records(
            customer,
            [
                RecordInput(
                    startDate=MIDNIGHT,
                    endDate=MIDNIGHT + timedelta(minutes=1),
                    creationDate=NOW,
                    sourceName="apple",
                    value="62",
                    unit="count/min",
                    type="HKQuantityTypeIdentifierHeartRate",
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        result = backfill_from_records()
        assert result.records == 0 and result.uploads == 0


@pytest.mark.django_db(transaction=True)
def test_backfill_commits_per_upload(monkeypatch, settings):
    """No giant transaction: uploads written before a failure stay committed."""
    from healthdatamodel import compat

    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    customer = User.objects.create_user(username="commit-user")
    _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(hours=1))
    _ingest_slot(customer, 1, 20.0, admin_create_date=NOW)
    settings.HEALTHDATAMODEL_SAVE_COMPACT = True

    real = compat.apply_compact
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(compat, "apply_compact", flaky)
    with pytest.raises(RuntimeError):
        backfill_from_records()
    row = ActivityDay.objects.get(customer=customer)
    assert row.values[0] == pytest.approx(10.0)
    assert row.values[1] is None


class TestBackfillCommand:
    def test_summary_and_progress(self, customer, legacy_only):
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(hours=1))
        _ingest_slot(customer, 1, 20.0, admin_create_date=NOW)
        out = StringIO()
        call_command("backfill_compact", "--progress-every", "1", stdout=out)
        text = out.getvalue()
        assert "progress: 1 uploads" in text
        assert "progress: 2 uploads" in text
        assert f"batch done: 1 customers through pk {customer.pk}" in text
        assert "Backfill complete: 1 customers scanned, 2 uploads / 2 records" in text

    def test_customer_range_and_resume_hint(self, customer, legacy_only):
        other = User.objects.create_user(username="cmd-other")
        for c in (customer, other):
            _ingest_slot(c, 0, 10.0, admin_create_date=NOW)
        out = StringIO()
        call_command(
            "backfill_compact",
            "--customer-range",
            str(customer.pk),
            str(customer.pk),
            stdout=out,
        )
        assert set(ActivityDay.objects.values_list("customer_id", flat=True)) == {
            customer.pk
        }
        assert (
            f"resume with --customer-range {customer.pk + 1} {customer.pk}"
            in out.getvalue()
        )

    def test_customers_option(self, customer, legacy_only):
        other = User.objects.create_user(username="cmd-other2")
        for c in (customer, other):
            _ingest_slot(c, 0, 10.0, admin_create_date=NOW)
        call_command(
            "backfill_compact", "--customers", str(other.pk), stdout=StringIO()
        )
        assert set(ActivityDay.objects.values_list("customer_id", flat=True)) == {
            other.pk
        }

    def test_since_option(self, customer, legacy_only):
        _ingest_slot(customer, 0, 10.0, admin_create_date=NOW - timedelta(days=10))
        _ingest_slot(customer, 1, 20.0, admin_create_date=NOW)
        call_command("backfill_compact", "--since", "2025-06-09", stdout=StringIO())
        assert _row_values(customer)[:2] == [None, pytest.approx(20.0)]

    def test_bad_since_rejected(self):
        with pytest.raises(CommandError):
            call_command("backfill_compact", "--since", "not-a-date")

    def test_customers_and_range_are_exclusive(self):
        with pytest.raises(CommandError):
            call_command(
                "backfill_compact", "--customers", "1", "--customer-range", "1", "2"
            )
