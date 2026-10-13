"""
High-level query API for health data.

Instead of querying ``Record`` objects directly, callers should use the
functions here to remain insulated from internal storage changes.

Sleep
-----
- :func:`get_sleep_hours_by_day` — total hours per day (simple form)
- :func:`get_sleep_by_day`       — hours *and* wake time per day
- :func:`get_sleep_intervals_by_day` — every stage interval of the chosen
  device per day (:class:`SleepInterval`)
- :func:`get_sleep_summary_by_day` — sleep start/end, efficiency,
  awakenings, naps per day (:class:`DailySleepSummary`)
- :func:`get_sleep_regularity_index` — Sleep Regularity Index / consistency
  over the last few days (:class:`SleepRegularity`)

The pure calculations behind the last two live in :mod:`healthdatamodel.sleep`
(:func:`summarize_sleep_intervals`, :func:`sleep_regularity_from_intervals`)
and are re-exported here.

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

import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, NamedTuple

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


from django.db import connection
from django.db.models import Case, F, FloatField, IntegerField, Q, Value, When, Window
from django.db.models.expressions import Func
from django.db.models.functions import Cast, Rank, Replace

from healthdatamodel.compact import read_compact_enabled
from healthdatamodel.constants import (
    ASLEEP_STAGES,
    HK_TYPE_TO_COMPACT_METRIC,
    MINUTES_PER_DAY,
    SLEEP_ASLEEP_VALUE_PREFIX,
    SLEEP_TYPE,
    ConnectionStatus,
    DataSource,
    sleep_stage_for_value,
)
from healthdatamodel.models import (
    ActivityDay,
    DataSourceRanking,
    Record,
    SleepDay,
    WearableConnection,
)
from healthdatamodel.sleep import (
    DailySleepSummary,
    SleepInterval,
    SleepRegularity,
    sleep_regularity_from_intervals,
    summarize_sleep_intervals,
)

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


# SLEEP_TYPE is defined in constants and re-exported here for compatibility.
_SLEEP_TYPE = SLEEP_TYPE
_SLEEP_VALUE_PREFIX = SLEEP_ASLEEP_VALUE_PREFIX


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
    """Return the preferred sleep device brand from WearableConnection, or ''.

    An active connection flagged ``preferred_for_sleep`` (with a non-blank
    brand) wins; otherwise fall back to the primary connection — the most
    recently connected active row, matching ``WearableConnection.active_for``.
    """
    active = WearableConnection.objects.filter(
        customer=customer,
        status=ConnectionStatus.ACTIVE,
    ).order_by("-connected_at", "-pk")
    conn = active.filter(preferred_for_sleep=True).exclude(device_brand="").first()
    if conn is None:
        conn = active.first()
    return conn.device_brand if conn else ""


def _rank_sleep_devices(customer: Any, devices: list[str]) -> list[str]:
    """Order *devices* by sleep preference (best first).

    Preference: the customer's preferred sleep brand (see
    :func:`_preferred_sleep_brand`), then the default order (oura, whoop, apple, garmin), then
    alphabetical.
    """
    if len(devices) <= 1:
        return devices
    preferred = _preferred_sleep_brand(customer)
    sort_order = _DEFAULT_SLEEP_DEVICE_SORT_ORDER + sorted(d.lower() for d in devices)
    if preferred:
        sort_order = [preferred.lower()] + sort_order
    return sorted(
        devices,
        key=lambda d: (
            sort_order.index(d.lower()) if d.lower() in sort_order else len(sort_order)
        ),
    )


class _SleepSelection(NamedTuple):
    """The upload + device chosen for one sleep-day window.

    ``intervals`` holds every stage interval (unclipped, deduplicated) of
    that device and upload which overlaps ``[window_start, window_end)``.
    Shared by :func:`get_sleep_by_day` and :func:`get_sleep_intervals_by_day`
    so the two can never choose differently.
    """

    device: str
    window_start: datetime
    window_end: datetime
    intervals: list[tuple[datetime, datetime, str]]


def _select_sleep_legacy(
    customer: Any, day: date, boundary_hour: int
) -> _SleepSelection | None:
    """Legacy (``Record``) selection: latest upload among asleep records
    overlapping the window, then the best-ranked device of that upload."""
    start_time, end_time = _day_window(day, boundary_hour)
    overlaps_window = (
        Q(startDate__lt=end_time, startDate__gte=start_time)
        | Q(endDate__gt=start_time, endDate__lte=end_time)
        | Q(startDate__lt=start_time, endDate__gt=end_time)  # spans entire window
    )

    sleep_qs = Record.objects.filter(
        customer=customer,
        type=_SLEEP_TYPE,
        value__startswith=_SLEEP_VALUE_PREFIX,
    ).filter(overlaps_window)

    most_recent = sleep_qs.order_by("admin_create_date").last()
    if most_recent is None:
        return None

    upload_dt = most_recent.admin_create_date
    devices = list(
        sleep_qs.filter(admin_create_date=upload_dt)
        .values_list("sourceName", flat=True)
        .distinct()
    )
    device = _rank_sleep_devices(customer, devices)[0]

    # Every stage of the chosen device and upload, not just asleep ones.
    records = (
        Record.objects.filter(
            customer=customer,
            type=_SLEEP_TYPE,
            admin_create_date=upload_dt,
            sourceName=device,
        )
        .filter(overlaps_window)
        .values_list("startDate", "endDate", "value")
    )
    intervals = set()
    for record_start, record_end, value in records:
        stage = sleep_stage_for_value(value)
        if stage is not None:
            intervals.add((record_start, record_end, str(stage.value)))
    return _SleepSelection(device, start_time, end_time, sorted(intervals))


def _parse_sleep_intervals(
    row_intervals: list[Any],
) -> list[tuple[datetime, datetime, str]]:
    """Parse a SleepDay ``intervals`` JSON list into (start, end, stage)."""
    return [
        (datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso), stage)
        for start_iso, end_iso, stage in row_intervals
    ]


def _select_sleep_compact(
    customer: Any, start: date, end: date, boundary_hour: int
) -> dict[date, _SleepSelection | None]:
    """Compact-table (``SleepDay``) selection for every day in the range.

    Stored rows are canonicalised to the 14:00 UTC boundary; because their
    interval pieces exactly tile the original records, any requested
    ``boundary_hour`` can be served by clipping pieces from the (at most two)
    stored days overlapping each requested window.
    """
    rows = list(
        SleepDay.objects.filter(
            customer=customer,
            day__gte=start - timedelta(days=1),
            day__lte=end + timedelta(days=1),
        )
    )
    parsed = [(row, _parse_sleep_intervals(row.intervals)) for row in rows]

    results: dict[date, _SleepSelection | None] = {}
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        window_start, window_end = _day_window(day, boundary_hour)

        def overlapping(
            intervals: list[tuple[datetime, datetime, str]],
            window_start: datetime = window_start,
            window_end: datetime = window_end,
        ) -> list[tuple[datetime, datetime, str]]:
            return [
                (s, e, stage)
                for s, e, stage in intervals
                if max(s, window_start) < min(e, window_end)
            ]

        # Candidate rows: any asleep interval strictly overlapping the window.
        candidates = [
            row
            for row, intervals in parsed
            if any(stage in ASLEEP_STAGES for _, _, stage in overlapping(intervals))
        ]
        if not candidates:
            results[day] = None
            continue

        # Latest upload wins, mirroring the legacy per-window behaviour.
        upload_dt = max(row.admin_create_date for row in candidates)
        devices = _rank_sleep_devices(
            customer,
            sorted(
                {row.device for row in candidates if row.admin_create_date == upload_dt}
            ),
        )
        device = devices[0]
        chosen = {
            entry
            for row, intervals in parsed
            if row.admin_create_date == upload_dt and row.device == device
            for entry in overlapping(intervals)
        }
        results[day] = _SleepSelection(device, window_start, window_end, sorted(chosen))
    return results


def _select_sleep(
    customer: Any, start: date, end: date, boundary_hour: int
) -> dict[date, _SleepSelection | None]:
    """Per-day upload/device selection, dispatching on the read mode."""
    if read_compact_enabled():
        return _select_sleep_compact(customer, start, end, boundary_hour)
    return {
        start + timedelta(days=i): _select_sleep_legacy(
            customer, start + timedelta(days=i), boundary_hour
        )
        for i in range((end - start).days + 1)
    }


def _daily_sleep(selection: _SleepSelection | None) -> DailySleep:
    """Hours + wake time from a selection (the :func:`get_sleep_by_day` math).

    Asleep pairs are deduplicated by ``(start, end)`` and summed without
    merging overlaps, exactly as before the selection refactor.
    """
    if selection is None:
        return DailySleep(hours=None, wake_time=None)
    window_start, window_end = selection.window_start, selection.window_end
    pairs = {(s, e) for s, e, stage in selection.intervals if stage in ASLEEP_STAGES}
    minutes = int(
        sum(
            (min(e, window_end) - max(s, window_start)).total_seconds()
            for s, e in pairs
        )
        // 60
    )
    wake_time = min(max(e for _, e in pairs), window_end)
    return DailySleep(hours=minutes / 60.0, wake_time=wake_time)


def _clipped_intervals(selection: _SleepSelection | None) -> list[SleepInterval]:
    """All stages of a selection clipped to its window, sorted by start."""
    if selection is None:
        return []
    clipped = {
        (max(s, selection.window_start), min(e, selection.window_end), stage)
        for s, e, stage in selection.intervals
    }
    return [
        SleepInterval(start=s, end=e, stage=stage, device=selection.device)
        for s, e, stage in sorted(clipped)
        if s < e
    ]


def _active_data_source(customer: Any) -> str:
    """Return the data_source of the most-recently-connected active
    WearableConnection, or ''.

    Ordering matches the convention used by callers such as
    ``Customer._active_connection``: when multiple active connections exist,
    the most recently connected one wins, with ``pk`` as a stable tiebreaker.
    """
    conn = (
        WearableConnection.objects.filter(
            customer=customer,
            status=ConnectionStatus.ACTIVE,
        )
        .order_by("-connected_at", "-pk")
        .first()
    )
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
    if read_compact_enabled():
        return _has_competing_sources_compact(customer, source, start, end)
    return Record.objects.filter(
        ~Q(source=source),
        customer=customer,
        startDate__gte=start,
        endDate__lte=end,
    ).exists()


def _has_competing_sources_compact(
    customer: Any, source: str, start: datetime, end: datetime
) -> bool:
    """Compact-table implementation of :func:`has_competing_sources`.

    Mirrors the legacy check (records fully inside ``[start, end]``): an
    activity slot or sleep interval piece from another source contained in
    the window counts as competition.
    """
    day_lo = start.date()
    day_hi = end.date() + timedelta(days=1)

    activity_rows = ActivityDay.objects.filter(
        ~Q(source=source),
        customer=customer,
        day__gte=day_lo - timedelta(days=1),
        day__lte=day_hi,
    ).values_list("day", "resolution_minutes", "values")
    for day, resolution, values in activity_rows:
        day_start = datetime.combine(day, time(0)).replace(tzinfo=timezone.utc)
        for index, value in enumerate(values):
            if value is None:
                continue
            slot_start = day_start + timedelta(minutes=index * resolution)
            slot_end = slot_start + timedelta(minutes=resolution)
            if slot_start >= start and slot_end <= end:
                return True

    sleep_rows = SleepDay.objects.filter(
        ~Q(source=source),
        customer=customer,
        day__gte=day_lo - timedelta(days=1),
        day__lte=day_hi,
    ).values_list("intervals", flat=True)
    for intervals in sleep_rows:
        for start_iso, end_iso, _stage in intervals:
            piece_start = datetime.fromisoformat(start_iso)
            piece_end = datetime.fromisoformat(end_iso)
            if piece_start >= start and piece_end <= end:
                return True
    return False


def ensure_ranks(customer: Any) -> None:
    """Ensure one :class:`~healthdatamodel.models.DataSourceRanking` row per
    :class:`~healthdatamodel.constants.DataSource` value exists for *customer*,
    with the customer's active data source ranked first.

    Considers ranks valid only when: correct count, correct sources, ranks
    ``1..N`` in order, *and* the preferred source is already first.  Otherwise
    all existing rows are deleted and rebuilt.
    """
    n_sources = len(DataSource.values)
    existing = list(
        DataSourceRanking.objects.filter(customer=customer).order_by("rank")
    )
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
        day: _daily_sleep(selection)
        for day, selection in _select_sleep(
            customer, start, end, day_boundary_hour
        ).items()
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
    return {
        day: result.hours
        for day, result in get_sleep_by_day(
            customer, start, end, day_boundary_hour
        ).items()
    }


def get_sleep_intervals_by_day(
    customer: Any,
    start: date,
    end: date,
    day_boundary_hour: int = 14,
) -> dict[date, list[SleepInterval]]:
    """
    Return every stage interval of each night's chosen device and upload.

    The upload and device are chosen exactly as in :func:`get_sleep_by_day`
    (same internal routine): the latest ``admin_create_date`` among asleep
    records overlapping the window, then the device ranking.  All stages of
    that device and upload (asleep stages, ``awake``, ``in_bed``) are
    returned, clipped to the window and sorted by start.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    start, end:
        Day range (inclusive).
    day_boundary_hour:
        UTC hour that marks the end of each sleep day (default 14 → 2 pm).

    Returns
    -------
    dict[date, list[SleepInterval]]
        One key per day in the range; an empty list when the night has no
        asleep records.
    """
    return {
        day: _clipped_intervals(selection)
        for day, selection in _select_sleep(
            customer, start, end, day_boundary_hour
        ).items()
    }


def get_sleep_summary_by_day(
    customer: Any,
    start: date,
    end: date,
    day_boundary_hour: int = 14,
    tz: tzinfo | None = None,
    main_sleep_gap_minutes: int = 120,
) -> dict[date, DailySleepSummary]:
    """
    Return a :class:`DailySleepSummary` for each day in ``[start, end]``.

    Runs :func:`~healthdatamodel.sleep.summarize_sleep_intervals` on the
    output of :func:`get_sleep_intervals_by_day`; see that function for the
    definitions of main sleep, time in bed, efficiency, awakenings and naps.

    Parameters
    ----------
    tz:
        Time zone for ``sleep_start`` / ``sleep_end`` (default UTC).  Only
        the presentation changes; the day windows stay in UTC.
    main_sleep_gap_minutes:
        Gap (minutes) that splits the night into separate sessions.

    Returns
    -------
    dict[date, DailySleepSummary]
        Every numeric field is ``None`` for days without asleep records.
    """
    results = {}
    for day, intervals in get_sleep_intervals_by_day(
        customer, start, end, day_boundary_hour
    ).items():
        window_start, window_end = _day_window(day, day_boundary_hour)
        results[day] = summarize_sleep_intervals(
            intervals,
            window_start,
            window_end,
            main_sleep_gap_minutes=main_sleep_gap_minutes,
            tz=tz,
        )
    return results


def get_sleep_regularity_index(
    customer: Any,
    end_day: date,
    days: int = 4,
    day_boundary_hour: int = 14,
) -> SleepRegularity:
    """
    Sleep Regularity Index over the *days* sleep days ending at *end_day*.

    Fetches the asleep intervals of each night's chosen device via
    :func:`get_sleep_intervals_by_day` and applies
    :func:`~healthdatamodel.sleep.sleep_regularity_from_intervals` (Phillips
    et al. 2017; WHOOP-style 4-day consistency).  Nights without records are
    excluded rather than counted as awake.  ``days`` must be ≥ 2; with the
    default of 4 at most ``3 × 1440`` minute pairs are compared.  ``sri`` is
    ``None`` when fewer than two nights have data or no pairs exist.
    """
    if days < 2:
        raise ValueError("days must be at least 2")
    intervals = get_sleep_intervals_by_day(
        customer, end_day - timedelta(days=days - 1), end_day, day_boundary_hour
    )
    return sleep_regularity_from_intervals(
        intervals, end_day, days=days, day_boundary_hour=day_boundary_hour
    )


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
    if read_compact_enabled():
        return _get_activity_records_compact(
            customer, metric, start, end, resolution_minutes
        )
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
        .values(
            "startDate",
            "endDate",
            "value_nonneg",
            "source_rank_rank",
            "source_update_rank",
        )
        .query.sql_with_params()
    )

    with connection.cursor() as cursor:
        cursor.execute(
            f"""WITH ranked AS ({sql})
