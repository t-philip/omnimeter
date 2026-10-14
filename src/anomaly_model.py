"""Layer 2 -- context-aware expected-value model: a
per-metric ridge regression of each metric's DAILY total against its own
calendar/weather/occupancy context, flagging a day whose actual usage sits
far above what that context would predict -- "3x baseline" becomes "3x more
than expected given it was cold and the house was full".

Four build decisions and how they were settled:
  - Model type: ridge regression, not gradient-boosted trees -- simpler,
    trivial on a small host at this data volume (a few thousand rows per
    metric, <10 features), lower overfit risk given patchy occupancy_log
    coverage.
  - Feature encoding: follows from that choice (see _COMMON_FEATURES/
    _METRIC_EXTRA_FEATURES below), not an independent decision.
  - Training cadence: full nightly retrain (scripts/anomaly.py), no
    incremental update -- refitting this few rows costs milliseconds
    regardless, so incremental update would only add state to manage.
  - Residual threshold: RESIDUAL_PERCENTILE below.

Deliberate v1 scoping (flagged, not silently done): this predicts DAILY
totals, not an hourly figure -- "hour-of-day" is a candidate feature, but
Layer 2's own job ("a boiler degrading over a month", "a standby baseline
that crept up", "a quarter trending above weather-adjusted normal") is a
slow, day-or-longer signal, matches the digest's own daily cadence, and
extends the existing daily find_outlier_days() detector -- an hourly model
would need an hourly target and a different notification design. Tariff
period is dropped for the same reason it would be redundant: a T1/T2
day/night tariff structure is already explained as well by the day-of-week/
season terms as by a separate categorical.

No numpy/scikit-learn dependency -- requirements.txt carries none today, and
adding one for a handful of small linear-algebra ops would be a heavier
addition than the feature justifies on a small host. The matrices here
are at most ~10 columns, so a pure-Python normal-equations solve is
microseconds slower than BLAS and immeasurable next to the network/DB work
the rest of this app already does per request.

Runs on its own nightly timer (scripts/anomaly.py -- omnimeter-anomaly.timer),
independent of meter_poller (Layer 1) and health_check (Layer 3): unlike
those two, Layer 2 is explicitly a digest, not an interrupt, so it has
no reason to run more often than once a day, after that day's weather
(omnimeter-weather.timer, 05:00 UTC) and rollups have settled.
"""

import json
import math
import sqlite3
from datetime import datetime, timedelta

from . import aggregate, localtime, telegram_notifier, weather

_LOCAL_TZ = localtime.LOCAL_TZ

# Below this many usable training rows, the cyclic day-of-year term has no
# real seasonal variety to fit against and a model trained on it would be
# overfitting calendar noise, not learning a pattern -- it must
# degrade gracefully. Below this floor, train_model() stores nothing and
# run_daily_check() simply skips that metric; the existing, wholly separate
# aggregate.find_outlier_days() ratio detector is completely unaffected and
# keeps working exactly as it does today.
MIN_HISTORY_DAYS = 90

# Regularization strength applied to standardized features. One documented
# default, same reasoning as weather.DEFAULT_HDD_BASE_C: nobody has grounds
# to tune this without their own residual-quality data to calibrate against.
RIDGE_ALPHA = 1.0

# Percentile of a metric's own historical (training) residual distribution
# beyond which a day is flagged: 99th-99.5th, chosen to match Layer 2's
# digest role (a handful
# of flagged days a year per metric, not the ~18/year the 95th percentile
# would give, nor the ~once-every-3-years the 99.9th would). 99.0 is the
# concrete default. Per-household DB-editability (matching
# runaway_detector_config's precedent) is a natural follow-up once
# digest output shows the default needs raising or lowering -- not built
# preemptively here, the same restraint Layer 1 applied to its own
# threshold before data existed to justify one.
RESIDUAL_PERCENTILE = 99.0

# (daily table, value column, display unit) per metric.
_METRIC_TABLES = {
    "water": ("water_daily", "usage_l", "L"),
    "gas": ("gas_daily", "usage_m3", "m3"),
    "power": ("power_daily", "import_kwh", "kWh"),
}

