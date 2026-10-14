# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.1.0] - Unreleased

Per-night sleep summary and Sleep Regularity Index. Additive: no schema
change, no migration, no settings change (`HEALTHDATAMODEL_SAVE_RECORDS`
stays `True` by default; the 1.0.x upgrade guidance applies unchanged).
Design: [`docs/design-v1.1.0-sleep-metrics.md`](docs/design-v1.1.0-sleep-metrics.md).

### Added

- `healthdatamodel.query.get_sleep_intervals_by_day(customer, start, end,
  day_boundary_hour=14)` — every stage interval (asleep stages, `awake`,
  `in_bed`) of each night's chosen upload and device, as `SleepInterval`
  named tuples clipped to the night window; same-stage intervals that
  overlap or touch are merged. The upload/device choice is the same internal
  routine `get_sleep_by_day` uses, so both always describe the same night.
- `get_sleep_summary_by_day(customer, start, end, day_boundary_hour=14,
  tz=None, main_sleep_gap_minutes=120)` — `DailySleepSummary` per day:
  main-sleep `sleep_start` / `sleep_end`, `asleep_minutes`,
  `in_bed_minutes` (from `in_bed` data, else inferred), `awake_minutes`,
  `efficiency`, `awakenings`, `nap_minutes`, `device`, `has_in_bed_data`.
  `tz` only converts `sleep_start` / `sleep_end` for display; installs that
  store local wall-clock time labelled UTC should leave it unset.
- `get_sleep_regularity_index(customer, end_day, days=4,
  day_boundary_hour=14)` — Sleep Regularity Index (Phillips et al. 2017,
  `−100 + 200 × matches / pairs` over minutes 24 h apart) and
  `consistency_pct`; nights without data are excluded, not counted as awake.
- `healthdatamodel.sleep`: the pure (no database) `summarize_sleep_intervals`
  and `sleep_regularity_from_intervals`, plus `SleepInterval`,
  `DailySleepSummary`, `SleepRegularity` (all re-exported by `query`).
- `constants.sleep_stage_for_value` and `SLEEP_ASLEEP_VALUE_PREFIX`.
- `verify_compact` compares `get_sleep_intervals_by_day` in both read modes
  (new `sleep_stages` row; awake and in-bed intervals were not checked
  before).

### Fixed

- Compact sleep: an uncatalogued value starting with
  `HKCategoryValueSleepAnalysisAsleep` is stored as `unspecified` instead of
  being left out of `SleepDay` (the legacy read path counts such values as
  asleep). Applies to live dual writes, `backfill_compact` /
  `replay_records`, `restore_records` and `reassign_customer_health_data`.
  Rows written by 1.0.0 are not rewritten; re-run `backfill_compact` for
  affected customers.
- `backfill_coverage` / `compact_status` count those values as compactable
  sleep, so a customer whose only sleep history used one is reported
  pending until backfilled instead of looking fully covered.
- Legacy sleep reads sort candidate device names before ranking (as the
  compact path does), so devices that tie in the ranking (names differing
  only in case) resolve the same way in both read modes and on every
  database.

## [1.0.0] - 2026-10-14

Compact storage. Sleep and activity are additionally stored in two
deduplicated tables, `SleepDay` and `ActivityDay`, next to the append-only
`Record` log. The defaults are safe for a large existing install: setting
nothing gives you the new tables (DDL only), dual writes, and unchanged
reads from `Record`. Switching reads over is an explicit, per-environment
step; follow [`docs/UPGRADING-1.0.md`](docs/UPGRADING-1.0.md). Design:
[`docs/design-v1.0.0-compact-storage.md`](docs/design-v1.0.0-compact-storage.md).

### Added

- Models `SleepDay` (one row per customer, device and 2 pm–2 pm UTC sleep
  day; a newer upload replaces the day) and `ActivityDay` (one slot vector
  per customer, source, device, metric, UTC day and resolution; newer
  uploads overwrite slot by slot). Values are normalised at write time
  (kcal, non-negative, steps as int, energy to 0.1 kcal). Migrations
  `0011_activityday_sleepday` (schema only) and `0012_backfill_compact`
  (no-op unless opted in).
- Settings, all optional:
  `HEALTHDATAMODEL_SAVE_RECORDS` (default `True`, keep it on for 1.0.x),
  `HEALTHDATAMODEL_SAVE_COMPACT` (`True`),
  `HEALTHDATAMODEL_READ_COMPACT` (`False`; the read switch and the rollback
  lever), `HEALTHDATAMODEL_BACKFILL_IN_MIGRATION` (`False`; small installs
  only).
- `ingest_records(..., save_records=...)` and the same argument on the other
  ingest functions. Records the compact schema cannot hold are always
  written to `Record`.
