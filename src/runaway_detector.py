"""Layer 1 fast-path runaway detector -- catches leaks, stuck valves,
running taps and burst pipes. See the three-signal design in
DESIGN_SPEC; this module only implements water (gas and power need
a different discriminator and are later phases).

Two independent detectors live here, and neither substitutes for the other:

**Tier 1** (check_water) watches the *current run* -- an unbroken chain of
non-idle polls -- and describes it with three signals: elapsed duration,
accumulated volume, and the trailing-5-minute mean rate. Three OR'd gates
read those signals. One number could never work: a bath and a burst pipe
have the same flow rate, and what separates them is that a bath stops.

**Tier 2** (check_water_background) watches 15-minute buckets instead, and
exists to close a floor Tier 1 cannot reach at any threshold: a run
survives only if *every* poll advances the meter by at least one 0.1 L
tick, i.e. ~0.3 L/min ~ 430 L/day. Below that most polls read zero, the run
resets, and volume never accumulates -- so a 0.1 L/min wall pinhole
(144 L/day) is not "slow to reach" a volume gate, it is structurally
invisible to one. Tier 2's floor is set by the bucket rather than the poll
(~0.007 L/min, reliable from ~0.02 L/min), which is exactly that gap.

Runs inside meter_poller's poll loop, right after ingest_all() -- the
poller already holds the freshest reading every 20s, so detection latency
is bounded by the poll interval, not by any daily/hourly rollup.
Deliberately does NOT read the raw per-poll measurement value out of
ingest_all(): it re-reads the latest water_readings row from the DB
instead, so this module needs zero changes to
meter_ingest.py/homewizard_api_client.py. Callers must only invoke
check_water() when meter_poller's ingest_all() reported the watermeter as
'ok' this cycle -- otherwise there is no fresh sample to compare against.

**Resolution.** meter_ingest stamps api_live rows to the *minute*, while
the poller polls every 20s, so three polls upsert the same row and
water_readings is a 1-minute series -- exactly the resolution the gates
were validated against. That is why the trailing rate and the Tier 2
buckets need no new state columns: both are queries over rows that already
exist. An earlier version of this docstring claimed polling at 20s is
"strictly more sensitive, never less" than 1-minute data. That is true for
the gates here (every one is >=10 L/min or 250 L) but backwards in general:
at low flows the finer resolution is *less* sensitive, because a sub-tick
poll reads zero and kills the run. That blind spot is Tier 2's whole
reason for existing.
"""

import sqlite3
from datetime import datetime, timedelta

from . import aggregate, alert_delivery, localtime

_LOCAL_TZ = localtime.LOCAL_TZ

# A run is considered dead (not resumable) once its last non-idle sample is
# older than this many seconds -- comfortably more than one missed poll at
# meter_poller's 20s interval, without waiting so long that a run genuinely
# resuming after a brief service restart gets discarded and has to
# re-cross the threshold from zero ("restart after a stale gap").
STALE_GAP_SECONDS = 90

# The sustained-rate signal is a TRAILING mean, not a run average. A
# run can open with a low-flow trickle before real flow begins; a run
# average stays diluted by that lead-in and crosses any bar late, while the
# trailing mean crosses it sooner.
TRAIL_WINDOW_MINUTES = 5

# Tier 2's unit. A bucket is "flowing" if the meter advanced at all within
# it -- 0.1 L per 15 min, three orders of magnitude below Tier 1's floor.
T2_BUCKET_MINUTES = 15

_SEC_FMT = "%Y-%m-%d %H:%M:%S"
_MIN_FMT = "%Y-%m-%d %H:%M"

# Timestamps in this module are compared as strings against
# water_readings.time, which production writes at minute resolution and the
# tests drive at poll resolution. Both are zero-padded and one is a prefix
# of the other, so lexicographic order is chronological order across the
# two -- "13:19" and "13:19:40" sort correctly against each other without
# normalising either one first.

# A dismissed alert nudges the parameter that actually fired it just past
# the value that fired it, rather than an arbitrary percentage bump -- see
# record_dismissal. One margin per unit, because 10 L/min is a rounding
# error on a volume gate and a doubling of a rate gate.
_DISMISSAL_MARGIN_L = 10.0
_DISMISSAL_MARGIN_RATE = 1.0
_DISMISSAL_MARGIN_HOURS = 0.25