# Feature order is fixed per metric and stored alongside the model
# (anomaly_model_state.feature_names) so prediction never has to guess it
# back from a dict -- the stored order is authoritative, this tuple is only
# ever consulted at training time.
_COMMON_FEATURES = ("dow_sin", "dow_cos", "doy_sin", "doy_cos", "occupant_count")
_METRIC_EXTRA_FEATURES = {
    # Raw mean temperature, not heating-degree-days: HDD floors at 0 for any
    # day at or above 18C, which would hide exactly the warm-weather signal
    # that matters for water (hot dry spells correlate with garden
    # watering, the main legitimate driver of large daytime water draws).
    "water": ("mean_temp_c",),
    # Heating-degree-days drive gas usage.
    "gas": ("hdd",),
    # Irradiance is load-bearing for power: it is what makes "import
    # collapsed to zero" read as a sunny midday rather than an anomaly. A
    # charging session is a known state, not a fault to explain
    # away -- reuses aggregate.find_ev_charge_days rather than reinventing
    # session detection.
    "power": ("hdd", "solar_radiation", "ev_charging_day"),
}


def _cyclic(value: float, period: float) -> tuple[float, float]:
    angle = 2.0 * math.pi * value / period
    return math.sin(angle), math.cos(angle)


def _context_for_date(
    metric: str,
    date_str: str,
    weather_by_date: dict[str, sqlite3.Row],
    occupancy_by_date: dict[str, int],
    away_days: set[str],
    ev_days: set[str],
) -> dict[str, float] | None:
    """Feature vector for one date, or None if a metric-required weather
    field is missing for it (weather is opt-in -- a metric that needs
    it just can't be scored/trained on that date)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    dow_sin, dow_cos = _cyclic(d.weekday(), 7.0)
    doy_sin, doy_cos = _cyclic(d.timetuple().tm_yday, 365.25)

    if date_str in occupancy_by_date:
        occupant_count = float(occupancy_by_date[date_str])
    elif date_str in away_days:
        # Training-only backfill: an unambiguous sustained-zero
        # absence is labelled away even with no occupancy_log entry, so
        # historical away periods don't poison the model as "just a quiet
        # week". This path is also
        # reached by run_daily_check() for a not-yet-logged away day, which
        # is correct there too: it is reading a settled, already-complete
        # day from *_daily, never inferring "away" from an in-progress
        # reading, which the live runaway detector must never do.
        occupant_count = 0.0
    else:
        # No log entry and not an unambiguous absence -- assume normally
        # occupied. Same "home unless proven away" default as
        # runaway_detector.is_away().
        occupant_count = 1.0

    features: dict[str, float] = {
        "dow_sin": dow_sin,
        "dow_cos": dow_cos,
        "doy_sin": doy_sin,
        "doy_cos": doy_cos,
        "occupant_count": occupant_count,
    }

    w = weather_by_date.get(date_str)

    if metric in ("gas", "power"):
        if w is None or w["temperature_2m_mean"] is None:
            return None
        hdd = weather.heating_degree_days(w["temperature_2m_mean"])
        if hdd is None:
            return None
        features["hdd"] = hdd

    if metric == "water":
        if w is None or w["temperature_2m_mean"] is None:
            return None
        features["mean_temp_c"] = w["temperature_2m_mean"]

    if metric == "power":
        if w is None or w["shortwave_radiation_sum"] is None:
            return None
        features["solar_radiation"] = w["shortwave_radiation_sum"]
        features["ev_charging_day"] = 1.0 if date_str in ev_days else 0.0

    return features


def _weather_by_date(conn: sqlite3.Connection, date_from: str, date_to: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT date, temperature_2m_mean, shortwave_radiation_sum FROM weather_daily "
        "WHERE date >= ? AND date <= ?",
        (date_from, date_to),
    ).fetchall()
    return {r["date"]: r for r in rows}


def _training_data(
    conn: sqlite3.Connection, metric: str, cutoff_date: str
) -> tuple[list[dict[str, float]], list[float]]:
    """Rows strictly before cutoff_date. train_model() passes yesterday's own
    date as the cutoff -- not just today's -- so the day about to be scored
    (run_daily_check always scores yesterday) is never part of its own
    baseline. Training a night's model on the very data point it's about to
    judge would let one huge anomalous day drag its own expected value and
    residual-percentile threshold up to absorb itself, exactly the kind of
    circularity this project avoids elsewhere.
    The one-day lag this introduces costs nothing: the excluded day still
    joins the training set on every subsequent night once a newer day
    becomes the one being scored."""
    table, value_col, _unit = _METRIC_TABLES[metric]
    rows = conn.execute(
        f"SELECT date, {value_col} AS v FROM {table} WHERE {value_col} IS NOT NULL AND date < ? ORDER BY date",
        (cutoff_date,),
    ).fetchall()
    if not rows:
        return [], []

    weather_by_date = _weather_by_date(conn, rows[0]["date"], rows[-1]["date"])
    occ_rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log")]
    occupancy_by_date = (
        aggregate.expand_occupancy_by_day(occ_rows, rows[0]["date"], rows[-1]["date"]) if occ_rows else {}
    )
    away_days = aggregate.find_away_days(conn)
    ev_days = aggregate.find_ev_charge_days(conn) if metric == "power" else set()

    feature_dicts: list[dict[str, float]] = []
    targets: list[float] = []
    for r in rows:
        ctx = _context_for_date(metric, r["date"], weather_by_date, occupancy_by_date, away_days, ev_days)
        if ctx is None:
            continue
        feature_dicts.append(ctx)
        targets.append(r["v"])
    return feature_dicts, targets


def _standardize(matrix: list[list[float]]) -> tuple[list[list[float]], list[float], list[float]]:
    n = len(matrix)
    p = len(matrix[0])
    means = [sum(row[j] for row in matrix) / n for j in range(p)]
    stds = []
    for j in range(p):
        variance = sum((row[j] - means[j]) ** 2 for row in matrix) / n
        # A feature with zero variance in the training window (e.g. no EV
        # charge day seen yet) would divide by zero -- treated as already
        # standardized (contributes a constant 0 after centering, and ridge
        # shrinks its coefficient toward 0 regardless since it explains none
        # of the target's variance).
        stds.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    standardized = [[(row[j] - means[j]) / stds[j] for j in range(p)] for row in matrix]
    return standardized, means, stds


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan elimination with partial pivoting for a small (p x p,
    p <= ~10 here) dense linear system. Pure Python is fine at this size --
    see this module's docstring for why no numpy dependency was added."""
    n = len(b)
    aug = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        if abs(pivot) < 1e-12:
            raise ValueError("singular matrix in ridge fit -- check for duplicated/degenerate features")
        aug[col] = [x / pivot for x in aug[col]]
        for r in range(n):
            if r != col:
                factor = aug[r][col]
                aug[r] = [x - factor * y for x, y in zip(aug[r], aug[col], strict=True)]
    return [row[-1] for row in aug]


