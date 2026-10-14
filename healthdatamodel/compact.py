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
#
# The defaults are chosen so that a consumer who sets *nothing* gets the
# safe, reversible state: the 1.0 migrations create the compact tables (DDL
# only), every upload is dual-written to both ``Record`` and the compact
# tables, and reads keep using the legacy ``Record`` path.  Switching reads
# over, and seeding the compact tables from history, are explicit opt-ins —
# see ``docs/UPGRADING-1.0.md``.

#: setting name -> default.  Single source of truth for the helpers below and
#: for ``manage.py compact_status``.
SETTINGS_DEFAULTS: dict[str, bool] = {
    # Write-through to the legacy Record log.
    "HEALTHDATAMODEL_SAVE_RECORDS": True,
    # Write to SleepDay / ActivityDay.
    "HEALTHDATAMODEL_SAVE_COMPACT": True,
    # Serve the query API from SleepDay / ActivityDay instead of Record.
    "HEALTHDATAMODEL_READ_COMPACT": False,
    # Let the 0012 data migration seed the compact tables from Record.
    # Only for small installs: it runs inside the migration transaction.
    "HEALTHDATAMODEL_BACKFILL_IN_MIGRATION": False,
}


def _setting(name: str) -> bool:
    return bool(getattr(django_settings, name, SETTINGS_DEFAULTS[name]))


def effective_settings() -> dict[str, bool]:
    """Return every compact-storage setting with its effective value."""
    return {name: _setting(name) for name in SETTINGS_DEFAULTS}


def save_records_enabled() -> bool:
    return _setting("HEALTHDATAMODEL_SAVE_RECORDS")


def save_compact_enabled() -> bool:
    return _setting("HEALTHDATAMODEL_SAVE_COMPACT")


def read_compact_enabled() -> bool:
    return _setting("HEALTHDATAMODEL_READ_COMPACT")


def backfill_in_migration_enabled() -> bool:
    return _setting("HEALTHDATAMODEL_BACKFILL_IN_MIGRATION")


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
        return round(value)
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


def _write_db() -> str:
    """Database alias the compact tables are written to (router-aware)."""
    return router.db_for_write(SleepDay)


def _can_lock() -> bool:
    return connections[_write_db()].features.has_select_for_update


def _lock_if_supported(queryset):
    if _can_lock():
        return queryset.select_for_update()
    return queryset


#: ``admin_create_date`` of the empty rows :func:`_ensure_rows` inserts so
#: that every key can be row-locked.  They are always overwritten in the same
#: transaction, so they never become visible to other connections.
_PLACEHOLDER_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ensure_rows(model: Any, rows: list[Any], sort_fields: list[str]) -> None:
    """Insert *rows* (empty placeholders) where their key does not exist yet.

    ``SELECT ... FOR UPDATE`` can only lock rows that exist.  Without this,
    two transactions writing the same *new* key concurrently (two uploads in
    flight for one customer, or the backfill replaying an old upload while a
    live upload creates the same day) would both read "no row", and the
    later ``INSERT ... ON CONFLICT DO UPDATE`` would overwrite the earlier
    commit with a vector / interval list that lacks its data.  Inserting the
    keys first (``ON CONFLICT DO NOTHING``) makes the second transaction
    wait on the unique index until the first commits; its following
    ``SELECT ... FOR UPDATE`` then sees and locks the committed row.

    Only needed (and only done) on backends with row locking; SQLite
    serialises writers itself.
    """
    if not rows or not _can_lock():
        return
    # A stable key order keeps concurrent writers from deadlocking on the
    # unique index.
    rows.sort(key=lambda row: tuple(getattr(row, f) for f in sort_fields))
    model.objects.bulk_create(rows, ignore_conflicts=True)


def _is_placeholder(row: Any, payload: Any) -> bool:
    return row.admin_create_date == _PLACEHOLDER_DATE and not any(
        value is not None for value in payload
    )