# Fresh-install defaults, matching db.py's INSERT OR IGNORE seed for the
# 'water' row -- used only if a metric has no config row yet (e.g. a metric
# added in a later phase before its own seed row exists).
_DEFAULT_THRESHOLDS = {"home_threshold_l": 250.0, "away_threshold_l": 10.0, "away_after_days": 2}

# Gate parameters, matching db.py's column defaults. These defaults are
# conservative: G1 has no duration qualifier because no ordinary domestic use
# reaches its volume; G3's bar is 20 L/min, above typical sustained
# legitimate draw; a 30 L/min bar would delay detection with little safety
# benefit.
_DEFAULT_GATES = {
    "g2_min_minutes": 15,
    "g2_rate_l_min": 10.0,
    "g3_min_minutes": 5,
    "g3_rate_l_min": 20.0,
    "t2_night_hours": 2.5,
    "t2_night_start": 1,
    "t2_night_end": 6,
    "t2_away_hours": 2.0,
    "t2_zero_tolerance": 1,
}

_TIER2_GATES = ("T2-night", "T2-away")


def _now() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, _SEC_FMT).replace(tzinfo=_LOCAL_TZ)


def _parse_reading_time(s: str) -> datetime:
    """water_readings.time is minute-resolution in production (meter_ingest
    stamps '%Y-%m-%d %H:%M'); the tests drive it at poll resolution. Accept
    either rather than forcing one on the caller."""
    return datetime.strptime(s, _SEC_FMT if len(s) > 16 else _MIN_FMT).replace(tzinfo=_LOCAL_TZ)


def get_thresholds(conn: sqlite3.Connection, metric: str) -> dict:
    row = conn.execute(
        "SELECT home_threshold_l, away_threshold_l, away_after_days "
        "FROM runaway_detector_config WHERE metric = ?",
        (metric,),
    ).fetchone()
    return dict(row) if row is not None else dict(_DEFAULT_THRESHOLDS)


def get_gate_config(conn: sqlite3.Connection, metric: str) -> dict:
    """The Tier 1 rate gates and the Tier 2 window gates. Deliberately kept
    separate from get_thresholds(): that function's three keys are the
    Settings form's own contract (app.py spreads them straight into the API
    response), and widening it would silently change that payload's
    shape."""
    columns = ", ".join(_DEFAULT_GATES)
    row = conn.execute(
        f"SELECT {columns} FROM runaway_detector_config WHERE metric = ?", (metric,)
    ).fetchone()
    return dict(row) if row is not None else dict(_DEFAULT_GATES)


def is_away(conn: sqlite3.Connection, metric: str, now: datetime | None = None) -> bool:
    """True if away-mode thresholds should apply right now.

    Declared occupancy always wins: a logged occupancy_log entry covering
    `now` with occupant_count > 0 forces home-mode even mid an away-streak
    -- this is a stated fact, not inferred from consumption, so it does not
    reintroduce circularity.

    Otherwise, away-mode engages once the trailing `away_after_days`
    *completed* calendar days (ending yesterday -- today's totals are not
    settled yet) are all in aggregate.find_away_days(). Reading from
    settled daily rows rather than live readings is what makes this safe
    against auto-relaxing mid-fault: a same-day return to high usage cannot
    flip the state back until tomorrow, by which point a real leak would
    already have fired at the tight threshold.
    """
    now = now or _now()
    now_str = now.strftime(_MIN_FMT)
    declared = conn.execute(
        "SELECT 1 FROM occupancy_log WHERE date_from <= ? AND date_to >= ? AND occupant_count > 0 LIMIT 1",
        (now_str, now_str),
    ).fetchone()
    if declared is not None:
        return False

    n = get_thresholds(conn, metric)["away_after_days"]
    away_days = aggregate.find_away_days(conn)
    today = now.date()
    trailing = [(today - timedelta(days=i)).isoformat() for i in range(1, n + 1)]
    return all(d in away_days for d in trailing)


