"""Tests for the fixture helpers in healthdatamodel.testing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.constants import DataSource
from healthdatamodel.models import Record
from healthdatamodel.query import SLEEP_TYPE, ActivityMetric, get_sleep_hours_by_day
from healthdatamodel.schemas import RecordInput
from healthdatamodel.testing import (
    DEFAULT_RECORD_START,
    create_record,
    create_records,
    record_input,
)

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def customer():
    return User.objects.create_user(username="fixture-user")


class TestRecordInput:
    def test_defaults(self):
        r = record_input()
        assert r.startDate == DEFAULT_RECORD_START
        assert r.endDate == DEFAULT_RECORD_START + timedelta(minutes=15)
        assert r.type == ActivityMetric.STEPS
        assert r.value == "0"
        assert r.sourceName == "test"
        assert r.unit is None

    def test_end_follows_overridden_start(self):
        start = datetime(2024, 2, 29, 23, 50, tzinfo=timezone.utc)
        r = record_input(startDate=start, value=12)
        assert r.endDate == start + timedelta(minutes=15)
        assert r.value == "12"

    def test_explicit_end(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        r = record_input(startDate=start, endDate=start + timedelta(days=1))
        assert r.endDate - r.startDate == timedelta(days=1)


class TestCreateRecords:
    def test_mixed_inputs_and_defaults(self, customer):
        acd = datetime(2025, 1, 2, tzinfo=timezone.utc)
        out = create_records(
            customer,
            [record_input(value="5"), {"value": 7, "unit": "count"}],
            source=DataSource.FITBIT,
            admin_create_date=acd,
        )
        assert [r.value for r in out] == ["5", "7"]
        assert all(isinstance(r, RecordInput) for r in out)
        rows = Record.objects.filter(customer=customer)
        assert rows.count() == 2
        assert set(rows.values_list("source", flat=True)) == {"fitbit"}
        assert set(rows.values_list("admin_create_date", flat=True)) == {acd}

    def test_per_row_source_and_admin_create_date(self, customer):
        t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 2, tzinfo=timezone.utc)
        create_records(
            customer,
            [
                {"value": 1, "source": DataSource.GARMIN, "admin_create_date": t1},
                {"value": 2, "admin_create_date": t2},
                {"value": 3, "customer": "ignored"},
            ],
            source=DataSource.OURA,
        )
        got = {
            r.value: (r.source, r.admin_create_date)
            for r in Record.objects.filter(customer=customer)
        }
        assert got["1"] == ("garmin", t1)
        assert got["2"] == ("oura", t2)
        assert got["3"][0] == "oura"

    def test_default_admin_create_date_shared(self, customer):
        create_records(customer, [{}, {}])
        assert (
            Record.objects.filter(customer=customer)
            .values("admin_create_date")
            .distinct()
            .count()
            == 1
        )

    def test_create_record_drop_in_and_visible_to_query_api(self, customer):
        day = DEFAULT_RECORD_START.date()
        create_record(
            customer=customer,
            startDate=datetime.combine(day - timedelta(days=1), datetime.min.time())
            .replace(hour=23)
            .replace(tzinfo=timezone.utc),
            endDate=datetime.combine(day, datetime.min.time())
            .replace(hour=7)
            .replace(tzinfo=timezone.utc),
            type=SLEEP_TYPE,
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            source=DataSource.APPLE_HEALTH,
            sourceName="apple",
            creationDate=datetime.now(timezone.utc),
            admin_create_date=datetime.now(timezone.utc),
        )
        assert get_sleep_hours_by_day(customer, day, day) == {day: 8.0}
