# v1.0.0 design — compact storage model

Status: implemented (see PR); one refinement found during implementation is
noted inline: writes carrying the *same* ``admin_create_date`` as the stored
row are treated as one logical upload and **merge** (sleep: interval union;
activity: same-slot sum), because the legacy read path groups by that
timestamp and counts everything at the winning one.  Only a *newer*
timestamp replaces/overwrites.
Informed by: `wellness_format_analysis.md` (2026-05 wellrider extract analysis — 245 MB raw
→ 3.1 MB compact, 1.3%, with zero information loss for SRI + activity metrics).

## Goal

Replace the append-only `Record` log as the *system of record* with a compact,
deduplicated inner format:

- **No user–device–day duplicates.** One row per key; new data replaces or merges into
  the existing row instead of appending.
- **Sleep**: one row per `(customer, device, sleep_day)` holding the night's intervals.
  A new upload for that key *clears and replaces* the day (2pm–2pm UTC window).
- **Activity**: one row per `(customer, source, device, metric, day, resolution)` holding
  a fixed-length vector of slot values (96 slots at 15 min; 1 slot at 1440 min).
- `Record` stays as an optional write-through log (`save_records=True` today, flipped to
  `False` in a later release), matching production where `Record` is archived past 30 days.

Expected impact based on the extract analysis: ~95–98% storage reduction for sleep +
activity types, ~100× fewer rows written per upload, and range reads that fetch one row
per day instead of 96 — plus the activity read path stops requiring PostgreSQL.

## Why this is semantics-preserving

The current query layer already imposes exactly these semantics at read time:

- `_sleep_for_day` uses **only the most recent `admin_create_date` upload** for a day
  window, then picks one device. Older uploads for the same day are dead weight —
  replace-on-write stores the same answer directly.
- `get_activity_records` picks, per `(customer, startDate, endDate, type)` interval, the
  **best-ranked source** and within a source the **latest upload**. Superseded rows are
  dead weight — slot-wise "newest non-null wins" stores the same answer directly.
- Source-ranked deduplication (via `DataSourceRanking`) stays a **read-time** concern:
  we keep one vector per source and rank at query time, so re-ranking a customer's
  sources never requires rewriting stored data.

The compact tables therefore hold the *fixed point* of what the fancy queries compute,
and the public query API (`get_sleep_by_day`, `get_activity_by_day`,
`get_activity_records`, …) keeps identical signatures and return values.

## New models

Two tables, both keyed to forbid duplicates. Vectors and intervals are stored in
`JSONField` for v1 (see "Decisions" below for JSONField vs `ArrayField`).

```python
class SleepDay(models.Model):
    """One night of sleep for one (customer, device), replace-on-write.

    The sleep day `day` covers the UTC window (day-1 14:00, day 14:00].
    Intervals are clipped to the window; an interval spanning 14:00 is split
    across two SleepDay rows at ingest.
    """

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source = models.CharField(max_length=100, choices=DataSource.choices)
    device = models.CharField(max_length=200)   # Record.sourceName, the sleep-dedup axis
    day = models.DateField()
    # [[start_iso, end_iso, stage], ...] — stage in SleepStage.values.
    # All HKCategoryValueSleepAnalysis* intervals are kept (incl. awake / in_bed);
    # the query layer filters to asleep stages. Once save_records=False this is
    # the only copy, so we do not discard stage information at write time.
    intervals = models.JSONField(default=list)
    admin_create_date = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "device", "day"],
                name="unique_sleepday_customer_device_day",
            )
        ]
        indexes = [models.Index(fields=["customer", "day"], name="sleepday_customer_day_idx")]


class ActivityDay(models.Model):
    """One day of one metric from one (source, device), slot-merge-on-write.

    `values` has length 1440 // resolution_minutes. Entries are float or None;
    None means "no data for this slot" (preserving the None-vs-0.0 distinction
    the query API exposes). Incoming non-None slots overwrite stored slots —
    the per-slot equivalent of the current "latest admin_create_date wins".
    """

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source = models.CharField(max_length=100, choices=DataSource.choices)
    device = models.CharField(max_length=200, default="")  # Record.sourceName
    metric = models.CharField(max_length=20, choices=CompactMetric.choices)
    day = models.DateField()                                # UTC calendar day
    resolution_minutes = models.PositiveSmallIntegerField() # 15, 1440, any divisor of 1440
    values = models.JSONField()
    admin_create_date = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "source", "device", "metric", "day", "resolution_minutes"],
                name="unique_activityday_key",
            )
        ]
        indexes = [
            models.Index(fields=["customer", "metric", "day"], name="activityday_cust_metric_day_idx")
        ]
```

New constants in `constants.py`:

