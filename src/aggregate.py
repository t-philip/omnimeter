"""Roll raw cumulative-meter readings up into daily summaries.

Two kinds of raw column exist:
  - cumulative meters (kWh import/export, gas m3, water dl, battery kWh) —
    daily usage is the delta between consecutive readings, attributed to the
    day the earlier reading falls on.
  - gauges (battery SoC %, L1/L2/L3 max W) — daily summary is min/max/avg
    over that day's readings, not a delta.

Where both 15-minute and daily-granularity rows exist for the same date
(current-year data overlaps with the long-range daily export), the
15-minute rows win — they're dropped from the daily-only fallback rather
than mixed in, so a day's closing reading always comes from the finer
source when one is available.

Cumulative-meter readings can also contain two kinds of real-world anomaly,
observed in real water export data:
  - transient sensor dropouts: a handful of readings report 0 (or some other
    implausibly low value) then recover to roughly where they left off within
    hours — an artifact of a brief communication glitch, not real usage.
  - genuine resets: a sustained drop that never recovers to the prior level
    (a meter/sensor replacement) — the counter legitimately restarts near
    zero and climbs normally from there afterward.
`clean_cumulative_glitches` distinguishes the two with a bounded lookahead:
a drop that recovers to >= the last good value within the window is excised
as a glitch; a drop that doesn't recover within the window is accepted as
the start of a new segment (its own delta against the prior segment would be
negative and is already skipped by `compute_daily_deltas`).
"""

import sqlite3
from bisect import bisect_left, insort
from collections import defaultdict, deque
from datetime import datetime, timedelta


def _date_of(ts: str) -> str:
    return ts[:10]


def _parse_time(ts: str) -> datetime:
    """Parse the fixed-width naive-local 'YYYY-MM-DD HH:MM' timestamp used by
    every readings table.

    Hand-sliced rather than strptime: this is the hottest call in the module
    (every delta pair, in every column, of every category) and strptime's
    format-string machinery dominated the full-history scans -- the
    reconciliation pass measured 20.4s before this change. Same result, same
    ValueError on malformed input, roughly 4x faster."""
    return datetime(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]), int(ts[11:13]), int(ts[14:16]))


def clean_cumulative_glitches(
    points: list[tuple[str, float]], lookahead_hours: float = 24 * 14
) -> list[tuple[str, float]]:
    """points: sorted [(time_str, value), ...] for one cumulative series.

    Drops points that are part of a transient dip (recovers to >= the last
    good value within lookahead_hours); keeps points that start a sustained
    drop (treated as a genuine meter reset -> new segment)."""
    if not points:
        return points

    cleaned = [points[0]]
    last_good_value = points[0][1]
    i = 1
    n = len(points)
    while i < n:
        t, v = points[i]
        if v >= last_good_value:
            cleaned.append((t, v))
            last_good_value = v
            i += 1
            continue

        # v < last_good_value: scan ahead for a recovery within the window.
        cutoff = _parse_time(t) + timedelta(hours=lookahead_hours)
        j = i
        recovered_idx = None
        while j < n:
            tj, vj = points[j]
            if _parse_time(tj) > cutoff:
                break
            if vj >= last_good_value:
                recovered_idx = j
                break
            j += 1

        if recovered_idx is not None:
            cleaned.append(points[recovered_idx])
            last_good_value = points[recovered_idx][1]
            i = recovered_idx + 1
        else:
            # Sustained drop with no recovery in window -- genuine reset.
            cleaned.append((t, v))
            last_good_value = v
            i += 1
    return cleaned


# Higher wins when multiple sources cover the same date. 'live' (historical
# Home Assistant sync data -- that sync path was removed 2026-07-28, so this is
# no longer an active write target, but years of real rows already sit at
# this granularity and must keep outranking any CSV re-import for the same
# dates) ranks above '15min' (HomeWizard CSV export) which ranks above
# 'daily' (the long-range rollup export, the only source for periods before
# 2022). 'api_live' (the real-time local API poller, now the sole active
# live-data source) is deliberately left unranked -- see
# meter_ingest.py's module docstring for why rank 0 is sufficient.
_GRANULARITY_RANK = {"live": 3, "15min": 2, "daily": 1}


def filter_preferred_granularity(rows: list[dict]) -> list[dict]:
    """For each date, keep rows only from the single highest-ranked
    granularity present that date (see _GRANULARITY_RANK).

    Unranked granularities (anything not in _GRANULARITY_RANK, e.g. the
    'api_live' local-API poller) default to rank 0 -- by design, so they
    never outrank a real source (see meter_ingest.py's module
    docstring). REAL BUG fixed 2026-07-24: a date whose *only* rows are all
    rank 0 used to crash with a KeyError below -- `rank >
    best_rank_by_date.get(d, 0)` is never true for an all-zero date (0 > 0 is
    false), so that date was never seeded into best_rank_by_date at all, even
    though it's a real date that needs an entry. Every granularity in use
    until the local-API poller was added was ranked, so this path
    was never actually reachable before 'api_live' existed -- surfaced the
    first time a date's water_readings had no CSV/HA row at all, only
    api_live ones.
    """
    best_rank_by_date: dict[str, int] = {}
    for r in rows:
        d = _date_of(r["time"])
        rank = _GRANULARITY_RANK.get(r["granularity"], 0)
        if d not in best_rank_by_date or rank > best_rank_by_date[d]:
            best_rank_by_date[d] = rank
    return [r for r in rows if _GRANULARITY_RANK.get(r["granularity"], 0) == best_rank_by_date.get(_date_of(r["time"]), 0)]


# A cumulative delta only means "usage" across a *contiguous* pair of
# readings. When the series has a gap, the delta bridging it is not one day's
# usage — it's however much the meter advanced across the whole gap, which
# would otherwise land in its entirety on the earlier reading's date as a
# phantom spike.
#
# This is the still-live second half of the ~10,373 kWh phantom-day bug that
# import_combined_kwh fixed (see db.py). Power is the only category whose two
# sources write *different* columns (CSV -> *_t1/t2_kwh, HA -> *_combined_kwh),
# so a single date preferring '15min' inside the 'live' era — an HA outage day
# later covered by a CSV upload, i.e. the documented workflow — punches a gap
# into both column series at once. The combined series then bridges over that
# day (double-counting it onto the day before), and the t1/t2 series bridges
# all the way from the end of the CSV era, landing weeks of cumulative growth
# on one old date. Gas/battery/water are immune: both their sources share one
# column, so their series stay contiguous.
#
# A gapped interval is dropped rather than spread across the days it covers:
# the total is real, but its distribution is unknown, and inventing a flat
# profile would be exactly the kind of silently-wrong number this dashboard
# labels estimates to avoid. The cost is that days with no readings report
# nothing instead of a fabricated value.
#
# 26h: daily-granularity rows are 24h apart and the DST fall-back day is 25h
# long — 26 clears both with margin while still catching real gaps.
MAX_DELTA_SPAN_HOURS = 26


def compute_daily_deltas(rows: list[dict], value_key: str) -> dict[str, float]:
    """Cumulative-meter rows -> {date: usage_that_day}.

    Attributes each interval's delta to the date of the interval's *earlier*
    reading — correct for both 15-minute data (many small deltas each land on
    their own day) and daily data (a single day-spanning delta lands on the
    day it started, i.e. the day it actually covers).

    Intervals spanning more than MAX_DELTA_SPAN_HOURS are gaps in the series,
    not usage, and contribute nothing — see MAX_DELTA_SPAN_HOURS."""
    points = sorted(
        ((r["time"], r[value_key]) for r in rows if r.get(value_key) is not None),
        key=lambda p: p[0],
    )
    points = clean_cumulative_glitches(points)
    max_span = timedelta(hours=MAX_DELTA_SPAN_HOURS)
    usage: dict[str, float] = defaultdict(float)
    # strict=False, not True: this is the pairwise idiom -- points and
    # points[1:] are *meant* to differ in length by exactly one, so zip
    # stopping at the shorter sequence is the intended behaviour, not a bug
    # strict=True would guard against.
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        delta = v1 - v0
        if delta < 0:
            continue  # meter reset or bad export row — skip rather than corrupt the total
        if _parse_time(t1) - _parse_time(t0) > max_span:
            continue  # gap in the series, not a day's usage
        usage[_date_of(t0)] += delta
    return dict(usage)


def compute_daily_extrema(rows: list[dict], value_key: str) -> dict[str, dict]:
    """Gauge-style rows -> {date: {min, max, avg}}."""
    by_date: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = r.get(value_key)
        if v is not None:
            by_date[_date_of(r["time"])].append(v)
    return {
        d: {"min": min(vs), "max": max(vs), "avg": sum(vs) / len(vs)}
        for d, vs in by_date.items()
    }


def compute_daily_last_value(rows: list[dict], value_key: str) -> dict[str, float]:
    """Gauge-style rows -> {date: value of that date's chronologically last
    reading} -- e.g. end-of-day battery SoC, as distinct from the day's
    average (compute_daily_extrema)."""
    latest: dict[str, tuple[str, float]] = {}
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        d = _date_of(r["time"])
        if d not in latest or r["time"] > latest[d][0]:
            latest[d] = (r["time"], v)
    return {d: v for d, (_, v) in latest.items()}


# ---------------------------------------------------------------------------
# Data quality diagnostics. Purely read-only reporting functions --
# none of these feed rebuild_all() or change any *_daily table. Raised after
# a real incident (a day's total was present but silently wrong, 2x reality,
# and nothing on the dashboard caught it): this is the broader "is my data
# trustworthy" check the gap detector alone doesn't answer.
#
# Deliberately informational, not alerting: this whole feature is a manual,
# button-triggered report (see /api/data-quality), not a push notification,
# so a flagged-but-legitimate day (e.g. an EV-charging day pushing daily
# import to several times a household's normal baseline -- confirmed real in
# the development dataset, where one such day reached 32.88 kWh against a
# ~4 kWh/day baseline) costs nothing. No
# attempt is made here to guess or suppress a cause -- results are presented
# neutrally (date + how far from baseline) for a human to interpret.
# ---------------------------------------------------------------------------

