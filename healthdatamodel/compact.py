"""
Compact storage core: routing records into SleepDay / ActivityDay rows.

This module holds the write-side logic shared by the live ingest path
(:mod:`healthdatamodel.ingest`) and the backfill path
(:mod:`healthdatamodel.compat`).  Both feed record-shaped objects (anything
with ``startDate``, ``endDate``, ``sourceName``, ``value``, ``unit`` and
``type`` attributes — ``RecordInput`` or ``Record`` instances) through
:func:`apply_compact`, which guarantees backfilled state is identical to what
live dual-writing would have produced.

Semantics (mirroring what the legacy query layer computes at read time):

Sleep
    Records are split at the 14:00 UTC sleep-day boundary and grouped by
    ``(device, sleep_day)``.  Every key present in an upload **replaces** the
    stored row — the write-side equivalent of "latest upload wins".

Activity
    Records at day-dividing resolutions are slotted into per-day vectors
    keyed by ``(device, metric, resolution, day)``.  Incoming slots
    **overwrite** stored slots; untouched slots keep their value — the
    write-side equivalent of "latest upload wins per interval".  Values are
    normalised once here (kcal, non-negative, steps as int, energy rounded
    to 0.1 kcal) so the read side is a straight sum.

Anything that cannot be represented compactly (unknown types, unknown sleep
values, non-grid-aligned intervals, unparseable values) is returned to the
caller as a leftover so it can be persisted to ``Record`` — nothing is ever
silently dropped.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from django.conf import settings as django_settings
from django.db import connections, router, transaction

from healthdatamodel.constants import (
    HK_SLEEP_VALUE_TO_STAGE,
    HK_TYPE_TO_COMPACT_METRIC,
    MINUTES_PER_DAY,
    SLEEP_DAY_BOUNDARY_HOUR,
    SLEEP_TYPE,
    CompactMetric,
)
from healthdatamodel.models import ActivityDay, SleepDay

logger = logging.getLogger(__name__)

# (device, sleep_day) -> [(start, end, stage), ...]
_SleepGroups = dict[tuple[str, date], list[tuple[datetime, datetime, str]]]
# (device, metric, resolution_minutes, day) -> {slot_index: value}
_ActivityGroups = dict[tuple[str, str, int, date], dict[int, float]]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def save_records_enabled() -> bool:
    return getattr(django_settings, "HEALTHDATAMODEL_SAVE_RECORDS", True)


def save_compact_enabled() -> bool:
    return getattr(django_settings, "HEALTHDATAMODEL_SAVE_COMPACT", True)


def read_compact_enabled() -> bool:
    return getattr(django_settings, "HEALTHDATAMODEL_READ_COMPACT", True)


# ---------------------------------------------------------------------------
# Sleep-day windows
# ---------------------------------------------------------------------------


def sleep_window(
    day: date, boundary_hour: int = SLEEP_DAY_BOUNDARY_HOUR
) -> tuple[datetime, datetime]:
    """Return the ``[start, end)`` UTC window for *day*."""
    end = datetime.combine(day, time(boundary_hour)).replace(tzinfo=timezone.utc)
    return end - timedelta(days=1), end


def sleep_day_for(dt: datetime) -> date:
    """Return the sleep day whose canonical window contains *dt*."""
    dt = dt.astimezone(timezone.utc)
    if dt.time() >= time(SLEEP_DAY_BOUNDARY_HOUR):
        return dt.date() + timedelta(days=1)
    return dt.date()


def split_sleep_interval(
    start: datetime, end: datetime
) -> Iterator[tuple[date, datetime, datetime]]:
    """Split ``[start, end)`` at canonical boundaries.

    Yields ``(sleep_day, clipped_start, clipped_end)`` pieces with positive
    duration.  Concatenated pieces cover exactly the original interval, so
    the query layer can re-window them for any ``day_boundary_hour``.
    """
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        return
    day = sleep_day_for(start)
    while True:
        window_start, window_end = sleep_window(day)
        piece_start = max(start, window_start)
        piece_end = min(end, window_end)
        if piece_end > piece_start:
            yield day, piece_start, piece_end
        if end <= window_end:
            return
        day += timedelta(days=1)


# ---------------------------------------------------------------------------
# Normalisation / slotting
# ---------------------------------------------------------------------------


def normalize_activity_value(
    metric: CompactMetric, raw: Any, unit: str | None
) -> float | int | None:
    """Normalise a record value for compact storage, or ``None`` if unparseable.

    Mirrors the legacy read-time pipeline (comma → point, cal → kcal,
    clamp negatives) and applies the write-time precision policy from the
    format analysis: steps as int, energy rounded to 0.1 kcal (the
    zero-information-loss boundary).
    """
    try:
        value = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if unit in ("cal", "calories"):
        value /= 1000.0
    value = max(0.0, value)
    if metric == CompactMetric.STEPS:
        return int(round(value))
    return round(value, 1)


def _slot_for(record: Any) -> tuple[int, int] | None:
    """Return ``(resolution_minutes, slot_index)`` for a grid-aligned record.

    ``None`` when the interval doesn't divide the day evenly or isn't
    aligned to its own resolution grid within the UTC day.
    """
    start = record.startDate.astimezone(timezone.utc)
    end = record.endDate.astimezone(timezone.utc)
    total_minutes = (end - start).total_seconds() / 60
    if total_minutes <= 0 or total_minutes != int(total_minutes):
        return None
    resolution = int(total_minutes)
    if MINUTES_PER_DAY % resolution != 0:
        return None
    day_start = datetime.combine(start.date(), time(0)).replace(tzinfo=timezone.utc)
    offset_minutes = (start - day_start).total_seconds() / 60
    offset = int(offset_minutes)
    if offset_minutes != offset or offset % resolution != 0:
        return None
    return resolution, offset // resolution


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_records(
    records: Iterable[Any],
) -> tuple[_SleepGroups, _ActivityGroups, list[Any]]:
    """Split *records* into sleep groups, activity groups, and leftovers."""
    sleep: _SleepGroups = defaultdict(list)
    activity: _ActivityGroups = defaultdict(dict)
    leftovers: list[Any] = []

    for record in records:
        device = record.sourceName
        if record.type == SLEEP_TYPE:
            stage = HK_SLEEP_VALUE_TO_STAGE.get(record.value)
            if stage is None:
                leftovers.append(record)
                continue
            for day, piece_start, piece_end in split_sleep_interval(
                record.startDate, record.endDate
            ):
                sleep[(device, day)].append((piece_start, piece_end, stage.value))
        elif record.type in HK_TYPE_TO_COMPACT_METRIC:
            metric = HK_TYPE_TO_COMPACT_METRIC[record.type]
            slot = _slot_for(record)
            value = normalize_activity_value(metric, record.value, record.unit)
            if slot is None or value is None:
                leftovers.append(record)
                continue
            resolution, index = slot
            day = record.startDate.astimezone(timezone.utc).date()
            slots = activity[(device, metric.value, resolution, day)]
            # Same-slot duplicates within one upload are summed, matching the
            # legacy query where tied rows all pass the rank filters.
            slots[index] = slots.get(index, 0) + value
        else:
            leftovers.append(record)

    return sleep, activity, leftovers


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _customer_pk(customer: Any) -> Any:
    return customer.pk if hasattr(customer, "pk") else customer


def _lock_if_supported(queryset):
    model = queryset.model
    if connections[router.db_for_write(model)].features.has_select_for_update:
        return queryset.select_for_update()
    return queryset


def _upsert_sleep(
    customer_pk: Any,
    source: str,
    groups: _SleepGroups,
    admin_create_date: datetime,
) -> None:
    queryset = _lock_if_supported(
        SleepDay.objects.filter(
            customer_id=customer_pk,
            device__in={key[0] for key in groups},
            day__in={key[1] for key in groups},
        )
    )
    existing = {(row.device, row.day): row for row in queryset}

    rows = []
    for (device, day), pieces in groups.items():
        entries = {
            (piece_start.isoformat(), piece_end.isoformat(), stage)
            for piece_start, piece_end, stage in pieces
        }
        stored = existing.get((device, day))
        if stored is not None and stored.admin_create_date == admin_create_date:
            # Same upload timestamp split across calls: one logical upload —
            # merge, mirroring the legacy per-window "all records at the
            # winning admin_create_date are counted".
            entries |= {tuple(entry) for entry in stored.intervals}
        # A different (newer) timestamp replaces the day wholesale.
        rows.append(
            SleepDay(
                customer_id=customer_pk,
                source=source,
                device=device,
                day=day,
                intervals=[list(entry) for entry in sorted(entries)],
                admin_create_date=admin_create_date,
            )
        )
    SleepDay.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["customer", "device", "day"],
        update_fields=["source", "intervals", "admin_create_date"],
    )


def _merge_activity(
    customer_pk: Any,
    source: str,
    groups: _ActivityGroups,
    admin_create_date: datetime,
) -> None:
    queryset = _lock_if_supported(
        ActivityDay.objects.filter(
            customer_id=customer_pk,
            source=source,
            device__in={key[0] for key in groups},
            metric__in={key[1] for key in groups},
            resolution_minutes__in={key[2] for key in groups},
            day__in={key[3] for key in groups},
        )
    )
    existing = {
        (row.device, row.metric, row.resolution_minutes, row.day): row
        for row in queryset
    }

    rows = []
    for (device, metric, resolution, day), slots in groups.items():
        length = MINUTES_PER_DAY // resolution
        stored = existing.get((device, metric, resolution, day))
        same_upload = (
            stored is not None and stored.admin_create_date == admin_create_date
        )
        values: list[float | int | None]
        if stored is not None and len(stored.values) == length:
            values = list(stored.values)
        else:
            values = [None] * length
        for index, value in slots.items():
            if same_upload and values[index] is not None:
                # Same upload timestamp split across calls: legacy returns
                # both tied rows, which day-level aggregation sums.
                value = values[index] + value
            values[index] = value if isinstance(value, int) else round(value, 1)
        rows.append(
            ActivityDay(
                customer_id=customer_pk,
                source=source,
                device=device,
                metric=metric,
                day=day,
                resolution_minutes=resolution,
                values=values,
                admin_create_date=admin_create_date,
            )
        )
    ActivityDay.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=[
            "customer",
            "source",
            "device",
            "metric",
            "day",
            "resolution_minutes",
        ],
        update_fields=["values", "admin_create_date"],
    )


def apply_compact(
    customer: Any,
    records: Iterable[Any],
    source: str,
    admin_create_date: datetime,
) -> list[Any]:
    """Write *records* to the compact tables; return uncompactable leftovers.

    *customer* may be a user instance or a primary key (the backfill path
    passes pks).  *records* may be ``RecordInput`` objects (live ingest) or
    ``Record`` model instances (backfill) — anything with the record-shaped
    attributes.
    """
    sleep, activity, leftovers = route_records(records)
    if not sleep and not activity:
        return leftovers
    customer_pk = _customer_pk(customer)
    with transaction.atomic():
        if sleep:
            _upsert_sleep(customer_pk, source, sleep, admin_create_date)
        if activity:
            _merge_activity(customer_pk, source, activity, admin_create_date)
    return leftovers
