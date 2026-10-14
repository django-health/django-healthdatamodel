"""Compare compact-table query results against the legacy Record path.

The parity gate before switching reads to the compact tables
(``HEALTHDATAMODEL_READ_COMPACT = True``).  For the customers with the most
recent uploads (or ``--customers``), runs the public query API with
``READ_COMPACT`` forced to True and then to False, and diffs, per day:

* sleep — ``get_sleep_by_day`` hours and wake time, both **exactly**
  (compact sleep intervals are stored at full precision, so any difference
  is a real one);
* activity — for each metric, ``get_activity_by_day`` (daily records,
  ``1440``) and ``get_activity_records`` at each other ``--resolutions``
  value, compared slot by slot after summing tied rows (legacy returns one
  tuple per tied device row, compact one pre-summed tuple per slot), within
  ``--tolerance`` per value.  Steps are stored as whole numbers, so for
  steps the tolerance is at least 0.5.

Prints a per-check table and a final ``PASS`` / ``FAIL`` line; exits
non-zero on any mismatch.  The legacy activity path requires PostgreSQL; on
other backends only sleep is compared.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import override_settings

from healthdatamodel.models import Record
from healthdatamodel.query import (
    ActivityMetric,
    get_activity_by_day,
    get_activity_records,
    get_sleep_by_day,
)

_SCAN_PAGE = 5000

#: Absolute slack for float noise in "exact" comparisons (sleep hours).
_EXACT = 1e-9

#: Compact steps are rounded to whole numbers at write time, so a stored
#: step value can differ from the raw legacy value by up to 0.5.
_STEPS_ROUNDING = 0.5


def _approx_equal(a, b, tolerance: float) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tolerance


def _metric_tolerance(metric: ActivityMetric, tolerance: float) -> float:
    if metric == ActivityMetric.STEPS:
        return max(tolerance, _STEPS_ROUNDING)
    return tolerance


def recent_customer_ids(sample: int, scan_limit: int) -> list:
    """Customer pks with the most recent uploads, newest first.

    Walks ``Record`` backwards by primary key (insertion order, i.e. upload
    order) — an index scan, cheap even on a very large table — collecting
    distinct customers until *sample* are found or *scan_limit* rows have
    been read.
    """
    found: list = []
    seen: set = set()
    last_pk = None
    scanned = 0
    while len(found) < sample and scanned < scan_limit:
        queryset = Record.objects.order_by("-pk")
        if last_pk is not None:
            queryset = queryset.filter(pk__lt=last_pk)
        page = list(queryset.values_list("pk", "customer_id")[:_SCAN_PAGE])
        if not page:
            break
        for pk, customer_id in page:
            if customer_id not in seen:
                seen.add(customer_id)
                found.append(customer_id)
                if len(found) == sample:
                    break
        scanned += len(page)
        last_pk = page[-1][0]
    return found


class _Stats:
    def __init__(self):
        self.days = 0
        self.with_data = 0
        self.mismatched = 0


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--sample",
            type=int,
            default=20,
            help="Number of customers with the most recent uploads to check "
            "(default 20).  Ignored with --customers.",
        )
        parser.add_argument(
            "--customers",
            nargs="+",
            metavar="PK",
            help="Check exactly these customer primary keys.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Days of history to compare, ending today UTC (default 30).",
        )
        parser.add_argument(
            "--end",
            type=date.fromisoformat,
            help="Last day to compare (YYYY-MM-DD, default today UTC).",
        )
        parser.add_argument(
            "--tolerance",
            type=float,
            default=0.2,
            help="Numeric tolerance per activity value; compact energy "
            "values are rounded to 0.1 kcal at write time (default 0.2; "
            "steps always allow at least 0.5, sleep is compared exactly).",
        )
        parser.add_argument(
            "--resolutions",
            type=int,
            nargs="+",
            default=[15, 1440],
            help="Activity resolutions in minutes to compare (default 15 1440).",
        )
        parser.add_argument(
            "--scan-limit",
            type=int,
            default=500_000,
            help="Max Record rows scanned (newest first) to find --sample "
            "customers (default 500000).",
        )
        parser.add_argument(
            "--max-report",
            type=int,
            default=20,
            help="Print at most this many mismatch details (default 20).",
        )

    def handle(self, *args, **options):
        if options["days"] < 1:
            raise CommandError("--days must be >= 1")
        end = options["end"] or datetime.now(timezone.utc).date()
        start = end - timedelta(days=options["days"] - 1)
        tolerance = options["tolerance"]
        self._max_report = options["max_report"]
        self._reported = 0

        if options["customers"]:
            customer_ids = options["customers"]
        else:
            customer_ids = recent_customer_ids(options["sample"], options["scan_limit"])
        if not customer_ids:
            self.stdout.write(
                "PASS: no customers with Record data — nothing to compare."
            )
            return
        customers = list(get_user_model().objects.filter(pk__in=customer_ids))
        missing = {str(pk) for pk in customer_ids} - {str(c.pk) for c in customers}
        if missing:
            raise CommandError(f"Unknown customer pk(s): {', '.join(sorted(missing))}")

        activity_supported = connection.vendor == "postgresql"
        checks: dict[str, _Stats] = {"sleep": _Stats()}
        if activity_supported:
            for metric in ActivityMetric:
                for resolution in options["resolutions"]:
                    checks[f"{metric.name}@{resolution}min"] = _Stats()
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Legacy activity path requires PostgreSQL — comparing sleep only."
                )
            )

        self.stdout.write(
            f"verify_compact: {len(customers)} customer(s), {start}..{end} "
            f"({options['days']} days), tolerance {tolerance}"
        )
        for customer in customers:
            self._compare_sleep(customer, start, end, checks["sleep"])
            if not activity_supported:
                continue
            for metric in ActivityMetric:
                for resolution in options["resolutions"]:
                    stats = checks[f"{metric.name}@{resolution}min"]
                    self._compare_activity(
                        customer,
                        metric,
                        resolution,
                        start,
                        end,
                        _metric_tolerance(metric, tolerance),
                        stats,
                    )

        self.stdout.write(
            f"  {'check':<28}{'days':>8}{'with data':>11}{'mismatched':>12}"
        )
        for name, stats in checks.items():
            self.stdout.write(
                f"  {name:<28}{stats.days:>8}{stats.with_data:>11}{stats.mismatched:>12}"
            )
        total_days = sum(s.days for s in checks.values())
        mismatches = sum(s.mismatched for s in checks.values())
        scope = "" if activity_supported else " (sleep only)"
        if mismatches:
            self.stdout.write(
                self.style.ERROR(
                    f"FAIL: {mismatches} mismatched day(s) out of {total_days} "
                    f"compared{scope}."
                )
            )
            raise CommandError(f"{mismatches} mismatch(es) between compact and legacy")
        self.stdout.write(
            self.style.SUCCESS(
                f"PASS: compact and legacy reads agree on all {total_days} "
                f"customer-day checks{scope}."
            )
        )

    # -- comparisons -------------------------------------------------------

    def _report(self, line: str) -> None:
        if self._reported < self._max_report:
            self.stdout.write(f"  MISMATCH {line}")
        elif self._reported == self._max_report:
            self.stdout.write("  ... (further mismatches not shown; see --max-report)")
        self._reported += 1

    def _compare_sleep(self, customer, start, end, stats):
        with override_settings(HEALTHDATAMODEL_READ_COMPACT=True):
            compact = get_sleep_by_day(customer, start, end)
        with override_settings(HEALTHDATAMODEL_READ_COMPACT=False):
            legacy = get_sleep_by_day(customer, start, end)
        for day in compact:
            comp, leg = compact[day], legacy[day]
            stats.days += 1
            if comp.hours is not None or leg.hours is not None:
                stats.with_data += 1
            if not _approx_equal(comp.hours, leg.hours, _EXACT) or (
                comp.wake_time != leg.wake_time
            ):
                stats.mismatched += 1
                self._report(
                    f"sleep customer={customer.pk} {day}: compact={comp} legacy={leg}"
                )

    def _compare_activity(
        self, customer, metric, resolution, start, end, tolerance, stats
    ):
        if resolution == 1440:
            with override_settings(HEALTHDATAMODEL_READ_COMPACT=True):
                compact = get_activity_by_day(customer, metric, start, end)
            with override_settings(HEALTHDATAMODEL_READ_COMPACT=False):
                legacy = get_activity_by_day(customer, metric, start, end)
            for day in compact:
                stats.days += 1
                if compact[day] is not None or legacy[day] is not None:
                    stats.with_data += 1
                if not _approx_equal(compact[day], legacy[day], tolerance):
                    stats.mismatched += 1
                    self._report(
                        f"{metric.name}@1440min customer={customer.pk} {day}: "
                        f"compact={compact[day]} legacy={legacy[day]}"
                    )
            return

        start_dt = datetime.combine(start, time(0)).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(end + timedelta(days=1), time(0)).replace(
            tzinfo=timezone.utc
        )

        def by_day(records):
            # day -> {slot start: summed value} (sums legacy tied device rows)
            out: dict[date, dict[datetime, float]] = defaultdict(
                lambda: defaultdict(float)
            )
            for slot_start, _slot_end, value in records:
                out[slot_start.date()][slot_start] += value
            return out

        with override_settings(HEALTHDATAMODEL_READ_COMPACT=True):
            compact = by_day(
                get_activity_records(customer, metric, start_dt, end_dt, resolution)
            )
        with override_settings(HEALTHDATAMODEL_READ_COMPACT=False):
            legacy = by_day(
                get_activity_records(customer, metric, start_dt, end_dt, resolution)
            )
        for i in range((end - start).days + 1):
            day = start + timedelta(days=i)
            stats.days += 1
            comp, leg = compact.get(day, {}), legacy.get(day, {})
            if not comp and not leg:
                continue
            stats.with_data += 1
            bad = sorted(
                slot
                for slot in set(comp) | set(leg)
                if not _approx_equal(comp.get(slot), leg.get(slot), tolerance)
            )
            if bad:
                stats.mismatched += 1
                first = bad[0]
                self._report(
                    f"{metric.name}@{resolution}min customer={customer.pk} {day}: "
                    f"{len(bad)} slot(s) differ, first {first:%H:%M} "
                    f"compact={comp.get(first)} legacy={leg.get(first)}"
                )
