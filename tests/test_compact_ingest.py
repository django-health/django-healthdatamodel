"""
Tests for the compact write path: routing, merge semantics, normalisation,
and the ``save_records`` / settings flags.

All tests run against SQLite (:memory:).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from healthdatamodel.compact import split_sleep_interval
from healthdatamodel.constants import CompactMetric, DataSource, SleepStage
from healthdatamodel.ingest import ingest_compact_activity, ingest_records
from healthdatamodel.models import ActivityDay, Record, SleepDay
from healthdatamodel.query import ActivityMetric
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
    return User.objects.create_user(username="compact-ingest-user")


def _sleep_input(start, end, value=ASLEEP, sourceName="apple"):
    return RecordInput(
        startDate=start,
        endDate=end,
        creationDate=NOW,
        sourceName=sourceName,
        value=value,
        type=SLEEP_TYPE,
    )


def _activity_input(
    start,
    end,
    value,
    unit="kcal",
    type=ActivityMetric.ACTIVE_CALORIES.value,
    sourceName="apple",
):
    return RecordInput(
        startDate=start,
        endDate=end,
        creationDate=NOW,
        sourceName=sourceName,
        value=value,
        unit=unit,
        type=type,
    )


# ---------------------------------------------------------------------------
# split_sleep_interval
# ---------------------------------------------------------------------------


class TestSplitSleepInterval:
    def test_interval_within_one_window(self):
        start = datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc)
        end = datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc)
        pieces = list(split_sleep_interval(start, end))
        assert pieces == [(TODAY, start, end)]

    def test_interval_spanning_boundary_split(self):
        # 13:00 → 15:00 spans the 14:00 boundary → one piece per sleep day
        start = datetime.combine(TODAY, time(13)).replace(tzinfo=timezone.utc)
        end = datetime.combine(TODAY, time(15)).replace(tzinfo=timezone.utc)
        boundary = datetime.combine(TODAY, time(14)).replace(tzinfo=timezone.utc)
        pieces = list(split_sleep_interval(start, end))
        assert pieces == [
            (TODAY, start, boundary),
            (TODAY + timedelta(days=1), boundary, end),
        ]

    def test_pieces_tile_the_original_interval(self):
        # multi-day interval: pieces are contiguous and cover [start, end)
        start = datetime.combine(YESTERDAY, time(13)).replace(tzinfo=timezone.utc)
        end = datetime.combine(TODAY, time(15)).replace(tzinfo=timezone.utc)
        pieces = list(split_sleep_interval(start, end))
        assert pieces[0][1] == start
        assert pieces[-1][2] == end
        for (_, _, prev_end), (_, next_start, _) in zip(pieces, pieces[1:]):
            assert prev_end == next_start

    def test_start_exactly_on_boundary(self):
        start = datetime.combine(TODAY, time(14)).replace(tzinfo=timezone.utc)
        end = datetime.combine(TODAY, time(15)).replace(tzinfo=timezone.utc)
        pieces = list(split_sleep_interval(start, end))
        assert pieces == [(TODAY + timedelta(days=1), start, end)]

    def test_zero_length_yields_nothing(self):
        start = datetime.combine(TODAY, time(3)).replace(tzinfo=timezone.utc)
        assert list(split_sleep_interval(start, start)) == []


# ---------------------------------------------------------------------------
# Sleep: replace-on-write
# ---------------------------------------------------------------------------


class TestSleepDayWrites:
    def test_one_row_per_device_day(self, customer):
        ingest_records(
            customer,
            [
                _sleep_input(
                    datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
                ),
                _sleep_input(
                    datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                ),
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = SleepDay.objects.get(customer=customer)
        assert row.day == TODAY
        assert row.device == "apple"
        assert len(row.intervals) == 2

    def test_new_upload_replaces_day(self, customer):
        ingest_records(
            customer,
            [
                _sleep_input(
                    datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(2)).replace(tzinfo=timezone.utc),
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW - timedelta(hours=2),
        )
        ingest_records(
            customer,
            [
                _sleep_input(
                    datetime.combine(TODAY, time(2, 30)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = SleepDay.objects.get(customer=customer)
        assert len(row.intervals) == 1  # replaced, not appended
        assert row.admin_create_date == NOW

    def test_different_devices_kept_separate(self, customer):
        night = (
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
        )
        ingest_records(
            customer,
            [
                _sleep_input(*night, sourceName="apple"),
                _sleep_input(*night, sourceName="garmin"),
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        assert SleepDay.objects.filter(customer=customer).count() == 2

    def test_stages_preserved_including_awake(self, customer):
        ingest_records(
            customer,
            [
                _sleep_input(
                    datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(1)).replace(tzinfo=timezone.utc),
                    value="HKCategoryValueSleepAnalysisAsleepDeep",
                ),
                _sleep_input(
                    datetime.combine(TODAY, time(1)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(1, 30)).replace(tzinfo=timezone.utc),
                    value="HKCategoryValueSleepAnalysisAwake",
                ),
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = SleepDay.objects.get(customer=customer)
        stages = {entry[2] for entry in row.intervals}
        assert stages == {SleepStage.DEEP.value, SleepStage.AWAKE.value}

    def test_unknown_sleep_value_goes_to_record_only(self, customer):
        ingest_records(
            customer,
            [
                _sleep_input(
                    datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
                    datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
                    value="100",  # aggregate value, not an HK stage
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        assert SleepDay.objects.count() == 0
        assert Record.objects.count() == 1


# ---------------------------------------------------------------------------
# Activity: slot-merge-on-write
# ---------------------------------------------------------------------------


class TestActivityDayWrites:
    def test_vector_shape_and_placement(self, customer):
        ingest_records(
            customer,
            [
                _activity_input(
                    MIDNIGHT + timedelta(minutes=30),
                    MIDNIGHT + timedelta(minutes=45),
                    "12.5",
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = ActivityDay.objects.get(customer=customer)
        assert row.metric == CompactMetric.ACTIVE_KCAL
        assert row.resolution_minutes == 15
        assert len(row.values) == 96
        assert row.values[2] == pytest.approx(12.5)
        assert row.values[0] is None and row.values[3] is None

    def test_slot_merge_preserves_untouched_slots(self, customer):
        ingest_records(
            customer,
            [_activity_input(MIDNIGHT, MIDNIGHT + timedelta(minutes=15), "10")],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW - timedelta(hours=2),
        )
        ingest_records(
            customer,
            [
                _activity_input(
                    MIDNIGHT + timedelta(minutes=15),
                    MIDNIGHT + timedelta(minutes=30),
                    "20",
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = ActivityDay.objects.get(customer=customer)
        assert row.values[0] == pytest.approx(10.0)
        assert row.values[1] == pytest.approx(20.0)

    def test_slot_merge_overwrites_same_slot(self, customer):
        for value, when in (("10", NOW - timedelta(hours=2)), ("8", NOW)):
            ingest_records(
                customer,
                [_activity_input(MIDNIGHT, MIDNIGHT + timedelta(minutes=15), value)],
                source=DataSource.APPLE_HEALTH,
                admin_create_date=when,
            )
        row = ActivityDay.objects.get(customer=customer)
        assert row.values[0] == pytest.approx(8.0)

    def test_steps_stored_as_int(self, customer):
        ingest_records(
            customer,
            [
                _activity_input(
                    MIDNIGHT,
                    MIDNIGHT + timedelta(minutes=15),
                    "123.7",
                    unit="count",
                    type=ActivityMetric.STEPS.value,
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = ActivityDay.objects.get(customer=customer)
        assert row.metric == CompactMetric.STEPS
        assert row.values[0] == 124
        assert isinstance(row.values[0], int)

    def test_energy_rounded_to_tenth_and_cal_converted(self, customer):
        ingest_records(
            customer,
            [
                _activity_input(
                    MIDNIGHT, MIDNIGHT + timedelta(minutes=15), "12345.6", unit="cal"
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = ActivityDay.objects.get(customer=customer)
        assert row.values[0] == pytest.approx(12.3)

    def test_daily_resolution_single_slot(self, customer):
        ingest_records(
            customer,
            [_activity_input(MIDNIGHT, MIDNIGHT + timedelta(days=1), "500")],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = ActivityDay.objects.get(customer=customer)
        assert row.resolution_minutes == 1440
        assert row.values == [500.0]

    def test_devices_get_separate_rows(self, customer):
        ingest_records(
            customer,
            [
                _activity_input(MIDNIGHT, MIDNIGHT + timedelta(minutes=15), "10"),
                _activity_input(
                    MIDNIGHT,
                    MIDNIGHT + timedelta(minutes=15),
                    "5",
                    sourceName="iphone",
                ),
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        assert ActivityDay.objects.filter(customer=customer).count() == 2

    def test_non_grid_interval_goes_to_record_only(self, customer):
        # 17 minutes doesn't divide 1440 → uncompactable
        ingest_records(
            customer,
            [_activity_input(MIDNIGHT, MIDNIGHT + timedelta(minutes=17), "10")],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        assert ActivityDay.objects.count() == 0
        assert Record.objects.count() == 1

    def test_unaligned_interval_goes_to_record_only(self, customer):
        # 15-minute duration but starting at :05 — off the grid
        ingest_records(
            customer,
            [
                _activity_input(
                    MIDNIGHT + timedelta(minutes=5),
                    MIDNIGHT + timedelta(minutes=20),
                    "10",
                )
            ],
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        assert ActivityDay.objects.count() == 0
        assert Record.objects.count() == 1

    def test_ingest_compact_activity_writes_vectors(self, customer):
        ingest_compact_activity(
            customer,
            ActivityMetric.ACTIVE_CALORIES,
            MIDNIGHT,
            [([10.0, 20.0, 30.0], "apple")],
            resolution_minutes=15,
            unit="kcal",
            source=DataSource.APPLE_HEALTH,
            admin_create_date=NOW,
        )
        row = ActivityDay.objects.get(customer=customer)
        assert row.values[:3] == [10.0, 20.0, 30.0]


# ---------------------------------------------------------------------------
# save_records / settings flags
# ---------------------------------------------------------------------------


class TestSaveRecordsFlag:
    def _night(self):
        return _sleep_input(
            datetime.combine(YESTERDAY, time(23)).replace(tzinfo=timezone.utc),
            datetime.combine(TODAY, time(7)).replace(tzinfo=timezone.utc),
        )

    def test_default_dual_writes(self, customer):
        ingest_records(customer, [self._night()], source=DataSource.APPLE_HEALTH)
        assert Record.objects.count() == 1
        assert SleepDay.objects.count() == 1

    def test_save_records_false_skips_record_table(self, customer):
        ingest_records(
            customer,
            [self._night()],
            source=DataSource.APPLE_HEALTH,
            save_records=False,
        )
        assert Record.objects.count() == 0
        assert SleepDay.objects.count() == 1

    def test_save_records_setting_default(self, customer, settings):
        settings.HEALTHDATAMODEL_SAVE_RECORDS = False
        ingest_records(customer, [self._night()], source=DataSource.APPLE_HEALTH)
        assert Record.objects.count() == 0
        assert SleepDay.objects.count() == 1

    def test_kwarg_overrides_setting(self, customer, settings):
        settings.HEALTHDATAMODEL_SAVE_RECORDS = False
        ingest_records(
            customer,
            [self._night()],
            source=DataSource.APPLE_HEALTH,
            save_records=True,
        )
        assert Record.objects.count() == 1

    def test_uncompactable_saved_to_record_despite_flag(self, customer):
        # Unknown HK type: never representable compactly → must not be dropped
        heart_rate = RecordInput(
            startDate=MIDNIGHT,
            endDate=MIDNIGHT + timedelta(minutes=1),
            creationDate=NOW,
            sourceName="apple",
            value="62",
            unit="count/min",
            type="HKQuantityTypeIdentifierHeartRate",
        )
        ingest_records(
            customer,
            [heart_rate, self._night()],
            source=DataSource.APPLE_HEALTH,
            save_records=False,
        )
        assert SleepDay.objects.count() == 1
        record = Record.objects.get()
        assert record.type == "HKQuantityTypeIdentifierHeartRate"

    def test_save_compact_false_restores_legacy_behaviour(self, customer, settings):
        settings.HEALTHDATAMODEL_SAVE_COMPACT = False
        ingest_records(customer, [self._night()], source=DataSource.APPLE_HEALTH)
        assert Record.objects.count() == 1
        assert SleepDay.objects.count() == 0

    def test_save_compact_false_and_save_records_false_still_persists(
        self, customer, settings
    ):
        # Contradictory config: nothing must ever be silently dropped.
        settings.HEALTHDATAMODEL_SAVE_COMPACT = False
        settings.HEALTHDATAMODEL_SAVE_RECORDS = False
        ingest_records(customer, [self._night()], source=DataSource.APPLE_HEALTH)
        assert Record.objects.count() == 1
