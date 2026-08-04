"""Compare compact-table query results against the legacy Record path.

The consumer's pre-flight check before turning ``Record`` writes off: for a
sample of customers with data, runs the public query API in both read modes
and diffs the results.  Exits non-zero when any mismatch is found.

The legacy activity path requires PostgreSQL; on other backends only sleep
is compared.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.test.utils import override_settings

from healthdatamodel.models import Record
from healthdatamodel.query import (
    ActivityMetric,
    get_activity_by_day,
    get_sleep_by_day,
)


def _approx_equal(a, b, tolerance: float) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tolerance


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--sample",
            type=int,
            default=20,
            help="Number of customers to check (default 20).",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Days of history to compare (default 30).",
        )
        parser.add_argument(
            "--tolerance",
            type=float,
            default=0.2,
            help="Numeric tolerance; compact energy values are rounded to "
            "0.1 kcal at write time (default 0.2).",
        )

    def handle(self, *args, **options):
        end = date.today()
        start = end - timedelta(days=options["days"])
        tolerance = options["tolerance"]
        customer_ids = list(
            Record.objects.values_list("customer_id", flat=True).distinct()[
                : options["sample"]
            ]
        )
        if not customer_ids:
            self.stdout.write("No customers with Record data — nothing to verify.")
            return

        from django.contrib.auth import get_user_model

        customers = get_user_model().objects.filter(pk__in=customer_ids)
        activity_supported = connection.vendor == "postgresql"
        if not activity_supported:
            self.stdout.write(
                self.style.WARNING(
                    "Legacy activity path requires PostgreSQL — comparing sleep only."
                )
            )

        mismatches = 0
        for customer in customers:
            with override_settings(HEALTHDATAMODEL_READ_COMPACT=True):
                compact_sleep = get_sleep_by_day(customer, start, end)
            with override_settings(HEALTHDATAMODEL_READ_COMPACT=False):
                legacy_sleep = get_sleep_by_day(customer, start, end)
            for day in compact_sleep:
                compact_day, legacy_day = compact_sleep[day], legacy_sleep[day]
                if not _approx_equal(
                    compact_day.hours, legacy_day.hours, tolerance
                ) or (compact_day.wake_time != legacy_day.wake_time):
                    mismatches += 1
                    self.stdout.write(
                        f"SLEEP mismatch customer={customer.pk} {day}: "
                        f"compact={compact_day} legacy={legacy_day}"
                    )

            if not activity_supported:
                continue
            for metric in ActivityMetric:
                with override_settings(HEALTHDATAMODEL_READ_COMPACT=True):
                    compact_activity = get_activity_by_day(customer, metric, start, end)
                with override_settings(HEALTHDATAMODEL_READ_COMPACT=False):
                    legacy_activity = get_activity_by_day(customer, metric, start, end)
                for day in compact_activity:
                    if not _approx_equal(
                        compact_activity[day], legacy_activity[day], tolerance
                    ):
                        mismatches += 1
                        self.stdout.write(
                            f"ACTIVITY mismatch customer={customer.pk} "
                            f"{metric.name} {day}: "
                            f"compact={compact_activity[day]} "
                            f"legacy={legacy_activity[day]}"
                        )

        if mismatches:
            self.stderr.write(self.style.ERROR(f"{mismatches} mismatch(es) found."))
            raise SystemExit(1)
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified {len(customer_ids)} customer(s) over {options['days']} "
                "days — compact and legacy paths agree."
            )
        )
