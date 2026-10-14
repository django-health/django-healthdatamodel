# v1.1.0 design — per-night sleep summary and Sleep Regularity Index

Status: implemented. Version 1.1.0 (minor bump: new, additive query API; no
schema change, no migration). Builds on the v1.0.0 compact storage, which
keeps every sleep stage (including awake and in-bed) in `SleepDay.intervals`.

Note: the v1.0.0 design pencilled in flipping `HEALTHDATAMODEL_SAVE_RECORDS`
to `False` for 1.1.0. That flip is **not** part of this release; the default
stays `True` and the flip moves to a later release.

## Goals

The consuming app wants to show members, per night: sleep start and wake
time, sleep efficiency, awakenings and naps; and over the last four nights a
WHOOP-style sleep consistency score. Both read modes
(`HEALTHDATAMODEL_READ_COMPACT` on/off) must give the same answers.

Source shapes the definitions have to handle:

| Source | What arrives |
|---|---|
| Apple Health | Asleep stages only (core / deep / REM / unspecified), each with its own bounds. Gaps between them are awake time; no in-bed data. |
| Google Health, Health Connect | Asleep stages plus awake, out-of-bed (stored as awake) and sometimes in-bed intervals. |
| Older Android builds | One stage-less `...Asleep` block per session. |

## API

In `healthdatamodel.query` (database) and `healthdatamodel.sleep` (pure,
re-exported by `query`):

```python
class SleepInterval(NamedTuple): start, end, stage, device

get_sleep_intervals_by_day(customer, start, end, day_boundary_hour=14)
    -> dict[date, list[SleepInterval]]
get_sleep_summary_by_day(customer, start, end, day_boundary_hour=14,
                         tz=None, main_sleep_gap_minutes=120)
    -> dict[date, DailySleepSummary]
get_sleep_regularity_index(customer, end_day, days=4, day_boundary_hour=14)
    -> SleepRegularity

summarize_sleep_intervals(intervals, window_start, window_end,
                          main_sleep_gap_minutes=120, *, tz=None)
    -> DailySleepSummary
sleep_regularity_from_intervals(asleep_intervals_by_day, end_day, days=4,
                                day_boundary_hour=14) -> SleepRegularity
```

### One selection routine

`get_sleep_by_day` already chooses, per night window, the latest upload
(`admin_create_date`) among asleep records overlapping the window and then
one device by preference. That choice was refactored into one internal
routine per read mode (`_select_sleep_legacy`, `_select_sleep_compact`)
which now returns *all* stages of the chosen device and upload.
`get_sleep_by_day` and `get_sleep_intervals_by_day` both derive from it, so
the summary can never describe a different night or device than the hours
figure. `get_sleep_by_day` output is unchanged (it still counts only the
asleep subset, deduplicated by `(start, end)`, without merging overlaps).

`get_sleep_intervals_by_day` clips to the window and then merges intervals
of the **same stage** that overlap or touch. The compact tables store a
record that crosses the canonical 14:00 UTC boundary as two pieces, so with
any other `day_boundary_hour` the unmerged list would hold one interval in
legacy mode and two in compact mode; the per-stage union is identical in
both. (Different stages may still overlap, e.g. `in_bed` around `core`.)
The summary and SRI take unions anyway, so they are unaffected.

In legacy mode the candidate device names are sorted before ranking, as the
compact path already did, so devices that tie in `_rank_sleep_devices`
(names differing only in case) resolve the same way in both modes instead
of depending on database row order.

### Read-mode parity fixes shipped alongside

- `constants.sleep_stage_for_value` maps any uncatalogued value starting
  with `HKCategoryValueSleepAnalysisAsleep` to `unspecified`, because the
  legacy read path counts such values as asleep by prefix. It is used by
  `compact.route_records`, so it applies to every compact write: live dual
  writes (`ingest_records`), the backfill and `replay_records`,
  `restore_records` and `reassign_customer_health_data`. Rows written by
  1.0.0 are not rewritten; re-run `backfill_compact` (idempotent) for
  customers who sent such values to pick them up from what `Record` still
  holds.
- `compat.backfill_coverage` (used by `compact_status`) counts those values
  as compactable sleep too. Before, a customer whose only sleep history used
  such a value looked as if they had no compactable sleep, so
  `compact_status` could report READY while `SleepDay` lacked it.
- `verify_compact` gains a `sleep_stages` check that compares
  `get_sleep_intervals_by_day` exactly in both modes. The existing `sleep`
  check only looks at asleep hours and wake time; the new functions also
  read awake and in-bed intervals.

## Time zones and the sleep-day boundary

