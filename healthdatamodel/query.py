"""
High-level query API for health data.

Instead of querying ``Record`` objects directly, callers should use the
functions here to remain insulated from internal storage changes.

Sleep
-----
- :func:`get_sleep_hours_by_day` — total hours per day (simple form)
- :func:`get_sleep_by_day`       — hours *and* wake time per day

Activity
--------
- :func:`get_activity_records`  — source-ranked records at any resolution
- :func:`get_activity_by_day`   — daily totals (convenience wrapper)

The sleep functions return values keyed by every day in the requested range:

* ``None``  — no records found (device not worn / data not synced)
* ``0.0``   — records exist but the computed value is zero
* float     — computed value

:func:`get_sleep_by_day` additionally carries the wake time (end of the last
sleep interval, capped at the day boundary, not rounded).

:func:`get_activity_records` and :func:`get_activity_by_day` require
PostgreSQL (window-function CTEs for source-ranked deduplication).
Sleep functions work with any Django-supported backend.

Ranking / source utilities
--------------------------
- :func:`ensure_ranks` — ensure one
  :class:`~healthdatamodel.models.DataSourceRanking` row exists per data
  source for a customer; called automatically by the activity functions.
- :func:`has_competing_sources` — check whether records from other sources
  exist in a window; useful on the ingest path to decide whether
  source-ranked deduplication is needed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any, NamedTuple

from django.db import connection
from django.db.models import Case, F, FloatField, IntegerField, Q, Value, When, Window
from django.db.models.expressions import Func
from django.db.models.functions import Cast, Rank, Replace

from healthdatamodel.constants import ConnectionStatus, DataSource
from healthdatamodel.models import DataSourceRanking, Record, WearableConnection

_DEFAULT_SLEEP_DEVICE_SORT_ORDER = ["oura", "whoop", "apple", "garmin"]


class DailySleep(NamedTuple):
    """Sleep result for a single calendar day.

    Attributes
    ----------
    hours:
        Total hours of sleep within the day window (``None`` if no records).
    wake_time:
        End of the last sleep interval, capped at the day boundary.
        ``None`` when ``hours`` is ``None``.  Not rounded — apply
        ``round_up_15`` or similar in the caller if needed.
    """

    hours: float | None
    wake_time: datetime | None


class SleepValue(StrEnum):
    """HKCategoryValueSleepAnalysis* strings used as ``Record.value`` for sleep.

    Use these on both the insert and query side so the strings stay in sync.
    ``ASLEEP_*`` variants count as sleep; ``AWAKE`` and ``IN_BED`` do not.
    """

    ASLEEP_UNSPECIFIED = "HKCategoryValueSleepAnalysisAsleepUnspecified"
    ASLEEP_CORE = "HKCategoryValueSleepAnalysisAsleepCore"
    ASLEEP_DEEP = "HKCategoryValueSleepAnalysisAsleepDeep"
    ASLEEP_REM = "HKCategoryValueSleepAnalysisAsleepREM"
    AWAKE = "HKCategoryValueSleepAnalysisAwake"
    IN_BED = "HKCategoryValueSleepAnalysisInBed"


SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

_SLEEP_TYPE = SLEEP_TYPE
_SLEEP_VALUE_PREFIX = "HKCategoryValueSleepAnalysisAsleep"


class ActivityMetric(StrEnum):
    """HK type strings used as ``Record.type`` for activity metrics.

    Use these on both the insert and query side so the strings stay in sync.
    """

    ACTIVE_CALORIES = "HKQuantityTypeIdentifierActiveEnergyBurned"
    BASAL_CALORIES = "HKQuantityTypeIdentifierBasalEnergyBurned"
    STEPS = "HKQuantityTypeIdentifierStepCount"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Epoch(Func):
    """Extract duration as integer seconds (PostgreSQL-specific)."""

    template = "EXTRACT(epoch FROM %(expressions)s)::INTEGER"
    output_field = IntegerField()


def _day_window(day: date, boundary_hour: int) -> tuple[datetime, datetime]:
    end = datetime.combine(day, time(boundary_hour)).replace(tzinfo=timezone.utc)
    return end - timedelta(days=1), end


def _preferred_sleep_brand(customer: Any) -> str:
    """Return the preferred sleep device brand from WearableConnection, or ''."""
    conn = WearableConnection.objects.filter(
        customer=customer,
        preferred_for_sleep=True,
        status=ConnectionStatus.ACTIVE,
    ).first()
    if conn:
        return conn.device_brand
    conn = WearableConnection.objects.filter(
        customer=customer,
        status=ConnectionStatus.ACTIVE,
    ).order_by("connected_at").first()
    return conn.device_brand if conn else ""


def _sleep_for_day(customer: Any, day: date, boundary_hour: int) -> DailySleep:
    start_time, end_time = _day_window(day, boundary_hour)

    sleep_qs = Record.objects.filter(
        customer=customer,
        type=_SLEEP_TYPE,
        value__startswith=_SLEEP_VALUE_PREFIX,
    ).filter(
        Q(startDate__lt=end_time, startDate__gte=start_time)
        | Q(endDate__gt=start_time, endDate__lte=end_time)
        | Q(startDate__lt=start_time, endDate__gt=end_time)  # record spans entire window
    )

    most_recent = sleep_qs.order_by("admin_create_date").last()
    if most_recent is None:
        return DailySleep(hours=None, wake_time=None)

    upload_dt = most_recent.admin_create_date
    devices = list(
        sleep_qs.filter(admin_create_date=upload_dt)
        .values_list("sourceName", flat=True)
        .distinct()
    )

    if len(devices) > 1:
        preferred = _preferred_sleep_brand(customer)
        sort_order = _DEFAULT_SLEEP_DEVICE_SORT_ORDER + sorted(d.lower() for d in devices)
        if preferred:
            sort_order = [preferred.lower()] + sort_order
        devices = sorted(
            devices,
            key=lambda d: sort_order.index(d.lower())
            if d.lower() in sort_order
            else len(sort_order),
        )

    records_for_device = sleep_qs.filter(admin_create_date=upload_dt, sourceName=devices[0])
    pairs = {(r.startDate, r.endDate) for r in records_for_device}
    minutes = int(
        sum(
            (min(end, end_time) - max(start, start_time)).total_seconds() for start, end in pairs
        )
        // 60
    )
    wake_time = min(max(end for _, end in pairs), end_time)
    return DailySleep(hours=minutes / 60.0, wake_time=wake_time)


def _active_data_source(customer: Any) -> str:
    """Return the data_source of the most-recently-connected active
    WearableConnection, or ''.

    Ordering matches the convention used by callers such as
    ``Customer._active_connection``: when multiple active connections exist,
    the most recently connected one wins, with ``pk`` as a stable tiebreaker.
    """
    conn = WearableConnection.objects.filter(
        customer=customer,
        status=ConnectionStatus.ACTIVE,
    ).order_by("-connected_at", "-pk").first()
    return conn.data_source if conn else ""


# ---------------------------------------------------------------------------
# Public API — ranking / source utilities
# ---------------------------------------------------------------------------


def has_competing_sources(
    customer: Any,
    source: str,
    start: datetime,
    end: datetime,
) -> bool:
    """Return ``True`` if records from any source *other than* ``source`` exist
    for *customer* in the window ``[start, end)``.

    Useful on the ingest path: after bulk-inserting records from one source,
    call this to decide whether source-ranked deduplication is needed before
    computing derived stats.  If it returns ``False``, the just-inserted
    in-memory records can be used directly.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    source:
        The data source whose records were just inserted (e.g.
        ``DataSource.APPLE_HEALTH``).  Records matching this source are
        excluded from the check.
    start:
        Inclusive window start (datetime, UTC).
    end:
        Exclusive window end (datetime, UTC).
    """
    return Record.objects.filter(
        ~Q(source=source),
        customer=customer,
        startDate__gte=start,
        endDate__lte=end,
    ).exists()


def ensure_ranks(customer: Any) -> None:
    """Ensure one :class:`~healthdatamodel.models.DataSourceRanking` row per
    :class:`~healthdatamodel.constants.DataSource` value exists for *customer*,
    with the customer's active data source ranked first.

    Considers ranks valid only when: correct count, correct sources, ranks
    ``1..N`` in order, *and* the preferred source is already first.  Otherwise
    all existing rows are deleted and rebuilt.
    """
    n_sources = len(DataSource.values)
    existing = list(DataSourceRanking.objects.filter(customer=customer).order_by("rank"))
    preferred = _active_data_source(customer)
    valid = (
        len(existing) == n_sources
        and {x.dataSource for x in existing} == set(DataSource.values)
        and [x.rank for x in existing] == list(range(1, n_sources + 1))
        and (not preferred or existing[0].dataSource == preferred)
    )
    if valid:
        return

    if existing:
        DataSourceRanking.objects.filter(customer=customer).delete()

    order: list[str] = list(DataSource.values)
    if preferred and preferred in order:
        order.remove(preferred)
        order = [preferred] + order

    DataSourceRanking.objects.bulk_create(
        [
            DataSourceRanking(customer=customer, dataSource=ds, rank=i + 1)
            for i, ds in enumerate(order)
        ]
    )


# ---------------------------------------------------------------------------
# Public API — sleep
# ---------------------------------------------------------------------------


def get_sleep_by_day(
    customer: Any,
    start: date,
    end: date,
    day_boundary_hour: int = 14,
) -> dict[date, DailySleep]:
    """
    Return sleep hours and wake time for each day in ``[start, end]`` inclusive.

    The sleep window for each calendar day runs from
    ``(day_boundary_hour - 24h)`` to ``day_boundary_hour`` UTC.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    start:
        First day of the range (inclusive).
    end:
        Last day of the range (inclusive).
    day_boundary_hour:
        UTC hour that marks the end of each sleep day (default 14 → 2 pm).

    Returns
    -------
    dict[date, DailySleep]
        Each value is a :class:`DailySleep` named tuple:

        * ``hours`` — sleep hours (``None`` if no records, ``0.0`` if zero)
        * ``wake_time`` — end of last sleep interval capped at day boundary,
          or ``None`` if no records.  Not rounded.
    """
    return {
        start + timedelta(days=i): _sleep_for_day(
            customer, start + timedelta(days=i), day_boundary_hour
        )
        for i in range((end - start).days + 1)
    }


def get_sleep_hours_by_day(
    customer: Any,
    start: date,
    end: date,
    day_boundary_hour: int = 14,
) -> dict[date, float | None]:
    """
    Return sleep hours for each day in ``[start, end]`` inclusive.

    Convenience wrapper around :func:`get_sleep_by_day` that drops the wake
    time.  Use :func:`get_sleep_by_day` directly when the wake time is needed.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    start:
        First day of the range (inclusive).
    end:
        Last day of the range (inclusive).
    day_boundary_hour:
        UTC hour that marks the end of each sleep day (default 14 → 2 pm).

    Returns
    -------
    dict[date, float | None]
        * ``None``  — no sleep records found for that night window
        * ``0.0``   — records found but they cover zero minutes of sleep
        * float     — sleep hours (e.g. ``7.5`` for 7 h 30 m)
    """
    return {day: result.hours for day, result in get_sleep_by_day(customer, start, end, day_boundary_hour).items()}


# ---------------------------------------------------------------------------
# Public API — activity
# ---------------------------------------------------------------------------


def get_activity_records(
    customer: Any,
    metric: ActivityMetric,
    start: datetime,
    end: datetime,
    resolution_minutes: int = 15,
) -> list[tuple[datetime, datetime, float]]:
    """
    Return source-ranked deduplicated activity records at *resolution_minutes*.

    Each element is ``(startDate, endDate, value)``, ordered by ``startDate``.
    Only intervals where a record exists are returned — gaps are **not**
    filled.  The caller is responsible for gap-filling if a complete time
    series is required.

    Rankings are initialised automatically via :func:`ensure_ranks` if not
    already present.

    .. note::
        This function requires PostgreSQL.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    metric:
        Which health metric to query (see :class:`ActivityMetric`).
    start:
        Inclusive start of the query window (datetime, UTC).
    end:
        Exclusive end of the query window (datetime, UTC).
    resolution_minutes:
        Duration of each record in minutes (default 15).  Use 1440 for
        daily records.

    Returns
    -------
    list[tuple[datetime, datetime, float]]
        ``(startDate, endDate, value)`` tuples, sorted ascending by start.
        Values are in kcal for calorie metrics and count for steps.
    """
    ensure_ranks(customer)

    sql, params = (
        Record.objects.annotate(
            source_source=F("customer__datasourceranking__dataSource"),
            source_rank=F("customer__datasourceranking__rank"),
            source_rank_rank=Window(
                expression=Rank(),
                partition_by=["customer", "startDate", "endDate", "type"],  # type: ignore[list-item]
                order_by=F("source_rank").asc(),
            ),
            source_update_rank=Window(
                expression=Rank(),
                partition_by=["customer", "startDate", "endDate", "type", "source"],  # type: ignore[list-item]
                order_by=F("admin_create_date").desc(),
            ),
            resolution=_Epoch(F("endDate") - F("startDate")),
            value_point=Replace(F("value"), Value(","), Value(".")),
            value_num=Case(
                When(
                    Q(unit="cal") | Q(unit="calories"),
                    then=Cast(F("value_point"), output_field=FloatField()) / 1000,
                ),
                default=Cast(F("value_point"), output_field=FloatField()),
            ),
            value_nonneg=Case(
                When(Q(value_num__lt=0), then=Value(0.0)),
                default=F("value_num"),
            ),
        )
        .filter(
            Q(source=F("source_source")),
            Q(resolution=resolution_minutes * 60),
            Q(customer=customer),
            Q(startDate__gte=start),
            Q(endDate__lte=end),
            Q(type=metric.value),
        )
        .values("startDate", "endDate", "value_nonneg", "source_rank_rank", "source_update_rank")
        .query.sql_with_params()
    )

    with connection.cursor() as cursor:
        cursor.execute(
            """WITH ranked AS ({})
