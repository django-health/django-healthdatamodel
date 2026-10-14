"""Tests for ``manage.py verify_compact`` (compact vs legacy parity gate)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from healthdatamodel.compat import backfill_from_records
from healthdatamodel.constants import DataSource
from healthdatamodel.ingest import ingest_records
from healthdatamodel.management.commands.verify_compact import recent_customer_ids
from healthdatamodel.models import ActivityDay, SleepDay
from healthdatamodel.query import ActivityMetric
from healthdatamodel.schemas import RecordInput

User = get_user_model()
pytestmark = pytest.mark.django_db

TODAY = date(2025, 6, 10)
NOW = datetime(2025, 6, 10, 8, 0, tzinfo=timezone.utc)
MIDNIGHT = datetime.combine(TODAY, time(0)).replace(tzinfo=timezone.utc)
POSTGRES = connection.vendor == "postgresql"
needs_postgres = pytest.mark.skipif(
    not POSTGRES, reason="legacy activity path requires PostgreSQL"
)


def _upload(customer, admin_create_date=NOW, kcal="10.0"):
    night = RecordInput(
        startDate=datetime.combine(TODAY - timedelta(days=1), time(23)).replace(
            tzinfo=timezone.utc
        ),
        endDate=datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
        creationDate=NOW,
        sourceName="apple",
        value="HKCategoryValueSleepAnalysisAsleepUnspecified",
        type="HKCategoryTypeIdentifierSleepAnalysis",
    )
    slot = RecordInput(
        startDate=MIDNIGHT,
        endDate=MIDNIGHT + timedelta(minutes=15),
        creationDate=NOW,
        sourceName="apple",
        value=kcal,
        unit="kcal",
        type=ActivityMetric.ACTIVE_CALORIES.value,
    )
    daily = RecordInput(
        startDate=MIDNIGHT,
        endDate=MIDNIGHT + timedelta(days=1),
        creationDate=NOW,
        sourceName="apple",
        value="5000",
        unit="count",
        type=ActivityMetric.STEPS.value,
    )
    ingest_records(
        customer,
        [night, slot, daily],
        source=DataSource.APPLE_HEALTH,
        admin_create_date=admin_create_date,
    )


def _row(out, check):
    """The summary-table cells (days, with data, mismatched) for *check*."""
    line = next(line for line in out.splitlines() if line.split()[:1] == [check])
    return line.split()[1:]


def _verify(*args):
    out = StringIO()
    call_command(
        "verify_compact", "--end", str(TODAY), "--days", "3", *args, stdout=out
    )
    return out.getvalue()


def _verify_fails(*args):
    out = StringIO()
    with pytest.raises(CommandError):
        call_command(
            "verify_compact", "--end", str(TODAY), "--days", "3", *args, stdout=out
        )
    return out.getvalue()


@pytest.fixture
def customer():
    return User.objects.create_user(username="verify-user")


@pytest.mark.parametrize("read_compact", [True, False])
def test_pass_on_dual_written_data(customer, settings, read_compact):
    # The configured read mode must not matter: both are forced explicitly.
    settings.HEALTHDATAMODEL_READ_COMPACT = read_compact
    _upload(customer)
    out = _verify()
    assert "verify_compact: 1 customer(s), 2025-06-08..2025-06-10 (3 days)" in out
    assert "PASS:" in out
    assert _row(out, "sleep") == ["3", "1", "0"]
    if POSTGRES:
        for check in ("ACTIVE_CALORIES@15min", "STEPS@1440min"):
            assert _row(out, check) == ["3", "1", "0"]


@pytest.mark.parametrize("read_compact", [True, False])
def test_fail_on_sleep_mismatch(customer, settings, read_compact):
    settings.HEALTHDATAMODEL_READ_COMPACT = read_compact
    _upload(customer)
    row = SleepDay.objects.get()
    row.intervals = row.intervals[:1]
    row.intervals[0][1] = (
        datetime.combine(TODAY, time(3)).replace(tzinfo=timezone.utc).isoformat()
    )
    row.save()
    out = _verify_fails()
    assert f"MISMATCH sleep customer={customer.pk} {TODAY}" in out
    assert "FAIL: 1 mismatched day(s)" in out


def test_fail_when_not_backfilled_then_pass(customer, settings):
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _upload(customer)
    settings.HEALTHDATAMODEL_SAVE_COMPACT = True
    out = _verify_fails()
    assert "FAIL:" in out
    backfill_from_records()
    assert "PASS:" in _verify()


@needs_postgres
def test_fail_on_activity_slot_mismatch(customer):
    _upload(customer)
    row = ActivityDay.objects.get(metric="active_kcal")
    row.values[0] = 99.0
    row.save()
    out = _verify_fails()
    assert (
        f"MISMATCH ACTIVE_CALORIES@15min customer={customer.pk} {TODAY}: "
        "1 slot(s) differ, first 00:00 compact=99.0 legacy=10.0"
    ) in out
    assert _row(out, "ACTIVE_CALORIES@15min") == ["3", "1", "1"]


@needs_postgres
def test_tolerance(customer):
    _upload(customer)
    row = ActivityDay.objects.get(metric="active_kcal")
    row.values[0] = 10.5
    row.save()
    _verify_fails()
    assert "PASS:" in _verify("--tolerance", "1.0")


@pytest.mark.skipif(POSTGRES, reason="sqlite-only behaviour")
def test_sqlite_compares_sleep_only(customer):
    _upload(customer)
    out = _verify()
    assert "comparing sleep only" in out
    assert "PASS:" in out and "(sleep only)" in out


def test_recent_customers_sampled_newest_first():
    users = [User.objects.create_user(username=f"recent-{i}") for i in range(3)]
    for i, user in enumerate(users):
        _upload(user, admin_create_date=NOW + timedelta(minutes=i))
    _upload(users[0], admin_create_date=NOW + timedelta(minutes=10))
    assert recent_customer_ids(2, scan_limit=1000) == [users[0].pk, users[2].pk]
    assert recent_customer_ids(10, scan_limit=1000) == [
        users[0].pk,
        users[2].pk,
        users[1].pk,
    ]


def test_customers_option_limits_scope(customer):
    other = User.objects.create_user(username="verify-other")
    _upload(customer)
    _upload(other)
    SleepDay.objects.filter(customer=other).delete()  # other would fail
    out = _verify("--customers", str(customer.pk))
    assert "1 customer(s)" in out and "PASS:" in out
    _verify_fails("--customers", str(other.pk))


def test_unknown_customer_rejected():
    with pytest.raises(CommandError, match="Unknown customer"):
        _verify("--customers", "999999")


def test_no_data_passes():
    assert "PASS: no customers with Record data" in _verify()


def test_sleep_compared_exactly(customer):
    # A 6-minute (0.1 h) difference is real: compact sleep is stored at full
    # precision, so --tolerance must not hide it.
    _upload(customer)
    row = SleepDay.objects.get()
    end = datetime.combine(TODAY, time(6, 54)).replace(tzinfo=timezone.utc)
    row.intervals = [[row.intervals[0][0], end.isoformat(), row.intervals[0][2]]]
    row.save()
    out = _verify_fails("--tolerance", "1.0")
    assert f"MISMATCH sleep customer={customer.pk} {TODAY}" in out


@needs_postgres
def test_fractional_steps_within_rounding(customer):
    # Steps are stored as whole numbers; a fractional legacy value differs by
    # up to 0.5, which the default tolerance (0.2) must not flag for steps.
    slot = RecordInput(
        startDate=MIDNIGHT,
        endDate=MIDNIGHT + timedelta(minutes=15),
        creationDate=NOW,
        sourceName="apple",
        value="10.4",
        unit="count",
        type=ActivityMetric.STEPS.value,
    )
    ingest_records(
        customer, [slot], source=DataSource.APPLE_HEALTH, admin_create_date=NOW
    )
    assert ActivityDay.objects.get(metric="steps").values[0] == 10
    out = _verify()
    assert "PASS:" in out
    assert _row(out, "STEPS@15min") == ["3", "1", "0"]
