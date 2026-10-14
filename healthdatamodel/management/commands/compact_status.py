"""Report the state of the compact tables: the gate before flipping reads.

Prints:

* the effective compact-storage settings (and whether each is set
  explicitly or is the package default);
* SleepDay / ActivityDay row counts, customers covered, min/max day and the
  newest compact ``admin_create_date``;
* dual-write lag: compactable ``Record`` rows newer than the newest compact
  upload (should be 0 while dual-writing; found by walking ``Record``
  newest-first by primary key, bounded by ``--lag-scan-limit``);
* backfill coverage: customers with compactable ``Record`` data whose
  compact history does not yet reach back to their oldest ``Record`` day
  (should be 0 before switching reads; see
  :func:`healthdatamodel.compat.backfill_coverage`).

Ends with a READY / NOT READY line; ``--fail-if-not-ready`` turns NOT READY
into a non-zero exit.  ``--skip-coverage`` skips the per-customer scan.
See docs/UPGRADING-1.0.md.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Min

from healthdatamodel.compact import effective_settings
from healthdatamodel.compat import COMPACTABLE_TYPES, backfill_coverage
from healthdatamodel.models import ActivityDay, Record, SleepDay

_SCAN_PAGE = 5000


def record_lag(newest_compact, scan_limit: int) -> tuple[int, bool]:
    """Count compactable Record rows with ``admin_create_date`` after
    *newest_compact*, walking newest-first by pk.

    Stops at the first page with no such rows (Record pks follow upload
    order) or after *scan_limit* rows.  Returns ``(count, truncated)``.
    """
    count = 0
    scanned = 0
    last_pk = None
    while scanned < scan_limit:
        queryset = Record.objects.order_by("-pk")
        if last_pk is not None:
            queryset = queryset.filter(pk__lt=last_pk)
        page = list(
            queryset.values_list("pk", "type", "admin_create_date")[:_SCAN_PAGE]
        )
        if not page:
            return count, False
        newer = [
            row for row in page if newest_compact is None or row[2] > newest_compact
        ]
        count += sum(1 for _pk, type_, _adc in newer if type_ in COMPACTABLE_TYPES)
        if not newer:
            return count, False
        scanned += len(page)
        last_pk = page[-1][0]
    return count, True


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument(
            "--customers",
            nargs="+",
            metavar="PK",
            help="Restrict the coverage scan to these customer pks.",
        )
        scope.add_argument(
            "--customer-range",
            nargs=2,
            metavar=("LO", "HI"),
            help="Restrict the coverage scan to LO <= customer pk <= HI.",
        )
        parser.add_argument(
            "--skip-coverage",
            action="store_true",
            help="Skip the per-customer backfill coverage scan.",
        )
        parser.add_argument(
            "--show-pending",
            type=int,
            default=20,
            help="List up to N pending customer pks (default 20).",
        )
        parser.add_argument(
            "--lag-scan-limit",
            type=int,
            default=1_000_000,
            help="Max Record rows walked to measure dual-write lag (default 1000000).",
        )
        parser.add_argument(
            "--fail-if-not-ready",
            action="store_true",
            help="Exit non-zero unless the verdict is READY.",
        )

    def handle(self, *args, **options):
        out = self.stdout.write
        problems: list[str] = []

        out("Settings (effective):")
        for name, value in effective_settings().items():
            origin = "set" if hasattr(settings, name) else "default"
            out(f"  {name} = {value} ({origin})")
        current = effective_settings()
        if not current["HEALTHDATAMODEL_SAVE_COMPACT"]:
            problems.append("HEALTHDATAMODEL_SAVE_COMPACT is False (no dual writes)")
        if not current["HEALTHDATAMODEL_SAVE_RECORDS"]:
            problems.append(
                "HEALTHDATAMODEL_SAVE_RECORDS is False (no rollback path; "
                "not supported during the 1.0 switch-over)"
            )

        out("Compact tables:")
        newest = None
        compact_customers: set = set()
        for model in (SleepDay, ActivityDay):
            stats = model.objects.aggregate(
                min_day=Min("day"), max_day=Max("day"), latest=Max("admin_create_date")
            )
            customers = set(
                model.objects.order_by()
                .values_list("customer_id", flat=True)
                .distinct()
            )
            compact_customers |= customers
            rows = model.objects.count()
            out(
                f"  {model.__name__}: {rows} rows, {len(customers)} customers, "
                f"days {stats['min_day']}..{stats['max_day']}, "
                f"newest upload {stats['latest']}"
            )
            if stats["latest"] is not None and (
                newest is None or stats["latest"] > newest
            ):
                newest = stats["latest"]
        out(f"  customers with any compact row: {len(compact_customers)}")

        lag, truncated = record_lag(newest, options["lag_scan_limit"])
        shown = f">= {lag}" if truncated else str(lag)
        out(
            f"Dual-write lag: {shown} compactable Record row(s) newer than the "
            f"newest compact upload ({newest})"
        )
        if lag:
            problems.append(f"{shown} Record rows not yet in the compact tables")

        if options["skip_coverage"]:
            out("Backfill coverage: skipped (--skip-coverage)")
            problems.append("coverage not checked")
        else:
            customer_range = (
                tuple(options["customer_range"]) if options["customer_range"] else None
            )
            coverage = backfill_coverage(
                customers=options["customers"], customer_range=customer_range
            )
            scope = (
                "selected customers"
                if options["customers"] or customer_range
                else "all customers"
            )
            out(f"Backfill coverage ({scope}):")
            out(f"  customers scanned: {coverage.customers_scanned}")
            out(f"  with compactable Record data: {coverage.customers_with_records}")
            out(f"  oldest compactable Record day: {coverage.oldest_record_day}")
            out(
                "  backfilled: "
                f"{coverage.customers_with_records - len(coverage.pending)}"
            )
            out(f"  pending: {len(coverage.pending)}")
            if coverage.pending:
                listed = coverage.pending[: options["show_pending"]]
                more = len(coverage.pending) - len(listed)
                out(
                    "  pending customer pks: "
                    + " ".join(str(pk) for pk in listed)
                    + (f" ... (+{more} more)" if more else "")
                )
                problems.append(f"{len(coverage.pending)} customer(s) not backfilled")

        if problems:
            verdict = (
                "NOT READY to set HEALTHDATAMODEL_READ_COMPACT = True: "
                + "; ".join(problems)
            )
            out(self.style.WARNING(verdict))
            if options["fail_if_not_ready"]:
                raise CommandError(verdict)
        else:
            out(
                self.style.SUCCESS(
                    "READY: dual-writing, no lag, backfill covers every customer "
                    "in scope. Run verify_compact before switching reads."
                )
            )
