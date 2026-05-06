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
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from healthdatamodel.models import Record
from healthdatamodel.query import ActivityMetric
from healthdatamodel.schemas import RecordInput


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
) -> None:
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
    """
    if admin_create_date is None:
        admin_create_date = datetime.now(timezone.utc)
    models = [_to_django(customer, r, source, admin_create_date) for r in records]
    Record.objects.bulk_create(models, batch_size=batch_size, ignore_conflicts=True)


async def aingest_records(
    customer: Any,
    records: list[RecordInput],
    source: str,
    admin_create_date: datetime | None = None,
    batch_size: int = 1000,
) -> None:
    """Async variant of :func:`ingest_records` using ``abulk_create``."""
    if admin_create_date is None:
        admin_create_date = datetime.now(timezone.utc)
    models = [_to_django(customer, r, source, admin_create_date) for r in records]
    await Record.objects.abulk_create(
        models, batch_size=batch_size, ignore_conflicts=True
    )


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
