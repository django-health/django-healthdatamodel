from django.db import migrations
from django.db.models import F


def backfill_activated_at(apps, schema_editor):
    """Seed activated_at from connected_at for rows that predate the field."""
    WearableConnection = apps.get_model("healthdatamodel", "WearableConnection")
    WearableConnection.objects.filter(activated_at__isnull=True).update(
        activated_at=F("connected_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("healthdatamodel", "0009_wearableconnection_activated_at"),
    ]

    operations = [
        migrations.RunPython(backfill_activated_at, migrations.RunPython.noop),
    ]
