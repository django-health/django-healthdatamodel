# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
