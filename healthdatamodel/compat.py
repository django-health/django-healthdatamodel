"""
Backfill the compact tables (SleepDay / ActivityDay) from the legacy Record log.

``Record`` rows are replayed **grouped by upload** — ``(customer,
admin_create_date, source)`` — oldest first, through the same routing code the
live ingest path uses (:func:`healthdatamodel.compact.apply_compact`).
Replaying through one code path guarantees the backfilled state is identical
to what live dual-writing would have produced, and makes the operation
idempotent: re-running converges to the same rows.

Entry points
------------
``backfill_from_records``
    Replay from the ``Record`` table (optionally filtered by customer /
    date).  Ships as both the ``0010_backfill_compact`` data migration and
    the ``backfill_compact`` management command.

``replay_records``
    Replay any iterable of record-shaped objects (e.g. rows loaded from an
    archive of the ``Record`` table).  The iterable must be sorted by
    ``(customer_id, admin_create_date)``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from healthdatamodel.compact import apply_compact
from healthdatamodel.models import Record

logger = logging.getLogger(__name__)


def replay_records(records: Iterable[Any]) -> tuple[int, int]:
    """Replay record-shaped objects through the compact router.

    *records* must be sorted by ``(customer_id, admin_create_date)`` so that
    newer uploads overwrite older ones, exactly as live dual-writing would
    have.  Each object needs ``customer_id``, ``admin_create_date``,
    ``source``, and the record-shaped attributes used by the router
    (``startDate``, ``endDate``, ``sourceName``, ``value``, ``unit``,
    ``type``).

    Returns ``(records_seen, uploads_replayed)``.
    """
    group: list[Any] = []
    group_key: tuple[Any, datetime, str] | None = None
    seen = 0
    uploads = 0

    def flush() -> None:
        nonlocal uploads
        if not group or group_key is None:
            return
        customer_pk, admin_create_date, source = group_key
        apply_compact(customer_pk, group, source, admin_create_date)
        uploads += 1

    for record in records:
        seen += 1
        key = (record.customer_id, record.admin_create_date, record.source)
        if key != group_key:
            flush()
            group = []
            group_key = key
        group.append(record)
    flush()
    return seen, uploads


def backfill_from_records(
    customers: Iterable[Any] | None = None,
    since: datetime | None = None,
    chunk_size: int = 2000,
) -> tuple[int, int]:
    """Seed the compact tables from the ``Record`` table.

    Safe to run while dual-writes are live (the compact writers take the
    same row locks as ingest) and safe to re-run (idempotent).

    Parameters
    ----------
    customers:
        Optional iterable of customer instances or pks to restrict to.
    since:
        Only replay records with ``admin_create_date >= since``.  Useful for
        incremental catch-up runs.
    chunk_size:
        Rows fetched per query via ``iterator()``.

    Returns
    -------
    tuple[int, int]
        ``(records_seen, uploads_replayed)``.
    """
    queryset = Record.objects.all()
    if customers is not None:
        queryset = queryset.filter(customer__in=list(customers))
    if since is not None:
        queryset = queryset.filter(admin_create_date__gte=since)
    queryset = queryset.order_by("customer_id", "admin_create_date", "source", "pk")
    seen, uploads = replay_records(queryset.iterator(chunk_size=chunk_size))
    logger.info(
        "healthdatamodel backfill: replayed %d records across %d uploads",
        seen,
        uploads,
    )
    return seen, uploads