SELECT
    r."startDate",
    r."endDate",
    r."value_nonneg"
FROM ranked r
WHERE r."source_rank_rank" = %s AND r."source_update_rank" = %s
ORDER BY r."startDate" """,
            [*params, 1, 1],
        )
        rows = cursor.fetchall()

    return [(row[0], row[1], float(row[2])) for row in rows]


def _get_activity_records_compact(
    customer: Any,
    metric: ActivityMetric,
    start: datetime,
    end: datetime,
    resolution_minutes: int,
) -> list[tuple[datetime, datetime, float]]:
    """Compact-table implementation of :func:`get_activity_records`.

    Pure Python over per-day vectors — works on any backend.  Per slot, the
    best-ranked source with data wins; devices within the winning source are
    summed (mirroring the legacy rank-tie behaviour).  Note the compact
    tables only hold resolutions that divide a day evenly.
    """
    compact_metric = HK_TYPE_TO_COMPACT_METRIC.get(metric.value)
    if compact_metric is None or MINUTES_PER_DAY % resolution_minutes != 0:
        return []
    if end <= start:
        return []
    ensure_ranks(customer)
    rank_by_source = dict(
        DataSourceRanking.objects.filter(customer=customer).values_list(
            "dataSource", "rank"
        )
    )

    rows = ActivityDay.objects.filter(
        customer=customer,
        metric=compact_metric.value,
        resolution_minutes=resolution_minutes,
        day__gte=start.date(),
        day__lte=(end - timedelta(microseconds=1)).date(),
    ).values_list("source", "day", "values")

    # (day, slot) -> {source: summed value across devices}
    slots: dict[tuple[date, int], dict[str, float]] = defaultdict(dict)
    for source, day, values in rows:
        if source not in rank_by_source:
            continue  # legacy path drops sources without a ranking row
        day_start = datetime.combine(day, time(0)).replace(tzinfo=timezone.utc)
        for index, value in enumerate(values):
            if value is None:
                continue
            slot_start = day_start + timedelta(minutes=index * resolution_minutes)
            slot_end = slot_start + timedelta(minutes=resolution_minutes)
            if slot_start < start or slot_end > end:
                continue
            by_source = slots[(day, index)]
            by_source[source] = by_source.get(source, 0.0) + float(value)

    results: list[tuple[datetime, datetime, float]] = []
    for day, index in sorted(slots):
        by_source = slots[(day, index)]
        best_source = min(by_source, key=lambda s: rank_by_source[s])
        slot_start = datetime.combine(day, time(0)).replace(
            tzinfo=timezone.utc
        ) + timedelta(minutes=index * resolution_minutes)
        slot_end = slot_start + timedelta(minutes=resolution_minutes)
        results.append((slot_start, slot_end, by_source[best_source]))
    return results


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
    end_dt = datetime.combine(end + timedelta(days=1), time(0)).replace(
        tzinfo=timezone.utc
    )

    records = get_activity_records(
        customer, metric, start_dt, end_dt, resolution_minutes=1440
    )

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
