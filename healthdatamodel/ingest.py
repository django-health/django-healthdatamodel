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

Compact storage (v1.0)
----------------------
All ingest functions also write to the compact tables
(:class:`~healthdatamodel.models.SleepDay` /
:class:`~healthdatamodel.models.ActivityDay`) unless
``settings.HEALTHDATAMODEL_SAVE_COMPACT`` is ``False``.  The legacy
``Record`` write-through is controlled by the ``save_records`` argument,
defaulting to ``settings.HEALTHDATAMODEL_SAVE_RECORDS`` (``True``).
Records the compact schema cannot represent (unknown types, non-grid
intervals) are written to ``Record`` even when ``save_records`` is off, so
nothing is ever silently dropped.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from asgiref.sync import sync_to_async

from healthdatamodel import compact
from healthdatamodel.models import Record, Workout, WorkoutMetadataEntry
from healthdatamodel.query import ActivityMetric
from healthdatamodel.schemas import RecordInput, WorkoutInput

logger = logging.getLogger(__name__)


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


def _records_to_persist(
    records: list[RecordInput],
    leftovers: list[RecordInput],
    save_records: bool,
    source: str,
) -> list[RecordInput]:
    """Pick which records go to the legacy ``Record`` table.

    With ``save_records`` on, everything is written through (unchanged legacy
    behaviour).  With it off, only uncompactable leftovers are persisted —
    with a warning, since the caller presumably expected everything to land
    in the compact tables.
    """
    if save_records:
        return records
    if leftovers:
        logger.warning(
            "healthdatamodel: %d of %d records from source %s could not be "
            "stored compactly and were written to Record despite "
            "save_records=False (types: %s)",
            len(leftovers),
            len(records),
            source,
            sorted({r.type for r in leftovers}),
        )
    return leftovers


def ingest_records(
    customer: Any,
    records: list[RecordInput],
    source: str,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
    save_records: bool | None = None,
) -> None:
    """Save *records* to the database.

    Records are written to the compact tables
    (:class:`~healthdatamodel.models.SleepDay` /
    :class:`~healthdatamodel.models.ActivityDay`) and, depending on
    *save_records*, to the legacy ``Record`` table.

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
    save_records:
        Write through to the legacy ``Record`` table.  ``None`` (default)
        falls back to ``settings.HEALTHDATAMODEL_SAVE_RECORDS`` (``True``).
        Uncompactable records are written to ``Record`` regardless.
    """
    if admin_create_date is None:
        admin_create_date = datetime.now(timezone.utc)
    if save_records is None:
        save_records = compact.save_records_enabled()

    leftovers = list(records)
    if compact.save_compact_enabled():
        leftovers = compact.apply_compact(customer, records, source, admin_create_date)

    to_persist = _records_to_persist(records, leftovers, save_records, source)
    models = [_to_django(customer, r, source, admin_create_date) for r in to_persist]
    Record.objects.bulk_create(models, batch_size=batch_size, ignore_conflicts=True)


async def aingest_records(
    customer: Any,
    records: list[RecordInput],
    source: str,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
    save_records: bool | None = None,
) -> None:
    """Async variant of :func:`ingest_records` using ``abulk_create``.

    The compact write runs in a thread via ``sync_to_async`` (it needs
    ``transaction.atomic`` + row locking, which the async ORM doesn't
    support yet).
    """
    if admin_create_date is None:
        admin_create_date = datetime.now(timezone.utc)
    if save_records is None:
        save_records = compact.save_records_enabled()

    leftovers = list(records)
    if compact.save_compact_enabled():
        leftovers = await sync_to_async(compact.apply_compact)(
            customer, records, source, admin_create_date
        )

    to_persist = _records_to_persist(records, leftovers, save_records, source)
    models = [_to_django(customer, r, source, admin_create_date) for r in to_persist]
    await Record.objects.abulk_create(
        models, batch_size=batch_size, ignore_conflicts=True
    )


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


def ingest_workouts(
    customer: Any,
    workouts: list[WorkoutInput],
    source: str,
    batch_size: int = 1000,
) -> None:
    """Save *workouts* to the database.

    Mirrors :func:`ingest_records`. Persists each ``WorkoutInput`` as one
    ``Workout`` row plus ``WorkoutMetadataEntry`` rows for the caller-supplied
    metadata and for the ``caloriesBurned`` / ``distance`` fields that don't
    have first-class columns on ``Workout``.

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
        Rows per ``INSERT`` statement when bulk-creating the metadata entries.

    Notes
    -----
    Workouts are inserted one-at-a-time so the auto-generated primary keys are
    available immediately for the metadata FK. Metadata entries are then
    bulk-inserted in a single statement per ``batch_size``.
    """
    metadata: list[WorkoutMetadataEntry] = []
    for workout_input in workouts:
        workout = _workout_to_django(customer, workout_input, source)
        workout.save()
        metadata.extend(_workout_metadata_entries(workout, workout_input))
    if metadata:
        WorkoutMetadataEntry.objects.bulk_create(metadata, batch_size=batch_size)


async def aingest_workouts(
    customer: Any,
    workouts: list[WorkoutInput],
    source: str,
    batch_size: int = 1000,
) -> None:
    """Async variant of :func:`ingest_workouts`."""
    metadata: list[WorkoutMetadataEntry] = []
    for workout_input in workouts:
        workout = _workout_to_django(customer, workout_input, source)
        await workout.asave()
        metadata.extend(_workout_metadata_entries(workout, workout_input))
    if metadata:
        await WorkoutMetadataEntry.objects.abulk_create(metadata, batch_size=batch_size)


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
    save_records: bool | None = None,
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
    save_records:
        Write through to the legacy ``Record`` table.  ``None`` (default)
        falls back to ``settings.HEALTHDATAMODEL_SAVE_RECORDS`` (``True``).

    Returns
    -------
    dict[date, float | None] | None
        Daily totals when ``return_results=True``; ``None`` otherwise.
    """
    records = expand_compact_activity(
        metric, start, values_by_source, resolution_minutes, unit
    )
    ingest_records(
        customer, records, source, admin_create_date, batch_size, save_records
    )
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
    save_records: bool | None = None,
) -> dict[date, float | None] | None:
    """Async variant of :func:`ingest_compact_activity`."""
    records = expand_compact_activity(
        metric, start, values_by_source, resolution_minutes, unit
    )
    await aingest_records(
        customer, records, source, admin_create_date, batch_size, save_records
    )
    if not return_results:
        return None
    n = max((len(v) for v, _ in values_by_source), default=0)
    end_date = (
        start + timedelta(minutes=n * resolution_minutes) - timedelta(seconds=1)
    ).date()
    return _day_totals_from_records(records, metric, start.date(), end_date)
