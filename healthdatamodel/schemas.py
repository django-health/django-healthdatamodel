"""
Pydantic models for the healthdatamodel ingest API.

``RecordInput`` and ``WorkoutInput`` are the public API objects callers build
before passing data to the ingest functions.  The underlying Django models are
an internal implementation detail — callers never interact with them directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import dateutil.parser
from pydantic import BaseModel, field_validator


class MetadataEntry(BaseModel):
    key: str
    value: str


class RecordInput(BaseModel):
    """A single health record, in the Apple HealthKit schema.

    Use :class:`healthdatamodel.query.ActivityMetric` and
    :class:`healthdatamodel.query.SleepValue` for the ``type`` and ``value``
    fields respectively so strings stay in sync across ingest and query.
    """

    recordId: str | None = None
    startDate: datetime
    endDate: datetime
    creationDate: datetime
    sourceVersion: str | None = None
    sourceName: str
    value: str
    unit: str | None = None
    type: str
    device: str | None = None
    deviceModel: str | None = None
    title: str | None = None
    notes: str | None = None
    metadataEntry: list[MetadataEntry] | None = None

    @field_validator("startDate", "endDate", "creationDate", mode="before")
    @classmethod
    def parse_datetime(cls, value: object) -> object:
        if isinstance(value, str):
            return dateutil.parser.parse(value).replace(tzinfo=timezone.utc)
        return value

    @field_validator("value", mode="before")
    @classmethod
    def parse_locale_number(cls, value: object) -> object:
        """Normalise locale-formatted numbers to plain US decimals.

        Accepts ``1.233,45`` (European) and ``1,233.45`` (US grouped) and
        normalises both to ``1233.45``.  Negative strings are coerced to
        ``"0.0"`` since health records cannot be negative.

        Plain ``int`` / ``float`` values (e.g. Fitbit API numbers) are
        accepted and stringified first, so callers need not ``str()`` them.
        """
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str) and "," in value:
            if value[-3] == ",":
                return value.replace(".", "").replace(",", ".")
            else:
                return value.replace(",", "")
        if isinstance(value, str) and "-" in value:
            return "0.0"
        return value


class WorkoutInput(BaseModel):
    """A single workout session, in the Apple HealthKit schema."""

    recordId: str | None = None
    startDate: datetime
    endDate: datetime
    creationDate: datetime
    sourceVersion: str | None = None
    sourceName: str
    metadataEntry: list[MetadataEntry] | None = None
    device: str | None = None
    durationUnit: str
    duration: float
    workoutActivityType: str
    caloriesBurned: float | None = None
    caloriesUnit: str | None = None
    distance: float | None = None
    distanceUnit: str | None = None

    @field_validator("startDate", "endDate", "creationDate", mode="before")
    @classmethod
    def parse_datetime(cls, value: object) -> object:
        if isinstance(value, str):
            return dateutil.parser.parse(value).replace(tzinfo=timezone.utc)
        return value

    @field_validator("duration", mode="before")
    @classmethod
    def parse_float(cls, value: object) -> object:
        if isinstance(value, str):
            return float(value)
        return value


# ---------------------------------------------------------------------------
# Read-side value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredRecord:
    """A health record as returned by the read helpers.

    Returned by :func:`healthdatamodel.query.get_sleep_records` and
    :func:`healthdatamodel.query.iter_records`, and accepted by
    :func:`healthdatamodel.ingest.restore_records`.  It deliberately carries
    no primary key: from v1.0 records may be served from compact storage,
    where individual rows have no stable id.  Treat it as a read-only value.
    """

    customer_id: int
    startDate: datetime
    endDate: datetime
    creationDate: datetime
    sourceName: str
    source: str
    value: str
    type: str
    admin_create_date: datetime
    unit: str | None = None
    sourceVersion: str | None = None
    device: str | None = None


@dataclass(frozen=True)
class StoredWorkout:
    """A workout as returned by :func:`healthdatamodel.query.iter_workouts`.

    Metadata entries are not included; like :class:`StoredRecord` it has no
    primary key and is a read-only value.
    """

    customer_id: int
    startDate: datetime
    endDate: datetime
    creationDate: datetime
    sourceName: str
    source: str
    durationUnit: str
    duration: int
    workoutActivityType: str
    admin_create_date: datetime
    sourceVersion: str | None = None
    device: str | None = None