```python
class CompactMetric(models.TextChoices):
    ACTIVE_KCAL = "active_kcal", "Active energy (kcal)"
    BASAL_KCAL = "basal_kcal", "Basal energy (kcal)"
    STEPS = "steps", "Steps"

# HK type string <-> compact metric mapping lives next to ActivityMetric.

class SleepStage(models.TextChoices):
    UNSPECIFIED = "unspecified", ...   # HKCategoryValueSleepAnalysisAsleepUnspecified
    CORE = "core", ...
    DEEP = "deep", ...
    REM = "rem", ...
    AWAKE = "awake", ...
    IN_BED = "in_bed", ...
```

### Value normalization at write time (from the analysis §4)

False precision was the biggest compression killer in the extract analysis; the same
values bloat Postgres rows and JSON blobs. Normalize once, at ingest:

- unit → canonical: `cal`/`calories` ÷ 1000 → kcal (currently done at *read* time);
- negatives clamped to 0 (currently done at read time);
- `steps` → `int`; energy → rounded to 0.1 kcal (the zero-information-loss boundary);
- device strings pass through unchanged (low cardinality; Postgres handles it).

Because normalization moves to write time, the read path becomes a straight sum.

## Ingest changes

`ingest_records` / `aingest_records` grow a router; public signatures gain one kwarg:

```python
def ingest_records(customer, records, source, admin_create_date=None,
                   batch_size=1000, save_records=None) -> None
```

- `save_records=None` → falls back to `settings.HEALTHDATAMODEL_SAVE_RECORDS`
  (**default `True` in v1.0.0**). When resolved-True, the legacy `Record.bulk_create`
  happens exactly as today.
- Compact writes always happen (escape hatch: `settings.HEALTHDATAMODEL_SAVE_COMPACT`,
  default `True`, for a consumer that needs to stage the upgrade).

Routing rules per `RecordInput`:

1. **Sleep** (`type == SLEEP_TYPE`): intervals are split at 14:00 UTC boundaries and
   clipped, grouped by `(device=sourceName, sleep_day)`. For every key present in the
   upload, the existing `SleepDay` row is **replaced** with that upload's intervals.
   (An upload that mentions a day at all is treated as authoritative for that
   day+device — identical to the current "latest upload wins" query behavior.)
2. **Compactable activity** (`type` in `ActivityMetric`, `endDate - startDate` divides
   1440 min, interval aligned to its own resolution grid): values land in day vectors,
   grouped by `(source, device=sourceName, metric, day, resolution)`. **Slot-merge**:
   incoming non-None slots overwrite; untouched slots keep their stored value.
   `ingest_compact_activity` gets a fast path that skips `expand_compact_activity`'s
   per-interval Record objects entirely when `save_records` resolves False, slicing the
   input arrays straight into day vectors.
3. **Everything else** (unknown HK types, non-grid-aligned intervals): written to
   `Record` **even when `save_records=False`**, with a logged warning. Nothing is ever
   silently dropped; these types are rare and the 30-day archive policy bounds their
   cost. (Decision d3 below.)

Write mechanics: `transaction.atomic()` + `select_for_update()` on the affected compact
rows (no-op on SQLite, correct on Postgres), merge in Python, then
`bulk_create(update_conflicts=True, unique_fields=…, update_fields=…)`. Per-customer
uploads touch a handful of rows, so lock scope is tiny. Async variants mirror this via
`sync_to_async`-free native async ORM calls where available.

## Query changes

Public API unchanged; internals dispatch on `settings.HEALTHDATAMODEL_READ_COMPACT`
(default `True`; setting it `False` is the instant rollback lever while dual-writing).

- `get_sleep_by_day`: one range query over `SleepDay` for `[start, end]` instead of
  3 queries × N days over `Record`. Per day: pick device via the existing
  `WearableConnection` preference / fallback order, merge asleep-stage intervals in
  Python, sum minutes, `wake_time = min(max(ends), boundary)`.
- `get_activity_records` / `get_activity_by_day`: one range query over `ActivityDay`
  for the metric + resolution, `ensure_ranks` as today, then in Python: per day pick the
  best-ranked source with data, **sum non-None slots across devices within that source**
  (matching today's behavior where same-source same-interval device rows tie at rank 1
  and are summed by `get_activity_by_day`), expand vectors to
  `(startDate, endDate, value)` tuples.
- `has_competing_sources`: reimplemented against `ActivityDay`/`SleepDay` (an
  `exists()` over the key columns — no more scanning record rows).

**This drops the PostgreSQL requirement for the activity read path** — the window-function
CTE disappears. The whole query API becomes backend-agnostic and fully testable in the
existing SQLite CI (which today cannot exercise `get_activity_records` at all).

## Migration & backfill

New migrations:

- `0009_sleepday_activityday` — schema only.
- `0010_backfill_compact` — `RunPython` data migration seeding compact tables from
  `Record`, honoring `settings.HEALTHDATAMODEL_SKIP_BACKFILL_MIGRATION` (default
  `False`) so large installs can skip it and run the management command instead.