OUTLIER_BASELINE_WINDOW_DAYS = 30  # trailing window (by index, not calendar days) for the baseline median
OUTLIER_MIN_HISTORY_DAYS = 14  # don't flag until this many prior rows exist
OUTLIER_HIGH_RATIO = 3.0  # flag if value > baseline * this
OUTLIER_LOW_RATIO = 0.25  # flag if value < baseline * this

# A ratio is only meaningful once the baseline is a real number rather than
# near-zero noise. This used to be an absolute 0.5, which was unit-blind: the
# same constant guarded kWh, m3 and litres. Measured consequences on real
# data -- 0.5 is 0.4% of a typical water baseline (so it never fired) but sat
# *above* a typical summer gas baseline of ~0.3 m3, silently disabling gas
# outlier detection for most of the year. A genuine 5x summer gas spike
# produced zero flags while the identical relative spike in water produced
# four. Now relative to the metric's own long-run median, so it means the
# same thing in every unit and scales automatically to any household.
OUTLIER_BASELINE_EPSILON_RATIO = 0.1

# The "long-run" median is taken over a trailing window, not the whole
# history. Measured on real data: 699 consecutive water_daily rows, spanning
# nearly two years, are exactly 0.0 -- the water meter was installed some time
# after the P1 power/gas meter, and the long-range export padded the
# pre-installation period with zeros to align the two series. That dead era is
# 63% of the water history, so a whole-history median came out at 0.00 and
# switched water outlier detection off entirely. A trailing window steps past
# a dead era on its own, and stays causal.
OUTLIER_LONG_RUN_WINDOW_DAYS = 365
GRANULARITY_DISAGREEMENT_TOLERANCE_PCT = 15.0


def _median_sorted(s: list[float]) -> float:
    """Median of an already-sorted list. Split out so the causal long-run
    median in find_outlier_days can maintain one sorted list incrementally
    (bisect.insort) instead of re-sorting the whole history at every date."""
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _median(values: list[float]) -> float:
    return _median_sorted(sorted(values))


OUTLIER_LOW_BASELINE_STABILITY_RATIO = 0.5

# Found by actually using this feature against real data: the "low" direction
# was flooding the report -- 713 flags across all 4 categories, 181 of them
# an exact 0.0 value. Root cause: for seasonal metrics (gas in summer, solar
# export in winter, battery on a cloudy stretch, water during low
# occupancy), the trailing baseline itself is naturally small during the
# off-season, so *any* zero/near-zero day computes ratio ~= 0 -- always
# below OUTLIER_LOW_RATIO. Zero usage in the off-season isn't an anomaly,
# it's just the season; flagging it as one drowned out the findings that
# actually mattered. import_kwh doesn't have this problem (an occupied
# household realistically never has a "no-power season"), so the fix can't
# just be "never flag low" -- it needs to distinguish "this metric is
# naturally quiet right now" from "this metric just had a real drop."
#
# Fix: before evaluating the low-direction ratio, require the *trailing*
# baseline itself to be at least OUTLIER_LOW_BASELINE_STABILITY_RATIO of the
# metric's own long-run (whole-series) median. During an already-quiet
# season the trailing baseline collapses below that floor on its own, so
# low-flagging is skipped there entirely -- in practice this suppresses the
# seasonal false positives (closer to never flagging low for genuinely
# seasonal metrics) while still catching a real drop from an
# otherwise-stable baseline like import_kwh (whose trailing baseline stays
# close to its long-run median all year, so the floor is essentially always
# met). Scales automatically per metric/category (kWh vs m3 vs L) since it's
# relative to that series' own median, not a hardcoded absolute number.


def find_outlier_days(daily_rows: list[dict], value_key: str, today: str | None = None) -> list[dict]:
    """*_daily rows (the already-authoritative, correctness-fixed totals --
    deliberately not re-derived from raw readings a second way) -> days
    whose value is far outside its own recent trailing baseline.

    Baseline is the median of the preceding OUTLIER_BASELINE_WINDOW_DAYS
    rows *by index*, not by calendar proximity, so an occasional missing day
    doesn't distort the window; a date needs at least OUTLIER_MIN_HISTORY_DAYS
    of prior rows before it's eligible, avoiding noisy flags at the very
    start of a category's history. See OUTLIER_LOW_BASELINE_STABILITY_RATIO
    above for why "low" additionally requires an already-established
    baseline, not just a low ratio.

    The most recent row is never evaluated -- found live (a user asked about
    "Battery discharge_kwh: 2026-08-03 down0.0x baseline (0.00 vs 2.17)"
    a few hours into that actual day): today's total is still accumulating,
    not genuinely low, and comparing a partial day against a baseline of
    complete days would flag every category, every single morning, until
    enough of the day had passed. Same principle _find_daily_gaps already
    applies to gaps -- the latest date is never itself "missing," that's
    staleness, a different, dedicated check (/api/data-freshness)."""
    points = sorted(
        ((r["date"], r[value_key]) for r in daily_rows if r.get(value_key) is not None),
        key=lambda p: p[0],
    )
    if not points:
        return []
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    outliers: list[dict] = []
    # Values strictly before the date under test, bounded to the trailing
    # OUTLIER_LONG_RUN_WINDOW_DAYS. `recent_sorted` supports an O(1) median
    # read; `recent_order` remembers insertion order so the value leaving the
    # window can be removed from the sorted copy as well.
    recent_sorted: list[float] = []
    recent_order: deque[float] = deque()
    for i, (d, v) in enumerate(points):
        if i >= OUTLIER_MIN_HISTORY_DAYS and d < today:
            long_run_median = _median_sorted(recent_sorted)
            # A metric that has never been used (a no-gas household) has no
            # meaningful ratio at all -- and this also guarantees the
            # baseline floor below is > 0, so the division is always safe.
            if long_run_median > 0:
                window_start = max(0, i - OUTLIER_BASELINE_WINDOW_DAYS)
                baseline = _median([x for _, x in points[window_start:i]])
                if baseline >= long_run_median * OUTLIER_BASELINE_EPSILON_RATIO:
                    ratio = v / baseline
                    low_baseline_floor = long_run_median * OUTLIER_LOW_BASELINE_STABILITY_RATIO
                    if ratio > OUTLIER_HIGH_RATIO:
                        outliers.append(
                            {"date": d, "value": v, "baseline_median": baseline, "ratio": ratio, "direction": "high"}
                        )
                    elif ratio < OUTLIER_LOW_RATIO and baseline >= low_baseline_floor:
                        outliers.append(
                            {"date": d, "value": v, "baseline_median": baseline, "ratio": ratio, "direction": "low"}
                        )
        insort(recent_sorted, v)
        recent_order.append(v)
        if len(recent_order) > OUTLIER_LONG_RUN_WINDOW_DAYS:
            recent_sorted.pop(bisect_left(recent_sorted, recent_order.popleft()))
    return outliers


def group_outlier_episodes(outliers: list[dict]) -> list[dict]:
    """Runs of CONSECUTIVE dates sharing a direction -> one episode each.
    Same move already made elsewhere for glitches: count how many
    distinct things happened, not how many rows were touched.

    A 16-day absence is ONE event, not 16 notes. Measured on a real
    database: 794 raw notes collapse to 345 episodes all-time, and the
    default 90-day view goes 75 -> 43. The runs this collapses are exactly
    the phenomena already understood -- `gas low 2024-01-11 -> 2024-01-26`
    (away), `gas high 2023-10-24 -> 2023-11-08` (heating season starting).

    Callers pass one metric's outliers at a time, so direction is the only
    grouping key needed here; consumption_notes_report adds the metric.
    Deliberately does NOT also merge metrics within a category+direction:
    measured at 345 -> 330 episodes, a negligible gain that costs knowing
    which metric was actually odd.

    Each episode reports its most extreme day (`peak_date` and the
    ratio/value/baseline the fingerprint of the run is judged by), not an
    average -- averaging a run that starts mild and ends severe would
    understate exactly the episodes worth looking at. `occupancy` is carried
    only when every day of the run logged the same headcount; a run spanning
    a change (or partly unlogged) reports None rather than picking one day's
    value to speak for all of them."""
    by_direction: dict[str, list[dict]] = defaultdict(list)
    for o in outliers:
        by_direction[o["direction"]].append(o)

    episodes: list[dict] = []
    for direction, items in by_direction.items():
        items = sorted(items, key=lambda o: o["date"])
        run: list[dict] = [items[0]]
        for prev, nxt in zip(items, items[1:], strict=False):
            # Date arithmetic, not string adjacency -- month and year
            # boundaries are consecutive days like any other.
            if datetime.fromisoformat(nxt["date"]) - datetime.fromisoformat(prev["date"]) == timedelta(days=1):
                run.append(nxt)
            else:
                episodes.append(_episode_from_run(run, direction))
                run = [nxt]
        episodes.append(_episode_from_run(run, direction))
    return sorted(episodes, key=lambda e: e["start"])


# The rule is "zero gas and zero water", with a little slack because
# "someone might enter to check the house". These values are measured, not
# chosen: tested against 212 days across 10 confirmed absences and 62 days
# across 6 known-occupied periods, they catch 208/212 absences (up from
# 169/212 at exactly zero) with ZERO occupied days misread as empty. Loosening
# further buys nothing -- 0.15/20L catches the same 208 while starting to call
# occupied days empty.
#
# The slack is what a brief visit costs: a flush or two and the boiler ticking.
# It is deliberately far below real activity -- days with contractor work in
# the house ran 42.7 L and 81.3 L and must stay visible, not be waved through.
AWAY_MAX_GAS_M3 = 0.10
AWAY_MAX_WATER_L = 10.0