def _close_episode(conn: sqlite3.Connection, started_at: str, ended_at_str: str) -> None:
    conn.execute(
        "UPDATE alert_episodes SET ended_at = ? "
        "WHERE kind = 'runaway' AND metric = 'water' AND started_at = ? AND ended_at IS NULL",
        (ended_at_str, started_at),
    )


def _reading_at_or_before(conn: sqlite3.Connection, ts: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT time, water_usage_dl FROM water_readings "
        "WHERE granularity = 'api_live' AND time <= ? AND water_usage_dl IS NOT NULL "
        "ORDER BY time DESC LIMIT 1",
        (ts,),
    ).fetchone()


def trailing_rate(conn: sqlite3.Connection, now: datetime, run_started_at: str | None) -> float:
    """Mean L/min over the trailing TRAIL_WINDOW_MINUTES of meter readings.

    Computed straight from the cumulative readings -- (latest - baseline)
    over the minutes between them -- so no rolling buffer has to be kept in
    detector_state and a service restart mid-run loses nothing.

    The window never reaches back past the start of the run: a 3-minute run
    must not be able to claim a 5-minute sustained rate it has not earned.
    While the run is shorter than the window the mean is taken over what
    exists, never padded with zeros.

    One consequence of minute-resolution readings is worth stating: within
    the first minute of a run the baseline row *is* the latest row (the
    same minute was upserted before and after flow began), so the rate
    reads 0.0, and for the next few minutes it is measured from the run's
    first reading rather than the one before it. Immaterial -- every rate
    gate also requires at least 5 minutes of run, and from 5 minutes on
    the window sits wholly inside the run and this reproduces the
    validated trailing mean exactly.
    """
    latest = _reading_at_or_before(conn, now.strftime(_SEC_FMT))
    if latest is None:
        return 0.0

    cutoff = (now - timedelta(minutes=TRAIL_WINDOW_MINUTES)).strftime(_SEC_FMT)
    if run_started_at is not None:
        pre_run = conn.execute(
            "SELECT time FROM water_readings WHERE granularity = 'api_live' AND time < ? "
            "AND water_usage_dl IS NOT NULL ORDER BY time DESC LIMIT 1",
            (run_started_at,),
        ).fetchone()
        if pre_run is not None and pre_run["time"] > cutoff:
            cutoff = pre_run["time"]

    base = _reading_at_or_before(conn, cutoff)
    if base is None:
        return 0.0
    minutes = (_parse_reading_time(latest["time"]) - _parse_reading_time(base["time"])).total_seconds() / 60.0
    if minutes <= 0:
        return 0.0
    litres = max(latest["water_usage_dl"] - base["water_usage_dl"], 0.0) / 10.0
    return litres / minutes


def _evaluate_gates(
    conn: sqlite3.Connection,
    now: datetime,
    run_started_at: str,
    run_volume: float,
    rate: float,
    away: bool,
) -> tuple[str, float] | None:
    """Which gate, if any, the current run has satisfied -- as
    (fired_via, that gate's own operative value). Gates are OR'd and
    evaluated most-specific first (G3, then G2, then G1) so an episode is
    attributed to the gate that best describes it rather than to whichever
    one happens to be checked first."""
    thresholds = get_thresholds(conn, "water")

    if away:
        # Away mode is carried forward from the single-threshold design
        # unchanged, and deliberately NOT endorsed: the entire 1-minute
        # history sits inside a declared-occupied window, so the rate gates
        # have never been exercised against an empty house. A much lower
        # volume gate on its own is the conservative thing to keep here
        # until there is data to tune against.
        if run_volume >= thresholds["away_threshold_l"]:
            return "G1", thresholds["away_threshold_l"]
        return None

    gates = get_gate_config(conn, "water")
    elapsed_min = (now - _parse_dt(run_started_at)).total_seconds() / 60.0

    # G3 -- extreme rate. Burst pipe or failed valve, caught on rate before
    # volume has had time to pile up.
    if elapsed_min >= gates["g3_min_minutes"] and rate >= gates["g3_rate_l_min"]:
        return "G3", gates["g3_rate_l_min"]
    # G2 -- sustained. A fixture left running: a moderate rate that should
    # have stopped by now.
    if elapsed_min >= gates["g2_min_minutes"] and rate >= gates["g2_rate_l_min"]:
        return "G2", gates["g2_rate_l_min"]
    # G1 -- volume. The backstop, for anything relentless enough to evade
    # both rate gates. No duration qualifier, on purpose: see _DEFAULT_GATES.
    if run_volume >= thresholds["home_threshold_l"]:
        return "G1", thresholds["home_threshold_l"]
    return None


