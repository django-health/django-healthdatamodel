# django-healthdatamodel

[![PyPI](https://img.shields.io/pypi/v/django-healthdatamodel.svg)](https://pypi.org/project/django-healthdatamodel/)
[![CI](https://github.com/andyreagan/django-healthdatamodel/actions/workflows/ci.yml/badge.svg)](https://github.com/andyreagan/django-healthdatamodel/actions/workflows/ci.yml)

A reusable Django app for storing and querying health data in a schema inspired by [Apple HealthKit](https://developer.apple.com/documentation/healthkit).

## Models

### Record

Stores individual health measurements. Each record has a `type` (e.g. `HKQuantityTypeIdentifierActiveEnergyBurned`), a `value`, a `unit`, and a `startDate`/`endDate` range. Records are associated with a user via `settings.AUTH_USER_MODEL`.

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
    ActivityMetric, SleepValue, SLEEP_TYPE, DailySleep,
    ensure_ranks, has_competing_sources,
    get_sleep_hours_by_day, get_sleep_by_day,
    get_activity_by_day, get_activity_records,
)
```

### Type constants

`ActivityMetric` and `SleepValue` are `StrEnum` subclasses — their values are the HK type strings, usable wherever a raw string is expected. Use them on both the ingest and query side so strings stay in sync.

```python
ActivityMetric.ACTIVE_CALORIES  # "HKQuantityTypeIdentifierActiveEnergyBurned"
ActivityMetric.BASAL_CALORIES   # "HKQuantityTypeIdentifierBasalEnergyBurned"
ActivityMetric.STEPS            # "HKQuantityTypeIdentifierStepCount"

SleepValue.ASLEEP_UNSPECIFIED   # "HKCategoryValueSleepAnalysisAsleepUnspecified"
SleepValue.ASLEEP_CORE          # "HKCategoryValueSleepAnalysisAsleepCore"
SleepValue.ASLEEP_DEEP          # "HKCategoryValueSleepAnalysisAsleepDeep"
SleepValue.ASLEEP_REM           # "HKCategoryValueSleepAnalysisAsleepREM"
SleepValue.AWAKE                # "HKCategoryValueSleepAnalysisAwake"
SleepValue.IN_BED               # "HKCategoryValueSleepAnalysisInBed"

SLEEP_TYPE                      # "HKCategoryTypeIdentifierSleepAnalysis"
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

Sleep functions work with any Django-supported backend (SQLite, PostgreSQL, etc.).

### Activity

```python
from datetime import date, datetime, timezone
from healthdatamodel.query import ActivityMetric, get_activity_by_day, get_activity_records

# Daily totals
totals = get_activity_by_day(customer, ActivityMetric.ACTIVE_CALORIES, date(2025, 6, 1), date(2025, 6, 7))
# {date: kcal | None}

# Records at any resolution (default 15 min)
start = datetime(2025, 6, 1, tzinfo=timezone.utc)
end   = datetime(2025, 6, 8, tzinfo=timezone.utc)
records = get_activity_records(customer, ActivityMetric.STEPS, start, end, resolution_minutes=15)
# [(startDate, endDate, value), ...]  — gaps not filled
```

`get_activity_by_day` is a convenience wrapper around `get_activity_records(resolution_minutes=1440)`.

Both require **PostgreSQL** (window-function CTEs for source-ranked deduplication).

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
    ...
]
ingest_records(customer, records, source=DataSource.APPLE_HEALTH)
```

**Compact format** — float arrays at a fixed resolution, one array per source:

```python
from healthdatamodel.ingest import ingest_compact_activity, aingest_compact_activity

ingest_compact_activity(
    customer=customer,
    metric=ActivityMetric.ACTIVE_CALORIES,
    start=week_start,                          # datetime
    values_by_source=[
        ([300.0, 0.0, 250.0, ...], "apple"),   # one array of 15-min values per source
    ],
    resolution_minutes=15,
    unit="kcal",
    source=DataSource.APPLE_HEALTH,
)
```

One `Record` row is stored **per source per interval** — source-ranked deduplication happens at query time via `get_activity_records`.

### Async usage

Both formats have `async` variants that use Django's `abulk_create`:

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

records = expand_compact_activity(metric, start, values_by_source, resolution_minutes, unit)
totals = get_activity_by_day_from_records(records, metric, start_date, end_date)
# dict[date, float | None] — no database query
```

## Admin

Admin classes (`WorkoutAdmin`, `RecordAdmin`, `WearableConnectionAdmin`, etc.) are defined in `healthdatamodel.admin` but **not registered** — registration is left to the host project:

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

## Demo project

A minimal Django project is included under `demo/` to show the models and admin working end-to-end against Django's built-in `auth.User`:

```
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then visit `/admin/`.