- Management commands: `backfill_compact` (batched, idempotent, resumable
  replay of `Record` into the compact tables; `--customers`,
  `--customer-range`, `--since`, `--customer-batch-size`, `--chunk-size`,
  `--progress-every`), `compact_status` (settings, row counts, dual-write
  lag, per-customer backfill coverage, READY / NOT READY;
  `--fail-if-not-ready`) and `verify_compact` (diffs the query API in both
  read modes; exits non-zero on any mismatch).
- `healthdatamodel.compat`: `backfill_from_records`, `replay_records`
  (e.g. for archived history), `backfill_coverage`.
- Admin classes `SleepDayAdmin` / `ActivityDayAdmin` (not registered).
- Android Health Connect sleep values (`...Asleep`, `...OutOfBed`) map to
  compact sleep stages.
- CI job running the suite against PostgreSQL (the legacy activity read
  path needs it).

### Changed

- The query API (`get_sleep_by_day`, `get_sleep_hours_by_day`,
  `get_activity_records`, `get_activity_by_day`, `has_competing_sources`)
  reads the compact tables when `HEALTHDATAMODEL_READ_COMPACT = True`. The
  compact activity path works on any database backend.
- `ingest_records` writes the compact tables and `Record` in one
  transaction; `aingest_records` runs it in a worker thread for the same
  guarantee (it no longer uses `abulk_create`). Compact writes lock every
  affected key, including keys that don't exist yet, so concurrent uploads
  for one customer and a backfill running next to live ingest cannot
  overwrite each other.
- The 0.11 consumer helpers are table-aware:
  - `delete_records` and `delete_customer_health_data` also delete the
    customer's `SleepDay` / `ActivityDay` rows, in one transaction.
    `delete_customer_health_data` returns two new keys, `sleep_days` and
    `activity_days`; `records=True` covers all three tables.
  - `reassign_customer_health_data` moves the source customer's compact
    rows to the target, folding rows whose key the target already has into
    the target's row (newer upload wins; older fills empty activity slots),
    then rebuilds the target's compact rows from its merged `Record` rows
    with the backfill's replay path, all in one transaction. It needs
    `HEALTHDATAMODEL_SAVE_RECORDS = True` (raises `ImproperlyConfigured`
    otherwise) and rejects `from_customer == to_customer`.
  - `restore_records` also replays restored sleep and activity rows into the
    compact tables (unless `HEALTHDATAMODEL_SAVE_COMPACT = False`): an
    older restored upload never overwrites newer data, and repeating a
    restore leaves the compact tables unchanged. The whole call now runs in
    one transaction (all or nothing).
  - `has_records`, `get_data_source_summary`, `get_sleep_records`,
    `infer_resolution_minutes`, `iter_records` and `iter_workouts` still
    read `Record` / `Workout` in both read modes, so they require
    `HEALTHDATAMODEL_SAVE_RECORDS = True`.
  - `testing.create_record` / `create_records` dual-write like any ingest,
    so fixtures are visible in both read modes.

## [0.11.0] - 2026-10-14

Public helper API covering every place host projects still reach into
`Record` / `Workout` / `DataSourceRanking` directly, so that v1.0 (compact
storage) can keep its tables in sync without further consumer changes.
No schema changes, no migrations.

### Added

- `healthdatamodel.customer_data`: `has_records`, `delete_records`,
  `delete_customer_health_data` (per-model counts, atomic),
  `reassign_customer_health_data` (account merge), `get_data_source_summary`
  + `DataSourceSummary` (per-source last/first upload, newest `endDate`,
  rank; any backend).
- `healthdatamodel.query`: `get_sleep_records`, `infer_resolution_minutes`,
  `iter_records`, `iter_workouts`.
- `healthdatamodel.ingest.restore_records`: re-insert archived rows keeping
  each row's own `source`, `creationDate` and `admin_create_date`.
- `healthdatamodel.schemas.StoredRecord` / `StoredWorkout`: pk-less,
  read-only value objects returned by the readers.
- `healthdatamodel.testing`: `record_input`, `create_records`,
  `create_record` fixture helpers that go through `ingest_records`.
- README "Consumer helpers" section. Direct ORM access to `Record` /
  `Workout` is unsupported from v1.0 onward.
- CI: `python manage.py check --fail-level WARNING` step (demo project), so
  model system-check errors such as `models.E034` fail the build.

### Changed

- `ingest_records` / `aingest_records` / `ingest_workouts` /
  `aingest_workouts` return the number of rows saved (previously `None`).
- `ingest_workouts` runs in a single transaction and bulk-inserts workouts on
  backends that return primary keys from bulk inserts; `aingest_workouts`
  wraps it with `sync_to_async`.
- `RecordInput.value` accepts `int` / `float` and stringifies it (negative
  numbers become `"0.0"`, matching negative strings).
