"""
Customer-level lifecycle helpers: delete, reassign, presence checks and the
per-source summary.

These are the supported entry points for operations that touch *all* of a
customer's health data.  From v1.0 records are additionally held in the
compact storage tables (``SleepDay`` / ``ActivityDay``), which must stay in
sync with ``Record``; the delete and reassign helpers here do that, whereas
direct ORM access to ``Record`` / ``Workout``
(``customer.record_set.all().delete()``, ``Record.objects.filter(...).update(...)``)
silently leaves the compact tables stale.

Table awareness in 1.0
----------------------
- :func:`delete_records`, :func:`delete_customer_health_data` and
  :func:`reassign_customer_health_data` act on ``Record`` **and** the compact
  tables, in one transaction.
- :func:`has_records` and :func:`get_data_source_summary` read ``Record``
  only, so they require ``HEALTHDATAMODEL_SAVE_RECORDS = True`` (the 1.0.x
  invariant; see ``docs/UPGRADING-1.0.md``).
  :func:`reassign_customer_health_data` requires it too, because it rebuilds
  the target's compact rows from ``Record``.

Entry points
------------
- :func:`has_records` — does the customer have any record at all?
- :func:`delete_records` — delete only the customer's records
- :func:`delete_customer_health_data` — delete records, workouts and rankings
- :func:`reassign_customer_health_data` — move records / workouts to another
  customer (account merge)
- :func:`get_data_source_summary` — per-source sync dates and rank
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import connections, router, transaction
from django.db.models import Max, Min

from healthdatamodel import compact
from healthdatamodel.models import (
    ActivityDay,
    DataSourceRanking,
    Record,
    SleepDay,
    Workout,
    WorkoutMetadataEntry,
)

__all__ = [
    "DataSourceSummary",
    "delete_customer_health_data",
    "delete_records",
    "get_data_source_summary",
    "has_records",
    "reassign_customer_health_data",
]


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def has_records(customer: Any) -> bool:
    """Return ``True`` if *customer* has at least one health record.

    Cheaper than ``count() > 0``: it stops at the first row.

    Reads ``Record`` only, so it requires
    ``HEALTHDATAMODEL_SAVE_RECORDS = True`` (the 1.0.x invariant).  It also
    returns ``False`` once a customer's rows have aged out of ``Record``
    under your retention policy, even if the compact tables still hold their
    history.
    """
    return Record.objects.filter(customer=customer).exists()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def _db() -> str:
    return router.db_for_write(Record)


def _delete_record_tables(customer: Any) -> dict[str, int]:
    """Delete the customer's ``Record``, ``SleepDay`` and ``ActivityDay`` rows."""
    counts = {}
    for key, model in (
        ("records", Record),
        ("sleep_days", SleepDay),
        ("activity_days", ActivityDay),
    ):
        counts[key], _ = model.objects.filter(customer=customer).delete()
    return counts


def delete_records(customer: Any) -> int:
    """Delete every health record belonging to *customer*.

    Deletes the customer's ``Record`` rows and, from 1.0, their compact
    ``SleepDay`` / ``ActivityDay`` rows, in one transaction.  Workouts and
    data-source rankings are left alone (see
    :func:`delete_customer_health_data` for the full wipe).  Used by e.g. a
    fitness-data or onboarding reset.

    Returns
    -------
    int
        Number of ``Record`` rows deleted (the compact rows are deleted but
        not counted here; :func:`delete_customer_health_data` reports them).
    """
    with transaction.atomic(using=_db()):
        return _delete_record_tables(customer)["records"]


