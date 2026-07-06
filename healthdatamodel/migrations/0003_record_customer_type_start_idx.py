from django.db import migrations, models


def create_index(apps, schema_editor):
    # Build CONCURRENTLY on PostgreSQL to avoid locking the table in production;
    # other backends (e.g. SQLite) get a plain, portable CREATE INDEX.
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            'CREATE INDEX CONCURRENTLY IF NOT EXISTS "record_customer_type_start_idx" '
            'ON "healthdatamodel_record" ("customer_id", "type", "startDate")'
        )
    else:
        schema_editor.execute(
            'CREATE INDEX IF NOT EXISTS "record_customer_type_start_idx" '
            'ON "healthdatamodel_record" ("customer_id", "type", "startDate")'
        )


def drop_index(apps, schema_editor):
    keyword = "CONCURRENTLY " if schema_editor.connection.vendor == "postgresql" else ""
    schema_editor.execute(
        f'DROP INDEX {keyword}IF EXISTS "record_customer_type_start_idx"'
    )


class Migration(migrations.Migration):
    # CONCURRENTLY cannot run inside a transaction; atomic=False required.
    atomic = False

    dependencies = [
        ("healthdatamodel", "0002_wearableconnection"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(create_index, drop_index),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="record",
                    index=models.Index(
                        fields=["customer", "type", "startDate"],
                        name="record_customer_type_start_idx",
                    ),
                ),
            ],
        ),
    ]
