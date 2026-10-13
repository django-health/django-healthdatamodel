"""
Pure sleep calculations: per-night summary and Sleep Regularity Index.

Nothing in this module touches the database, so consumers can run the same
calculations on in-memory intervals (e.g. straight from an upload payload).
The database-backed wrappers live in :mod:`healthdatamodel.query`
(:func:`~healthdatamodel.query.get_sleep_intervals_by_day`,
:func:`~healthdatamodel.query.get_sleep_summary_by_day`,
:func:`~healthdatamodel.query.get_sleep_regularity_index`).

All durations are whole minutes computed as ``int(total_seconds // 60)``,
matching the rest of the query layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from itertools import pairwise
from typing import Any, NamedTuple

from healthdatamodel.constants import ASLEEP_STAGES, MINUTES_PER_DAY, SleepStage

__all__ = [
    "DailySleepSummary",
    "SleepInterval",
    "SleepRegularity",
    "sleep_regularity_from_intervals",
    "summarize_sleep_intervals",
]

_Span = tuple[datetime, datetime]


class SleepInterval(NamedTuple):
    """One stage interval of one night, as returned by
    :func:`~healthdatamodel.query.get_sleep_intervals_by_day`.

    ``stage`` is a :class:`~healthdatamodel.constants.SleepStage` value
    (``"core"``, ``"awake"``, ``"in_bed"``, ...).  ``device`` is the
    ``sourceName`` of the device chosen for the night.
    """

    start: datetime
    end: datetime
    stage: str
    device: str


class DailySleepSummary(NamedTuple):
    """Per-night sleep summary (see :func:`summarize_sleep_intervals`).

    Every field except ``device`` and ``has_in_bed_data`` is ``None`` when the
    night has no asleep data.
    """

    sleep_start: datetime | None
    sleep_end: datetime | None
    asleep_minutes: int | None
    in_bed_minutes: int | None
    awake_minutes: int | None
    efficiency: float | None
    awakenings: int | None
    nap_minutes: int | None
    device: str | None
    has_in_bed_data: bool


class SleepRegularity(NamedTuple):
    """Sleep Regularity Index result (see
    :func:`sleep_regularity_from_intervals`)."""

    sri: float | None
    consistency_pct: float | None
    days_with_data: int
    minutes_compared: int


EMPTY_SUMMARY = DailySleepSummary(
    sleep_start=None,
    sleep_end=None,
    asleep_minutes=None,
    in_bed_minutes=None,
    awake_minutes=None,
    efficiency=None,
    awakenings=None,
    nap_minutes=None,
    device=None,
    has_in_bed_data=False,
)


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------


def sleep_day_window(day: date, day_boundary_hour: int = 14) -> _Span:
    """Return the ``[start, end)`` UTC window of sleep day *day*.

    The window runs from ``day_boundary_hour`` UTC on the previous day to
    ``day_boundary_hour`` UTC on *day*.
    """
    end = datetime.combine(day, time(day_boundary_hour)).replace(tzinfo=timezone.utc)
    return end - timedelta(days=1), end


def _merge(spans: Iterable[_Span]) -> list[_Span]:
    """Union of *spans*: sorted, overlapping or abutting spans merged."""
    merged: list[_Span] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _seconds(spans: Iterable[_Span]) -> float:
    return sum((end - start).total_seconds() for start, end in spans)


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


def _clip(spans: Iterable[_Span], lo: datetime, hi: datetime) -> list[_Span]:
    return [
        (max(start, lo), min(end, hi))
        for start, end in spans
        if max(start, lo) < min(end, hi)
    ]


def _as_interval(item: Any) -> tuple[datetime, datetime, str, str | None]:
    """Accept a :class:`SleepInterval` or a ``(start, end, stage[, device])``
    tuple."""
    if isinstance(item, SleepInterval):
        return item.start, item.end, str(item.stage), item.device
    start, end, stage, *rest = item
    return start, end, str(stage), (rest[0] if rest else None)


# ---------------------------------------------------------------------------
# Per-night summary
# ---------------------------------------------------------------------------


def summarize_sleep_intervals(
    intervals: Iterable[SleepInterval | tuple[Any, ...]],
    window_start: datetime,
    window_end: datetime,
    main_sleep_gap_minutes: int = 120,
    *,
    tz: tzinfo | None = None,
) -> DailySleepSummary:
    """Summarise one night of stage intervals.

    Parameters
    ----------
    intervals:
        :class:`SleepInterval` items, or ``(start, end, stage[, device])``
        tuples, for one device and one night (any stages; tz-aware
        datetimes).  Intervals are clipped to ``[window_start, window_end)``.
    window_start, window_end:
        The sleep-day window (see :func:`sleep_day_window`).
    main_sleep_gap_minutes:
        Intervals (of any stage) separated by more than this many minutes
        belong to different sessions.
    tz:
        Time zone for ``sleep_start`` / ``sleep_end`` (default UTC).

    Definitions
    -----------
    * **A** (asleep time) is the union of asleep-stage intervals (core, deep,
      REM, unspecified).
    * All intervals are clustered into **sessions**; the **main sleep** is the
      session with the most asleep minutes (ties go to the later session).
      ``sleep_start`` / ``sleep_end`` are its first asleep start and last
      asleep end.
    * **B** (time in bed) is the union of the main session's ``in_bed``
      intervals together with ``[sleep_start, sleep_end]`` when the device
      sent ``in_bed`` data (``has_in_bed_data``); otherwise just
      ``[sleep_start, sleep_end]`` (inferred, e.g. Apple Watch).
    * ``asleep_minutes = |A ∩ main session|``, ``in_bed_minutes = |B|``,
      ``awake_minutes = in_bed_minutes - asleep_minutes``,
      ``efficiency = asleep_minutes / in_bed_minutes`` (``None`` when
      ``in_bed_minutes`` is 0).
    * ``awakenings`` counts maximal gaps of at least one minute in A strictly
      between ``sleep_start`` and ``sleep_end``.  An explicit ``awake``
      interval and an unrecorded gap count the same.
    * ``nap_minutes`` is asleep time in the other sessions.

    Returns :data:`EMPTY_SUMMARY`-style all-``None`` values (with ``device``
    filled in when known) when there is no asleep time in the window.
    """
    clipped: list[tuple[datetime, datetime, str]] = []
    device: str | None = None
    for item in intervals:
        start, end, stage, item_device = _as_interval(item)
        if device is None:
            device = item_device
        start, end = max(start, window_start), min(end, window_end)
        if start < end:
            clipped.append((start, end, stage))

    asleep = _merge((s, e) for s, e, stage in clipped if stage in ASLEEP_STAGES)
    if not asleep:
        return EMPTY_SUMMARY._replace(device=device)

    # Sessions: cluster every interval, splitting on gaps > the threshold.
    gap = timedelta(minutes=main_sleep_gap_minutes)
    sessions: list[_Span] = []
    for start, end, _stage in sorted(clipped):
        if sessions and start - sessions[-1][1] <= gap:
            if end > sessions[-1][1]:
                sessions[-1] = (sessions[-1][0], end)
        else:
            sessions.append((start, end))

    # Main sleep: most asleep seconds; ``>=`` keeps the later one on ties.
    main = sessions[0]
    main_seconds = -1.0
    for session in sessions:
        seconds = _seconds(_clip(asleep, *session))
        if seconds >= main_seconds:
            main, main_seconds = session, seconds
    total_asleep_seconds = _seconds(asleep)

    main_asleep = _clip(asleep, *main)
    sleep_start = main_asleep[0][0]
    sleep_end = main_asleep[-1][1]

    in_bed = [
        (s, e)
        for s, e, stage in clipped
        if stage == SleepStage.IN_BED and s >= main[0] and e <= main[1]
    ]
    has_in_bed_data = bool(in_bed)
    bed = _merge([*in_bed, (sleep_start, sleep_end)])

    asleep_minutes = _minutes(main_seconds)
    in_bed_minutes = _minutes(_seconds(bed))
    awakenings = sum(
        1
        for (_, prev_end), (next_start, _) in pairwise(main_asleep)
        if (next_start - prev_end).total_seconds() >= 60
    )

    out_tz = tz or timezone.utc
    return DailySleepSummary(
        sleep_start=sleep_start.astimezone(out_tz),
        sleep_end=sleep_end.astimezone(out_tz),
        asleep_minutes=asleep_minutes,
        in_bed_minutes=in_bed_minutes,
        awake_minutes=in_bed_minutes - asleep_minutes,
        efficiency=(asleep_minutes / in_bed_minutes) if in_bed_minutes else None,
        awakenings=awakenings,
        nap_minutes=_minutes(total_asleep_seconds - main_seconds),
        device=device,
        has_in_bed_data=has_in_bed_data,
    )


# ---------------------------------------------------------------------------
# Sleep Regularity Index
# ---------------------------------------------------------------------------

_DayIntervals = Sequence[SleepInterval | tuple[Any, ...]]


def _asleep_spans(items: _DayIntervals) -> list[_Span]:
    spans = []
    for item in items:
        if isinstance(item, SleepInterval) or len(item) >= 3:
            start, end, stage, _device = _as_interval(item)
            if stage not in ASLEEP_STAGES:
                continue
        else:
            start, end = item
        spans.append((start, end))
    return spans


def sleep_regularity_from_intervals(
    asleep_intervals_by_day: Mapping[date, _DayIntervals],
    end_day: date,
    days: int = 4,
    day_boundary_hour: int = 14,
) -> SleepRegularity:
    """Sleep Regularity Index over the *days* sleep days ending at *end_day*.

    Implements the Sleep Regularity Index of Phillips et al. (2017), the
    basis of WHOOP's 4-day sleep consistency score: the probability that
    the sleep/wake state is the same at two time points 24 hours apart,
    rescaled to −100 (perfectly irregular) … 100 (perfectly regular).

    Parameters
    ----------
    asleep_intervals_by_day:
        Per sleep day, either ``(start, end)`` asleep pairs or
        :class:`SleepInterval` items / ``(start, end, stage[, device])``
        tuples (non-asleep stages are ignored).  A day that is missing or
        maps to an empty list has **no data**: all its minutes are undefined
        and excluded, rather than counted as awake.
    end_day:
        Last sleep day of the range.
    days:
        Number of sleep days (must be ≥ 2).  With ``days=4`` at most
        ``3 × 1440 = 4320`` minute pairs are compared.
    day_boundary_hour:
        UTC hour that ends each sleep day (default 14).

    Algorithm
    ---------
    A minute-resolution state series covers the continuous span from the
    window start of ``end_day − (days − 1)`` to the window end of
    ``end_day``.  ``s(t) = 1`` if minute *t* starts inside an asleep
    interval of that day, ``0`` otherwise, undefined on days without data.
    Then ``SRI = −100 + 200 × matches / pairs`` where *pairs* counts the
    minutes *t* with both ``s(t)`` and ``s(t + 1440)`` defined and
    *matches* those with ``s(t) == s(t + 1440)``.
    ``consistency_pct = (SRI + 100) / 2``.

    Returns ``sri=None`` (and ``consistency_pct=None``) when fewer than two
    days have data or no minute pairs exist (e.g. the two days with data are
    not consecutive).
    """
    if days < 2:
        raise ValueError("days must be at least 2")

    first_day = end_day - timedelta(days=days - 1)
    series: list[bytearray | None] = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        items = asleep_intervals_by_day.get(day)
        if not items:
            series.append(None)
            continue
        window_start, window_end = sleep_day_window(day, day_boundary_hour)
        states = bytearray(MINUTES_PER_DAY)
        for start, end in _clip(_asleep_spans(items), window_start, window_end):
            # Minute m is asleep when its start lies in [start, end).
            lo = -int(-(start - window_start).total_seconds() // 60)
            hi = -int(-(end - window_start).total_seconds() // 60)
            for minute in range(lo, min(hi, MINUTES_PER_DAY)):
                states[minute] = 1
        series.append(states)

    days_with_data = sum(1 for states in series if states is not None)
    pairs = matches = 0
    for today, tomorrow in pairwise(series):
        if today is None or tomorrow is None:
            continue
        pairs += MINUTES_PER_DAY
        matches += sum(1 for a, b in zip(today, tomorrow) if a == b)

    if days_with_data < 2 or pairs == 0:
        return SleepRegularity(
            sri=None,
            consistency_pct=None,
            days_with_data=days_with_data,
            minutes_compared=pairs,
        )
    sri = -100.0 + 200.0 * matches / pairs
    return SleepRegularity(
        sri=sri,
        consistency_pct=(sri + 100.0) / 2.0,
        days_with_data=days_with_data,
        minutes_compared=pairs,
    )
