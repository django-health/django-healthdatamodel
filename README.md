# django-healthdatamodel

[![PyPI](https://img.shields.io/pypi/v/django-healthdatamodel.svg)](https://pypi.org/project/django-healthdatamodel/)
[![CI](https://github.com/andyreagan/django-healthdatamodel/actions/workflows/ci.yml/badge.svg)](https://github.com/andyreagan/django-healthdatamodel/actions/workflows/ci.yml)

A reusable Django app for storing and querying health data in a schema inspired by [Apple HealthKit](https://developer.apple.com/documentation/healthkit).

## Models

### SleepDay / ActivityDay — compact storage (v1.0)

The compact storage format introduced in v1.0, written alongside `Record` (dual-write) and read when `HEALTHDATAMODEL_READ_COMPACT = True` (see [settings](#compact-storage-settings--migration-v10) and the [upgrade checklist](docs/UPGRADING-1.0.md)). One row per key, no duplicates:

- **`SleepDay`** — one night of sleep per `(customer, device, day)`, where `day` is the 2 pm–2 pm UTC sleep window. Intervals (with sleep stage) live in a JSON list on the row. A new upload for that user–device **replaces** the day.
- **`ActivityDay`** — one day of one metric per `(customer, source, device, metric, day, resolution)`, holding a vector of slot values (96 slots at 15-minute resolution; 1 slot at daily). New uploads **merge slot-wise**: incoming slots overwrite, untouched slots keep their value. `None` slots mean "no data", preserving the None-vs-0.0 distinction in the query API.

Values are normalised at write time (kcal, non-negative, steps as int, energy rounded to 0.1 kcal), so reads are straight sums. Compared to the `Record` log this writes roughly 100× fewer rows, and the compact activity query path doesn't need PostgreSQL.

### Record

Stores individual health measurements in the original append-only log format. Each record has a `type` (e.g. `HKQuantityTypeIdentifierActiveEnergyBurned`), a `value`, a `unit`, and a `startDate`/`endDate` range. Records are associated with a user via `settings.AUTH_USER_MODEL`. Since v1.0 this is an optional write-through log (see `HEALTHDATAMODEL_SAVE_RECORDS` below).

### Workout

Stores workout sessions. Each entry has a `workoutActivityType`, a duration, a time range, and source metadata.

### WearableConnection

Tracks a user's connected wearable devices. A user can have multiple simultaneous connections (e.g. Apple Watch for activity, a Garmin for sleep). Each connection has a `data_source` (the data pipeline: `apple_health`, `fitbit`, `health_connect`), a `device_brand`, a lifecycle `status` (`active` / `disconnected`), and a `preferred_for_sleep` flag.

### DataSourceRanking

When a user has records from more than one active data source for the same time window, `DataSourceRanking` determines which source takes precedence. Rankings are maintained automatically by the query API.

## Installation

```
pip install django-healthdatamodel
```

Add to `INSTALLED_APPS` and run migrations:

```python
INSTALLED_APPS = [
    ...
    "healthdatamodel",
]
```

```
python manage.py migrate
```

The models use `settings.AUTH_USER_MODEL` so they work with any custom user model.

## Query API (`healthdatamodel.query`)

The query module provides day-level aggregates and record-level queries. Callers never touch `Record` directly.

```python
from healthdatamodel.query import (
    ActivityMetric,
    SleepValue,
    SLEEP_TYPE,
    DailySleep,
    ensure_ranks,
    has_competing_sources,
    get_sleep_hours_by_day,
    get_sleep_by_day,
    get_activity_by_day,
    get_activity_records,
)
```

### Type constants

`ActivityMetric` and `SleepValue` are `StrEnum` subclasses — their values are the HK type strings, usable wherever a raw string is expected. Use them on both the ingest and query side so strings stay in sync.

```python
ActivityMetric.ACTIVE_CALORIES  # "HKQuantityTypeIdentifierActiveEnergyBurned"
ActivityMetric.BASAL_CALORIES  # "HKQuantityTypeIdentifierBasalEnergyBurned"
ActivityMetric.STEPS  # "HKQuantityTypeIdentifierStepCount"

SleepValue.ASLEEP_UNSPECIFIED  # "HKCategoryValueSleepAnalysisAsleepUnspecified"
SleepValue.ASLEEP_CORE  # "HKCategoryValueSleepAnalysisAsleepCore"
SleepValue.ASLEEP_DEEP  # "HKCategoryValueSleepAnalysisAsleepDeep"
SleepValue.ASLEEP_REM  # "HKCategoryValueSleepAnalysisAsleepREM"
SleepValue.AWAKE  # "HKCategoryValueSleepAnalysisAwake"
SleepValue.IN_BED  # "HKCategoryValueSleepAnalysisInBed"

SLEEP_TYPE  # "HKCategoryTypeIdentifierSleepAnalysis"
```

### Sleep

```python
from datetime import date
from healthdatamodel.query import get_sleep_hours_by_day, get_sleep_by_day

# Hours only
hours = get_sleep_hours_by_day(customer, date(2025, 6, 1), date(2025, 6, 7))
# {date(2025, 6, 1): 7.5, date(2025, 6, 2): None, ...}
# None  → no records for that night
# 0.0   → records exist but cover zero sleep
# float → hours slept

# Hours + wake time
results = get_sleep_by_day(customer, date(2025, 6, 1), date(2025, 6, 7))
# {date: DailySleep(hours=7.5, wake_time=datetime(..., 7, 0, tzinfo=utc)), ...}
```

`DailySleep.wake_time` is the end of the last sleep interval capped at the day boundary, unrounded. Apply `round_up_15` or similar in the caller if needed.

The day boundary defaults to **14:00 UTC** (2 pm), giving a window of 2 pm the previous day → 2 pm the current day. Pass `day_boundary_hour` to override.

Device preference is read from `WearableConnection`. When multiple sleep sources are present, the `preferred_for_sleep` device wins; the default fallback order is `oura → whoop → apple → garmin`.

Sleep functions work with any Django-supported backend (SQLite, PostgreSQL, etc.) in both read modes. The compact tables store sleep at the canonical 2 pm boundary, but the stored interval pieces tile the original records exactly, so they can still serve any `day_boundary_hour`.

### Activity

```python
from datetime import date, datetime, timezone
from healthdatamodel.query import (
    ActivityMetric,
    get_activity_by_day,
    get_activity_records,
)

# Daily totals
totals = get_activity_by_day(
    customer, ActivityMetric.ACTIVE_CALORIES, date(2025, 6, 1), date(2025, 6, 7)
)
# {date: kcal | None}

# Records at any resolution (default 15 min)
start = datetime(2025, 6, 1, tzinfo=timezone.utc)
end = datetime(2025, 6, 8, tzinfo=timezone.utc)
records = get_activity_records(
    customer, ActivityMetric.STEPS, start, end, resolution_minutes=15
)
# [(startDate, endDate, value), ...]  — gaps not filled
```

`get_activity_by_day` is a convenience wrapper around `get_activity_records(resolution_minutes=1440)`.

The legacy read path (`HEALTHDATAMODEL_READ_COMPACT = False`, the default in 1.0) requires **PostgreSQL** (window-function CTEs for source-ranked deduplication). With the compact read path (`HEALTHDATAMODEL_READ_COMPACT = True`) they work on **any backend**.

### Ranking and source utilities

```python
from healthdatamodel.query import ensure_ranks, has_competing_sources
from healthdatamodel.constants import DataSource

# Ensure DataSourceRanking rows exist for the customer.
# No-op if valid; called automatically by the activity functions.
ensure_ranks(customer)

# Check whether records from other sources exist in a window.
# Useful on the ingest path — if False, in-memory records can be
# used directly without re-querying the DB.
if has_competing_sources(customer, DataSource.APPLE_HEALTH, start, end):
    totals = get_activity_by_day(customer, metric, start_date, end_date)
else:
    totals = ...  # use in-memory results from ingest (see below)
```

## Ingest API (`healthdatamodel.ingest`)

The ingest module saves health data without exposing `Record` model objects to callers. Build
`RecordInput` objects (or use the compact helpers) and pass them to the ingest functions.

```python
from healthdatamodel.schemas import RecordInput
from healthdatamodel.ingest import ingest_records, aingest_records  # async variant
from healthdatamodel.constants import DataSource
```

**Full format** — supply `RecordInput` objects directly (Apple Health XML, Health Connect, etc.):

```python
records = [
    RecordInput(
        startDate=start,
        endDate=end,
        creationDate=created,
        sourceName="Apple Watch",
        value="350.5",
        unit="kcal",
        type=ActivityMetric.ACTIVE_CALORIES,
    ),
    ...,
]
ingest_records(customer, records, source=DataSource.APPLE_HEALTH)
```

**Compact format** — float arrays at a fixed resolution, one array per source:

```python
from healthdatamodel.ingest import ingest_compact_activity, aingest_compact_activity

ingest_compact_activity(
    customer=customer,
    metric=ActivityMetric.ACTIVE_CALORIES,
    start=week_start,  # datetime
    values_by_source=[
        ([300.0, 0.0, 250.0, ...], "apple"),  # one array of 15-min values per source
    ],
    resolution_minutes=15,
    unit="kcal",
    source=DataSource.APPLE_HEALTH,
)
```

One `Record` row is stored **per source per interval** — source-ranked deduplication happens at query time via `get_activity_records`.

Every ingest call also writes the compact tables (`SleepDay` / `ActivityDay`, see [below](#compact-storage-settings--migration-v10)), in the same transaction as the `Record` insert.

### Async usage

Both formats have `async` variants. They run the sync ingest in a worker thread (`sync_to_async`), because the compact write needs a transaction with row locks:

```python
await aingest_records(customer, records, source=DataSource.APPLE_HEALTH)
await aingest_compact_activity(customer, metric, start, values_by_source, ...)
```

### Fast path: in-memory results after ingest

After inserting data from a single source, `has_competing_sources` will return `False`, meaning the query functions would return the same data you just inserted. `ingest_compact_activity` (and its async variant) accept a `return_results=True` flag that returns the computed day-level aggregates from memory rather than re-querying the database:

```python
totals = ingest_compact_activity(
    customer, metric, start, values_by_source, ..., return_results=True
)
# Returns dict[date, float | None] computed in-memory — no round-trip to DB
```

This is equivalent to calling `get_activity_by_day` immediately after ingest when there is only one source. Only reliable when `has_competing_sources` would return `False`.

### In-memory query

`get_activity_by_day_from_records` performs the same daily aggregation as `get_activity_by_day` but operates on a list of `RecordInput` objects already in memory:

```python
from healthdatamodel.query import get_activity_by_day_from_records
from healthdatamodel.ingest import expand_compact_activity

records = expand_compact_activity(
    metric, start, values_by_source, resolution_minutes, unit
)
totals = get_activity_by_day_from_records(records, metric, start_date, end_date)
# dict[date, float | None] — no database query
```

## Consumer helpers

**These functions are the supported entry points for everything a host
project does with a customer's health data.** Direct ORM access to `Record`,
`Workout` or `WorkoutMetadataEntry` (`customer.record_set...`,
`Record.objects.filter(...).update(...)`, `Record(...)` +
`bulk_create`, raw SQL over `healthdatamodel_record`) is **unsupported from
v1.0 onward**: v1.0 adds compact storage tables that must be kept in sync
with `Record`, and only these helpers (plus the query and ingest APIs above)
do that. They were introduced in v0.11 as thin wrappers over the existing
tables; from v1.0 the ones that write are table-aware.

**Which tables each helper touches in 1.0:**

| Helper | Tables | Notes |
| --- | --- | --- |
| `delete_records`, `delete_customer_health_data` | `Record` + `SleepDay` + `ActivityDay` (+ workouts / rankings) | One transaction. |
| `reassign_customer_health_data` | `Record` + `SleepDay` + `ActivityDay` + `Workout` | One transaction. Rebuilds the target's compact rows from `Record`, so **requires `HEALTHDATAMODEL_SAVE_RECORDS = True`**. |
| `ingest.restore_records` | `Record` + `SleepDay` + `ActivityDay` | One transaction; compact writes use the backfill's replay rules. |
| `testing.create_record(s)` | `Record` + `SleepDay` + `ActivityDay` | Through `ingest_records`; visible in both read modes. |
| `has_records`, `get_data_source_summary`, `query.get_sleep_records`, `query.infer_resolution_minutes`, `query.iter_records` | `Record` only | **Require `HEALTHDATAMODEL_SAVE_RECORDS = True`** and only see what your `Record` retention keeps, whatever `HEALTHDATAMODEL_READ_COMPACT` says. |
| `query.iter_workouts` | `Workout` only | |

| Need | Helper |
| --- | --- |
| Save records / workouts | `ingest.ingest_records`, `ingest.ingest_workouts` |
| Restore archived rows verbatim | `ingest.restore_records` |
| Does the customer have any data? | `customer_data.has_records` |
| Fitness / onboarding reset | `customer_data.delete_records` |
| Delete everything (data rights) | `customer_data.delete_customer_health_data` |
| Account merge | `customer_data.reassign_customer_health_data` |
| Per-source sync dates + rank | `customer_data.get_data_source_summary` |
| Resolution of the newest activity data | `query.infer_resolution_minutes` |
| Raw sleep records in a window | `query.get_sleep_records` |
| Export a customer's rows | `query.iter_records`, `query.iter_workouts` |
| Test fixtures | `testing.create_records`, `testing.create_record`, `testing.record_input` |

### Customer lifecycle (`healthdatamodel.customer_data`)

```python
from healthdatamodel.customer_data import (
    has_records,
    delete_records,
    delete_customer_health_data,
    reassign_customer_health_data,
    get_data_source_summary,
)

has_records(customer)  # -> bool

delete_records(customer)  # -> int (Record rows); also deletes compact rows

delete_customer_health_data(customer)  # one transaction
# -> {"records": 120, "sleep_days": 30, "activity_days": 90,
#     "workouts": 3, "workout_metadata": 9, "rankings": 8}
delete_customer_health_data(
    customer, rankings=False
)  # flags: records (Record + compact tables) / workouts / rankings

# Account merge: re-point rows (no copy).
if not has_records(current) and has_records(existing):
    reassign_customer_health_data(existing, current)  # -> {"records": n, "workouts": n}
```

`delete_customer_health_data` never touches `WearableConnection`.
`reassign_customer_health_data` moves records and workouts (metadata follows
its workout) but not `DataSourceRanking` (rebuilt by `ensure_ranks`) or
`WearableConnection`; pass `workouts=False` to move records only. `Record`
rows are not de-duplicated. Compact rows are: where the target already has a
row for the same sleep `(device, day)` or activity key, the two are merged
(the newer upload wins, the older one fills empty activity slots), and then
the target's compact rows are rebuilt from its merged `Record` rows with the
backfill's replay rules, so the target reads as if every upload had been
ingested for it. It raises `ImproperlyConfigured` if
`HEALTHDATAMODEL_SAVE_RECORDS` is `False`, and `ValueError` if both
arguments are the same customer.

`get_data_source_summary(customer)` returns `dict[str, DataSourceSummary]`
keyed by data source, covering every source that has records **or** a
ranking row:

```python
summary = get_data_source_summary(customer)
s = summary["apple_health"]
s.last_sync_date  # MAX(admin_create_date): when data was last received
s.connected_date  # MIN(admin_create_date): when data was first received
s.most_recent_data_date  # MAX(endDate): freshness of the data itself
s.rank  # DataSourceRanking.rank, or None
s.has_data  # connected_date is not None
```

A ranked source with no records has all dates `None`; a source with records
but no ranking row has `rank=None`; blank/`NULL` sources are omitted. Works
on any backend.

### Record-level readers (`healthdatamodel.query`)

These return frozen `StoredRecord` / `StoredWorkout` dataclasses
(`healthdatamodel.schemas`), not model instances. They have the familiar
field names (`startDate`, `endDate`, `value`, `sourceName`, `source`,
`type`, `unit`, `admin_create_date`, ...) plus `customer_id`, but **no
primary key**. In 1.0 they read `Record` / `Workout` in both read modes, so
they require `HEALTHDATAMODEL_SAVE_RECORDS = True`.

```python
from healthdatamodel.query import (
    get_sleep_records,
    infer_resolution_minutes,
    iter_records,
    iter_workouts,
)

# Every sleep-analysis record (all values, all sources, all uploads) with
# start <= startDate <= end, ordered by startDate.
rows = get_sleep_records(customer, start_dt, end_dt)
rows = get_sleep_records(customer, start_dt, end_dt, asleep_only=True)

# Interval length, in whole minutes, of the most recently uploaded
# step/active-energy record lying inside [start, end]; None if there is none.
infer_resolution_minutes(customer, wk_start, wk_end)  # e.g. 15 or 1440
infer_resolution_minutes(customer, wk_start, wk_end, source=DataSource.FITBIT)
infer_resolution_minutes(customer, wk_start, wk_end, types=[ActivityMetric.STEPS])

# Exports: newest first by default, streamed in chunks.
for r in iter_records(customer, limit=10_000):
    ...
for w in iter_workouts(customer, limit=5_000, order="startDate"):
    ...
```

### Restoring archived records (`healthdatamodel.ingest.restore_records`)

`ingest_records` stamps one `source` and one `admin_create_date` on the
whole batch. To put back rows exported from `healthdatamodel_record` (e.g.
a parquet archive) use `restore_records`, which keeps each row's own
customer, `source`, `creationDate` and `admin_create_date`:

```python
from healthdatamodel.ingest import restore_records

n = restore_records(df.to_dict("records"), batch_size=1000)
```

It accepts `StoredRecord` objects or mappings (`customer_id` or `customer`,
plus `startDate`, `endDate`, `creationDate`, `sourceName`, `source`, `value`,
`type`, `admin_create_date`; optional `unit`, `sourceVersion`, `device`).
An archived `id` is ignored, NaN in an optional column becomes `NULL`, and
the iterable is consumed one batch at a time. The whole call is one
transaction (all or nothing).

From 1.0 the restored sleep and activity rows are also written to the
compact tables (unless `HEALTHDATAMODEL_SAVE_COMPACT = False`), grouped per
upload `(customer, admin_create_date, source)` and replayed oldest first: a
restored upload older than what is stored never overwrites newer data
(activity only fills empty slots), and one with the same `admin_create_date`
replaces its own earlier write. So repeating a restore leaves the compact
tables unchanged, although `Record`, which has no unique key, stores the
rows twice. Pass each upload whole within one call. Its compactable rows
are kept in memory until the input is exhausted, so restore a large archive
one customer (or one batch of customers) per call.

### Ingest notes

`ingest_records` returns the number of records submitted and
`ingest_workouts` the number of workouts saved.
`ingest_workouts` runs in one transaction and bulk-inserts where the backend
returns primary keys. `RecordInput.value` accepts plain numbers (e.g. Fitbit
API values) and stringifies them; negative values are stored as `"0.0"`, as
negative strings already were.

## Compact storage settings & migration (v1.0)

> **Upgrading an existing install from 0.11.x?** Follow
> [`docs/UPGRADING-1.0.md`](docs/UPGRADING-1.0.md), a numbered checklist
> covering settings, migrate, dual-write soak, batched backfill,
> verification, the per-environment read switch, and rollback.

The defaults are deliberately conservative. A project that sets nothing gets:
**DDL only** (migration `0011` creates the two empty compact tables, and
`0012` is a no-op), **dual writes** (every upload goes to both `Record` and
the compact tables), and **legacy reads** (the query API keeps reading
`Record`). Switching reads over is an explicit opt-in, made after the backfill
and verification.

| Setting | Default | Meaning |
|---|---|---|
| `HEALTHDATAMODEL_SAVE_RECORDS` | `True` | Write every upload through to the legacy `Record` log. This is the rollback path, so keep it on throughout 1.0.x. `reassign_customer_health_data` and the `Record`-reading helpers (`has_records`, `get_data_source_summary`, `get_sleep_records`, `infer_resolution_minutes`, `iter_records`) depend on it. Per-call override: `ingest_records(..., save_records=...)`. |
| `HEALTHDATAMODEL_SAVE_COMPACT` | `True` | Write every upload to `SleepDay` / `ActivityDay`. |
| `HEALTHDATAMODEL_READ_COMPACT` | `False` | Serve the query API from the compact tables. Set it to `True` per environment once `compact_status` and `verify_compact` pass. Setting it back to `False` rolls back with no data loss. The legacy path needs PostgreSQL for activity reads. |
| `HEALTHDATAMODEL_BACKFILL_IN_MIGRATION` | `False` | Let migration `0012` seed the compact tables from `Record` inside the migration transaction. **Only for small installs** (development, demo, CI). |

New installs with no legacy data can set `HEALTHDATAMODEL_READ_COMPACT = True`
right away. The compact path works on any database backend.

Records the compact schema can't represent (unknown HK types, intervals not
aligned to a day-dividing grid) are always written to `Record`, even with
`save_records=False`, so nothing is silently dropped. The package never
deletes `Record` rows.

### Management commands

```
# Seed the compact tables from Record. Idempotent, resumable, commits per
# upload, safe while dual-writes are live.
python manage.py backfill_compact [--customers PK ... | --customer-range LO HI]
                                  [--since ISO-DATETIME] [--customer-batch-size 200]
                                  [--chunk-size 2000] [--progress-every 1000]

# The gate before switching reads: settings, row counts, dual-write lag,
# per-customer backfill coverage, and a READY / NOT READY verdict.
python manage.py compact_status [--customers PK ... | --customer-range LO HI]
                                [--skip-coverage] [--show-pending 20]
                                [--lag-scan-limit 1000000] [--fail-if-not-ready]

# Parity check: runs the query API in both read modes and diffs per day.
# Prints PASS / FAIL and exits non-zero on any mismatch.
python manage.py verify_compact [--sample 20 | --customers PK ...] [--days 30]
                                [--end YYYY-MM-DD] [--tolerance 0.2]
                                [--resolutions 15 1440] [--scan-limit 500000]
                                [--max-report 20]
```

`verify_compact` compares sleep hours and wake times exactly and activity
values within `--tolerance` (steps always allow at least 0.5, since they are
stored as whole numbers). The known differences between the two read paths
(0.1 kcal rounding, the exact-resolution grid requirement, and others) are
listed in
[`docs/UPGRADING-1.0.md`](docs/UPGRADING-1.0.md#known-differences-between-legacy-and-compact-reads).

## Admin

Admin classes (`WorkoutAdmin`, `RecordAdmin`, `SleepDayAdmin`, `ActivityDayAdmin`, `WearableConnectionAdmin`, etc.) are defined in `healthdatamodel.admin` but **not registered** — registration is left to the host project:

```python
from django.contrib import admin
from healthdatamodel.admin import WearableConnectionAdmin as Base
from healthdatamodel.models import WearableConnection


@admin.register(WearableConnection)
class WearableConnectionAdmin(Base):
    search_fields = list(Base.search_fields) + ["customer__your_custom_field"]
```

## Test utilities

`healthdatamodel.testing` provides `set_customer_device()`, which creates or updates a `WearableConnection` for a customer and deactivates any conflicting connections:

```python
from healthdatamodel.testing import set_customer_device

set_customer_device(customer, data_source="apple_health", device_brand="apple")
```

Create fixture records **through the ingest path** so they are visible to
the query API in both read modes (records made with `Record.objects.create`
never reach the compact tables, so compact reads don't see them):

```python
from healthdatamodel.testing import create_record, create_records, record_input

# Drop-in for Record.objects.create(...): same keyword fields, sensible defaults
# (15-min step-count interval, value "0", sourceName "test", creationDate now).
create_record(
    customer=customer,
    startDate=start,
    endDate=end,
    type=SLEEP_TYPE,
    value=SleepValue.ASLEEP_UNSPECIFIED,
    source=DataSource.APPLE_HEALTH,  # default apple_health
    admin_create_date=ts,  # default now
)

# Many at once: RecordInput objects or dicts of RecordInput fields; dicts may
# carry their own "source" / "admin_create_date".
create_records(
    customer,
    [
        {"startDate": t, "value": 20, "type": ActivityMetric.ACTIVE_CALORIES}
        for t in times
    ],
    source=DataSource.FITBIT,
    admin_create_date=ts,
)

record_input(
    startDate=t, value=5
)  # just build a RecordInput (endDate = start + 15 min)
```

## Demo project

A minimal Django project is included under `demo/` to show the models and admin working end-to-end against Django's built-in `auth.User`:

```
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then visit `/admin/`.
