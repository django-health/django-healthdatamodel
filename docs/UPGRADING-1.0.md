# Upgrading from 0.11.x to 1.0.0

1.0.0 adds compact storage: `SleepDay` and `ActivityDay` sit next to the
append-only `Record` log (see [the design doc](design-v1.0.0-compact-storage.md)).
This checklist moves a live install to it with no downtime and with a rollback
that is a settings change at every step.

The upgrade happens in stages, and you can stop at any stage for as long as you
need:

| Stage | Writes | Reads | How you get there |
|---|---|---|---|
| A. after Step 2 | `Record` + compact | `Record` (legacy) | bump the pin and `migrate` (DDL only) |
| B. after Step 4 | `Record` + compact | `Record` (legacy) | `backfill_compact` in batches |
| C. after Step 6 | `Record` + compact | compact | `HEALTHDATAMODEL_READ_COMPACT = True` |

Going from A to B is safe to repeat. Going back from C to B means setting
`HEALTHDATAMODEL_READ_COMPACT = False`. Nothing in 1.0.0 stops `Record`
writes or deletes `Record` rows.

---

## 0. Prerequisites

Check each item before you start. If one fails, fix it first.

1. **You are on 0.11.x and use only the package helpers to write data.** Every
   write to `Record` must go through `healthdatamodel.ingest`
   (`ingest_records`, `aingest_records`, `ingest_compact_activity`,
   `aingest_compact_activity`), and every deletion through the 0.11 helpers.
   Check that the consumer has no direct ORM writes or deletes:

   ```
   grep -rnE "Record\.objects\.(create|bulk_create|update|filter\(.*\)\.(update|delete)|get_or_create|update_or_create)|Record\(" --include=*.py . | grep -v healthdatamodel
   grep -rnE "Workout\.objects\.(create|bulk_create|update|delete)" --include=*.py . | grep -v healthdatamodel
   ```

   Any hit bypasses the compact tables: compact reads won't see that data,
   and `verify_compact` will report it as a mismatch. Raw SQL and ETL jobs
   that write to the `healthdatamodel_record` table count as direct writes
   too.
2. **Test fixtures build health data with `healthdatamodel.testing` and the
   ingest helpers**, not with `Record.objects.create`. Otherwise your test
   suite only exercises the legacy path, and when you flip reads in Step 6
   your tests will read empty compact tables.
3. **You have a domain-level parity check.** This is your own command or
   script that computes your business outputs (points, scores, goals) for a
   set of customers and date ranges, run once with
   `HEALTHDATAMODEL_READ_COMPACT=False` and once with `True`, and diffed.
   `verify_compact` checks the package query API. Your parity check covers
   how you use it.
4. **Activity reads use PostgreSQL.** The legacy activity path needs it, as
   it did on 0.11. The compact path works on any backend.
5. **You have a recent database snapshot or backup**, and enough disk space.
   The compact tables are roughly 1–5 % of the size of `Record` for the same
   data.

## Step 1. Add the settings before bumping the pin

Add all four settings explicitly, with these values, in every environment.
They equal the 1.0.0 defaults. Setting them explicitly means a later
release that changes a default can't change your behaviour when you bump the
pin. `compact_status` also reports them as `(set)`, so you can see they are
deliberate.

```python
# settings.py
# Keep writing the legacy log.  This is your rollback path; leave it True
# for the whole 1.0.x line.
HEALTHDATAMODEL_SAVE_RECORDS = True
# Dual-write every upload into SleepDay / ActivityDay from the first deploy.
HEALTHDATAMODEL_SAVE_COMPACT = True
# Keep serving reads from Record until Step 6.  Read it from the environment
# so each environment can switch over independently.
HEALTHDATAMODEL_READ_COMPACT = env.bool("HEALTHDATAMODEL_READ_COMPACT", default=False)
# Never replay a large Record table inside the 0012 migration transaction.
HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = False
```

