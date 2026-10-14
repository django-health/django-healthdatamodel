"""Optionally seed the compact tables (SleepDay / ActivityDay) from Record.

**A no-op by default.**  It only does work when the project sets
``HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = True``, which is meant for small
installs (development databases, demo projects, test fixtures): the whole
backfill then runs inside this migration's transaction.

Large installs leave the setting unset and run ``manage.py backfill_compact``
after migrating — it works in bounded customer batches, commits per upload,
is resumable (idempotent) and is safe to run while dual-writes are live.
See ``docs/UPGRADING-1.0.md``.

Note: this migration intentionally uses the *live* models/routing code rather
than historical models, so backfilled rows are produced by exactly the same
code path as live ingest.  That is safe here because it is shipped in the
same release that introduces the compact schema (0011) — anyone migrating
past 0012 has models matching the live code for these tables.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    from healthdatamodel.compact import backfill_in_migration_enabled

    if not backfill_in_migration_enabled():
        return
    from healthdatamodel.compat import backfill_from_records

    backfill_from_records()


class Migration(migrations.Migration):
    dependencies = [
        ("healthdatamodel", "0011_activityday_sleepday"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