def find_away_days(conn: sqlite3.Connection) -> set[str]:
    """Dates where gas and water were both at or below a trivial trickle --
    nobody was home.

    A far better absence detector than the outlier machinery
    that surrounds it: no baseline, no threshold, no seasonality, no tuning.
    Either the taps and the boiler moved or they did not.

    Deliberately does NOT use power. The data agrees
    exactly: power can never reach zero because the fridge, freezer and router
    draw continuously -- measured across 1,648 days, there is not one
    zero-power day, and the lowest ever recorded is 0.211 kWh. During confirmed
    absences power sits at its always-on floor (~1.3-1.5 kWh/day in summer,
    ~3-4 kWh in winter with frost protection), never zero. A zero-power day
    would therefore be an instrument fault, not an empty house.

    Requires a row in BOTH tables for the date, so nothing before water history
    begins can be classed as an absence -- "no water data" is not "no water
    used".

    Not exact zero -- see AWAY_MAX_GAS_M3 / AWAY_MAX_WATER_L above for the
    slack and the measurement behind it. Exact zero was the first cut and it
    missed a fifth of all confirmed absences, because gas keeps ticking over
    for heating with nobody in the house: one 23-day absence ran at
    0.03-0.08 m3/day throughout and was invisible to a zero-only rule."""
    gas = {r["date"]: r["usage_m3"] for r in conn.execute("SELECT date, usage_m3 FROM gas_daily")}
    water = {r["date"]: r["usage_l"] for r in conn.execute("SELECT date, usage_l FROM water_daily")}
    return {
        d
        for d, g in gas.items()
        if g is not None
        and g <= AWAY_MAX_GAS_M3
        and water.get(d) is not None
        and water[d] <= AWAY_MAX_WATER_L
    }


# An EV charge on an 11 kW three-phase charger (the common domestic
# three-phase rating) draws 11.1-11.9 kW for two to three solid hours. Every
# other hour in the development dataset, across every day with raw readings,
# peaks at 1.30 kW. The two populations are
# separated by a 10.2 kW gap with nothing whatsoever in it, so this constant
# is not a tuned threshold -- it sits in the middle of an empty chasm and
# could be moved several kW either way without changing a single result.
#
# Deliberately NOT a daily-total rule. Daily totals conflate a charge with a
# cold winter day: that dataset has 65 days over 25 kWh, and the winter
# ones are heating, not charging. Judging the hour-by-hour shape tells the
# two apart; judging the total cannot -- an earlier version of this check
# made exactly that unit-blind mistake.
EV_CHARGE_MIN_KW = 8.0


def find_ev_charge_days(conn: sqlite3.Connection) -> set[str]:
    """Dates on which the EV was charged, identified by load shape.

    Requires raw readings: only the api_live/live eras carry sub-daily power,
    so days before that (CSV-imported dailies) return nothing rather than a
    guess. That is the honest answer -- "cannot tell from a daily total" is
    not "did not happen" -- and the gap closes on its own as live data
    accumulates."""
    pts = [
        (r["time"], r["import_combined_kwh"])
        for r in conn.execute(
            "SELECT time, import_combined_kwh FROM power_readings "
            "WHERE import_combined_kwh IS NOT NULL ORDER BY time"
        )
    ]
    hourly: dict[str, float] = defaultdict(float)
    for (t1, v1), (_t2, v2) in zip(pts, pts[1:], strict=False):
        delta = v2 - v1
        # An implausibly large jump is a meter reset or a granularity seam,
        # not an hour of consumption -- data_integrity_report owns those.
        if 0 < delta < 30:
            hourly[t1[:13]] += delta
    return {hour[:10] for hour, kwh in hourly.items() if kwh >= EV_CHARGE_MIN_KW}


def suggest_absence_entries(conn: sqlite3.Connection) -> list[dict]:
    """Stretches the meters say the house was empty, which the occupancy log
    does not already record as empty.

    The problem this solves: a short trip taken *inside* a long visit. During
    the 88-day "Mom visiting NL" span the house was demonstrably empty on
    three separate stretches -- 12 days in total, all logged as "2 people
    home". That is not a rounding error; it drags the with-guests average down
    and inflates apparent per-person usage across the whole window.

    Nothing here writes anything. It surfaces candidates so a trip can be
    logged in one click instead of a hand-written API call, which is the only
    reason those 12 days went unrecorded in the first place.

    A stretch already covered by a count=0 entry is not suggested. One that is
    only partly covered still is -- a trip logged a day short is exactly the
    case worth correcting."""
    away = find_away_days(conn)
    if not away:
        return []

    rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log")]
    resolved = expand_occupancy_by_day(rows, min(away), max(away)) if rows else {}

    runs: list[list[str]] = []
    for d in sorted(away):
        if runs and datetime.fromisoformat(d) - datetime.fromisoformat(runs[-1][-1]) == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])

    out: list[dict] = []
    for run in runs:
        counts = [resolved.get(d) for d in run]
        if all(c == 0 for c in counts):
            continue  # already recorded as empty
        out.append(
            {
                "start": run[0],
                "end": run[-1],
                "days": len(run),
                # None marks a day with no entry covering it at all, which the
                # caller renders differently from "logged, but as occupied".
                "logged_counts": sorted({c for c in counts}, key=lambda c: (c is not None, c)),
            }
        )
    return out


def _event_dates(event: dict) -> list[str]:
    d = datetime.fromisoformat(event["start"]).date()
    end = datetime.fromisoformat(event["end"]).date()
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def group_consumption_events(notes_by_category: dict[str, list[dict]]) -> list[dict]:
    """Episodes that overlap in time and share a direction are ONE event.
    The same move as group_outlier_episodes, one level up: that
    collapsed consecutive days within a metric, this collapses coincident
    episodes across categories.

    A week away does not produce a gas fact, a water fact and a power fact --
    it produces one fact that shows up in three meters. Measured on a real
    database: the 90-day view goes 22 -> 14 and all-time 175 -> 128, and
    the resulting list reads as events ("low, 2026-05-07..05-13,
    gas+power+water") rather than as readings.

    Deliberately NOT merged: opposite directions, however well they line up.
    Gas down while water is up is two things happening, and saying so is the
    entire value of listing the categories.

    Merging is on strict overlap, not adjacency -- an episode ending the day
    before another starts stays separate. Adjacency merging was considered
    and rejected: it would chain unrelated episodes into ever-longer runs
    through a series of one-day touches, and the measurement above was taken
    on overlap, so changing the rule afterwards would invalidate it.

    Acknowledgement is NOT re-keyed here. An event carries its constituent
    notes in `parts`, and counts as acknowledged only when every part is --
    so every fingerprint already stored in acknowledged_issues keeps working,
    including the ones an earlier grouping change was careful to preserve. Acknowledging an event
    acknowledges its parts, which is exactly what the bulk endpoint does."""
    by_direction: dict[str, list[dict]] = defaultdict(list)
    for category, notes in notes_by_category.items():
        for n in notes:
            by_direction[n["direction"]].append({**n, "category": category})

    events: list[dict] = []
    for direction, notes in by_direction.items():
        group: list[dict] = []
        group_end = ""
        for n in sorted(notes, key=lambda x: (x["start"], x["end"])):
            if group and n["start"] <= group_end:
                group.append(n)
                group_end = max(group_end, n["end"])
            else:
                if group:
                    events.append(_event_from_group(group, direction))
                group = [n]
                group_end = n["end"]
        if group:
            events.append(_event_from_group(group, direction))
    return sorted(events, key=lambda e: (e["start"], e["direction"]))