| Setting | Default | Meaning |
|---|---|---|
| `HEALTHDATAMODEL_SAVE_RECORDS` | `True` | Write every upload through to `Record`. Per-call override: `ingest_records(..., save_records=...)`. Records the compact schema can't hold are always written to `Record`. |
| `HEALTHDATAMODEL_SAVE_COMPACT` | `True` | Write every upload to `SleepDay` / `ActivityDay`. |
| `HEALTHDATAMODEL_READ_COMPACT` | `False` | Serve the query API (`get_sleep_by_day`, `get_activity_by_day`, `get_activity_records`, `has_competing_sources`, …) from the compact tables instead of `Record`. |
| `HEALTHDATAMODEL_BACKFILL_IN_MIGRATION` | `False` | Let migration `0012` replay all of `Record` into the compact tables inside the migration transaction. Use it only for small installs (development, demo, CI databases). |

Deploy this settings change on 0.11 first if you want. 0.11 ignores these
settings.

## Step 2. Bump the pin and migrate

1. Set `django-healthdatamodel==1.0.0` and deploy the usual way, with
   `manage.py migrate` running **before** the new code takes traffic. New
   code writes to the compact tables, so they must exist first. Old pods that
   are still running during a rolling deploy write only to `Record`; the
   Step 4 backfill picks those rows up.
2. If you want to review the SQL before running it:

   ```
   python manage.py sqlmigrate healthdatamodel 0011
   ```

   **0011 is DDL only.** It runs `CREATE TABLE` for `healthdatamodel_sleepday`
   and `healthdatamodel_activityday`, adds their unique constraints and one
   index each, and adds a foreign key to your user table. The tables are
   empty, so it takes seconds. It doesn't touch `Record`. On PostgreSQL the
   foreign key briefly takes a `SHARE ROW EXCLUSIVE` lock on the user table.
   **0012 is a no-op** unless `HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = True`.
3. Confirm the migration state:

   ```
   $ python manage.py showmigrations healthdatamodel
   healthdatamodel
    [X] 0001_initial
    ...
    [X] 0010_backfill_activated_at
    [X] 0011_activityday_sleepday
    [X] 0012_backfill_compact
   ```

4. Run `python manage.py check`. It must report no issues.
5. Run `python manage.py compact_status --skip-coverage`. It should show:
   - every setting as `(set)` with the Step 1 values;
   - `SleepDay` / `ActivityDay` with 0 rows right after migrate, then
     growing as uploads arrive;
   - `Dual-write lag: >= N ...` until the first upload after the deploy
     (the compact tables are still empty). After that it should be
     `Dual-write lag: 0`.
6. You are now at **stage A**. Reads are unchanged: they still come from
   `Record`.

## Step 3. Dual-write soak

Leave stage A running for at least a few days, including a weekend and a full
upload cycle for every data source you support. Watch:

- **Ingest latency and error rate.** Each upload now also runs one small
  transaction against the compact tables, with row locks on a handful of
  rows.
- **`compact_status --skip-coverage`** once a day: `Dual-write lag` stays
  at 0, and the compact row counts and newest upload keep moving.
- **Your logs** for the warning `could not be stored compactly and were
  written to Record despite save_records=False`. It should never appear,
  because `SAVE_RECORDS` is True. If it does, a call site passes
  `save_records=False`.
- **Database size.** The compact tables should grow by about 1–5 % of the
  rate `Record` grows.

## Step 4. Backfill history in batches

`backfill_compact` replays `Record`, oldest upload first, through the same
writer that live ingest uses.

- **Idempotent.** Re-running it on customers it has already processed
  changes nothing: a replayed upload replaces its own earlier write instead
  of adding to it, and never overwrites a newer row.
- **Resumable.** If a run is interrupted, re-run it. It's safe to re-run
  from the start or from the last `batch done` line.
- **Bounded.** It works through customers in pk order, `--customer-batch-size`
  at a time (default 200), with one `Record` query per batch. Only the
  compactable types are read: sleep, active energy, basal energy and steps.
- **No long transactions.** Each upload commits on its own, so nothing is
  held open across the run.
- **Safe while dual-writes are live.** It takes the same row locks as
  ingest.

1. Find your user pk range:

   ```
   python manage.py shell -c "from django.contrib.auth import get_user_model as g; from django.db.models import Min, Max; print(g().objects.aggregate(Min('pk'), Max('pk')))"
   ```

2. Backfill in slices. For example, 50 000 customers per run, one run after
   another (in a one-off pod or job, with the output captured to a log):

   ```
   python manage.py backfill_compact --customer-range 1 50000
   python manage.py backfill_compact --customer-range 50001 100000
   ...
   ```

   Disjoint ranges touch different customers, so you can run them in
   parallel if the database has headroom. Useful options:
   `--customer-batch-size` (customers per `Record` query),
   `--progress-every N` (a progress line every N uploads), and
   `--customers PK ...` (specific customers).