def check_water(conn: sqlite3.Connection, now: datetime | None = None) -> dict | None:
    """Tier 1, run every poll. Returns the active alert_episodes row (as a
    dict) if the current run has satisfied a gate, else None. Callers
    (meter_poller) should pass the return value straight to maybe_notify()
    when it is not None."""
    now = now or _now()
    now_str = now.strftime(_SEC_FMT)

    latest = conn.execute(
        "SELECT water_usage_dl FROM water_readings WHERE granularity = 'api_live' ORDER BY time DESC LIMIT 1"
    ).fetchone()
    if latest is None or latest["water_usage_dl"] is None:
        return None
    current_dl = latest["water_usage_dl"]

    state = conn.execute("SELECT * FROM detector_state WHERE metric = 'water'").fetchone()

    if state is None or state["last_meter_value"] is None:
        # First ever sample for this install -- nothing to compare against yet.
        conn.execute(
            "INSERT INTO detector_state (metric, run_started_at, run_last_at, run_volume, last_meter_value) "
            "VALUES ('water', NULL, NULL, 0, ?) "
            "ON CONFLICT(metric) DO UPDATE SET last_meter_value = excluded.last_meter_value",
            (current_dl,),
        )
        conn.commit()
        return None

    run_started_at = state["run_started_at"]
    run_last_at = state["run_last_at"]
    run_volume = state["run_volume"]

    # Restart after a stale gap: a run that hasn't seen a fresh sample in a
    # while is dead, not resumable -- resuming it would let a run from
    # before an outage silently keep counting toward a leak that may no
    # longer be happening.
    if run_started_at is not None and run_last_at is not None:
        if (now - _parse_dt(run_last_at)).total_seconds() > STALE_GAP_SECONDS:
            _close_episode(conn, run_started_at, now_str)
            run_started_at, run_last_at, run_volume = None, None, 0.0

    delta_l = (current_dl - state["last_meter_value"]) / 10.0

    episode: dict | None = None

    if delta_l <= 0:
        # Idle poll, or a meter reset (delta < 0) -- treated the same way:
        # never add negative volume to a run. Any open run just ended.
        if run_started_at is not None:
            _close_episode(conn, run_started_at, now_str)
        run_started_at, run_last_at, run_volume = None, None, 0.0
    else:
        if run_started_at is None:
            run_started_at = now_str
            run_volume = delta_l
        else:
            run_volume += delta_l
        run_last_at = now_str

        away = is_away(conn, "water", now)
        rate = trailing_rate(conn, now, run_started_at)
        fired = _evaluate_gates(conn, now, run_started_at, run_volume, rate, away)

        if fired is not None:
            fired_via, operative = fired
            existing = conn.execute(
                "SELECT peak_rate FROM alert_episodes WHERE kind = 'runaway' AND metric = 'water' AND started_at = ?",
                (run_started_at,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO alert_episodes "
                    "(kind, metric, started_at, last_seen_at, peak_rate, total_volume, unit, "
                    " threshold_used, occupancy_state, fired_via, alert_count, notify_status) "
                    "VALUES ('runaway', 'water', ?, ?, ?, ?, 'L', ?, ?, ?, 0, NULL)",
                    (run_started_at, now_str, rate, run_volume, operative,
                     "away" if away else "home", fired_via),
                )
            else:
                # fired_via is not rewritten on later polls: it records
                # which gate *opened* the episode, which is the question
                # record_dismissal() and the dashboard both actually ask.
                # peak_rate carries the highest trailing rate seen rather
                # than a single poll's delta -- a 20-second sample is
                # mostly meter quantisation noise, and the trailing mean is
                # both the honest "peak sustained rate" for display and the
                # value a dismissal of G2/G3 has to nudge the gate past.
                peak_rate = max(existing["peak_rate"] or 0.0, rate)
                conn.execute(
                    "UPDATE alert_episodes SET last_seen_at = ?, peak_rate = ?, total_volume = ? "
                    "WHERE kind = 'runaway' AND metric = 'water' AND started_at = ? AND ended_at IS NULL",
                    (now_str, peak_rate, run_volume, run_started_at),
                )
            row = conn.execute(
                "SELECT * FROM alert_episodes WHERE kind = 'runaway' AND metric = 'water' AND started_at = ?",
                (run_started_at,),
            ).fetchone()
            episode = dict(row) if row is not None else None

    conn.execute(
        "UPDATE detector_state SET run_started_at = ?, run_last_at = ?, run_volume = ?, last_meter_value = ? "
        "WHERE metric = 'water'",
        (run_started_at, run_last_at, run_volume, current_dl),
    )
    conn.commit()
    return episode


# ---------------------------------------------------------------------------
# Tier 2 -- slow / background leak
# ---------------------------------------------------------------------------

def _bucket_start(t: datetime) -> datetime:
    return t.replace(minute=(t.minute // T2_BUCKET_MINUTES) * T2_BUCKET_MINUTES, second=0, microsecond=0)


def flow_buckets(
    conn: sqlite3.Connection, window_start: datetime, now: datetime
) -> list[tuple[datetime, float | None]]:
    """The window as consecutive 15-minute buckets, each carrying the litres
    the meter advanced within it.

    A bucket's litres is None when it cannot be measured -- no readings in
    it, or none in the bucket immediately before it. That is not the same
    as idle, and it must not be: bridging an outage would let a service
    restart manufacture a leak out of the single reading that straddles it.
    A None breaks a streak outright, where an idle bucket only spends the
    zero-tolerance budget.
    """
    first = _bucket_start(window_start)
    last = _bucket_start(now)
    rows = conn.execute(
        "SELECT time, water_usage_dl FROM water_readings "
        "WHERE granularity = 'api_live' AND time >= ? AND time <= ? AND water_usage_dl IS NOT NULL "
        "ORDER BY time",
        ((first - timedelta(minutes=T2_BUCKET_MINUTES)).strftime(_SEC_FMT), now.strftime(_SEC_FMT)),
    ).fetchall()

    ends: dict[datetime, float] = {}
    for row in rows:
        # Rows arrive in time order, so the last write per bucket wins --
        # which is the bucket's closing cumulative reading.
        ends[_bucket_start(_parse_reading_time(row["time"]))] = row["water_usage_dl"]

    out: list[tuple[datetime, float | None]] = []
    step = timedelta(minutes=T2_BUCKET_MINUTES)
    bucket = first
    while bucket <= last:
        prev, this = ends.get(bucket - step), ends.get(bucket)
        out.append((bucket, None if prev is None or this is None else max(this - prev, 0.0) / 10.0))
        bucket += step
    return out


def current_streak(
    buckets: list[tuple[datetime, float | None]], zero_tolerance: int
) -> list[tuple[datetime, float]]:
    """The stretch of flowing buckets ending at the most recent one,
    bridging at most `zero_tolerance` idle buckets IN TOTAL.

    Bridging is what decides how small a leak Tier 2 can see. A bucket
    registers only once the meter advances a full 0.1 L, so a leak near
    ~0.01 L/min sits on that boundary and will drop the odd bucket through
    phase drift alone. Demanding an unbroken chain would let precisely the
    smallest leaks reset the counter forever. At the chosen window lengths
    bridging costs nothing -- both settings are designed to raise no false
    alerts on ordinary domestic use.

    **The budget is for the whole streak, not per gap.** `zeros` is
    deliberately never reset on a flowing bucket. Resetting it would bound
    only *consecutive* idle buckets, which is a different and much weaker
    rule: flow/idle/flow/idle sustains forever at a 50% duty cycle, so an
    appliance drawing water once every ~25 minutes overnight reads as
    "near-continuous flow". That is not the rule the thresholds were
    designed around -- the per-gap reading can fire T2-away on an
    ordinary domestic stretch that the whole-streak rule does not.
    """
    start = end = None
    zeros = 0
    for i in range(len(buckets) - 1, -1, -1):
        litres = buckets[i][1]
        if litres is None:
            break
        if litres > 0:
            if end is None:
                end = i
            start = i
            continue
        zeros += 1
        if zeros > zero_tolerance:
            break
    if start is None or end is None:
        return []
    return [(t, v) for t, v in buckets[start:end + 1] if v is not None]


def away_since(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    """When the house most recently became empty, as far as the occupancy
    log knows: the end of the latest declared-occupied entry that has
    already passed. None if nothing is logged.

    T2-away's lookback has to be clipped to this. `is_away()` is otherwise
    date-based and flips at midnight, when the preceding hours were already
    inside a near-zero day -- but a declared-occupied entry ending mid-day
    flips it on the spot, and an unclipped `now - (t2_away_hours + 1)`
    window then reaches straight back into an afternoon when someone was
    demonstrably home, counting their water use toward an "empty house"
    streak. An unclipped window can
    produce false T2-away firings on ordinary stretches; clipping prevents this,
    which is what the thresholds assume.
    """
    row = conn.execute(
        "SELECT MAX(date_to) AS ended FROM occupancy_log "
        "WHERE occupant_count > 0 AND date_to <= ?",
        (now.strftime(_MIN_FMT),),
    ).fetchone()
    if row is None or row["ended"] is None:
        return None
    try:
        return _parse_reading_time(row["ended"])
    except ValueError:
        # A row that predates _migrate_occupancy_datetime and somehow
        # escaped it. Refusing to clip is the safe direction here only in
        # the sense that it cannot hide a leak -- but it can invent one, so
        # treat an unparseable bound as "no information" rather than
        # guessing a date.
        return None


def _close_background_episode(conn: sqlite3.Connection, via: str, now: datetime) -> None:
    conn.execute(
        "UPDATE alert_episodes SET ended_at = ? WHERE kind = 'runaway' AND metric = 'water' "
        "AND fired_via = ? AND ended_at IS NULL",
        (now.strftime(_SEC_FMT), via),
    )


def _check_background_window(
    conn: sqlite3.Connection,
    now: datetime,
    via: str,
    window_start: datetime,
    min_hours: float,
    zero_tolerance: int,
) -> dict | None:
    streak = current_streak(flow_buckets(conn, window_start, now), zero_tolerance)
    hours = len(streak) * T2_BUCKET_MINUTES / 60.0
    if hours < min_hours:
        _close_background_episode(conn, via, now)
        conn.commit()
        return None

    now_str = now.strftime(_SEC_FMT)
    volume = sum(v for _, v in streak)
    peak = max((v for _, v in streak), default=0.0) / T2_BUCKET_MINUTES

    # An already-open episode keeps its own started_at rather than taking
    # the recomputed streak start. The lookback window is bounded, so a
    # leak running longer than the window would otherwise appear to "start"
    # one bucket later on every poll and open a fresh episode each time.
    # total_volume is therefore the volume of the streak as observed within
    # the lookback, never revised downward -- for T2-night that is the
    # whole night, for T2-away it is the last t2_away_hours + 1.
    open_ep = conn.execute(
        "SELECT id FROM alert_episodes WHERE kind = 'runaway' AND metric = 'water' "
        "AND fired_via = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (via,),
    ).fetchone()
    if open_ep is not None:
        conn.execute(
            "UPDATE alert_episodes SET last_seen_at = ?, peak_rate = MAX(COALESCE(peak_rate, 0), ?), "
            "total_volume = MAX(COALESCE(total_volume, 0), ?) WHERE id = ?",
            (now_str, peak, volume, open_ep["id"]),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (open_ep["id"],)).fetchone())

    started_at = streak[0][0].strftime(_SEC_FMT)

    # This detector may already own a *closed* episode with exactly this
    # start. The streak oscillates at its own threshold: the current bucket
    # has not yet ticked 0.1 L, so the streak is one bucket short of
    # min_hours and the episode above is closed -- then the bucket ticks and
    # the same streak start comes back. Re-inserting would collide with
    # UNIQUE (kind, metric, started_at) and return None, stranding a live
    # leak behind a closed row that notify() refuses to escalate: exactly
    # the away case where nobody is home to see the banner. Resume the row
    # instead. It is the same leak, so it keeps its own alert_count (T2-night
    # stays at one message; T2-away keeps escalating where it left off) and
    # any acknowledgement still suppresses it.
    prior = conn.execute(
        "SELECT id FROM alert_episodes WHERE kind = 'runaway' AND metric = 'water' "
        "AND started_at = ? AND fired_via = ?",
        (started_at, via),
    ).fetchone()
    if prior is not None:
        conn.execute(
            "UPDATE alert_episodes SET ended_at = NULL, last_seen_at = ?, "
            "peak_rate = MAX(COALESCE(peak_rate, 0), ?), "
            "total_volume = MAX(COALESCE(total_volume, 0), ?) WHERE id = ?",
            (now_str, peak, volume, prior["id"]),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (prior["id"],)).fetchone())

    try:
        conn.execute(
            "INSERT INTO alert_episodes "
            "(kind, metric, started_at, last_seen_at, peak_rate, total_volume, unit, "
            " threshold_used, occupancy_state, fired_via, alert_count, notify_status) "
            "VALUES ('runaway', 'water', ?, ?, ?, ?, 'L', ?, ?, ?, 0, NULL)",
            (started_at, now_str, peak, volume, min_hours,
             "away" if via == "T2-away" else "home", via),
        )
    except sqlite3.IntegrityError:
        # UNIQUE (kind, metric, started_at) collided with an episode this
        # detector does not own -- the `prior` lookup above already claimed
        # any row with this start that is ours, so what is left is a Tier 1
        # episode that happens to have begun on this exact bucket boundary.
        # Tier 1 is the louder alert and already has the household covered,
        # so skip rather than overwrite a row belonging to another detector.
        conn.commit()
        return None
    conn.commit()
    row = conn.execute(
        "SELECT * FROM alert_episodes WHERE kind = 'runaway' AND metric = 'water' "
        "AND started_at = ? AND fired_via = ?",
        (started_at, via),
    ).fetchone()
    return dict(row) if row is not None else None


def check_water_background(conn: sqlite3.Connection, now: datetime | None = None) -> dict | None:
    """Tier 2, run every poll alongside check_water(). Shares no state with
    the run accumulator -- it reads 15-minute buckets straight out of
    water_readings.

    Two windows, both designed to raise no false alerts on ordinary
    domestic use:

    - **T2-away**: >= t2_away_hours of flowing buckets while the house is
      away, at any hour. Takes priority over the overnight window, being
      the tighter of the two -- two simultaneous episodes for one leak
      would be noise, not information.
    - **T2-night**: >= t2_night_hours of flowing buckets inside the
      overnight window, evaluated only while `now` is inside that window.
      It can therefore fire from t2_night_hours after the window opens; a
      leak continuous enough to matter fills the whole window and trips on
      its first night.
    """
    now = now or _now()
    gates = get_gate_config(conn, "water")
    tolerance = int(gates["t2_zero_tolerance"])

    if is_away(conn, "water", now):
        _close_background_episode(conn, "T2-night", now)
        away_hours = float(gates["t2_away_hours"])
        window_start = now - timedelta(hours=away_hours + 1)
        # Never look back past the moment the house became empty -- see
        # away_since(). Water drawn while someone was home is not evidence
        # of a leak in an empty house.
        since = away_since(conn, now)
        if since is not None and since > window_start:
            window_start = since
        return _check_background_window(
            conn, now, "T2-away", window_start, away_hours, tolerance
        )

    _close_background_episode(conn, "T2-away", now)

    start_h, end_h = int(gates["t2_night_start"]), int(gates["t2_night_end"])
    if start_h < end_h and start_h <= now.hour < end_h:
        return _check_background_window(
            conn,
            now,
            "T2-night",
            now.replace(hour=start_h, minute=0, second=0, microsecond=0),
            float(gates["t2_night_hours"]),
            tolerance,
        )

    _close_background_episode(conn, "T2-night", now)
    conn.commit()
    return None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _episode_text(row: sqlite3.Row, now: datetime) -> str:
    started = _parse_dt(row["started_at"])
    # Absolute start timestamp, not just "N min": a notification read hours
    # or days later must say *when* the run began without the reader having
    # to back-calculate from the message's own delivery time.
    start_str = started.strftime("%d %b %Y, %H:%M")
    count = f"(alert #{row['alert_count'] + 1})"
    volume = row["total_volume"] or 0.0

    if row["fired_via"] in _TIER2_GATES:
        hours = (now - started).total_seconds() / 3600.0
        where = "while the house is empty" if row["fired_via"] == "T2-away" else "overnight"
        return (
            f"💧 Possible background leak — water has flowed near-continuously {where} "
            f"since {start_str} ({hours:.1f} h), {volume:.0f} L. Nothing in the house "
            f"should run this long. {count}"
        )

    duration_min = round((now - started).total_seconds() / 60.0)
    return (
        f"⚠️ Possible water leak — continuous flow since {start_str} "
        f"({duration_min} min), {volume:.0f} L {count}"
    )


def maybe_notify(conn: sqlite3.Connection, episode_id: int, now: datetime | None = None) -> None:
    """Escalating-backoff Telegram notification for an active,
    unacknowledged episode -- see alert_delivery.notify() for the shared
    due/backoff/outcome-recording logic. A failed send is retried next
    cycle, never lost.

    T2-night is the one exception to the escalation: it sends once and then
    stays silent, because an overnight background leak is something to look
    at in the morning, not a reason to nag the house hourly through the
    night. Its banner is still persistent, so it cannot be missed -- only
    un-nagged. T2-away keeps the full escalation: nobody is home to see a
    banner.
    """
    now = now or _now()
    row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return
    if row["fired_via"] == "T2-night" and row["alert_count"] >= 1:
        return
    alert_delivery.notify(conn, episode_id, _episode_text(row, now), now)


def _dismissal_target(row: sqlite3.Row) -> tuple[str, float] | None:
    """Which config parameter a "not a leak" dismissal should nudge, and
    past what value.

    Under multi-gate firing, nudging the volume threshold for every
    dismissal would be actively harmful: dismissing a G3 rate alert would
    raise G1's volume gate, silently degrading the backstop while leaving
    the gate that actually misfired exactly where it was. The parameter
    that gets tuned has to be the one named by fired_via.
    """
    via = row["fired_via"]
    if via is None or via == "G1":
        # A NULL fired_via is a pre-1.14 episode, which by definition was
        # fired by the volume threshold.
        column = "away_threshold_l" if row["occupancy_state"] == "away" else "home_threshold_l"
        return column, (row["total_volume"] or 0.0) + _DISMISSAL_MARGIN_L
    if via in ("G2", "G3"):
        # peak_rate is the highest trailing-mean rate the episode reached,
        # which is the value the rate gate actually tripped on.
        column = "g2_rate_l_min" if via == "G2" else "g3_rate_l_min"
        return column, (row["peak_rate"] or 0.0) + _DISMISSAL_MARGIN_RATE
    if via in _TIER2_GATES:
        column = "t2_night_hours" if via == "T2-night" else "t2_away_hours"
        hours = (_parse_dt(row["last_seen_at"]) - _parse_dt(row["started_at"])).total_seconds() / 3600.0
        return column, hours + _DISMISSAL_MARGIN_HOURS
    return None


def record_dismissal(
    conn: sqlite3.Connection, episode_id: int, not_a_leak: bool = False, now: datetime | None = None
) -> None:
    """Acknowledges an episode -- stops further notification (maybe_notify
    skips anything with acknowledged_at set) and clears the banner. If
    dismissed specifically as "not a leak", nudges the parameter that fired
    it just past the value that fired it: self-tuning per gate, without
    guessing a percentage multiplier. MAX() means a dismissal can only ever
    loosen a gate, never tighten one behind the household's back."""
    now = now or _now()
    row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE alert_episodes SET acknowledged_at = ? WHERE id = ?",
        (now.strftime(_SEC_FMT), episode_id),
    )
    if not_a_leak:
        target = _dismissal_target(row)
        if target is not None:
            column, value = target
            conn.execute(
                f"UPDATE runaway_detector_config SET {column} = MAX({column}, ?) WHERE metric = ?",
                (value, row["metric"]),
            )
    conn.commit()