def delete_customer_health_data(
    customer: Any,
    *,
    records: bool = True,
    workouts: bool = True,
    rankings: bool = True,
) -> dict[str, int]:
    """Delete the customer's health data in one transaction.

    ``WearableConnection`` rows are *not* touched — they describe the
    connection, not the data; disconnect them separately if required.

    Parameters
    ----------
    customer:
        Any ``settings.AUTH_USER_MODEL`` instance.
    records:
        Delete the record-shaped tables: ``Record`` and the compact
        ``SleepDay`` / ``ActivityDay`` rows.
    workouts:
        Delete ``Workout`` rows and their ``WorkoutMetadataEntry`` rows.
    rankings:
        Delete ``DataSourceRanking`` rows (they are rebuilt on demand by
        :func:`~healthdatamodel.query.ensure_ranks`).

    Returns
    -------
    dict[str, int]
        Per-table counts, always with all six keys: ``"records"``,
        ``"sleep_days"``, ``"activity_days"``, ``"workouts"``,
        ``"workout_metadata"``, ``"rankings"``.  Skipped categories report
        ``0``.  (``sleep_days`` / ``activity_days`` are new in 1.0.)
    """
    counts = {
        "records": 0,
        "sleep_days": 0,
        "activity_days": 0,
        "workouts": 0,
        "workout_metadata": 0,
        "rankings": 0,
    }
    with transaction.atomic(using=_db()):
        if records:
            counts.update(_delete_record_tables(customer))
        if workouts:
            _total, per_model = Workout.objects.filter(customer=customer).delete()
            counts["workouts"] = per_model.get(Workout._meta.label, 0)
            counts["workout_metadata"] = per_model.get(
                WorkoutMetadataEntry._meta.label, 0
            )
        if rankings:
            counts["rankings"], _ = DataSourceRanking.objects.filter(
                customer=customer
            ).delete()
    return counts


# ---------------------------------------------------------------------------
# Reassignment (account merge)
# ---------------------------------------------------------------------------


def _pk(customer: Any) -> Any:
    return getattr(customer, "pk", customer)


def _locked(queryset):
    if connections[_db()].features.has_select_for_update:
        return queryset.select_for_update()
    return queryset


def _round_slot(value: float) -> float | int:
    return value if isinstance(value, int) else round(value, 1)


def _merge_sleep_row(src: SleepDay, dst: SleepDay) -> bool:
    """Fold *src* into *dst* (same key) with the live-ingest rules.

    The newer upload owns the day; equal upload timestamps are one logical
    upload and are unioned.  Returns ``True`` if *dst* changed.
    """
    if src.admin_create_date > dst.admin_create_date:
        dst.intervals = src.intervals
        dst.source = src.source
        dst.admin_create_date = src.admin_create_date
        return True
    if src.admin_create_date == dst.admin_create_date:
        union = {tuple(e) for e in dst.intervals} | {tuple(e) for e in src.intervals}
        dst.intervals = [list(e) for e in sorted(union)]
        return True
    return False


def _merge_activity_row(src: ActivityDay, dst: ActivityDay) -> bool:
    """Fold *src* into *dst* (same key) with the live-ingest rules.

    Per slot the newer upload wins and the older one only fills empty slots;
    equal upload timestamps are one logical upload and are summed.  Returns
    ``True`` if *dst* changed.
    """
    if len(src.values) != len(dst.values):  # pragma: no cover - same key
        if src.admin_create_date > dst.admin_create_date:
            dst.values = src.values
            dst.admin_create_date = src.admin_create_date
            return True
        return False
    if src.admin_create_date == dst.admin_create_date:
        dst.values = [
            b if a is None else a if b is None else _round_slot(a + b)
            for a, b in zip(dst.values, src.values)
        ]
        return True
    newer, older = (
        (src, dst) if src.admin_create_date > dst.admin_create_date else (dst, src)
    )
    dst.values = [n if n is not None else o for n, o in zip(newer.values, older.values)]
    dst.admin_create_date = newer.admin_create_date
    return True