3. Read the output. Each slice prints progress lines, a `batch done` line
   after every customer batch, and a summary:

   ```
   backfill_compact: customer pk 1..50000; settings {...}
     progress: 1000 uploads / 84210 records, 0 customers done, 12s (83 uploads/s)
     batch done: 200 customers through pk 200, 2311 uploads, 190544 records (resume with --customer-range 201 50000)
     ...
   Backfill complete: 50000 customers scanned, ... uploads / ... records replayed into the compact tables; last customer pk 50000.
   ```

4. **To resume after an interruption**, copy the `resume with ...` hint from
   the last `batch done` line. Overlapping a range you have already done is
   harmless.
5. **Check progress** with:

   ```
   python manage.py compact_status                          # everyone
   python manage.py compact_status --customer-range 1 50000 # one slice
   ```

   `pending` counts customers whose compact history doesn't yet reach back
   to their oldest compactable `Record` day. Live dual-writes only add recent
   days, so a customer the backfill hasn't reached still counts as pending
   even if they upload every day. Continue until the whole range shows
   `pending: 0` and the final line is `READY: ...`. Use
   `--fail-if-not-ready` in scripts.
6. If a few customers stay pending after their range has been backfilled, the
   usual cause is that their oldest record can't be compacted (see "Known
   differences" below). Check them with
   `verify_compact --customers <pk> --days <N>`, where N reaches back to
   their oldest day. If reads agree, they are fine.

You are now at **stage B**.

## Step 5. Verify

1. Run the package parity check in every environment. Production is the one
   that matters.

   ```
   python manage.py verify_compact --sample 200 --days 30
   python manage.py verify_compact --sample 50 --days 120
   python manage.py verify_compact --customers 123 456 789 --days 365   # known heavy / multi-device users
   ```

   It always forces both read modes, whatever `READ_COMPACT` is set to.
   `--sample` picks the customers with the most recent uploads. On PASS it
   prints a table and a final line:

   ```
   verify_compact: 200 customer(s), 2026-09-15..2026-10-14 (30 days), tolerance 0.2
     check                           days  with data  mismatched
     sleep                           6000       4120           0
     ACTIVE_CALORIES@15min           6000       3980           0
     ACTIVE_CALORIES@1440min         6000          0           0
     ...
   PASS: compact and legacy reads agree on all 42000 customer-day checks.
   ```

   The exit code is 0. Any mismatch prints `MISMATCH ...` lines (up to
   `--max-report`), ends with `FAIL: ...`, and exits non-zero. Activity is
   compared only on PostgreSQL. On other backends the line says
   `(sleep only)`.

   The default tolerance (`--tolerance 0.2`) is per value, and covers the
   0.1 kcal rounding (see below). Don't raise it to make a FAIL go away.
   Investigate the mismatch first.
2. Run your **domain-level parity check** (Prerequisite 3) over the same
   customers and windows. It must match too.
3. **If a customer mismatches**, the usual causes, in order of likelihood:
   - Their history isn't backfilled yet. Check `compact_status --customers <pk>`.
   - A direct ORM or SQL write to `Record` bypassed the compact tables
     (Prerequisite 1).
   - One of the documented known differences (below).
   - A live upload landed while that customer's own backfill was running,
     which is very rare. To rebuild one customer, run the following. It's
     safe only while `READ_COMPACT` is False in every environment that
     reads this database.

     ```
     python manage.py shell -c "from healthdatamodel.models import SleepDay, ActivityDay; [m.objects.filter(customer_id=123).delete() for m in (SleepDay, ActivityDay)]"
     python manage.py backfill_compact --customers 123
     python manage.py verify_compact --customers 123 --days 365
     ```

## Step 6. Flip reads, one environment at a time

Work through dev → QA → staging → production. In each environment:

1. Run `compact_status --fail-if-not-ready` and `verify_compact` (Step 5).
   Both must pass.
2. Set `HEALTHDATAMODEL_READ_COMPACT=True` and restart or redeploy.
3. Watch error rates and latency on every endpoint that reads sleep or
   activity, and watch your business metrics (points, goals, scores) for
   step changes. Re-run `verify_compact` and your parity check daily for a
   while. Both stay meaningful after the flip, because they force both
   modes.