Backfill implementation (`healthdatamodel/compat.py`):

- `backfill_from_records(customers=None, since=None, batch_size=…)` replays `Record`
  rows **grouped by upload (`admin_create_date`), oldest → newest**, through the *same
  routing code* the live ingest path uses. Replaying through one code path guarantees
  the backfilled state is byte-identical to what dual-writing would have produced, and
  makes the operation idempotent (re-running converges).
- `manage.py backfill_compact [--customers …] [--since …]` wraps it with batching and
  progress output for big tables; safe to run while dual-writes are live (same
  row-locking as ingest).
- `manage.py verify_compact [--sample N]` — parity checker: runs the query API in both
  read modes for sampled customers/date ranges and diffs the results. This is the
  consumer's pre-flight check before flipping `save_records` off.
- Production note: backfill only sees what's in `Record` (30-day archive horizon).
  History older than the archive cutoff is seeded from the archive extracts if wanted —
  out of scope for the package, but `backfill_from_records` accepts an iterable of
  record-shaped rows so an archive loader can reuse it.

## Settings summary (all new, all optional)

| Setting | v1.0.0 default | Purpose |
|---|---|---|
| `HEALTHDATAMODEL_SAVE_RECORDS` | `True` | write-through to legacy `Record` |
| `HEALTHDATAMODEL_SAVE_COMPACT` | `True` | escape hatch to stage the upgrade |
| `HEALTHDATAMODEL_READ_COMPACT` | `True` | instant rollback lever for the read path |
| `HEALTHDATAMODEL_SKIP_BACKFILL_MIGRATION` | `False` | big installs backfill via command |

## Rollout plan

1. **v1.0.0** — everything above. Consumers upgrade, `migrate` (auto-backfill for
   normal-sized installs), and silently start dual-writing + compact-reading. No caller
   code changes.
2. Consumers validate with `verify_compact`; production watches for a full
   archive cycle (30 days) so `Record` holds nothing the compact tables don't.
3. **v1.1.0** — `HEALTHDATAMODEL_SAVE_RECORDS` default flips to `False` (documented in
   changelog; consumers who still want the log pin it `True`). `Record` drains via the
   existing archive job.
4. **v2.0.0** — legacy read path (`READ_COMPACT=False` branch) removed; `Record` kept
   as an opt-in raw log only. `expand_compact_activity`'s Record-expansion becomes
   internal-only.

## Testing

- **Parity suite** (the core of v1.0.0 confidence): ingest generated payloads
  (multi-source, multi-device, partial-day uploads, re-uploads, boundary-spanning naps,
  comma-decimal values, `cal` units, negative values) with dual-write on; assert
  `get_sleep_by_day` / `get_activity_by_day` / `get_activity_records` return identical
  results with `READ_COMPACT` on vs off. Legacy activity comparisons need Postgres →
  add a Postgres service job to CI (SQLite job keeps covering the compact path, which
  it can now do fully).
- **Backfill parity**: ingest via legacy only, run `backfill_from_records`, compare.
- **Merge-semantics units**: slot overwrite vs preserve-None, sleep replace-on-upload,
  interval splitting at 14:00, multi-device same-source summing, idempotent re-backfill.
- **Migration check** already in CI catches drift.

## Decisions (with recommendations)

- **d1 — JSONField vs `django.contrib.postgres.ArrayField` for vectors.**
  Recommend **JSONField** for v1.0.0: works on every backend (keeps the SQLite demo,
  tests, and non-PG consumers working), and the row-count collapse is where ~all of the
  win comes from — 96 rows → 1 row dwarfs the jsonb-vs-`real[]` delta. A Postgres-only
  `real[]` + `SET COMPRESSION lz4` optimization can be a later additive migration if
  measurements justify it.
- **d2 — keep sleep stages (incl. awake/in_bed) in `SleepDay.intervals`.**
  Recommend **yes**. Current queries ignore stages, but once `save_records=False` the
  compact row is the only copy; stages cost little here (no row multiplication — they
  ride inside the JSON list) and are irrecoverable if dropped.
- **d3 — uncompactable record types when `save_records=False`.**
  Recommend **still write them to `Record`** with a warning, so turning the flag off can
  never silently discard data the compact schema doesn't model (heart rate, etc.).
- **d4 — `device` in the ActivityDay key.**
  Recommend **yes** (as specced). It removes any write-time cross-device merge policy,
  mirrors the sleep key, and read-time summing across devices reproduces today's
  behavior exactly.
- **Out of scope for v1.0.0**: `Workout`/`WorkoutMetadataEntry` (unchanged), high-res
  activity streams (the Strava-shaped `activity_stream` design from the analysis is a
  future, separate table), sub-15-min resolutions (schema supports any divisor of 1440,
  but 1-sec data should be event/stream-shaped per the analysis, not grid-shaped).