def _move_compact_rows(
    model: Any,
    from_pk: Any,
    to_pk: Any,
    key_fields: tuple[str, ...],
    payload_fields: list[str],
    merge: Any,
) -> None:
    """Re-point *from_pk*'s rows of *model* to *to_pk*.

    Rows whose key the target already has are folded into the target's row
    with *merge* and deleted; the rest are re-pointed with one ``UPDATE``.
    Both customers' rows are locked first.
    """
    source_rows = list(
        _locked(model.objects.filter(customer_id=from_pk).order_by(*key_fields))
    )
    if not source_rows:
        return
    target_rows = {
        tuple(getattr(row, f) for f in key_fields): row
        for row in _locked(
            model.objects.filter(customer_id=to_pk).order_by(*key_fields)
        )
    }
    conflicting = []
    changed = []
    for row in source_rows:
        target = target_rows.get(tuple(getattr(row, f) for f in key_fields))
        if target is None:
            continue
        conflicting.append(row.pk)
        if merge(row, target):
            changed.append(target)
    if conflicting:
        model.objects.filter(pk__in=conflicting).delete()
    if changed:
        model.objects.bulk_update(changed, payload_fields)
    model.objects.filter(customer_id=from_pk).update(customer_id=to_pk)


def reassign_customer_health_data(
    from_customer: Any,
    to_customer: Any,
    *,
    records: bool = True,
    workouts: bool = True,
) -> dict[str, int]:
    """Move health data from *from_customer* to *to_customer*.

    Intended for account merges.  ``Record`` and ``Workout`` rows are
    re-pointed, not copied, so *from_customer* ends up with none of the
    moved data.  ``Record`` rows are not de-duplicated: if *to_customer*
    already has overlapping records, both sets are kept (callers typically
    guard with ``not has_records(to_customer)``).

    The compact tables (1.0) are keyed per customer and day, so they cannot
    simply be re-pointed when the target already has a row for the same
    ``(device, day)`` (sleep) or ``(source, device, metric, day,
    resolution)`` (activity).  With ``records=True`` this helper therefore,
    in the same transaction:

    1. re-points *from_customer*'s ``SleepDay`` / ``ActivityDay`` rows to
       *to_customer*, folding any row whose key the target already has into
       the target's row with the live-ingest rules (the newer upload wins,
       the older one only fills empty activity slots, equal upload
       timestamps merge) — this keeps compact history that is no longer in
       ``Record`` (e.g. past your archive horizon);
    2. rebuilds *to_customer*'s compact rows from their merged ``Record``
       rows through the backfill's replay path
       (:func:`healthdatamodel.compat.backfill_from_records` with
       ``customers=[to_customer]``), so every day still in ``Record`` ends
       up exactly as if all uploads had been ingested for *to_customer*.
       Skipped when ``HEALTHDATAMODEL_SAVE_COMPACT`` is ``False``.

    The rebuild replays all of the target's compactable ``Record`` rows, so
    the cost grows with how much history ``Record`` holds for them.

    ``DataSourceRanking`` rows are not moved — they are derived from the
    target customer's active connection and rebuilt on demand by
    :func:`~healthdatamodel.query.ensure_ranks`.  ``WearableConnection`` rows
    are not moved either.  Workout metadata follows its workout implicitly.

    Raises
    ------
    django.core.exceptions.ImproperlyConfigured
        With ``records=True`` when ``HEALTHDATAMODEL_SAVE_RECORDS`` is
        ``False``: step 2 needs ``Record`` to hold every upload (the 1.0.x
        invariant).
    ValueError
        If both arguments are the same customer.

    Returns
    -------
    dict[str, int]
        ``{"records": n, "workouts": n}`` (``Record`` and ``Workout`` rows
        moved); skipped categories report ``0``.
    """
    from_pk, to_pk = _pk(from_customer), _pk(to_customer)
    if from_pk == to_pk:
        raise ValueError("from_customer and to_customer are the same customer")
    if records and not compact.save_records_enabled():
        raise ImproperlyConfigured(
            "reassign_customer_health_data(records=True) requires "
            "HEALTHDATAMODEL_SAVE_RECORDS = True: it rebuilds the target's "
            "compact rows from Record, which must hold every upload."
        )
    counts = {"records": 0, "workouts": 0}
    with transaction.atomic(using=_db()):
        if records:
            counts["records"] = Record.objects.filter(customer_id=from_pk).update(
                customer_id=to_pk
            )
            _move_compact_rows(
                SleepDay,
                from_pk,
                to_pk,
                ("device", "day"),
                ["intervals", "source", "admin_create_date"],
                _merge_sleep_row,
            )
            _move_compact_rows(
                ActivityDay,
                from_pk,
                to_pk,
                ("source", "device", "metric", "day", "resolution_minutes"),
                ["values", "admin_create_date"],
                _merge_activity_row,
            )
            if compact.save_compact_enabled():
                from healthdatamodel.compat import backfill_from_records

                backfill_from_records(customers=[to_pk], progress_every=0)
        if workouts:
            counts["workouts"] = Workout.objects.filter(customer_id=from_pk).update(
                customer_id=to_pk
            )
    return counts


