"""Seed the compact tables (SleepDay / ActivityDay) from the Record table.

Replays Record uploads oldest-first through the live compact writer.
Idempotent and resumable (re-running converges; restart from the last
customer reported as done), safe to run while dual-writes are live, and it
never holds one long transaction: every upload commits on its own.

Work a large table in bounded slices with ``--customer-range LO HI`` (and
``--customer-batch-size``); check coverage afterwards with
``manage.py compact_status``.  See docs/UPGRADING-1.0.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

import dateutil.parser
from django.core.management.base import BaseCommand, CommandError

from healthdatamodel.compact import effective_settings, save_compact_enabled
from healthdatamodel.compat import BackfillProgress, backfill_from_records


def _next_pk(pk):
    try:
        return int(pk) + 1
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument(
            "--customers",
            nargs="+",
            metavar="PK",
            help="Restrict to these customer primary keys.",
        )
        scope.add_argument(
            "--customer-range",
            nargs=2,
            metavar=("LO", "HI"),
            help="Restrict to customers with LO <= pk <= HI (inclusive).",
        )
        parser.add_argument(
            "--since",
            help="Only replay uploads with admin_create_date >= this "
            "ISO datetime (assumed UTC if naive).",
        )
        parser.add_argument(
            "--customer-batch-size",
            type=int,
            default=200,
            help="Customers per Record query (default 200).",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=2000,
            help="Rows fetched per round trip (default 2000).",
        )
        parser.add_argument(
            "--progress-every",
            type=int,
            default=1000,
            help="Print a progress line every N uploads (default 1000; 0 = off).",
        )

    def handle(self, *args, **options):
        since: datetime | None = None
        if options["since"]:
            try:
                since = dateutil.parser.isoparse(options["since"])
            except ValueError as exc:
                raise CommandError(f"--since: {exc}") from exc
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        if options["customer_batch_size"] < 1:
            raise CommandError("--customer-batch-size must be >= 1")
        customer_range = (
            tuple(options["customer_range"]) if options["customer_range"] else None
        )
        hi = customer_range[1] if customer_range else None

        scope = (
            f"customers {' '.join(options['customers'])}"
            if options["customers"]
            else f"customer pk {customer_range[0]}..{customer_range[1]}"
            if customer_range
            else "all customers"
        )
        self.stdout.write(
            f"backfill_compact: {scope}"
            + (f", uploads since {since.isoformat()}" if since else "")
            + f"; settings {effective_settings()}"
        )
        if not save_compact_enabled():
            self.stderr.write(
                self.style.WARNING(
                    "HEALTHDATAMODEL_SAVE_COMPACT is False: live uploads are not "
                    "being written to the compact tables, so they will fall "
                    "behind again as soon as this finishes."
                )
            )

        def on_progress(p: BackfillProgress) -> None:
            rate = p.uploads / p.elapsed if p.elapsed else 0.0
            self.stdout.write(
                f"  progress: {p.uploads} uploads / {p.records} records, "
                f"{p.customers} customers done, {p.elapsed:.0f}s "
                f"({rate:.0f} uploads/s)"
            )

        def on_batch(p: BackfillProgress) -> None:
            resume = _next_pk(p.last_customer)
            hint = (
                f" (resume with --customer-range {resume} {hi if hi is not None else '<HI>'})"
                if resume is not None and not options["customers"]
                else ""
            )
            self.stdout.write(
                f"  batch done: {p.customers} customers through pk "
                f"{p.last_customer}, {p.uploads} uploads, {p.records} records"
                f"{hint}",
            )

        result = backfill_from_records(
            customers=options["customers"],
            since=since,
            chunk_size=options["chunk_size"],
            customer_range=customer_range,
            customer_batch_size=options["customer_batch_size"],
            progress_every=options["progress_every"],
            on_progress=on_progress,
            on_batch=on_batch if options["verbosity"] >= 1 else None,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: {result.customers} customers scanned, "
                f"{result.uploads} uploads / {result.records} records replayed "
                f"into the compact tables; last customer pk {result.last_customer}."
            )
        )
        self.stdout.write("Check coverage with: manage.py compact_status")
