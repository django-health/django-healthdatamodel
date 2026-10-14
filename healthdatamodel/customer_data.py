"""
Customer-level lifecycle helpers: delete, reassign, presence checks and the
per-source summary.

These are the supported entry points for operations that touch *all* of a
customer's health data.  From v1.0 records are additionally held in compact
storage tables that must stay in sync with ``Record``; these helpers will do
that transparently, whereas direct ORM access to ``Record`` / ``Workout``
(``customer.record_set.all().delete()``, ``Record.objects.filter(...).update(...)``)
will silently leave the compact tables stale.

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

from django.db import transaction
from django.db.models import Max, Min

from healthdatamodel.models import (
    DataSourceRanking,
    Record,
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
    """
    return Record.objects.filter(customer=customer).exists()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def delete_records(customer: Any) -> int:
    """Delete every health record belonging to *customer*.

    Workouts and data-source rankings are left alone (see
    :func:`delete_customer_health_data` for the full wipe).  Used by e.g. a
    fitness-data or onboarding reset.

    Returns
    -------
    int
        Number of records deleted.
    """
    deleted, _ = Record.objects.filter(customer=customer).delete()
    return deleted


@transaction.atomic
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
        Delete ``Record`` rows.
    workouts:
        Delete ``Workout`` rows and their ``WorkoutMetadataEntry`` rows.
    rankings:
        Delete ``DataSourceRanking`` rows (they are rebuilt on demand by
        :func:`~healthdatamodel.query.ensure_ranks`).

    Returns
    -------
    dict[str, int]
        Per-model counts, always with all four keys: ``"records"``,
        ``"workouts"``, ``"workout_metadata"``, ``"rankings"``.  Skipped
        categories report ``0``.
    """
    counts = {"records": 0, "workouts": 0, "workout_metadata": 0, "rankings": 0}
    if records:
        counts["records"] = delete_records(customer)
    if workouts:
        _total, per_model = Workout.objects.filter(customer=customer).delete()
        counts["workouts"] = per_model.get(Workout._meta.label, 0)
        counts["workout_metadata"] = per_model.get(WorkoutMetadataEntry._meta.label, 0)
    if rankings:
        counts["rankings"], _ = DataSourceRanking.objects.filter(
            customer=customer
        ).delete()
    return counts


# ---------------------------------------------------------------------------
# Reassignment (account merge)
# ---------------------------------------------------------------------------


@transaction.atomic
def reassign_customer_health_data(
    from_customer: Any,
    to_customer: Any,
    *,
    records: bool = True,
    workouts: bool = True,
) -> dict[str, int]:
    """Move health data from *from_customer* to *to_customer*.

    Intended for account merges.  Rows are re-pointed, not copied, so
    *from_customer* ends up with none of the moved data.  Nothing is
    de-duplicated: if *to_customer* already has overlapping data, both sets
    are kept (callers typically guard with ``not has_records(to_customer)``).

    ``DataSourceRanking`` rows are not moved — they are derived from the
    target customer's active connection and rebuilt on demand by
    :func:`~healthdatamodel.query.ensure_ranks`.  ``WearableConnection`` rows
    are not moved either.  Workout metadata follows its workout implicitly.

    Returns
    -------
    dict[str, int]
        ``{"records": n, "workouts": n}``; skipped categories report ``0``.
    """
    counts = {"records": 0, "workouts": 0}
    if records:
        counts["records"] = Record.objects.filter(customer=from_customer).update(
            customer=to_customer
        )
    if workouts:
        counts["workouts"] = Workout.objects.filter(customer=from_customer).update(
            customer=to_customer
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