The sleep-day window is `day_boundary_hour` (default 14) to
`day_boundary_hour` in the stored timestamps' clock, i.e. "UTC". The main
consumer stores every datetime as the member's **local wall-clock time
labelled UTC** (an intentional convention), so for it the boundary is really
2 pm local, `sleep_start` / `sleep_end` come back as local times with a UTC
label, and it should leave `tz=None`. The `tz` parameter of the summary
functions is for consumers that store true UTC: it converts `sleep_start` /
`sleep_end` for presentation only and does not move the windows. Nothing in
1.1 changes behaviour for either convention.

## Definitions

All durations are whole minutes, `int(total_seconds // 60)`, as elsewhere in
the query layer.

- **A** = union of asleep-stage intervals (overlapping or touching merged).
- **Sessions**: all intervals (any stage) sorted and clustered; a gap of more
  than `main_sleep_gap_minutes` between one interval's end (the running max)
  and the next start starts a new session.
- **Main sleep** = the session with the most asleep minutes (tie: the later
  one). `sleep_start` / `sleep_end` = first asleep start / last asleep end in
  it.
- **B** (time in bed) = union of the main session's `in_bed` intervals and
  `[sleep_start, sleep_end]` when any `in_bed` interval exists
  (`has_in_bed_data=True`); otherwise `[sleep_start, sleep_end]`.
- `asleep_minutes = |A ∩ main|`, `in_bed_minutes = |B|`,
  `awake_minutes = in_bed − asleep`, `efficiency = asleep / in_bed`
  (`None` if in_bed is 0), `awakenings` = maximal gaps ≥ 1 minute in A
  strictly inside `[sleep_start, sleep_end]`, `nap_minutes` = asleep minutes
  in the other sessions.
- No asleep data → every numeric field (and `sleep_start` / `sleep_end`)
  is `None`.

### Why main-sleep clustering

The sleep-day window is 24 hours (2 pm–2 pm UTC), so it can hold an
afternoon nap as well as the night. Taking first-asleep to last-asleep over
the whole window would report "sleep start 3 pm" and count the afternoon and
evening as awake time, wrecking efficiency and awakenings. Clustering by gap
and picking the session with the most sleep isolates the main sleep without
needing local time or a fixed bedtime; everything else is reported as naps.
Splitting on gaps between *any* intervals (not just asleep ones) means an
explicit long awake interval keeps a night together rather than splitting
it. The 120-minute default is a parameter.

## Sleep Regularity Index

Phillips et al. (2017), the basis of WHOOP's 4-day sleep consistency:

1. Build a minute series over the continuous span from the window start of
   `end_day − (days − 1)` to the window end of `end_day` (`days × 1440`
   minutes). `s(t) = 1` if minute *t* starts inside an asleep interval of
   that night's chosen device, `0` otherwise, and **undefined** for every
   minute of a night with no sleep records.
2. `pairs` = minutes *t* where `s(t)` and `s(t + 1440)` are both defined;
   `matches` = those where they are equal.
3. `SRI = −100 + 200 × matches / pairs`; `consistency_pct = (SRI + 100) / 2`.

`days ≥ 2` (a `ValueError` otherwise). With `days = 4` at most 3 × 1440 =
4320 pairs are compared. `sri = None` when fewer than two nights have data or
no pairs exist (the nights with data are not consecutive).

Treating no-data nights as undefined rather than awake matters: a member who
did not wear the device for a night would otherwise look maximally irregular.

## Limitations

- **Apple time in bed is inferred** as `sleep_start`–`sleep_end`. Time spent
  in bed before falling asleep or after the final wake is invisible, so
  efficiency for Apple Health is an upper bound and is not directly
  comparable with sources that send `in_bed`.
- Only the chosen device of the latest upload is used per night (same as
  `get_sleep_by_day`); intervals from other devices are ignored, not merged.
- Minute resolution: SRI marks minute *t* asleep when its start lies in an
  asleep interval; sub-minute gaps are not awakenings.
- The day windows follow the stored clock (`day_boundary_hour`); `tz` only
  converts the reported `sleep_start` / `sleep_end`. For a consumer storing
  true UTC, a main sleep of a member far from UTC can straddle the 2 pm UTC
  boundary and be split across two days. (Not an issue under the
  local-time-labelled-UTC convention, where the boundary is 2 pm local.)
- DST: windows are fixed 24-hour spans of the stored clock. With true UTC the
  SRI compares instants exactly 24 h apart and `tz` renders each end with
  its own offset. Under the local-time-labelled-UTC convention a DST night is
  measured in wall-clock time (e.g. 23:00–07:00 counts as 8 h on the night
  the clocks spring forward).
- SRI over four nights is a noisy, short-window measure; the SRI literature
  usually uses a week or more. `days` is configurable.
- `asleep_minutes + nap_minutes` can differ slightly from
  `get_sleep_by_day(...).hours * 60`: the summary merges overlapping asleep
  intervals and rounds each component down separately.
