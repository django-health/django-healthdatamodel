"""Tests for ``manage.py compact_status`` and ``compat.backfill_coverage``."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from healthdatamodel.compat import backfill_coverage, backfill_from_records
from healthdatamodel.management.commands.compact_status import record_lag
from healthdatamodel.models import ActivityDay, SleepDay
from tests.test_verify_compact import NOW, TODAY, _upload

User = get_user_model()
pytestmark = pytest.mark.django_db

OLD = NOW - timedelta(days=20)


@pytest.fixture
def customer():
    return User.objects.create_user(username="status-user")


def _status(*args):
    out = StringIO()
    call_command("compact_status", *args, stdout=out)
    return out.getvalue()


def _legacy_history_then_live(customer, settings):
    """An upgrading install: old history only in Record, then a dual-written
    upload after the upgrade."""
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _upload(customer, admin_create_date=OLD)
    # Old upload covers an older day too.
    from healthdatamodel.models import Record

    for record in Record.objects.filter(customer=customer):
        record.pk = None
        record.startDate -= timedelta(days=20)
        record.endDate -= timedelta(days=20)
        record.save()
    settings.HEALTHDATAMODEL_SAVE_COMPACT = True
    _upload(customer, admin_create_date=NOW)


def test_reports_settings_with_origin(settings):
    settings.HEALTHDATAMODEL_READ_COMPACT = False
    out = _status()
    assert "HEALTHDATAMODEL_READ_COMPACT = False (set)" in out
    assert "HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = False (default)" in out
    assert "HEALTHDATAMODEL_SAVE_RECORDS = True (default)" in out


def test_counts_and_ready(customer):
    _upload(customer)
    out = _status()
    assert (
        f"SleepDay: 1 rows, 1 customers, days {TODAY}..{TODAY}, "
        f"newest upload {NOW}" in out
    )
    assert "ActivityDay: 2 rows, 1 customers" in out
    assert "customers with any compact row: 1" in out
    assert "Dual-write lag: 0 compactable Record row(s)" in out
    assert "pending: 0" in out
    assert "READY:" in out
    _status("--fail-if-not-ready")  # does not raise


def test_pending_until_backfilled(customer, settings):
    _legacy_history_then_live(customer, settings)
    # Live dual-writes covered today, but the 20-day-old history is missing.
    assert set(SleepDay.objects.values_list("day", flat=True)) == {TODAY}
    out = _status()
    assert "with compactable Record data: 1" in out
    assert f"oldest compactable Record day: {TODAY - timedelta(days=20)}" in out
    assert "pending: 1" in out
    assert f"pending customer pks: {customer.pk}" in out
    assert "NOT READY" in out
    with pytest.raises(CommandError, match="not backfilled"):
        _status("--fail-if-not-ready")

    backfill_from_records()
    out = _status("--fail-if-not-ready")
    assert "pending: 0" in out and "READY:" in out


def test_coverage_counts_unmapped_asleep_values(customer, settings):
    # A customer whose only sleep history uses an uncatalogued "...Asleep*"
    # value is compactable (prefix fallback), so is pending until backfilled
    # rather than silently treated as having no compactable data.
    from healthdatamodel.constants import DataSource
    from healthdatamodel.ingest import ingest_records
    from healthdatamodel.schemas import RecordInput

    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    ingest_records(
        customer,
        [
            RecordInput(
                startDate=OLD - timedelta(hours=9),
                endDate=OLD - timedelta(hours=1),
                creationDate=OLD,
                sourceName="android",
                value="HKCategoryValueSleepAnalysisAsleepLight",
                type="HKCategoryTypeIdentifierSleepAnalysis",
            )
        ],
        source=DataSource.HEALTH_CONNECT,
        admin_create_date=OLD,
    )
    settings.HEALTHDATAMODEL_SAVE_COMPACT = True
    result = backfill_coverage()
    assert result.customers_with_records == 1
    assert result.pending == [customer.pk]
    backfill_from_records()
    assert backfill_coverage().pending == []


def test_coverage_scope(customer, settings):
    other = User.objects.create_user(username="status-other")
    _legacy_history_then_live(customer, settings)
    _upload(other)
    assert backfill_coverage().pending == [customer.pk]
    assert backfill_coverage(customers=[other.pk]).pending == []
    result = backfill_coverage(customer_range=(other.pk, other.pk))
    assert result.customers_scanned == 1 and result.pending == []
    out = _status("--customers", str(other.pk))
    assert "Backfill coverage (selected customers)" in out and "pending: 0" in out


def test_lag_when_compact_writes_off(customer, settings):
    _upload(customer, admin_create_date=OLD)
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _upload(customer, admin_create_date=NOW)
    out = _status()
    assert "HEALTHDATAMODEL_SAVE_COMPACT = False (set)" in out
    assert "Dual-write lag: 3 compactable Record row(s)" in out
    assert "NOT READY" in out


def test_record_lag_truncates(customer, settings):
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _upload(customer)
    assert record_lag(None, scan_limit=10) == (3, False)
    assert record_lag(NOW - timedelta(seconds=1), scan_limit=10) == (3, False)
    assert record_lag(NOW, scan_limit=10) == (0, False)


def test_skip_coverage(customer):
    _upload(customer)
    out = _status("--skip-coverage")
    assert "Backfill coverage: skipped" in out and "NOT READY" in out


def test_empty_install():
    out = _status()
    assert "SleepDay: 0 rows, 0 customers" in out
    assert "pending: 0" in out and "READY:" in out
    assert ActivityDay.objects.count() == 0


def test_record_lag_reports_truncation(customer, settings, monkeypatch):
    from healthdatamodel.management.commands import compact_status

    monkeypatch.setattr(compact_status, "_SCAN_PAGE", 1)
    settings.HEALTHDATAMODEL_SAVE_COMPACT = False
    _upload(customer)
    assert record_lag(None, scan_limit=2) == (2, True)
