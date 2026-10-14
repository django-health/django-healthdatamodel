"""
Ingest API for health data.

Callers build :class:`~healthdatamodel.schemas.RecordInput` objects (or use
the compact helpers below) and pass them to the ingest functions.  The
underlying Django ``Record`` model is an internal detail — callers never touch
it directly.

Entry points
------------
``ingest_records`` / ``aingest_records``
    Save a list of :class:`~healthdatamodel.schemas.RecordInput` objects to
    the database.  Suitable for full-format payloads (Apple Health XML, Health
    Connect, Fitbit record-level API).

``ingest_compact_activity`` / ``aingest_compact_activity``
    Expand compact float arrays (one array per source) into individual records
    and save them.  One ``Record`` row is stored *per source per interval* so
    that source-ranked deduplication works correctly at query time.

    Pass ``return_results=True`` to get daily totals computed in memory
    instead of re-querying the database — useful after single-source ingests
    when no competing sources exist.

``ingest_workouts`` / ``aingest_workouts``
    Save :class:`~healthdatamodel.schemas.WorkoutInput` objects plus their
    metadata entries.

``restore_records``
    Re-insert previously archived records verbatim, preserving each row's
    own ``source``, ``creationDate`` and ``admin_create_date``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from typing import Any

from django.db import connections, router, transaction

from healthdatamodel.models import Record, Workout, WorkoutMetadataEntry
from healthdatamodel.query import ActivityMetric
from healthdatamodel.schemas import RecordInput, StoredRecord, WorkoutInput

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_django(
    customer: Any,
    record: RecordInput,
    source: str,
    admin_create_date: datetime,
) -> Record:
    return Record(
        customer=customer,
        startDate=record.startDate,
        endDate=record.endDate,
        creationDate=record.creationDate,
        sourceVersion=record.sourceVersion,
        sourceName=record.sourceName,
        source=source,
        value=record.value,
        unit=record.unit,
        type=record.type,
        device=record.device,
        admin_create_date=admin_create_date,
    )


def _day_totals_from_records(
    records: list[RecordInput],
    metric: ActivityMetric,
    start: date,
    end: date,
) -> dict[date, float | None]:
    """Aggregate RecordInput values to daily totals in memory.

    Sums all records for *metric* within ``[start, end]`` by calendar day.
    Designed for single-source ingests where source deduplication is not
    needed (``has_competing_sources`` would return False).
    """
    from collections import defaultdict

    raw: dict[date, float] = defaultdict(float)
    for r in records:
        if r.type != metric.value:
            continue
        r_date = r.startDate.date()
        if r_date < start or r_date > end:
            continue
        try:
            v = float(r.value)
        except (ValueError, TypeError):
            continue
        if r.unit in ("cal", "calories"):
            v /= 1000
        raw[r_date] += max(0.0, v)

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {day: raw.get(day) for day in days}


# ---------------------------------------------------------------------------
# Public API — full-format ingest
# ---------------------------------------------------------------------------


def ingest_records(
    customer: Any,
    records: list[RecordInput],
    source: str,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
) -> int:
    """Save *records* to the database.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    records:
        List of :class:`~healthdatamodel.schemas.RecordInput` objects to save.
    source:
        The data-pipeline identifier for all records (e.g.
        ``DataSource.APPLE_HEALTH``).  Stored in ``Record.source`` and used
        by source-ranking at query time.
    admin_create_date:
        Upload timestamp written to all rows.  Defaults to ``now()``.
    batch_size:
        Rows per ``INSERT`` statement.

    Returns
    -------
    int
        Number of records submitted (``len(records)``).
    """
    if admin_create_date is None:
        admin_create_date = datetime.now(timezone.utc)
    models = [_to_django(customer, r, source, admin_create_date) for r in records]
    Record.objects.bulk_create(models, batch_size=batch_size, ignore_conflicts=True)
    return len(models)


async def aingest_records(
    customer: Any,
    records: list[RecordInput],
    source: str,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
) -> int:
    """Async variant of :func:`ingest_records` using ``abulk_create``."""
    if admin_create_date is None:
        admin_create_date = datetime.now(timezone.utc)
    models = [_to_django(customer, r, source, admin_create_date) for r in records]
    await Record.objects.abulk_create(
        models, batch_size=batch_size, ignore_conflicts=True
    )
    return len(models)


# ---------------------------------------------------------------------------
# Public API — workout ingest
# ---------------------------------------------------------------------------


def _workout_metadata_entries(
    workout: Workout, workout_input: WorkoutInput
) -> list[WorkoutMetadataEntry]:
    """Build metadata rows that capture caller-supplied metadata plus the
    ``WorkoutInput`` fields that don't have first-class columns on ``Workout``
    (caloriesBurned/Unit, distance/Unit)."""
    entries: list[WorkoutMetadataEntry] = []
    for entry in workout_input.metadataEntry or []:
        entries.append(
            WorkoutMetadataEntry(workout=workout, key=entry.key, value=entry.value)
        )
    if workout_input.caloriesBurned is not None:
        entries.append(
            WorkoutMetadataEntry(
                workout=workout,
                key="caloriesBurned",
                value=str(workout_input.caloriesBurned),
            )
        )
    if workout_input.caloriesUnit is not None:
        entries.append(
            WorkoutMetadataEntry(
                workout=workout, key="caloriesUnit", value=workout_input.caloriesUnit
            )
        )
    if workout_input.distance is not None:
        entries.append(
            WorkoutMetadataEntry(
                workout=workout, key="distance", value=str(workout_input.distance)
            )
        )
    if workout_input.distanceUnit is not None:
        entries.append(
            WorkoutMetadataEntry(
                workout=workout, key="distanceUnit", value=workout_input.distanceUnit
            )
        )
    return entries


def _workout_to_django(
    customer: Any,
    workout: WorkoutInput,
    source: str,
) -> Workout:
    return Workout(
        customer=customer,
        startDate=workout.startDate,
        endDate=workout.endDate,
        creationDate=workout.creationDate,
        sourceVersion=workout.sourceVersion,
        sourceName=workout.sourceName,
        source=source,
        device=workout.device,
        durationUnit=workout.durationUnit,
        duration=int(workout.duration),
        workoutActivityType=workout.workoutActivityType,
    )


def _can_bulk_return_pks() -> bool:
    db = router.db_for_write(Workout)
    return connections[db].features.can_return_rows_from_bulk_insert


def ingest_workouts(
    customer: Any,
    workouts: list[WorkoutInput],
    source: str,
    batch_size: int = 1000,
) -> int:
    """Save *workouts* to the database.

    Mirrors :func:`ingest_records`. Persists each ``WorkoutInput`` as one
    ``Workout`` row plus ``WorkoutMetadataEntry`` rows for the caller-supplied
    metadata and for the ``caloriesBurned`` / ``caloriesUnit`` / ``distance``
    / ``distanceUnit`` fields that don't have first-class columns on
    ``Workout``.  ``duration`` is truncated to an integer (the column type).

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    workouts:
        List of :class:`~healthdatamodel.schemas.WorkoutInput` objects to save.
    source:
        The data-pipeline identifier for all workouts (e.g.
        ``DataSource.APPLE_HEALTH``).  Stored in ``Workout.source``.
    batch_size:
        Rows per ``INSERT`` statement.

    Returns
    -------
    int
        Number of workouts saved.

    Notes
    -----
    Runs in a single transaction: either every workout and metadata entry is
    saved or none is.  On backends that return primary keys from bulk
    inserts (PostgreSQL, SQLite ≥ 3.35, MariaDB ≥ 10.5) workouts are
    bulk-inserted; elsewhere they are saved one at a time so the keys are
    available for the metadata foreign key.
    """
    with transaction.atomic(using=router.db_for_write(Workout)):
        objs = [_workout_to_django(customer, w, source) for w in workouts]
        if _can_bulk_return_pks():
            Workout.objects.bulk_create(objs, batch_size=batch_size)
        else:
            for obj in objs:
                obj.save()
        metadata: list[WorkoutMetadataEntry] = []
        for obj, workout_input in zip(objs, workouts, strict=True):
            metadata.extend(_workout_metadata_entries(obj, workout_input))
        if metadata:
            WorkoutMetadataEntry.objects.bulk_create(metadata, batch_size=batch_size)
    return len(objs)


async def aingest_workouts(
    customer: Any,
    workouts: list[WorkoutInput],
    source: str,
    batch_size: int = 1000,
) -> int:
    """Async variant of :func:`ingest_workouts` (same transaction semantics)."""
    from asgiref.sync import sync_to_async

    return await sync_to_async(ingest_workouts)(customer, workouts, source, batch_size)


# ---------------------------------------------------------------------------
# Public API — restore archived records
# ---------------------------------------------------------------------------

_RESTORE_REQUIRED = (
    "startDate",
    "endDate",
    "creationDate",
    "sourceName",
    "source",
    "value",
    "type",
    "admin_create_date",
)
_RESTORE_OPTIONAL = ("unit", "sourceVersion", "device")


def _missing(value: Any) -> bool:
    """``None`` or a float NaN (what pandas puts in empty parquet cells)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _restored_to_django(row: StoredRecord | Mapping[str, Any]) -> Record:
    if isinstance(row, StoredRecord):
        return Record(
            customer_id=row.customer_id,
            startDate=row.startDate,
            endDate=row.endDate,
            creationDate=row.creationDate,
            sourceVersion=row.sourceVersion,
            sourceName=row.sourceName,
            source=row.source,
            value=row.value,
            unit=row.unit,
            type=row.type,
            device=row.device,
            admin_create_date=row.admin_create_date,
        )
    if "customer_id" in row and not _missing(row["customer_id"]):
        customer_id = int(row["customer_id"])
    elif "customer" in row and not _missing(row["customer"]):
        customer = row["customer"]
        customer_id = int(getattr(customer, "pk", customer))
    else:
        raise ValueError("restore row has neither 'customer_id' nor 'customer'")
    missing = [k for k in _RESTORE_REQUIRED if k not in row or _missing(row[k])]
    if missing:
        raise ValueError(f"restore row is missing required field(s): {missing}")
    fields: dict[str, Any] = {k: row[k] for k in _RESTORE_REQUIRED}
    fields["value"] = str(fields["value"])
    for k in _RESTORE_OPTIONAL:
        v = row.get(k)
        fields[k] = None if _missing(v) else v
    return Record(customer_id=customer_id, **fields)