SELECT
    r."startDate",
    r."endDate",
    r."value_nonneg"
FROM ranked r
WHERE r."source_rank_rank" = %s AND r."source_update_rank" = %s
ORDER BY r."startDate" """.format(sql),
            [*params, 1, 1],
        )
        rows = cursor.fetchall()

    return [(row[0], row[1], float(row[2])) for row in rows]


def get_activity_by_day(
    customer: Any,
    metric: ActivityMetric,
    start: date,
    end: date,
) -> dict[date, float | None]:
    """
    Return the daily total for *metric* for each day in ``[start, end]`` inclusive.

    Uses source-ranked deduplication: when a customer has records from more
    than one data source for the same day, only the highest-ranked source is
    counted.  Rankings are initialised automatically if not already present.

    Delegates to :func:`get_activity_records` with ``resolution_minutes=1440``.

    .. note::
        This function requires PostgreSQL.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    metric:
        Which health metric to aggregate (see :class:`ActivityMetric`).
    start:
        First day of the range (inclusive).
    end:
        Last day of the range (inclusive).

    Returns
    -------
    dict[date, float | None]
        * ``None``  — no records found for that day
        * ``0.0``   — records exist but the daily total is zero
        * float     — daily total (kcal for calorie metrics, count for steps)
    """
    start_dt = datetime.combine(start, time(0)).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), time(0)).replace(tzinfo=timezone.utc)

    records = get_activity_records(customer, metric, start_dt, end_dt, resolution_minutes=1440)

    raw: dict[date, float] = defaultdict(float)
    for start_ts, _end_ts, value in records:
        raw[start_ts.date()] += value

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {day: raw.get(day) for day in days}


def get_activity_by_day_from_records(
    records: list[Any],
    metric: ActivityMetric,
    start: date,
    end: date,
) -> dict[date, float | None]:
    """Return daily activity totals aggregated from in-memory *records*.

    Equivalent to :func:`get_activity_by_day` but operates on a list of
    :class:`~healthdatamodel.schemas.RecordInput` objects already in memory,
    avoiding a database round-trip.

    Designed for the fast path after single-source ingest when
    ``has_competing_sources`` would return ``False``.  If multiple sources are
    present, values are summed without source-ranking deduplication, which may
    over-count; use the database query path in that case.

    Parameters
    ----------
    records:
        List of :class:`~healthdatamodel.schemas.RecordInput` objects
        (typically the return value of :func:`~healthdatamodel.ingest.expand_compact_activity`).
    metric:
        Which health metric to aggregate.
    start:
        First day of the range (inclusive).
    end:
        Last day of the range (inclusive).

    Returns
    -------
    dict[date, float | None]
        * ``None``  — no records found for that day
        * ``0.0``   — records exist but the daily total is zero
        * float     — daily total
    """
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