def _event_from_group(group: list[dict], direction: str) -> dict:
    start = min(n["start"] for n in group)
    end = max(n["end"] for n in group)
    return {
        "start": start,
        "end": end,
        "days": (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1,
        "direction": direction,
        "categories": sorted({n["category"] for n in group}),
        "parts": sorted(group, key=lambda n: (n["category"], n["metric"])),
        # An event is only "reviewed" once everything under it has been. A
        # half-acknowledged event reading as acknowledged would hide the parts
        # still waiting to be looked at.
        "acknowledged": all(n["acknowledged"] for n in group),
    }


def _episode_from_run(run: list[dict], direction: str) -> dict:
    peak = (max if direction == "high" else min)(run, key=lambda o: o["ratio"])
    occupancies = {o.get("occupancy") for o in run}
    return {
        "start": run[0]["date"],
        "end": run[-1]["date"],
        "days": len(run),
        "direction": direction,
        "peak_date": peak["date"],
        "value": peak["value"],
        "baseline_median": peak["baseline_median"],
        "ratio": peak["ratio"],
        "occupancy": occupancies.pop() if len(occupancies) == 1 else None,
    }


def find_negative_deltas(rows: list[dict], value_key: str) -> list[dict]:
    """Same point-pairing pass as compute_daily_deltas, but *collects* the
    delta<0 cases it silently `continue`s past instead of discarding them --
    a meter reset or a genuinely bad row, worth surfacing rather than only
    silently excluded from the daily total. A parallel read-only pass, not a
    change to compute_daily_deltas itself -- zero risk to the already
    correctness-fixed rollup pipeline.

    Groups by granularity before pairing points -- pairing
    across granularities (e.g. a 'live' point immediately followed by a
    '15min' point) could report a spurious negative delta from nothing more
    than the two sources sampling slightly differently, not a real meter
    reset. Each finding carries its own granularity so a future delete
    action knows exactly which raw row it points at."""
    by_granularity: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        by_granularity[r["granularity"]].append((r["time"], v))

    negatives: list[dict] = []
    for granularity, pts in by_granularity.items():
        pts = sorted(pts)
        pts = clean_cumulative_glitches(pts)
        for (_t0, v0), (t1, v1) in zip(pts, pts[1:], strict=False):
            delta = v1 - v0
            if delta < 0:
                negatives.append(
                    {"time": t1, "from_value": v0, "to_value": v1, "delta": delta, "granularity": granularity}
                )
    return negatives


GLITCH_MATERIALITY_THRESHOLD = 0.05  # ignore dips smaller than this

# Follow-up finding, found by actually using the feature against real
# data: the naive "count of points removed" version of this function
# reported 921 "glitch corrections" for battery, which looked like a real
# problem. It wasn't -- every one of those removed points was device-level
# floating-point/reporting noise (the largest dip in ~2 years of real data
# was -0.001 kWh), nothing a human needed to see. Meanwhile water's one
# genuine 19-day meter outage (a real, large drop) should count as ONE thing
# worth noticing, not however many 15-minute points it happened to span.
# Counting *episodes* whose magnitude clears GLITCH_MATERIALITY_THRESHOLD,
# rather than raw removed points, gets both cases right.


def find_glitch_episodes(rows: list[dict], value_key: str, min_drop: float = GLITCH_MATERIALITY_THRESHOLD) -> list[dict]:
    """Distinct dip episodes clean_cumulative_glitches corrected for this
    series, each at least min_drop below the value immediately preceding it.
    One multi-point dropout (e.g. a real multi-day outage) is one episode
    regardless of how many raw points it spans; sub-threshold noise doesn't
    produce an episode at all. Grouped by granularity, same
    reasoning as find_negative_deltas -- returns
    [{start_time, end_time, magnitude, granularity}, ...], individually
    identifiable so each can be acknowledged on its own."""
    by_granularity: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        by_granularity[r["granularity"]].append((r["time"], v))

    episodes: list[dict] = []
    for granularity, pts in by_granularity.items():
        pts = sorted(pts)
        cleaned_times = {t for t, _ in clean_cumulative_glitches(pts)}
        prev_kept_value: float | None = None
        episode_min: float | None = None
        episode_start: str | None = None
        episode_end: str | None = None
        for t, v in pts:
            if t in cleaned_times:
                if episode_min is not None and prev_kept_value is not None and prev_kept_value - episode_min >= min_drop:
                    episodes.append(
                        {
                            "start_time": episode_start,
                            "end_time": episode_end,
                            "magnitude": prev_kept_value - episode_min,
                            "granularity": granularity,
                        }
                    )
                episode_min = None
                episode_start = None
                episode_end = None
                prev_kept_value = v
            else:
                if episode_min is None:
                    episode_start = t
                episode_min = v if episode_min is None else min(episode_min, v)
                episode_end = t
        if episode_min is not None and prev_kept_value is not None and prev_kept_value - episode_min >= min_drop:
            episodes.append(
                {
                    "start_time": episode_start,
                    "end_time": episode_end,
                    "magnitude": prev_kept_value - episode_min,
                    "granularity": granularity,
                }
            )
    return episodes


def count_glitch_corrections(rows: list[dict], value_key: str, min_drop: float = GLITCH_MATERIALITY_THRESHOLD) -> int:
    """Convenience wrapper -- len(find_glitch_episodes(...))."""
    return len(find_glitch_episodes(rows, value_key, min_drop))


# Two sources can only be compared on a date they BOTH cover essentially in
# full. Found by measuring this check against real data: all 6
# disagreements it reported sat on a real source-migration boundary
# (2026-07-13/14/24/28), where one poller stopped and another started
# partway through the day. Each source saw only its own slice, so comparing
# their totals compared a part-day against a fuller one -- a guaranteed
# "disagreement" from nothing but coverage. A 100% false-positive rate.
GRANULARITY_COMPARISON_MIN_COVERAGE_PCT = 90.0

_MINUTES_PER_DAY = 24 * 60


def _daily_totals_and_coverage(
    points: list[tuple[str, float]],
) -> tuple[dict[str, float], dict[str, float]]:
    """One granularity's points -> ({date: usage}, {date: minutes covered}).

    Pairs and attributes deltas exactly the way compute_daily_deltas does --
    glitch-cleaned, negatives skipped, over-long (gap-bridging) intervals
    dropped -- so a per-granularity total here is directly comparable to the
    authoritative *_daily rollup instead of being a second, subtly different
    computation of the same quantity.

    Crucially this pairs across the whole series rather than within each
    calendar date. Bucketing by date first (the previous implementation)
    silently broke 'daily'-granularity data: it has exactly one row per date,
    so a per-date bucket held a single point, no pair could be formed, and
    its total came out 0.0 -- making every date that had both a 'daily' row
    and a finer-granularity row read as a 100% disagreement. That combination
    happens to appear nowhere in the current database (the daily era ends
    cleanly before the 15min era starts in all four tables), so the bug was
    latent rather than firing -- but a gap's own "Fix" button invites
    uploading a long-range daily export to backfill a gap in the 15min era,
    which is exactly the overlap that would have triggered it.

    A negative delta still counts toward coverage: the source genuinely
    observed that interval, it just reported a reset across it."""
    points = clean_cumulative_glitches(sorted(points))
    max_span = timedelta(hours=MAX_DELTA_SPAN_HOURS)
    usage: dict[str, float] = defaultdict(float)
    covered: dict[str, float] = defaultdict(float)
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        span = _parse_time(t1) - _parse_time(t0)
        if span > max_span:
            continue  # gap in the series, not observed time
        d = _date_of(t0)
        covered[d] += span.total_seconds() / 60.0
        delta = v1 - v0
        if delta >= 0:
            usage[d] += delta
    return dict(usage), dict(covered)


def find_granularity_disagreements(
    rows: list[dict], value_key: str, tolerance_pct: float = GRANULARITY_DISAGREEMENT_TOLERANCE_PCT
) -> list[dict]:
    """For dates where more than one granularity independently covers the
    day, flags a disagreement beyond tolerance_pct between their totals.
    filter_preferred_granularity already picks a single winner per date for
    the live dashboard; this looks at what the *other* source(s) would have
    said, purely as a diagnostic.

    Only dates where every compared source covers at least
    GRANULARITY_COMPARISON_MIN_COVERAGE_PCT of the day are considered -- see
    that constant for why partial coverage otherwise guarantees a false
    positive."""
    by_granularity: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        by_granularity[r["granularity"]].append((r["time"], v))

    min_covered = _MINUTES_PER_DAY * GRANULARITY_COMPARISON_MIN_COVERAGE_PCT / 100.0
    totals_by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for granularity, pts in by_granularity.items():
        usage, covered = _daily_totals_and_coverage(pts)
        for d, minutes in covered.items():
            if minutes >= min_covered:
                totals_by_date[d][granularity] = usage.get(d, 0.0)

    disagreements: list[dict] = []
    for d, by_gran in sorted(totals_by_date.items()):
        if len(by_gran) < 2:
            continue
        hi, lo = max(by_gran.values()), min(by_gran.values())
        if hi <= 0:
            continue
        diff_pct = (hi - lo) / hi * 100.0
        if diff_pct > tolerance_pct:
            disagreements.append({"date": d, "by_granularity": by_gran, "diff_pct": diff_pct})
    return disagreements


def find_out_of_range_values(daily_rows: list[dict], value_key: str, lo: float, hi: float) -> list[dict]:
    """Gauge-style daily values (e.g. battery SoC%) outside a physically
    plausible [lo, hi] range -- a distinct implausibility class from
    negative deltas, which only apply to cumulative meters, not gauges."""
    out: list[dict] = []
    for r in daily_rows:
        v = r.get(value_key)
        if v is None:
            continue
        if v < lo or v > hi:
            out.append({"date": r["date"], "value": v, "metric": value_key})
    return out


# Per-category wiring, shared by data_integrity_report and
# consumption_notes_report so the two can never drift apart on which tables
# or metrics a category owns.
#
# `outlier_metrics` is every metric worth range/emptiness checking, and is
# what data_integrity_report's empty_run check walks. It is NOT the
# same question as "which metrics represent consumption" -- see
# `consumption_metrics` below. Changing one must never silently change the
# other, which is exactly why they are separate keys rather than one list.
#
# `consumption_metrics` is the subset a human would call *usage*:
# what the household actually drew. Deliberately excludes:
#   - power export_kwh: a RESIDUAL, not consumption. It is what was left over
#     after the house took what it wanted, so it moves with sunshine far more
#     than with behaviour -- it was 16 of 43 notes on the 90-day view,
#     the single largest source, and none of them told a user anything about
#     their own usage. Export anomalies are real and worth surfacing, but as
#     a panel-performance signal, not here.
#   - battery charge_kwh/discharge_kwh: internal circulation. Energy moving
#     between the panels, the battery and the house is not the household
#     consuming anything; it is the same kWh being counted on its way past.
# Both remain fully covered by data_integrity_report -- this narrows what
# counts as an interesting *behavioural* note, and removes no fault detection.
_QUALITY_CATEGORIES: dict[str, dict] = {
    "power": {
        "readings_table": "power_readings",
        "daily_table": "power_daily",
        "outlier_metrics": ["import_kwh", "export_kwh"],
        "consumption_metrics": ["import_kwh"],
        "raw_value_keys": ["import_combined_kwh", "export_combined_kwh"],
        "range_checks": {},
    },
    "gas": {
        "readings_table": "gas_readings",
        "daily_table": "gas_daily",
        "outlier_metrics": ["usage_m3"],
        "consumption_metrics": ["usage_m3"],
        "raw_value_keys": ["total_gas_m3"],
        "range_checks": {},
    },
    "water": {
        "readings_table": "water_readings",
        "daily_table": "water_daily",
        "outlier_metrics": ["usage_l"],
        "consumption_metrics": ["usage_l"],
        "raw_value_keys": ["water_usage_dl"],
        "range_checks": {},
    },
    "battery": {
        "readings_table": "battery_readings",
        "daily_table": "battery_daily",
        "outlier_metrics": ["charge_kwh", "discharge_kwh"],
        "consumption_metrics": [],
        "raw_value_keys": ["import_kwh", "export_kwh"],
        "range_checks": {"min_soc_pct": (0.0, 100.0), "max_soc_pct": (0.0, 100.0)},
    },
}


def _tag_finding(
    acknowledged: set[tuple[str, str, str]],
    category_name: str,
    issue_type: str,
    fingerprint: str,
    finding: dict,
) -> dict:
    """Attach a stable fingerprint plus the user's acknowledged flag.

    Tags rather than filters: an acknowledged finding stays in the list so
    the frontend can collapse it behind a "N acknowledged" toggle instead of
    it silently vanishing. Fingerprints are namespaced per
    category+issue_type+metric, so two metrics flagged at the same
    timestamp/date (e.g. power's import and export both resetting at once)
    never collide."""
    return {
        **finding,
        "fingerprint": fingerprint,
        "acknowledged": (category_name, issue_type, fingerprint) in acknowledged,
    }


# ---------------------------------------------------------------------------
# Daily-total reconciliation.
#
# Every other check in this module is a heuristic hunting for something that
# looks odd. This one is an INVARIANT: on a cumulative meter a day's usage is
# closing-minus-opening, full stop. compute_daily_deltas arrives there by
# summing many small deltas; this arrives there by subtracting two numbers.
# A bug in the summation cannot hide in both.
#
# Raised after a real incident where power import read ~2x reality for five
# days (rebuild_power_daily summed t1+t2+combined on rows where the api_live
# poller populates all three). Every heuristic in this module passed it; it
# was caught only by a user comparing the HomeWizard app against the
# dashboard by eye. This check would have reported stored = 2x expected on
# every affected date the first time it ran.
#
# Matters beyond any one deployment: any installation that mixes CSV uploads
# with live API polling can hit this same class of bug, and won't have
# someone who knows the pipeline watching the numbers.
#
# Scope, stated honestly: this verifies the SUMMATION, not the glitch
# cleaning. It re-derives a day's total from the same cleaned point series
# the rollup uses, so a bug in clean_cumulative_glitches would move both
# sides together and go unseen here -- that is what find_glitch_episodes is
# for. What it does catch is wrong column arithmetic, double-counted
# sources, and unit-conversion errors.
# ---------------------------------------------------------------------------

RECONCILIATION_TOLERANCE_PCT = 0.5


def _daily_endpoints(points: list[tuple[str, float]]) -> dict[str, dict]:
    """Cleaned, sorted points -> per-date {opening, closing, reset, gap}.

    Mirrors compute_daily_deltas' interval attribution exactly: an interval
    belongs to the date of its *earlier* endpoint. The intervals belonging to
    a single date are contiguous, so their deltas telescope -- summing them
    and subtracting opening from closing are two routes to the same number,
    which is precisely what makes this usable as a cross-check.

    `reset` and `gap` mark the two cases where compute_daily_deltas
    deliberately drops an interval (a negative delta, or a span exceeding
    MAX_DELTA_SPAN_HOURS). On those dates telescoping legitimately disagrees
    with the sum, so the date is reported as unverifiable rather than wrong."""
    max_span = timedelta(hours=MAX_DELTA_SPAN_HOURS)
    by_date: dict[str, dict] = {}
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        d = _date_of(t0)
        entry = by_date.setdefault(d, {"opening": v0, "closing": v1, "reset": False, "gap": False})
        entry["closing"] = v1
        if v1 < v0:
            entry["reset"] = True
        if _parse_time(t1) - _parse_time(t0) > max_span:
            entry["gap"] = True
    return by_date


def _expected_daily(rows: list[dict], value_key: str) -> dict[str, dict]:
    points = sorted(
        ((r["time"], r[value_key]) for r in rows if r.get(value_key) is not None),
        key=lambda p: p[0],
    )
    return _daily_endpoints(clean_cumulative_glitches(points))


# How each *_daily column maps back to raw cumulative column(s). `divisor`
# reproduces any unit conversion the rebuild applies (water stores dL raw and
# L daily), so a regression of the historical /100-instead-of-/10 bug would
# surface here as a 10x mismatch. It must be the *same operation* the rebuild
# uses, not an algebraic equivalent: expressing water's conversion as `* 0.1`
# instead of `/ 10.0` made 197 dates disagree in the last floating-point bit
# -- invisible at the 0.5% tolerance, but a false signal at zero tolerance,
# and exactly the kind of noise that erodes trust in a check like this.
_RECONCILIATION_METRICS: dict[str, list[dict]] = {
    "power": [
        {
            "daily_column": "import_kwh",
            "primary": "import_combined_kwh",
            "fallback": ["import_t1_kwh", "import_t2_kwh"],
            "divisor": 1.0,
        },
        {
            "daily_column": "export_kwh",
            "primary": "export_combined_kwh",
            "fallback": ["export_t1_kwh", "export_t2_kwh"],
            "divisor": 1.0,
        },
    ],
    "gas": [{"daily_column": "usage_m3", "primary": "total_gas_m3", "fallback": [], "divisor": 1.0}],
    "water": [{"daily_column": "usage_l", "primary": "water_usage_dl", "fallback": [], "divisor": 10.0}],
    "battery": [
        {"daily_column": "charge_kwh", "primary": "import_kwh", "fallback": [], "divisor": 1.0},
        {"daily_column": "discharge_kwh", "primary": "export_kwh", "fallback": [], "divisor": 1.0},
    ],
}


def reconcile_daily_totals(
    conn: sqlite3.Connection,
    acknowledged: set[tuple[str, str, str]] | None = None,
    tolerance_pct: float = RECONCILIATION_TOLERANCE_PCT,
) -> dict:
    """Per category: every stored daily total re-derived independently and
    compared. Read-only; never writes or corrects anything.

    Returns {category: {verified, mismatches[], unverifiable[]}}. Every
    date/metric pair lands in exactly one of the three, so
    verified + len(mismatches) + len(unverifiable) is the whole population --
    an unverifiable date is never quietly folded into the pass count, which
    is the difference between "7,920 verified, 6 unverifiable" and a bare
    green tick that hides its own exemptions."""
    acknowledged = acknowledged or set()
    report: dict[str, dict] = {}

    for name, spec in _QUALITY_CATEGORIES.items():
        raw_rows = filter_preferred_granularity(_rows_as_dicts(conn, spec["readings_table"]))
        daily_by_date = {r["date"]: r for r in _rows_as_dicts(conn, spec["daily_table"])}

        # The newest date is never checked. Raw readings keep arriving after
        # the rollup last ran, so the stored total for a day still in progress
        # is legitimately a little behind the raw meter -- measured live at
        # 0.014 kWh on power export. Left in, that would report a mismatch on
        # every single run forever and train the eye to ignore this check.
        # Same precedent find_outlier_days and _find_daily_gaps already set.
        newest_date = max(daily_by_date) if daily_by_date else None

        verified = 0
        mismatches: list[dict] = []
        unverifiable: list[dict] = []

        for metric in _RECONCILIATION_METRICS[name]:
            primary = _expected_daily(raw_rows, metric["primary"])
            fallbacks = [_expected_daily(raw_rows, col) for col in metric["fallback"]]

            for d in sorted(set(primary) | {date for f in fallbacks for date in f}):
                if d == newest_date:
                    continue
                daily_row = daily_by_date.get(d)
                if daily_row is None:
                    continue
                stored = daily_row.get(metric["daily_column"])
                if stored is None:
                    continue

                # Mirrors rebuild_power_daily: prefer the combined column when
                # the date has one, fall back to the t1+t2 pair only when it
                # genuinely doesn't (the CSV-only case).
                sources = [primary[d]] if d in primary else [f[d] for f in fallbacks if d in f]
                if not sources:
                    continue

                reason = None
                if any(s["reset"] for s in sources):
                    reason = "meter reset on this date"
                elif any(s["gap"] for s in sources):
                    reason = "interval over the span cap (gap)"
                fingerprint = f"{d}|{metric['daily_column']}"
                if reason:
                    unverifiable.append(
                        _tag_finding(
                            acknowledged,
                            name,
                            "reconciliation_unverifiable",
                            fingerprint,
                            {"date": d, "metric": metric["daily_column"], "reason": reason},
                        )
                    )
                    continue

                expected = sum(s["closing"] - s["opening"] for s in sources) / metric["divisor"]
                diff = stored - expected
                scale = max(abs(expected), abs(stored))
                diff_pct = (abs(diff) / scale * 100.0) if scale > 0 else 0.0
                if diff_pct <= tolerance_pct:
                    verified += 1
                else:
                    mismatches.append(
                        _tag_finding(
                            acknowledged,
                            name,
                            "reconciliation_mismatch",
                            fingerprint,
                            {
                                "date": d,
                                "metric": metric["daily_column"],
                                "stored": stored,
                                "expected": expected,
                                "diff": diff,
                                "diff_pct": diff_pct,
                            },
                        )
                    )

        report[name] = {"verified": verified, "mismatches": mismatches, "unverifiable": unverifiable}

    return report


# A leading run of zero-valued days shorter than this is not worth reporting
# -- a meter installed on a quiet day legitimately registers nothing for a
# little while.
EMPTY_RUN_MIN_DAYS = 30


def find_leading_empty_run(
    daily_rows: list[dict], value_key: str, min_days: int = EMPTY_RUN_MIN_DAYS
) -> dict | None:
    """A run of zero-valued days at the very START of a category's history --
    the signature of data covering a period before the meter physically
    existed. Returns {start, end, days} or None.

    POSITION is the whole discriminator, and it has to be. A *mid-series* run
    of zero days is real behaviour and must never be flagged: a household may
    be away for an extended period, and its gas
    legitimately reads 0.00 for weeks at a stretch. What cannot be real is a
    cumulative counter that has never registered anything at all -- before
    the first non-zero reading in a category's whole history, the meter was
    either absent or not reporting, and either way those days are "no data",
    not "no usage".

    Found by an earlier investigation, from real data: 699 consecutive water
    rows, plus ~321 more days after them, were exactly 0.0, because the
    water meter was installed some time after the P1 power/gas meter and
    the long-range export padded the pre-installation period with zeros so the
    two series would start together -- roughly three years of history before
    the first water reading that ever moved.

    Worth having because every other check in this module is individually
    correct and every one of them misses this: the gap detector sees the
    dates as present, the outlier check skips a zero baseline as
    meaningless, reconciliation correctly confirms 0 - 0 = 0, and there is
    no dip for the glitch detector to find."""
    points = sorted(
        ((r["date"], r[value_key]) for r in daily_rows if r.get(value_key) is not None),
        key=lambda p: p[0],
    )
    if not points:
        return None

    days = 0
    for _, v in points:
        if v != 0:
            break
        days += 1
    else:
        # Every day is zero -- the meter has never registered anything.
        pass

    if days < min_days:
        return None
    return {"start": points[0][0], "end": points[days - 1][0], "days": days}


def data_integrity_report(
    conn: sqlite3.Connection, acknowledged: set[tuple[str, str, str]] | None = None
) -> dict:
    """Read-only report of things that mean THE DATA ITSELF is suspect, per
    category: negative/skipped deltas, glitch-correction episodes,
    cross-source granularity disagreements, and (battery) out-of-range gauge
    values. Never written anywhere, never changes what rebuild_all() computes.

    Deliberately excludes outlier daily totals, which moved to
    consumption_notes_report -- see that function for why. Measured against
    a real database at the time of the split: this report holds 25
    genuine findings, where the combined report it replaced held 689, so the
    real instrument faults had been sitting under a ~96% noise floor."""
    acknowledged = acknowledged or set()
    report: dict[str, dict] = {}

    for name, spec in _QUALITY_CATEGORIES.items():
        raw_rows = _rows_as_dicts(conn, spec["readings_table"])
        daily_rows = _rows_as_dicts(conn, spec["daily_table"])

        negative_deltas: list[dict] = []
        glitch_episodes: list[dict] = []
        granularity_disagreements: list[dict] = []
        for value_key in spec["raw_value_keys"]:
            for n in find_negative_deltas(raw_rows, value_key):
                finding = {**n, "metric": value_key}
                negative_deltas.append(
                    _tag_finding(acknowledged, name, "negative_delta", f"{finding['time']}|{value_key}", finding)
                )
            for e in find_glitch_episodes(raw_rows, value_key):
                finding = {**e, "metric": value_key}
                glitch_episodes.append(
                    _tag_finding(acknowledged, name, "glitch_episode", f"{finding['start_time']}|{value_key}", finding)
                )
            for d in find_granularity_disagreements(raw_rows, value_key):
                finding = {**d, "metric": value_key}
                granularity_disagreements.append(
                    _tag_finding(
                        acknowledged, name, "granularity_disagreement", f"{finding['date']}|{value_key}", finding
                    )
                )

        implausible_values: list[dict] = []
        for metric, (lo, hi) in spec["range_checks"].items():
            for v in find_out_of_range_values(daily_rows, metric, lo, hi):
                implausible_values.append(
                    _tag_finding(acknowledged, name, "implausible_value", f"{v['date']}|{metric}", v)
                )

        empty_runs: list[dict] = []
        for metric in spec["outlier_metrics"]:
            run = find_leading_empty_run(daily_rows, metric)
            if run is not None:
                finding = {**run, "metric": metric}
                empty_runs.append(
                    _tag_finding(acknowledged, name, "empty_run", f"{run['start']}|{metric}", finding)
                )

        report[name] = {
            "negative_deltas": {"count": len(negative_deltas), "items": negative_deltas},
            "glitch_episodes": {"count": len(glitch_episodes), "items": glitch_episodes},
            "granularity_disagreements": granularity_disagreements,
            "implausible_values": implausible_values,
            "empty_runs": empty_runs,
        }

    return report


def _occupancy_by_day(conn: sqlite3.Connection) -> dict[str, int]:
    """occupancy_log -> {date: occupant_count}, used ONLY to annotate a
    consumption note, never to suppress one.

    Measured before wiring this in, against a real occupancy log: it covers 229
    days of a ~5.5-year history, so roughly 80% of notes land on days it says
    nothing about. Where it does cover, it explains well (16 of gas's 23
    covered low days were logged as nobody home). It is a helpful hint and
    nothing more -- a day spent unwell at home reads as perfectly normal
    occupancy while consumption legitimately halves, which no log can
    capture. That limit is precisely why annotation is the right role for it
    and suppression is not."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log").fetchall()]
    if not rows:
        return {}
    lo = min(r["date_from"] for r in rows)[:10]
    hi = max(r["date_to"] for r in rows)[:10]
    return expand_occupancy_by_day(rows, lo, hi)


def consumption_notes_report(
    conn: sqlite3.Connection,
    acknowledged: set[tuple[str, str, str]] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Read-only report of episodes whose consumption sat far outside its own
    recent baseline -- informational only, NOT a data-health signal.

    Split out of the old combined data_quality_report deliberately. A day of
    near-zero water because the house was empty, or because someone was ill in
    bed, is *correctly recorded data*: the meter did its job. Presenting that
    under a "data health" heading invited reading real life as a fault, and
    -- worse -- put it next to remediation actions whose end point is
    deleting accurate readings. Outlier days therefore live here, on their
    own, with no delete affordance; only acknowledge, which records "I've
    looked at this" and touches no reading.

    Each note carries `occupancy` (the logged headcount, or None if unlogged
    or inconsistent across the episode) purely as a hint -- see
    _occupancy_by_day for how partial that coverage really is.

    date_from/date_to scope the report, defaulting to unbounded.
    Before this was added, this was the ONE view in the app that ignored the selected
    range entirely, which is why it returned all five years (794 notes) while
    every chart beside it showed 90 days.

    Two ordering decisions here are load-bearing, and both were settled by
    measuring rather than reasoning:

    1. Detection runs over FULL history and only the OUTPUT is scoped. It is
       tempting to trim `daily_rows` to the window first and let the detector
       see less, but find_outlier_days builds a *trailing* baseline
       (OUTLIER_MIN_HISTORY_DAYS before a day is eligible at all,
       OUTLIER_LONG_RUN_WINDOW_DAYS for the long-run median) -- so trimming
       the input changes *which days are flagged*, not merely which are
       shown. Measured against the real database: a 7-day window yields 0
       notes instead of 4 (7 < the 14-day eligibility floor, so the shortest
       preset would show nothing at all, permanently), 30d gives 13 vs 26,
       90d 53 vs 75, and 1y gives 311 vs 261 -- MORE flags, because a
       truncated long-run median invents new ones. Not merely lossy;
       directionally inconsistent.

    2. Episodes are grouped over full history and then filtered by OVERLAP,
       never grouped from the already-filtered days. A window edge falling
       mid-run would otherwise split the run and silently change its start
       date -- and the start date IS the fingerprint, so an acknowledgement
       would appear to vanish purely because the user changed the date
       preset."""
    acknowledged = acknowledged or set()
    occupancy = _occupancy_by_day(conn)
    report: dict[str, dict] = {}

    for name, spec in _QUALITY_CATEGORIES.items():
        # A category with no consumption metrics at all (battery: charge and
        # discharge are internal circulation, not household usage) is omitted
        # rather than reported as an empty list. "Battery -- nothing unusual"
        # would be a false reassurance: it reads as "checked, all fine" when
        # the truth is that this report has nothing to say about it. Callers
        # iterate the returned keys, so the report stays self-describing.
        if not spec["consumption_metrics"]:
            continue
        daily_rows = _rows_as_dicts(conn, spec["daily_table"])
        notes: list[dict] = []
        for metric in spec["consumption_metrics"]:
            annotated = [
                {**o, "occupancy": occupancy.get(o["date"])} for o in find_outlier_days(daily_rows, metric)
            ]
            for episode in group_outlier_episodes(annotated):
                if date_from and episode["end"] < date_from:
                    continue
                if date_to and episode["start"] > date_to:
                    continue
                finding = {**episode, "metric": metric}
                # Keyed on the START date, matching glitch_episode's scheme
                # above. An explicit design decision, with its consequence
                # accepted knowingly: a run that later extends FORWARD keeps
                # its acknowledgement, one that extends BACKWARD loses it.
                # Also means single-day episodes keep the exact fingerprint
                # they had before grouping existed, so those acknowledgements
                # survive this change untouched.
                notes.append(
                    _tag_finding(acknowledged, name, "outlier_day", f"{finding['start']}|{metric}", finding)
                )
        report[name] = {"outlier_days": sorted(notes, key=lambda n: n["start"])}

    # Two views of the same findings, deliberately both returned.
    # `categories` drives the per-utility summary (Power / Gas / Water rows),
    # which has to stay per-category to be meaningful at all. `events` is the
    # detail list, where the unit a person cares about is the event, not the
    # meter that happened to notice it. Returning a nested shape rather than
    # adding "events" alongside the category keys keeps callers from having to
    # know which top-level keys are categories and which are not.
    events = group_consumption_events({name: entry["outlier_days"] for name, entry in report.items()})

    # An event every day of which falls inside a gas-and-water-zero stretch is
    # already explained: nobody was home. Tagged, never removed -- the house
    # rule throughout this module is that a finding stays listed with the
    # reason attached, so the count is always honest and the user decides what
    # to do about it.
    away = find_away_days(conn)
    charging = find_ev_charge_days(conn)
    for event in events:
        dates = _event_dates(event)
        if away and all(d in away for d in dates):
            event["explained_by"] = "nobody home"
        elif (
            event["direction"] == "high"
            and event["categories"] == ["power"]
            and charging
            and all(d in charging for d in dates)
        ):
            # Scoped to power-high events only. A charge explains the import
            # spike it caused and nothing else -- it must never be allowed to
            # wave away a water or gas anomaly that happened to fall on the
            # same day.
            event["explained_by"] = "EV charging"

    return {"categories": report, "events": events}


def _resolve_occupancy_overlaps(
    parsed: list[tuple[datetime, datetime, int]],
) -> list[tuple[datetime, datetime, int]]:
    """Collapse possibly-overlapping (start, end, occupant_count) entries
    into a non-overlapping timeline. occupancy_log entries are allowed to
    nest/overlap (e.g. a shorter trip inside a longer visit) -- at any
    instant covered by more than one entry, the entry with the shortest
    total duration wins (the more specific entry overrides the surrounding
    one), ties (equal duration) broken in favour of the later-starting
    entry. An instant not covered by any entry produces no output segment.
    Non-overlapping input passes through unchanged, one segment per entry."""
    if not parsed:
        return []
    breakpoints = sorted({t for start, end, _ in parsed for t in (start, end)})
    resolved: list[tuple[datetime, datetime, int]] = []
    for t1, t2 in zip(breakpoints, breakpoints[1:], strict=False):
        active = [(start, end, count) for start, end, count in parsed if start <= t1 and end >= t2]
        if not active:
            continue
        winner_count = min(active, key=lambda e: (e[1] - e[0], -e[0].timestamp()))[2]
        if resolved and resolved[-1][1] == t1 and resolved[-1][2] == winner_count:
            resolved[-1] = (resolved[-1][0], t2, winner_count)
        else:
            resolved.append((t1, t2, winner_count))
    return resolved


# How long an unlogged gap may be before it stops being bridged. See the
# bridging block inside expand_occupancy_by_day for the measurement -- the
# cap exists because the function's own documented intent ("a short unlogged
# stretch") was never enforced.
OCCUPANCY_BRIDGE_MAX_GAP_DAYS = 3


def expand_occupancy_by_day(rows: list[dict], date_from: str, date_to: str) -> dict[str, int]:
    """occupancy_log rows -> {date: occupant_count} for every day in
    [date_from, date_to] that's covered, directly or by bridging (see below),
    by at least one logged entry. Days with zero covered minutes are omitted
    entirely -- "unknown," never assumed to be "alone."

    Entries carry time-of-day ('YYYY-MM-DD HH:MM', not just a date), so a
    departure/return that doesn't align with midnight can be its own entry --
    which means more than one entry can touch the same calendar day (e.g.
    "away 08:00-18:00" then "occupied 18:00-24:00"). But the day-level stats
    this feeds (Alone/Occupied/Away, Comparison-tab avg headcount, chart
    overlay) all key off day-granularity usage totals (power_daily etc.),
    with no sub-day usage to correlate against -- so each day still resolves
    to a single occupant_count, chosen as whichever count covers the most
    minutes of that day ("majority of day wins"), ties broken in favour of
    the later (chronologically last) segment -- "whichever state the day
    ended in."

    Gap bridging (a follow-up to the time-of-day CR): a gap
    strictly BETWEEN two logged entries is filled by carrying the earlier
    entry's occupant_count forward until the next entry begins -- "nothing
    changed" is the reasonable default for a short unlogged stretch with real
    data on both sides. Deliberately NOT applied to a gap before the first
    entry or after the last one: there's no prior state to carry into the
    former, and bridging the latter would let a simply-forgotten log entry
    silently misreport an open-ended stretch of "current" days as a stale
    historical state, rather than surfacing as missing data the way it does
    today. Entries MAY overlap/nest in time (app.py no longer rejects this
    for occupancy_log) -- _resolve_occupancy_overlaps() collapses them to a
    non-overlapping timeline first (most-specific/shortest entry wins per
    instant), so the bridging and summing below never double-counts."""
    parsed = sorted(
        (
            (
                datetime.strptime(r["date_from"], "%Y-%m-%d %H:%M"),
                datetime.strptime(r["date_to"], "%Y-%m-%d %H:%M"),
                r["occupant_count"],
            )
            for r in rows
        ),
        key=lambda t: t[0],
    )
    resolved = _resolve_occupancy_overlaps(parsed)
    segments = list(resolved)
    for (_, prev_end, prev_count), (next_start, _, _) in zip(resolved, resolved[1:], strict=False):
        if prev_end < next_start:
            # Bridge only a SHORT gap. This function always claimed to
            # ("nothing changed" is the reasonable default for a *short*
            # unlogged stretch) but never enforced it, so a 3-day trip carried
            # its count across every unlogged day until the next entry --
            # measured at up to 75 days. With a log made largely of absences,
            # that propagated "house empty" across whole months: 272 days
            # resolved as empty while the meters showed real use, some at
            # 900 L/day. Capping restores those days to honest "unknown"
            # (absent from the result), which is what having no entry should
            # mean. Measured: 272 wrong -> 10, with 7/3/2-day caps performing
            # near-identically, so 3 is the conservative reading of "short".
            if (next_start - prev_end).days <= OCCUPANCY_BRIDGE_MAX_GAP_DAYS:
                segments.append((prev_end, next_start, prev_count))

    minutes_by_day: dict[str, dict[int, int]] = {}
    for start, end, count in sorted(segments, key=lambda t: t[0]):
        d = start.date()
        while d <= end.date():
            day_start = datetime.combine(d, datetime.min.time())
            day_end = day_start + timedelta(days=1)
            overlap_start = max(start, day_start)
            overlap_end = min(end, day_end)
            minutes = int((overlap_end - overlap_start).total_seconds() // 60)
            if minutes > 0:
                day_counts = minutes_by_day.setdefault(d.isoformat(), {})
                day_counts[count] = day_counts.get(count, 0) + minutes
            d += timedelta(days=1)

    by_day: dict[str, int] = {}
    for iso, counts in minutes_by_day.items():
        if not (date_from <= iso <= date_to):
            continue
        # counts preserves insertion order = chronological order a count was
        # first seen that day (segments were processed sorted by start time)
        # -- ">=" rather than ">" means a later-inserted count that ties the
        # current best overwrites it, giving the "later segment wins" tie-break.
        best_count: int | None = None
        best_minutes = -1
        for count, minutes in counts.items():
            if minutes >= best_minutes:
                best_count, best_minutes = count, minutes
        assert best_count is not None  # counts is only ever populated with >0 minutes, so never empty
        by_day[iso] = best_count
    return by_day


def _rows_as_dicts(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


# Tables that receive 'live' (HA-sourced) rows -- water has no HA source yet.
_LIVE_TABLES = ("power_readings", "battery_readings", "gas_readings")

PRUNE_KEEP_DAYS = 30


def prune_live_readings(conn: sqlite3.Connection, keep_days: int = PRUNE_KEEP_DAYS) -> int:
    """Downsample old 'live' rows to 15-minute resolution.

    HA history lands at minute resolution (~1,440 rows/day/category), which
    grows without bound and makes the full-table rollup rebuild progressively
    more expensive. Rows older than keep_days are thinned to :00/:15/:30/:45
    -- the same density as the HomeWizard 15-minute CSV export.

    Each date's chronologically *last* reading is always kept regardless of
    its minute, even though it's a cumulative-meter table: compute_daily_deltas
    attributes an interval to the date of its *earlier* endpoint, so deleting
    the true last reading of a day (if it doesn't happen to land on a quarter
    hour) would shift up to ~15 minutes of that day's usage onto the following
    day instead of just losing precision within the day. Keeping the exact
    last-of-day anchor makes day-level totals exactly reproducible from the
    thinned data, not just approximately so. The only real fidelity cost is
    that old days' SoC min/max become 15-min-coarse instead of minute-precise
    (their own end-of-day value is separately protected by the same anchor).
    Returns total rows deleted."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d 00:00")
    deleted = 0
    for table in _LIVE_TABLES:
        last_of_day = {
            row["t"]
            for row in conn.execute(
                f"SELECT MAX(time) AS t FROM {table} WHERE granularity = 'live' AND time < ? "
                "GROUP BY substr(time, 1, 10)",
                (cutoff,),
            )
        }
        candidates = conn.execute(
            f"SELECT time FROM {table} WHERE granularity = 'live' AND time < ?", (cutoff,)
        ).fetchall()
        to_delete = [
            (row["time"],)
            for row in candidates
            if row["time"][14:16] not in ("00", "15", "30", "45") and row["time"] not in last_of_day
        ]
        if to_delete:
            conn.executemany(f"DELETE FROM {table} WHERE time = ?", to_delete)
            deleted += len(to_delete)
    return deleted


def rebuild_power_daily(conn: sqlite3.Connection) -> None:
    all_rows = _rows_as_dicts(conn, "power_readings")
    rows = filter_preferred_granularity(all_rows)
    import_t1 = compute_daily_deltas(rows, "import_t1_kwh")
    import_t2 = compute_daily_deltas(rows, "import_t2_kwh")
    import_combined = compute_daily_deltas(rows, "import_combined_kwh")
    export_t1 = compute_daily_deltas(rows, "export_t1_kwh")
    export_t2 = compute_daily_deltas(rows, "export_t2_kwh")
    export_combined = compute_daily_deltas(rows, "export_combined_kwh")

    # l1/l2/l3_max_w deliberately use all_rows, NOT the granularity-filtered
    # `rows` above. Phase-load data only ever comes from CSV (15min/daily);
    # historical HA-sourced 'live' rows (that ingest path was removed
    # 2026-07-28, but years of real 'live' data remain in the DB) never had a
    # phase-load field wired in, so once 'live' is the
    # preferred/highest-ranked granularity for a date,
    # filter_preferred_granularity() would silently drop the only rows that
    # ever had phase data for that date, leaving l1/l2/l3_max_w NULL even
    # though a real 15min reading with real phase data is sitting right
    # there in the raw table. compute_daily_extrema already ignores None
    # values per row, so running it over all_rows just means: for any date,
    # pick up phase data from whichever rows actually have it, independent
    # of which granularity "won" for import/export on that date.
    l1 = compute_daily_extrema(all_rows, "l1_max_w")
    l2 = compute_daily_extrema(all_rows, "l2_max_w")
    l3 = compute_daily_extrema(all_rows, "l3_max_w")

    # A given date's rows are always a single granularity (see
    # filter_preferred_granularity), so historically at most one of {t1+t2}
    # or {combined} was ever non-empty for a date -- true while CSV (t1/t2
    # only) and the old HA 'live' sync (combined only) were the two sources,
    # since each wrote to a disjoint column family. That assumption broke
    # 2026-07-28: the api_live poller's _map_p1() writes
    # t1, t2, AND combined into every row, so summing all three double-counts
    # every api_live-only date (confirmed live -- OmniMeter reported
    # ~2x a real day's import). Prefer combined when present for a date (it's
    # the single authoritative total on both the 'live'-era and api_live-era
    # rows); fall back to t1+t2 only when combined is genuinely absent (the
    # CSV-only case).
    dates = (
        set(import_t1) | set(import_t2) | set(import_combined)
        | set(export_t1) | set(export_t2) | set(export_combined)
        | set(l1) | set(l2) | set(l3)
    )
    conn.execute("DELETE FROM power_daily")
    for d in dates:
        imp = import_combined[d] if d in import_combined else import_t1.get(d, 0.0) + import_t2.get(d, 0.0)
        exp = export_combined[d] if d in export_combined else export_t1.get(d, 0.0) + export_t2.get(d, 0.0)
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh, "
            "l1_max_w, l2_max_w, l3_max_w) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                d,
                imp,
                exp,
                imp - exp,
                l1.get(d, {}).get("max"),
                l2.get(d, {}).get("max"),
                l3.get(d, {}).get("max"),
            ),
        )


def rebuild_gas_daily(conn: sqlite3.Connection) -> None:
    rows = filter_preferred_granularity(_rows_as_dicts(conn, "gas_readings"))
    usage = compute_daily_deltas(rows, "total_gas_m3")
    conn.execute("DELETE FROM gas_daily")
    for d, v in usage.items():
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, ?)", (d, v))


def rebuild_water_daily(conn: sqlite3.Connection) -> None:
    rows = filter_preferred_granularity(_rows_as_dicts(conn, "water_readings"))
    usage = compute_daily_deltas(rows, "water_usage_dl")
    conn.execute("DELETE FROM water_daily")
    for d, v in usage.items():
        # 1 L = 10 dL (was incorrectly /100 -- understated every day's usage by 10x)
        conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, ?)", (d, v / 10.0))


def rebuild_battery_daily(conn: sqlite3.Connection) -> None:
    rows = filter_preferred_granularity(_rows_as_dicts(conn, "battery_readings"))
    charge = compute_daily_deltas(rows, "import_kwh")
    discharge = compute_daily_deltas(rows, "export_kwh")
    soc = compute_daily_extrema(rows, "soc_pct")
    eod_soc = compute_daily_last_value(rows, "soc_pct")

    dates = set(charge) | set(discharge) | set(soc)
    conn.execute("DELETE FROM battery_daily")
    for d in dates:
        s = soc.get(d, {})
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh, "
            "min_soc_pct, max_soc_pct, avg_soc_pct, eod_soc_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d, charge.get(d, 0.0), discharge.get(d, 0.0), s.get("min"), s.get("max"), s.get("avg"), eod_soc.get(d)),
        )


def energy_flow_matrix(
    solar_kwh: float,
    discharge_kwh: float,
    import_kwh: float,
    charge_kwh: float,
    export_kwh: float,
) -> dict:
    """Merit-order allocation of six period totals (3 sources -> 3 uses) for
    the Overview Sankey visual. OmniMeter measures node totals only -- which
    kWh of solar/battery/grid served which use isn't observable, so this
    applies a documented self-consumption-first assumption (an
    explicit choice over a proportional/independence split, which would
    produce physically nonsensical pairs like battery-to-battery or
    grid-in-to-grid-out): solar serves Load first, then Battery charge, then
    Grid out; Battery discharge serves remaining Load, then Grid out; Grid
    In serves remaining Load, then remaining Battery charge (grid-charging
    the battery is real behaviour for a HomeWizard Plug-In Battery doing
    price trading).

    Load is a residual, not measured: Load = Solar + Discharge + Import -
    Charge - Export. This differs from estimate_self_sufficiency's implied
    consumption (Import + Solar - Export), which ignores the battery
    entirely -- over a real period charge > discharge (round-trip loss), so
    this Load reads slightly lower. A short range can make the raw residual
    negative (the battery charged from grid before the window and
    discharged inside it, or export exceeds estimated production) --
    clamped to 0 here, with the shortfall reported as unbalanced_kwh rather
    than silently absorbed into a wrong Load figure.

    Two passes, not one. Total sources always equal total uses in aggregate
    (by construction of Load above), but restricting each source to its
    physically-sensible uses can still leave a source with leftover capacity
    and a use unserved by the exact same amount -- e.g. heavy battery
    discharge plus insufficient grid import to finish charging the battery,
    with solar and grid-out already fully claimed by Load. A Sankey diagram
    requires each node's ribbons to sum to its total, so pass 2 mops up any
    such leftover through whatever pairing is left, including the two
    otherwise-avoided pairs (battery-to-battery, grid-in-to-grid-out) if
    that is genuinely all that remains. This is a period-total artifact of
    forcing conservation, not a claim that discharge instantaneously
    recharged the battery -- and pass 1 already claimed every kWh that had a
    physically-sensible home, so pass 2 only ever moves what pass 1 couldn't.
    """
    load = solar_kwh + discharge_kwh + import_kwh - charge_kwh - export_kwh
    unbalanced_kwh = 0.0
    if load < 0:
        unbalanced_kwh = -load
        load = 0.0

    remaining_sources = {"solar": solar_kwh, "battery_discharge": discharge_kwh, "grid_in": import_kwh}
    remaining_uses = {"load": load, "battery_charge": charge_kwh, "grid_out": export_kwh}
    preferred_order = {
        "solar": ("load", "battery_charge", "grid_out"),
        "battery_discharge": ("load", "grid_out"),
        "grid_in": ("load", "battery_charge"),
    }
    # Mop-up order for pass 2 -- deliberately includes every use, so it's the
    # one path that can produce a forbidden pair.
    fallback_order = ("load", "battery_charge", "grid_out")

    flows_by_pair: dict[tuple[str, str], float] = {}
    # Separate from unbalanced_kwh: this tracks kWh that WAS placed
    # somewhere (conservation holds), just through the pass-2 mop-up rather
    # than a physically-sensible pairing. Confirmed against real data
    # that this isn't a rare corner case -- a single day can genuinely
    # contain both grid import (e.g. overnight battery charging) and solar
    # export (midday surplus), which day-level totals can't separate, so
    # this is reported honestly rather than left invisible in the diagram.
    fallback_kwh = 0.0

    def allocate(source_key: str, use_order: tuple[str, ...], *, is_fallback: bool) -> None:
        nonlocal fallback_kwh
        for use_key in use_order:
            if remaining_sources[source_key] <= 1e-9:
                break
            portion = min(remaining_sources[source_key], remaining_uses[use_key])
            if portion > 1e-9:
                pair = (source_key, use_key)
                flows_by_pair[pair] = flows_by_pair.get(pair, 0.0) + portion
                remaining_sources[source_key] -= portion
                remaining_uses[use_key] -= portion
                if is_fallback:
                    fallback_kwh += portion

    for source_key, use_order in preferred_order.items():
        allocate(source_key, use_order, is_fallback=False)
    for source_key in preferred_order:
        allocate(source_key, fallback_order, is_fallback=True)

    flows = [
        {"from": frm, "to": to, "kwh": round(kwh, 4)}
        for (frm, to), kwh in flows_by_pair.items()
        if kwh > 1e-9
    ]

    return {
        "sources": {
            "solar": round(solar_kwh, 2),
            "battery_discharge": round(discharge_kwh, 2),
            "grid_in": round(import_kwh, 2),
        },
        "uses": {
            "load": round(load, 2),
            "battery_charge": round(charge_kwh, 2),
            "grid_out": round(export_kwh, 2),
        },
        "flows": flows,
        "unbalanced_kwh": round(unbalanced_kwh, 2),
        "fallback_kwh": round(fallback_kwh, 2),
    }


def sum_energy_flow_matrices(daily_matrices: list[dict]) -> dict:
    """Merges a list of single-day energy_flow_matrix() results into one
    summed matrix for a period.

    This exists because calling energy_flow_matrix() ONCE on period totals
    is wrong, not just less precise: it conflates every day's own generation
    pattern into a single priority waterfall. Confirmed against real
    data over a 90-day range -- solar and battery discharge were fully
    absorbed by Load in a single pass before either had anything left for
    Grid out, so the entire period's real export (297 kWh, almost certainly
    mostly solar on sunny days) got attributed to the grid-in-to-grid-out
    fallback pair instead, which is exactly the nonsensical result the
    fallback pair is supposed to be a last resort for, not the common case.
    Running the merit order once per day and summing here keeps each day's
    own solar/battery/grid balance intact, so a sunny day's real surplus
    export is attributed to solar on that day, not smeared across a period
    dominated by other, cloudier or higher-load days.
    """
    sources_total: dict[str, float] = defaultdict(float)
    uses_total: dict[str, float] = defaultdict(float)
    flows_by_pair: dict[tuple[str, str], float] = defaultdict(float)
    unbalanced_total = 0.0
    fallback_total = 0.0

    for day in daily_matrices:
        for key, value in day["sources"].items():
            sources_total[key] += value
        for key, value in day["uses"].items():
            uses_total[key] += value
        for f in day["flows"]:
            flows_by_pair[(f["from"], f["to"])] += f["kwh"]
        unbalanced_total += day["unbalanced_kwh"]
        fallback_total += day["fallback_kwh"]

    flows = [
        {"from": frm, "to": to, "kwh": round(kwh, 4)}
        for (frm, to), kwh in flows_by_pair.items()
        if kwh > 1e-9
    ]

    return {
        "sources": {key: round(value, 2) for key, value in sources_total.items()},
        "uses": {key: round(value, 2) for key, value in uses_total.items()},
        "flows": flows,
        "unbalanced_kwh": round(unbalanced_total, 2),
        "fallback_kwh": round(fallback_total, 2),
    }


def rebuild_all(conn: sqlite3.Connection) -> None:
    prune_live_readings(conn)
    rebuild_power_daily(conn)
    rebuild_gas_daily(conn)
    rebuild_water_daily(conn)
    rebuild_battery_daily(conn)
    conn.commit()