**Rollback:** set `HEALTHDATAMODEL_READ_COMPACT=False` and restart. You lose
nothing: every upload is still written to `Record`, so the legacy path has
everything, within your `Record` retention window.

You are now at **stage C**.

## Step 7. What not to do

- **Never set `HEALTHDATAMODEL_SAVE_RECORDS = False`** during 1.0.x. It
  removes your rollback path. The legacy read path and `verify_compact` both
  need `Record`, and some call sites may rely on `Record` for types the
  compact schema doesn't model. A later release will cover turning it off.
- **Never set `HEALTHDATAMODEL_SAVE_COMPACT = False`** once you are past
  stage A. The compact tables go stale immediately. At stage C that means
  wrong reads.
- **Never set `HEALTHDATAMODEL_BACKFILL_IN_MIGRATION = True`** on a large
  database. It replays the whole of `Record` inside one migration
  transaction.
- **Don't use `--since` for the initial backfill.** It skips older history.
  Use it only for targeted catch-up after a full pass.
- **The package never deletes `Record` rows.** Archiving or deleting `Record`
  is your own retention job. Two rules apply:
  - Don't let it archive or delete rows for dates the backfill hasn't
    covered yet. Pause it, or limit it to rows older than what you have
    decided to drop, until `compact_status` reports `pending: 0` for every
    customer. Otherwise that history is lost from the compact tables. You
    can recover archived history by loading it and passing the rows (sorted
    by `(customer_id, admin_create_date)`) to
    `healthdatamodel.compat.replay_records`.
  - Once reads are compact, a rollback to legacy reads only covers what is
    still in `Record`.
- **Don't write to `Record` or the compact tables directly** (ORM, raw SQL,
  ETL), and don't edit compact rows by hand once reads are compact.

## Known differences between legacy and compact reads

The compact tables store the result that the legacy queries compute, so
results normally match. These are the documented exceptions. Most never show
up with normal uploads.

- **Energy is rounded to 0.1 kcal and steps to whole numbers** when written.
  The legacy path returns the raw value. Each stored value differs by at most
  0.05 kcal (or 0.5 steps), so a daily total built from 96 15-minute slots
  can drift a little more. `verify_compact` compares per value, with
  `--tolerance 0.2` by default.
- **Activity must sit on its own resolution grid to be compacted.** The
  record's duration must divide 1440 minutes evenly, and its start must be
  aligned to that grid within the UTC day: a 15-minute record must start at
  :00, :15, :30 or :45, and a 1440-minute (daily) record must start at
  00:00 UTC of the day it's stored under. The legacy path returns any record
  whose duration equals the requested resolution, aligned or not. Records
  that aren't aligned stay in `Record` only, and compact reads don't see
  them.
- **Values that don't parse as numbers** (after replacing `,` with `.`) are
  not compacted. On PostgreSQL the legacy activity query would fail on them.
- **Sources without a ranking row are dropped in both modes.** Activity is
  ranked by `DataSourceRanking`, which `ensure_ranks` maintains for every
  `DataSource` value, so a `Record.source` outside `DataSource` is never
  returned by either path.
- **Tied rows in `get_activity_records`.** When several devices from the
  winning source (or several rows in one upload) cover the same slot, the
  legacy path returns one tuple per row. The compact path returns one tuple
  per slot with the values summed. Per-slot sums and `get_activity_by_day`
  totals are the same.
- **Unknown sleep values.** The legacy path counts any value starting with
  `HKCategoryValueSleepAnalysisAsleep`. The compact path knows the values
  listed in `constants.HK_SLEEP_VALUE_TO_STAGE`. Any other value stays in
  `Record` only and compact reads ignore it.
- **`has_competing_sources`** looks at sleep and the three activity metrics
  in compact mode. In legacy mode it looks at any `Record` row, including
  types that aren't compacted, such as heart rate.
- **History outside `Record`.** The compact tables only hold what was in
  `Record` when you backfilled, plus everything ingested since. Archived
  history is included only if you replay it (see Step 7).

## Backing out of 1.0 entirely

At stage A or B, you can return to 0.11:

```
python manage.py migrate healthdatamodel 0010   # drops the two compact tables
# then pin django-healthdatamodel back to 0.11.x
```

Nothing is lost, because `Record` was written throughout.
