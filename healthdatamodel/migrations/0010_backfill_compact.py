"""Seed the compact tables (SleepDay / ActivityDay) from the Record table.

Skip on large installs by setting ``HEALTHDATAMODEL_SKIP_BACKFILL_MIGRATION =
True`` and running ``manage.py backfill_compact`` afterwards instead — the
command is batched, resumable (idempotent), and safe to run while dual-writes
are live.

Note: this migration intentionally uses the *live* models/routing code rather
than historical models, so backfilled rows are produced by exactly the same
code path as live ingest.  That is safe here because it is shipped in the
same release that introduces the compact schema (0009) — anyone migrating
past 0010 has models matching the live code for these tables.
"""

from django.conf import settings
from django.db import migrations


def forwards(apps, schema_editor):
    if getattr(settings, "HEALTHDATAMODEL_SKIP_BACKFILL_MIGRATION", False):
        return
    from healthdatamodel.compat import backfill_from_records

    backfill_from_records()


class Migration(migrations.Migration):
    dependencies = [
        ("healthdatamodel", "0009_activityday_sleepday"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
