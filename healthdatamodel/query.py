"""
High-level query API for health data.

Instead of querying ``Record`` objects directly, callers should use the
functions here to remain insulated from internal storage changes.

Sleep
-----
- :func:`get_sleep_hours_by_day`

Activity
--------
- :func:`get_activity_by_day`

Both return ``dict[date, float | None]`` keyed by every day in the requested
range:

* ``None``  — no records found for that day (device not worn / data not synced)
* ``0.0``   — records exist but the computed value is zero
* float     — the computed daily value

The activity query requires PostgreSQL (it uses window-function CTEs for
source-ranked deduplication).  Sleep works with any Django-supported backend.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any

from django.db import connection
from django.db.models import Case, F, FloatField, IntegerField, Q, Value, When, Window
from django.db.models.expressions import Func
from django.db.models.functions import Cast, Rank, Replace

from healthdatamodel.constants import ConnectionStatus, DataSource
from healthdatamodel.models import DataSourceRanking, Record, WearableConnection

_SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_SLEEP_VALUE_PREFIX = "HKCategoryValueSleepAnalysisAsleep"

# Preference order when a customer has no explicit preferred-sleep device set
_DEFAULT_SLEEP_DEVICE_SORT_ORDER = ["oura", "whoop", "apple", "garmin"]


class ActivityMetric(StrEnum):
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


def _sleep_hours_for_day(customer: Any, day: date, boundary_hour: int) -> float | None:
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
        return None

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
    return minutes / 60.0


def _active_data_source(customer: Any) -> str:
    """Return the data_source of the first active WearableConnection, or ''."""
    conn = WearableConnection.objects.filter(
        customer=customer,
        status=ConnectionStatus.ACTIVE,
    ).order_by("connected_at").first()
    return conn.data_source if conn else ""


def _ensure_ranks(customer: Any) -> None:
    """Ensure one DataSourceRanking row per DataSource value exists for *customer*."""
    n_sources = len(DataSource.values)
    existing = list(DataSourceRanking.objects.filter(customer=customer).order_by("rank"))
    valid = (
        len(existing) == n_sources
        and {x.dataSource for x in existing} == set(DataSource.values)
        and [x.rank for x in existing] == list(range(1, n_sources + 1))
    )
    if valid:
        return

    if existing:
        DataSourceRanking.objects.filter(customer=customer).delete()

    preferred = _active_data_source(customer)
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


def _get_daily_totals(
    customer: Any,
    record_type: str,
    start: date,
    end: date,
) -> dict[date, float]:
    """
    Return ``{date: sum_of_values}`` for daily-resolution records using
    source-ranked deduplication.  Requires PostgreSQL.
    """
    _ensure_ranks(customer)

    start_dt = datetime.combine(start, time(0)).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), time(0)).replace(tzinfo=timezone.utc)

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
            Q(resolution=1440 * 60),
            Q(customer=customer),
            Q(startDate__gte=start_dt),
            Q(endDate__lte=end_dt),
            Q(type=record_type),
        )
        .values("startDate", "endDate", "value_nonneg", "source_rank_rank", "source_update_rank")
        .query.sql_with_params()
    )

    with connection.cursor() as cursor:
        cursor.execute(
            """WITH ranked AS ({})
SELECT
    DATE(r."startDate") AS day,
    SUM(r."value_nonneg") AS total
FROM ranked r
WHERE r."source_rank_rank" = %s AND r."source_update_rank" = %s
GROUP BY DATE(r."startDate")""".format(sql),
            [*params, 1, 1],
        )
        rows = cursor.fetchall()

    return {row[0]: float(row[1]) for row in rows}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_sleep_hours_by_day(
    customer: Any,
    start: date,
    end: date,
    day_boundary_hour: int = 14,
) -> dict[date, float | None]:
    """
    Return sleep hours for each day in ``[start, end]`` inclusive.

    The sleep window for each calendar day runs from
    ``(day_boundary_hour - 24h)`` to ``day_boundary_hour`` UTC.
    The default boundary of 14 (2 pm) means the window is
    2 pm the previous calendar day → 2 pm the current calendar day.

    Device preference is read from :class:`~healthdatamodel.models.WearableConnection`.
    When a customer has multiple simultaneous sleep-data sources, the record
    set from the customer's preferred-sleep device (or the highest-priority
    device in the default order oura → whoop → apple → garmin) is used.

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
    return {
        start + timedelta(days=i): _sleep_hours_for_day(
            customer, start + timedelta(days=i), day_boundary_hour
        )
        for i in range((end - start).days + 1)
    }


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
    raw = _get_daily_totals(customer, metric.value, start, end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {day: raw.get(day) for day in days}