def restore_records(
    records: Iterable[StoredRecord | Mapping[str, Any]],
    batch_size: int = 1000,
) -> int:
    """Re-insert archived records exactly as they were stored.

    Unlike :func:`ingest_records`, every row keeps its **own** customer,
    ``source``, ``creationDate`` and ``admin_create_date`` — use this when
    restoring rows from an archive (e.g. parquet exported from
    ``healthdatamodel_record``) so that upload-time ordering, which the sleep
    and activity queries rely on, is preserved.  From v1.0 this also writes
    the compact storage tables.

    Parameters
    ----------
    records:
        Any iterable (consumed lazily, one batch at a time) of
        :class:`~healthdatamodel.schemas.StoredRecord` objects or mappings
        such as ``DataFrame.to_dict("records")`` rows.  A mapping needs
        ``customer_id`` (or ``customer``: an id or a user instance),
        ``startDate``, ``endDate``, ``creationDate``, ``sourceName``,
        ``source``, ``value``, ``type`` and ``admin_create_date``; ``unit``,
        ``sourceVersion`` and ``device`` are optional.  Other keys (e.g. an
        archived ``id``) are ignored — new primary keys are assigned.  NaN in
        an optional column is stored as ``NULL``; ``value`` is stringified.
        Datetimes should be timezone-aware.
    batch_size:
        Rows per ``INSERT`` statement.

    Returns
    -------
    int
        Number of records inserted.

    Raises
    ------
    ValueError
        If a mapping lacks a required field.  Batches already written are
        not rolled back; wrap the call in ``transaction.atomic()`` for
        all-or-nothing behaviour.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    inserted = 0
    rows = iter(records)
    while batch := [_restored_to_django(r) for r in islice(rows, batch_size)]:
        Record.objects.bulk_create(batch, batch_size=batch_size)
        inserted += len(batch)
    return inserted


# ---------------------------------------------------------------------------
# Public API — compact-format ingest
# ---------------------------------------------------------------------------


def expand_compact_activity(
    metric: ActivityMetric,
    start: datetime,
    values_by_source: list[tuple[list[float], str]],
    resolution_minutes: int,
    unit: str,
) -> list[RecordInput]:
    """Expand compact float arrays into :class:`~healthdatamodel.schemas.RecordInput` objects.

    One record is created per source per interval (not merged).
    Source-ranking deduplication happens at query time in the database.

    Parameters
    ----------
    metric:
        Which health metric these values represent.
    start:
        UTC datetime of the first interval's start.
    values_by_source:
        List of ``(values, source_name)`` pairs.  Each *source_name* is stored
        in ``Record.sourceName``; the outer ``source`` parameter passed to the
        ingest function sets ``Record.source`` (the pipeline).
    resolution_minutes:
        Duration of each interval in minutes.
    unit:
        Physical unit string (``"kcal"``, ``"count"``, etc.).
    """
    now = datetime.now(timezone.utc)
    records: list[RecordInput] = []
    for values, source_name in values_by_source:
        for i, value in enumerate(values):
            interval_start = start + timedelta(minutes=i * resolution_minutes)
            interval_end = start + timedelta(minutes=(i + 1) * resolution_minutes)
            records.append(
                RecordInput(
                    startDate=interval_start,
                    endDate=interval_end,
                    creationDate=now,
                    sourceName=source_name,
                    value=str(value),
                    unit=unit,
                    type=metric.value,
                )
            )
    return records


def ingest_compact_activity(
    customer: Any,
    metric: ActivityMetric,
    start: datetime,
    values_by_source: list[tuple[list[float], str]],
    resolution_minutes: int,
    unit: str,
    source: str,
    return_results: bool = False,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
) -> dict[date, float | None] | None:
    """Expand and save compact activity arrays, optionally returning daily totals.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    metric:
        Which health metric to store.
    start:
        UTC datetime of the first interval's start.
    values_by_source:
        List of ``(values, source_name)`` pairs.
    resolution_minutes:
        Duration of each interval in minutes.
    unit:
        Physical unit string.
    source:
        Data-pipeline identifier (``Record.source`` column), e.g.
        ``DataSource.APPLE_HEALTH``.
    return_results:
        If ``True``, return daily totals computed in memory rather than
        re-querying the database.  Only reliable when a single source is
        present (i.e. ``has_competing_sources`` would return ``False``).
    admin_create_date:
        Upload timestamp for all rows.  Defaults to ``now()``.
    batch_size:
        Rows per ``INSERT`` statement.

    Returns
    -------
    dict[date, float | None] | None
        Daily totals when ``return_results=True``; ``None`` otherwise.
    """
    records = expand_compact_activity(
        metric, start, values_by_source, resolution_minutes, unit
    )
    ingest_records(customer, records, source, admin_create_date, batch_size)
    if not return_results:
        return None
    n = max((len(v) for v, _ in values_by_source), default=0)
    end_date = (
        start + timedelta(minutes=n * resolution_minutes) - timedelta(seconds=1)
    ).date()
    return _day_totals_from_records(records, metric, start.date(), end_date)


async def aingest_compact_activity(
    customer: Any,
    metric: ActivityMetric,
    start: datetime,
    values_by_source: list[tuple[list[float], str]],
    resolution_minutes: int,
    unit: str,
    source: str,
    return_results: bool = False,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
) -> dict[date, float | None] | None:
    """Async variant of :func:`ingest_compact_activity`."""
    records = expand_compact_activity(
        metric, start, values_by_source, resolution_minutes, unit
    )
    await aingest_records(customer, records, source, admin_create_date, batch_size)
    if not return_results:
        return None
    n = max((len(v) for v, _ in values_by_source), default=0)
    end_date = (
        start + timedelta(minutes=n * resolution_minutes) - timedelta(seconds=1)
    ).date()
    return _day_totals_from_records(records, metric, start.date(), end_date)