def _ridge_fit(
    feature_matrix: list[list[float]], targets: list[float], alpha: float
) -> tuple[float, list[float], list[float], list[float]]:
    """Returns (intercept, coefficients, feature_means, feature_stds).
    Fits on standardized features (mean 0, unit variance) so one alpha
    regularizes every feature comparably regardless of its raw units (degC
    vs a 0/1 flag vs an MJ/m2 sum) -- fitting on raw units would over-
    penalize whichever feature happens to have the largest scale. The
    intercept is never regularized (standard ridge practice) and, because
    standardized features have mean 0, is exactly the target's own mean."""
    n = len(targets)
    p = len(feature_matrix[0])
    x_std, means, stds = _standardize(feature_matrix)
    y_mean = sum(targets) / n
    y_centered = [t - y_mean for t in targets]

    xtx = [[sum(x_std[k][i] * x_std[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
    for i in range(p):
        xtx[i][i] += alpha
    xty = [sum(x_std[k][i] * y_centered[k] for k in range(n)) for i in range(p)]

    coefs = _solve(xtx, xty)
    return y_mean, coefs, means, stds


def predict(intercept: float, coefs: list[float], means: list[float], stds: list[float], feature_values: list[float]) -> float:
    standardized = [(v - m) / s for v, m, s in zip(feature_values, means, stds, strict=True)]
    return intercept + sum(c * x for c, x in zip(coefs, standardized, strict=True))


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of a non-empty list (0 <= pct <= 100)."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def train_model(conn: sqlite3.Connection, metric: str, now: datetime | None = None) -> dict | None:
    """Fits (or refits) metric's Layer 2 model from *_daily history and
    stores it in anomaly_model_state, replacing any prior fit -- a full
    nightly retrain, not an incremental update, since refitting this few
    rows costs milliseconds regardless.
    Returns None (and clears any stored model) if there isn't yet enough
    settled history -- see MIN_HISTORY_DAYS and the graceful-degradation
    requirement."""
    now = now or datetime.now(_LOCAL_TZ)
    # Excludes yesterday too, not just today -- see _training_data's
    # docstring: yesterday is the day run_daily_check is about to score, and
    # must not be part of its own baseline.
    cutoff_date = (now.date() - timedelta(days=1)).isoformat()
    feature_names = _COMMON_FEATURES + _METRIC_EXTRA_FEATURES[metric]
    feature_dicts, targets = _training_data(conn, metric, cutoff_date)
    if len(targets) < MIN_HISTORY_DAYS:
        conn.execute("DELETE FROM anomaly_model_state WHERE metric = ?", (metric,))
        conn.commit()
        return None

    matrix = [[fd[name] for name in feature_names] for fd in feature_dicts]
    intercept, coefs, means, stds = _ridge_fit(matrix, targets, RIDGE_ALPHA)

    residuals = [t - predict(intercept, coefs, means, stds, row) for row, t in zip(matrix, targets, strict=True)]
    # Only the excess-usage direction matters here ("a boiler
    # degrading", "a standby baseline that crept up", "trending above
    # weather-adjusted normal" -- always excess, never deficit). A day using
    # LESS than expected is not what Layer 2 exists to catch; that direction
    # already belongs to aggregate.find_outlier_days (informational) and, for
    # water, to Layer 1's own away-mode collapse.
    positive_residuals = [r for r in residuals if r > 0] or [0.0]
    threshold = _percentile(positive_residuals, RESIDUAL_PERCENTILE)

    conn.execute(
        "INSERT INTO anomaly_model_state "
        "(metric, trained_at, training_rows, feature_names, feature_means, feature_stds, "
        " intercept, coefficients, residual_threshold) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(metric) DO UPDATE SET "
        "trained_at = excluded.trained_at, training_rows = excluded.training_rows, "
        "feature_names = excluded.feature_names, feature_means = excluded.feature_means, "
        "feature_stds = excluded.feature_stds, intercept = excluded.intercept, "
        "coefficients = excluded.coefficients, residual_threshold = excluded.residual_threshold",
        (
            metric,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            len(targets),
            json.dumps(feature_names),
            json.dumps(means),
            json.dumps(stds),
            intercept,
            json.dumps(coefs),
            threshold,
        ),
    )
    conn.commit()
    return {"metric": metric, "training_rows": len(targets), "residual_threshold": threshold}


def _record_context_anomaly(
    conn: sqlite3.Connection,
    metric: str,
    date_str: str,
    actual: float,
    expected: float,
    residual_threshold: float,
    unit: str,
    now: datetime,
) -> dict:
    """Writes a kind='context' alert_episodes row, auto-acknowledged at
    creation. Unlike Layer 1 ('runaway')/Layer 3 ('health'), Layer 2 is a
    digest, never the persistent OK-required banner -- setting
    acknowledged_at immediately keeps it out of /api/alerts/active (which
    filters WHERE acknowledged_at IS NULL) with zero frontend change, while
    still leaving a real, queryable row in /api/alerts/history for audit.

    Column reuse, documented here the same way health_monitor.py documents
    its own repurposing of total_volume for 'health' rows: for kind='context',
    total_volume is the day's ACTUAL value, peak_rate is the model's EXPECTED
    value (otherwise meaningless for a daily total), and threshold_used is
    the absolute cutoff (expected + residual_threshold) that would have
    triggered this flag -- so a past alert stays auditable/retunable, same
    reasoning as every other kind's threshold_used."""
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    started_at = f"{date_str} 00:00:00"
    conn.execute(
        "INSERT OR IGNORE INTO alert_episodes "
        "(kind, metric, started_at, last_seen_at, ended_at, peak_rate, total_volume, unit, "
        " threshold_used, alert_count, acknowledged_at) "
        "VALUES ('context', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (metric, started_at, now_str, now_str, expected, actual, unit, expected + residual_threshold, now_str),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM alert_episodes WHERE kind = 'context' AND metric = ? AND started_at = ?",
        (metric, started_at),
    ).fetchone()
    return dict(row)


def _send_digest(conn: sqlite3.Connection, flagged: list[dict]) -> None:
    lines = ["📊 OmniMeter anomaly digest:"]
    for ep in flagged:
        lines.append(
            f"• {ep['metric'].capitalize()}: {ep['total_volume']:.1f} {ep['unit']} "
            f"vs ~{ep['peak_rate']:.1f} {ep['unit']} expected"
        )
    text = "\n".join(lines)
    # Best-effort, same reasoning as Layer 1/3 -- must never raise
    # into the nightly timer. Unlike those layers there is no retry/backoff:
    # a digest describes a day that has already passed, so "try again in 15
    # minutes" buys nothing -- the row is already permanently recorded in
    # alert_episodes for later review; Telegram delivery here is a
    # convenience notification, not the record of truth.
    status, error = telegram_notifier.send_message(conn, text)
    for ep in flagged:
        conn.execute(
            "UPDATE alert_episodes SET notify_status = ?, notify_error = ? WHERE id = ?",
            (status, error, ep["id"]),
        )
    conn.commit()


def run_daily_check(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Layer 2 entry point (scripts/anomaly.py). For each metric: retrains
    its model (train_model), then scores YESTERDAY's now-settled day against
    it -- today's own total is still accumulating, the same exclusion
    aggregate.find_outlier_days() already applies to its own most-recent
    row. Returns the metrics flagged this run, each already written to
    alert_episodes (kind='context', auto-acknowledged) and, if any were
    flagged, already the subject of one best-effort Telegram digest."""
    now = now or datetime.now(_LOCAL_TZ)
    target_date = (now.date() - timedelta(days=1)).isoformat()
    flagged: list[dict] = []

    for metric, (table, value_col, unit) in _METRIC_TABLES.items():
        model = train_model(conn, metric, now)
        if model is None:
            continue
        model_row = conn.execute("SELECT * FROM anomaly_model_state WHERE metric = ?", (metric,)).fetchone()

        actual_row = conn.execute(f"SELECT {value_col} AS v FROM {table} WHERE date = ?", (target_date,)).fetchone()
        if actual_row is None or actual_row["v"] is None:
            continue

        weather_by_date = _weather_by_date(conn, target_date, target_date)
        occ_rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log")]
        occupancy_by_date = aggregate.expand_occupancy_by_day(occ_rows, target_date, target_date) if occ_rows else {}
        away_days = aggregate.find_away_days(conn)
        ev_days = aggregate.find_ev_charge_days(conn) if metric == "power" else set()
        ctx = _context_for_date(metric, target_date, weather_by_date, occupancy_by_date, away_days, ev_days)
        if ctx is None:
            continue

        feature_names = json.loads(model_row["feature_names"])
        means = json.loads(model_row["feature_means"])
        stds = json.loads(model_row["feature_stds"])
        coefs = json.loads(model_row["coefficients"])
        feature_values = [ctx[name] for name in feature_names]
        expected = predict(model_row["intercept"], coefs, means, stds, feature_values)
        actual = actual_row["v"]
        residual = actual - expected

        if residual > model_row["residual_threshold"]:
            episode = _record_context_anomaly(
                conn, metric, target_date, actual, expected, model_row["residual_threshold"], unit, now
            )
            flagged.append(episode)

    if flagged:
        _send_digest(conn, flagged)
    return flagged
