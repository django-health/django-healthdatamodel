"""
Backfill the compact tables (SleepDay / ActivityDay) from the legacy Record log.

``Record`` rows are replayed **grouped by upload** — ``(customer,
admin_create_date, source)`` — oldest first, through the same routing code the
live ingest path uses (:func:`healthdatamodel.compact.apply_compact`, in
``replay`` mode).  Replaying through one code path guarantees the backfilled
state is identical to what live dual-writing would have produced.

Operational properties (relied on by ``docs/UPGRADING-1.0.md``):

Idempotent / resumable
    Re-running over the same customers converges to the same rows: a replayed
    upload *replaces* what the same upload wrote before (instead of merging
    with it), and an upload older than the stored row never overwrites it.
    So an interrupted run can simply be restarted, from the beginning or from
    the last customer reported as done.

Bounded
    Customers are processed in batches (keyset-paginated over the user
    table); each batch issues one ``Record`` query restricted to those
    customers and to the compactable types (sleep + the three activity
    metrics), so no query ever spans the whole table.

No long transactions
    Each upload is written by :func:`~healthdatamodel.compact.apply_compact`
    in its own ``transaction.atomic()`` block.  Outside an enclosing
    transaction (the management command runs in autocommit) every upload
    commits as soon as it is written; there is never one giant transaction.
    (The ``0012`` migration, when opted in, is the exception: migrations run
    inside a transaction, which is why it is only for small installs.)

Safe alongside live dual-writes
    The writers take the same row locks as ingest, and replay never moves a
    stored row backwards in time.

Entry points
------------
``backfill_from_records``
    Replay from the ``Record`` table (optionally filtered by customer /
    customer-pk range / upload date).  Used by the ``backfill_compact``
    management command and, when opted in, the ``0012_backfill_compact``
    data migration.

``replay_records``
    Replay any iterable of record-shaped objects (e.g. rows loaded from an
    archive of the ``Record`` table).  The iterable must be sorted by
    ``(customer_id, admin_create_date)``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from django.contrib.auth import get_user_model

from healthdatamodel.compact import apply_compact
from healthdatamodel.constants import HK_TYPE_TO_COMPACT_METRIC, SLEEP_TYPE
from healthdatamodel.models import Record

logger = logging.getLogger(__name__)

#: Record.type values the compact tables can hold; everything else is left
#: alone by the backfill (it stays in Record).
COMPACTABLE_TYPES: tuple[str, ...] = (SLEEP_TYPE, *HK_TYPE_TO_COMPACT_METRIC)


class BackfillResult(NamedTuple):
    records: int
    """Record rows replayed."""
    uploads: int
    """Uploads (``(customer, admin_create_date, source)`` groups) replayed."""
    customers: int
    """Customers scanned (including those with no compactable records)."""
    last_customer: Any
    """Primary key of the last customer fully processed (``None`` if none)."""


@dataclass
class BackfillProgress:
    """Running totals passed to progress callbacks."""

    records: int = 0
    uploads: int = 0
    customers: int = 0
    last_customer: Any = None
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def result(self) -> BackfillResult:
        return BackfillResult(
            self.records, self.uploads, self.customers, self.last_customer
        )


ProgressCallback = Callable[[BackfillProgress], None]


def replay_records(
    records: Iterable[Any],
    on_upload: Callable[[int], None] | None = None,
) -> tuple[int, int]:
    """Replay record-shaped objects through the compact router.

    *records* must be sorted by ``(customer_id, admin_create_date)`` so that
    newer uploads overwrite older ones, exactly as live dual-writing would
    have.  Each object needs ``customer_id``, ``admin_create_date``,
    ``source``, and the record-shaped attributes used by the router
    (``startDate``, ``endDate``, ``sourceName``, ``value``, ``unit``,
    ``type``).

    *on_upload*, if given, is called after each upload with the number of
    records in it.

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
        apply_compact(customer_pk, group, source, admin_create_date, replay=True)
        uploads += 1
        if on_upload is not None:
            on_upload(len(group))

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


def _customer_batches(
    customers: Iterable[Any] | None,
    customer_range: tuple[Any, Any] | None,
    batch_size: int,
) -> Iterator[list[Any]]:
    """Yield lists of customer pks in ascending pk order (keyset paginated)."""
    queryset = get_user_model().objects.all()
    if customers is not None:
        pks = [getattr(c, "pk", c) for c in customers]
        queryset = queryset.filter(pk__in=pks)
    if customer_range is not None:
        lo, hi = customer_range
        queryset = queryset.filter(pk__gte=lo, pk__lte=hi)
    queryset = queryset.order_by("pk")
    last = None
    while True:
        page = queryset if last is None else queryset.filter(pk__gt=last)
        batch = list(page.values_list("pk", flat=True)[:batch_size])
        if not batch:
            return
        yield batch
        last = batch[-1]


def backfill_from_records(
    customers: Iterable[Any] | None = None,
    since: datetime | None = None,
    chunk_size: int = 2000,
    *,
    customer_range: tuple[Any, Any] | None = None,
    customer_batch_size: int = 200,
    progress_every: int = 1000,
    on_progress: ProgressCallback | None = None,
    on_batch: ProgressCallback | None = None,
) -> BackfillResult:
    """Seed the compact tables from the ``Record`` table.

    Safe to run while dual-writes are live and safe to re-run (idempotent);
    see the module docstring.

    Parameters
    ----------
    customers:
        Optional iterable of customer instances or pks to restrict to.
    since:
        Only replay records with ``admin_create_date >= since``.  Useful for
        incremental catch-up runs.
    chunk_size:
        Rows fetched per round trip via ``iterator()``.
    customer_range:
        Optional inclusive ``(lo, hi)`` customer-pk range — lets a very large
        table be worked in bounded, separately scheduled slices.
    customer_batch_size:
        Customers per ``Record`` query.  Bounds the size of each query (and,
        on PostgreSQL, of the server-side cursor materialised for it).
    progress_every:
        Call *on_progress* (or log at INFO) every this many uploads; ``0``
        disables.
    on_progress, on_batch:
        Optional callbacks receiving the running :class:`BackfillProgress`;
        *on_batch* is called after each customer batch completes (its
        ``last_customer`` is a safe resume point).

    Returns
    -------
    BackfillResult
        ``(records, uploads, customers, last_customer)``.
    """
    progress = BackfillProgress()

    def report(p: BackfillProgress) -> None:
        if on_progress is not None:
            on_progress(p)
        else:
            logger.info(
                "healthdatamodel backfill: %d uploads / %d records, "
                "%d customers done (last %s), %.0fs",
                p.uploads,
                p.records,
                p.customers,
                p.last_customer,
                p.elapsed,
            )

    def on_upload(n_records: int) -> None:
        progress.uploads += 1
        progress.records += n_records
        if progress_every and progress.uploads % progress_every == 0:
            report(progress)

    for batch in _customer_batches(customers, customer_range, customer_batch_size):
        queryset = Record.objects.filter(
            customer_id__in=batch, type__in=COMPACTABLE_TYPES
        )
        if since is not None:
            queryset = queryset.filter(admin_create_date__gte=since)
        queryset = queryset.order_by("customer_id", "admin_create_date", "source", "pk")
        replay_records(queryset.iterator(chunk_size=chunk_size), on_upload)
        progress.customers += len(batch)
        progress.last_customer = batch[-1]
        if on_batch is not None:
            on_batch(progress)

    logger.info(
        "healthdatamodel backfill: replayed %d records across %d uploads "
        "for %d customers",
        progress.records,
        progress.uploads,
        progress.customers,
    )
    return progress.result()