def _upsert_sleep(
    customer_pk: Any,
    source: str,
    groups: _SleepGroups,
    admin_create_date: datetime,
    replay: bool = False,
) -> None:
    _ensure_rows(
        SleepDay,
        [
            SleepDay(
                customer_id=customer_pk,
                source=source,
                device=device,
                day=day,
                intervals=[],
                admin_create_date=_PLACEHOLDER_DATE,
            )
            for device, day in groups
        ],
        ["device", "day"],
    )
    queryset = _lock_if_supported(
        SleepDay.objects.filter(
            customer_id=customer_pk,
            device__in={key[0] for key in groups},
            day__in={key[1] for key in groups},
        ).order_by("device", "day")
    )
    existing = {
        (row.device, row.day): row
        for row in queryset
        if not _is_placeholder(row, row.intervals)
    }

    rows = []
    for (device, day), pieces in groups.items():
        entries = {
            (piece_start.isoformat(), piece_end.isoformat(), stage)
            for piece_start, piece_end, stage in pieces
        }
        stored = existing.get((device, day))
        if replay and stored is not None:
            if stored.admin_create_date > admin_create_date:
                # Never move a day backwards: a newer upload (e.g. written
                # live while this replay was running) already owns it.
                continue
            if (
                stored.admin_create_date == admin_create_date
                and stored.source != source
            ):
                # Same timestamp from another pipeline: the key has no
                # source, and live ingest merges same-timestamp writes, so
                # union.  Re-runs stay idempotent (a union is stable).
                entries |= {tuple(entry) for entry in stored.intervals}
            # Same timestamp and source: the replay group *is* the whole
            # logical upload, so replace rather than merge (keeps re-runs
            # idempotent).
        elif stored is not None and stored.admin_create_date == admin_create_date:
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
    replay: bool = False,
) -> None:
    _ensure_rows(
        ActivityDay,
        [
            ActivityDay(
                customer_id=customer_pk,
                source=source,
                device=device,
                metric=metric,
                day=day,
                resolution_minutes=resolution,
                values=[None] * (MINUTES_PER_DAY // resolution),
                admin_create_date=_PLACEHOLDER_DATE,
            )
            for device, metric, resolution, day in groups
        ],
        ["device", "metric", "day", "resolution_minutes"],
    )
    queryset = _lock_if_supported(
        ActivityDay.objects.filter(
            customer_id=customer_pk,
            source=source,
            device__in={key[0] for key in groups},
            metric__in={key[1] for key in groups},
            resolution_minutes__in={key[2] for key in groups},
            day__in={key[3] for key in groups},
        ).order_by("device", "metric", "day", "resolution_minutes")
    )
    existing = {
        (row.device, row.metric, row.resolution_minutes, row.day): row
        for row in queryset
        if not _is_placeholder(row, row.values)
    }

    rows = []
    for (device, metric, resolution, day), slots in groups.items():
        length = MINUTES_PER_DAY // resolution
        stored = existing.get((device, metric, resolution, day))
        same_upload = (
            stored is not None and stored.admin_create_date == admin_create_date
        )
        # Replaying an upload older than the stored row (a live write landed
        # first, or an archive load): only fill slots nobody has written.
        older_replay = (
            replay
            and stored is not None
            and stored.admin_create_date > admin_create_date
        )
        values: list[float | int | None]
        if stored is not None and len(stored.values) == length:
            values = list(stored.values)
        else:
            values = [None] * length
        changed = False
        for index, value in slots.items():
            if older_replay and values[index] is not None:
                continue
            if same_upload and not replay and values[index] is not None:
                # Same upload timestamp split across calls: legacy returns
                # both tied rows, which day-level aggregation sums.  (A
                # replay group already holds the whole logical upload, so
                # replay overwrites instead — re-runs stay idempotent.)
                value = values[index] + value
            values[index] = value if isinstance(value, int) else round(value, 1)
            changed = True
        if older_replay and not changed:
            continue
        rows.append(
            ActivityDay(
                customer_id=customer_pk,
                source=source,
                device=device,
                metric=metric,
                day=day,
                resolution_minutes=resolution,
                values=values,
                admin_create_date=(
                    stored.admin_create_date if older_replay else admin_create_date
                ),
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
    *,
    replay: bool = False,
) -> list[Any]:
    """Write *records* to the compact tables; return uncompactable leftovers.

    *customer* may be a user instance or a primary key (the backfill path
    passes pks).  *records* may be ``RecordInput`` objects (live ingest) or
    ``Record`` model instances (backfill) — anything with the record-shaped
    attributes.

    The write runs in its own ``transaction.atomic()`` block (on the
    database the router picks for the compact tables), so outside an
    enclosing transaction each call (= one upload) commits on its own.
    Every affected key is row-locked for the duration (see
    :func:`_ensure_rows`), so concurrent uploads for one customer, and a
    backfill running alongside live ingest, serialise per key instead of
    overwriting each other.

    ``replay=True`` is used by the backfill (:mod:`healthdatamodel.compat`),
    where *records* is the **complete** logical upload (every ``Record`` row
    sharing ``(customer, admin_create_date, source)``).  Two rules change so
    that replaying is idempotent and never regresses newer data:

    * same ``admin_create_date`` as the stored row → **replace** (sleep) /
      **overwrite** slots (activity) instead of merging/summing — live
      ingest merges because one upload may arrive in several calls, but a
      replay group already contains all of them, so merging would
      double-count on every re-run.  (Exception: a sleep day stored at the
      same timestamp by a *different* ``source`` is unioned, as live ingest
      would, since the sleep key has no source column);
    * stored row is **newer** than the replayed upload → sleep keeps the
      stored day; activity only fills slots that are still ``None``.
    """
    sleep, activity, leftovers = route_records(records)
    if not sleep and not activity:
        return leftovers
    customer_pk = _customer_pk(customer)
    with transaction.atomic(using=_write_db()):
        if sleep:
            _upsert_sleep(customer_pk, source, sleep, admin_create_date, replay)
        if activity:
            _merge_activity(customer_pk, source, activity, admin_create_date, replay)
    return leftovers