# ---------------------------------------------------------------------------
# Per-source summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataSourceSummary:
    """Record- and ranking-derived status of one data source for a customer.

    Attributes
    ----------
    last_sync_date:
        Latest ``admin_create_date`` of the source's records — when data was
        last *received*.  ``None`` if the source has no records.
    connected_date:
        Earliest ``admin_create_date`` — when data was first received.
        ``None`` if the source has no records (see :attr:`has_data`).
    most_recent_data_date:
        Latest record ``endDate`` — how fresh the data itself is.
    rank:
        The source's ``DataSourceRanking.rank`` (1 = preferred), or ``None``
        if there is no ranking row for it.
    """

    last_sync_date: datetime | None
    connected_date: datetime | None
    most_recent_data_date: datetime | None
    rank: int | None

    @property
    def has_data(self) -> bool:
        """``True`` when at least one record exists for the source."""
        return self.connected_date is not None


def get_data_source_summary(customer: Any) -> dict[str, DataSourceSummary]:
    """Return per-source record dates and ranking for *customer*.

    The result has one entry for every data source that appears either in
    the customer's records (``Record.source``) or in their
    ``DataSourceRanking`` rows — i.e. the full outer join of the two.  A
    source with records but no ranking row has ``rank=None``; a ranked
    source with no records has all three dates ``None``.  Sources whose name
    is blank or ``NULL`` are omitted.  If the (unconstrained) ranking table
    holds more than one row for a source, the lowest rank is reported.

    Works on any database backend (two queries, merged in Python).

    The record dates come from ``Record``, so this requires
    ``HEALTHDATAMODEL_SAVE_RECORDS = True`` (the 1.0.x invariant) and only
    reflects what your ``Record`` retention keeps.

    Returns
    -------
    dict[str, DataSourceSummary]
        Keyed by data-source string (e.g. ``"apple_health"``).  No ordering is
        guaranteed.
    """
    record_stats = {
        row["source"]: row
        for row in Record.objects.filter(customer=customer)
        .order_by()
        .values("source")
        .annotate(
            last_sync_date=Max("admin_create_date"),
            connected_date=Min("admin_create_date"),
            most_recent_data_date=Max("endDate"),
        )
    }
    ranks: dict[str, int] = {}
    for source, rank in DataSourceRanking.objects.filter(customer=customer).values_list(
        "dataSource", "rank"
    ):
        if source and (source not in ranks or rank < ranks[source]):
            ranks[source] = rank

    summary: dict[str, DataSourceSummary] = {}
    for source in record_stats.keys() | ranks.keys():
        if not source:
            continue
        stats = record_stats.get(source, {})
        summary[source] = DataSourceSummary(
            last_sync_date=stats.get("last_sync_date"),
            connected_date=stats.get("connected_date"),
            most_recent_data_date=stats.get("most_recent_data_date"),
            rank=ranks.get(source),
        )
    return summary
