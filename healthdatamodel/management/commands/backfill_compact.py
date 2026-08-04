"""Seed the compact tables (SleepDay / ActivityDay) from the Record table.

For installs too large for the automatic ``0010_backfill_compact`` data
migration (skip it with ``HEALTHDATAMODEL_SKIP_BACKFILL_MIGRATION = True``).
Idempotent and safe to run while dual-writes are live.
"""

from __future__ import annotations

from datetime import datetime, timezone

import dateutil.parser
from django.core.management.base import BaseCommand

from healthdatamodel.compat import backfill_from_records


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--customers",
            nargs="+",
            type=int,
            help="Restrict to these customer primary keys.",
        )
        parser.add_argument(
            "--since",
            help="Only replay uploads with admin_create_date >= this "
            "ISO datetime (assumed UTC if naive).",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=2000,
            help="Rows fetched per query (default 2000).",
        )

    def handle(self, *args, **options):
        since: datetime | None = None
        if options["since"]:
            since = dateutil.parser.parse(options["since"])
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        seen, uploads = backfill_from_records(
            customers=options["customers"],
            since=since,
            chunk_size=options["chunk_size"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Replayed {seen} records across {uploads} uploads into the "
                "compact tables."
            )
        )
