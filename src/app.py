"""Flask app factory and routes for OmniMeter."""

import hmac
import math
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from flask import Flask, Response, g, jsonify, render_template, request

from . import (
    aggregate,
    costs,
    db,
    ingest,
    localtime,
    quarters,
    runaway_detector,
    solar_production,
    solaredge_status,
    tariff_parser,
    telegram_notifier,
    update_check,
    weather,
)
from .__version__ import __version__
from .credentials import default_credential_source
from .ingest_cli import DEFAULT_IMPORTS_DIR
from .solar_estimate import (
    estimate_daily_production,
    estimate_daily_production_from_radiation,
    reconcile_with_export,
)

# templates/ and static/ live at the app root (/opt/omnimeter/), not
# under src/ — Flask's default resolves both relative to this module's package,
# so they must be pointed at explicitly.
APP_ROOT = Path(__file__).resolve().parent.parent

# power_readings.time is stored as a naive local string ('YYYY-MM-DD HH:MM',
# see db.py) -- attach the correct DST offset here so the browser's Date
# parser resolves it to the right instant regardless of the viewer's own
# timezone. Set OMNIMETER_TIMEZONE in .env; see src/localtime.py, which is
# the single place the zone is resolved for every module that needs it.
_LOCAL_TZ = localtime.LOCAL_TZ


def _parse_finite(value, name: str, lo: float, hi: float) -> float:
    """float() that rejects NaN/inf and out-of-range values with a message
    suitable for a 400 response. NaN especially must never reach the DB — a
    stored NaN silently poisons every downstream average/estimate."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(f):
        raise ValueError(f"{name} must be a finite number")
    if not (lo <= f <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return f


def _parse_iso_date(value, name: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a YYYY-MM-DD date") from None


def _parse_iso_datetime(value, name: str) -> str:
    """Accepts the 'YYYY-MM-DDTHH:MM' string a <input type="datetime-local">
    submits (datetime.fromisoformat handles the 'T' separator natively) and
    normalizes to 'YYYY-MM-DD HH:MM' -- the same naive-local-string
    convention as power_readings.time, so plain string comparison
    (_overlapping_period, sort order) stays chronologically correct."""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a YYYY-MM-DD HH:MM date/time") from None


def _parse_int_range(value, name: str, lo: int, hi: int) -> int:
    """int() that rejects non-whole numbers (e.g. a stray '2.5' from a number
    input) rather than silently truncating, plus out-of-range values."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not f.is_integer():
        raise ValueError(f"{name} must be an integer") from None
    i = int(f)
    if not (lo <= i <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return i


def _parse_fiscal_start(data: dict, category: str) -> tuple[int, int]:
    try:
        month = int(data[f"{category}_fy_start_month"])
        day = int(data[f"{category}_fy_start_day"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{category}_fy_start_month and {category}_fy_start_day must be integers") from None
    try:
        date(2001, month, day)  # non-leap reference year -- rejects Feb 29 as an anchor
    except ValueError:
        raise ValueError(f"{category}_fy_start_month/{category}_fy_start_day is not a valid date") from None
    return month, day


def _find_daily_gaps(conn, category: str, table: str, acknowledged: set[tuple[str, str, str]] | None = None) -> dict:
    """Missing calendar dates in `table` between its own earliest and latest
    date -- bounded to the category's real coverage window so a
    period before the device existed isn't misreported as a data-health
    problem, and the most recent day is never itself "missing" (that's
    staleness, already covered by /api/data-freshness). Consecutive missing
    dates are grouped into a single {start, end} range. Each gap carries a
    fingerprint + acknowledged flag, same tag-don't-filter pattern
    as aggregate.data_quality_report -- an acknowledged gap stays in the
    list, just tagged, so the frontend can collapse it rather than hide it
    without a trace."""
    acknowledged = acknowledged or set()
    bounds = conn.execute(f"SELECT MIN(date) AS f, MAX(date) AS l FROM {table}").fetchone()
    first, last = bounds["f"], bounds["l"]
    if first is None:
        return {"first_date": None, "last_date": None, "gaps": []}
    present = {
        row["date"] for row in conn.execute(f"SELECT date FROM {table} WHERE date >= ? AND date <= ?", (first, last))
    }
    gaps: list[dict] = []
    gap_start = None
    d = date.fromisoformat(first)
    end = date.fromisoformat(last)
    while d <= end:
        iso = d.isoformat()
        if iso in present:
            if gap_start is not None:
                gap_end = (d - timedelta(days=1)).isoformat()
                fingerprint = f"{gap_start}|{gap_end}"
                gaps.append(
                    {
                        "start": gap_start,
                        "end": gap_end,
                        "fingerprint": fingerprint,
                        "acknowledged": (category, "gap", fingerprint) in acknowledged,
                    }
                )
                gap_start = None
        elif gap_start is None:
            gap_start = iso
        d += timedelta(days=1)
    return {"first_date": first, "last_date": last, "gaps": gaps}


_TOGGLE_FIELDS = (
    "homewizard_api_enabled",
    "import_power_enabled",
    "import_gas_enabled",
    "import_water_enabled",
    "pdf_import_enabled",
    "nightly_backup_enabled",
    # Without this the column exists but is unreachable
    # from the UI, so a self-hosting user could only enable weather by
    # editing the database by hand.
    "weather_enabled",
    # Same reasoning -- see update_check.py's module docstring.
    "update_check_enabled",
    # OmniMeter's third outbound internet call (Telegram, see
    # telegram_notifier.py) -- same opt-in reasoning, default 0.
    "anomaly_alerts_enabled",
    # SolarEdge cloud poller (scripts/solaredge_poll.py) -- an outbound-internet
    # feature, so opt-in like the others; the column existed but, without this
    # entry, could only be switched on by editing the database by hand.
    "solaredge_enabled",
)

# UI-visibility switches -- deliberately a separate full-replace
# singleton from _TOGGLE_FIELDS above (own form, own endpoint) even though
# both live in the feature_toggles table. Sharing one form/endpoint would
# mean saving either one resets the other's fields to disabled, since a
# full-replace POST treats every field it doesn't receive as "off" (see
# TestFeatureToggles.test_round_trips_disabled_state).
_VISIBILITY_FIELDS = (
    "show_gas_tab",
    "show_water_tab",
    "show_battery_tab",
    "show_sufficiency_tab",
    "show_solar_tab",
)


def _overlapping_period(
    conn,
    table: str,
    start: str,
    end: str,
    start_col: str = "period_start",
    end_col: str = "period_end",
):
    """Existing row in table whose [start_col, end_col] intersects
    [start, end], or None. Exact same-range rows are the caller's business
    (the PDF path deliberately upserts those); this catches partial overlaps,
    which would make rate_for()'s first-match lookup order-dependent.

    Used by rate_schedule/gas_rate_schedule only: whole calendar-day periods
    with inclusive boundaries, so two periods sharing a boundary DATE (one's
    end_col equal to the next's start_col) are a real overlap -- that date
    can't belong to both. occupancy_log entries are deliberately allowed to
    overlap/nest (see aggregate.expand_occupancy_by_day's most-specific-
    entry-wins resolution) so this check no longer applies to that table."""
    return conn.execute(
        f"SELECT {start_col}, {end_col} FROM {table} "
        f"WHERE {end_col} >= ? AND {start_col} <= ? "
        f"AND NOT ({start_col} = ? AND {end_col} = ?) LIMIT 1",
        (start, end, start, end),
    ).fetchone()


def _reconcile_open_ended_period(conn, table: str, new_start: str) -> None:
    """If `table` has a currently-open row (period_end == db.OPEN_ENDED_SENTINEL)
    that starts before `new_start`, shrink its period_end to the day before
    `new_start`. Without this, adding a period that supersedes an open-ended
    one would always be rejected as an overlap by _overlapping_period, since
    the sentinel end date is >= any real new_start. This is what lets an
    open-ended "effective from" rate (e.g. a supplier's
    prospective rate sheet) be superseded later without hand-editing the
    older row."""
    open_row = conn.execute(
        f"SELECT id, period_start FROM {table} WHERE period_end = ?",
        (db.OPEN_ENDED_SENTINEL,),
    ).fetchone()
    if open_row is None or open_row["period_start"] >= new_start:
        return
    shrunk_end = (date.fromisoformat(new_start) - timedelta(days=1)).isoformat()
    conn.execute(f"UPDATE {table} SET period_end = ? WHERE id = ?", (shrunk_end, open_row["id"]))


def _apply_rate_periods(conn, parsed: dict, source: str) -> int:
    """Insert/update rate_schedule and gas_rate_schedule from a parsed
    {"power": [RatePeriod...], "gas": [RatePeriod...]} result -- shared by
    the PDF and CSV import routes so the
    reconcile/overlap/upsert logic exists in exactly one place. Returns the
    count of periods skipped for partially overlapping an existing row."""
    skipped_overlaps = 0

    for p in parsed["power"]:
        existing = conn.execute(
            "SELECT id FROM rate_schedule WHERE period_start = ? AND period_end = ?",
            (p.period_start, p.period_end),
        ).fetchone()
        buy_sell = p.rate * 100.0  # EUR/kWh -> ct/kWh; sell = buy (net metering, see rate_schedule notes)
        if existing:
            conn.execute(
                "UPDATE rate_schedule SET buy_ct_per_kwh = ?, sell_ct_per_kwh = ?, source = ? WHERE id = ?",
                (buy_sell, buy_sell, source, existing["id"]),
            )
        else:
            # Reconcile before the overlap check, same order as the
            # Settings-UI manual-entry route -- a new
            # period from an open-ended-shaped source must be able to
            # supersede a currently-open row rather than always being
            # rejected as an overlap against the sentinel end date.
            #
            # Only an OPEN-ENDED incoming period may do this. A *closed*
            # historical period (a supplier's retrospective rate statement, or a
            # hand-typed CSV row off an old bill) must not shrink the live
            # open row: that would leave everything after the closed
            # period's end uncovered, and rate_for() would silently fall
            # back to the historical bill's rate as merely "stale". Such a
            # period falls through to the overlap check below and is
            # skipped with a reported count -- the original behaviour.
            if p.period_end == db.OPEN_ENDED_SENTINEL:
                _reconcile_open_ended_period(conn, "rate_schedule", p.period_start)
            if _overlapping_period(conn, "rate_schedule", p.period_start, p.period_end):
                # A partial overlap would make rate_for()'s lookup order-
                # dependent; exact matches were already handled above.
                skipped_overlaps += 1
            else:
                conn.execute(
                    "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (p.period_start, p.period_end, buy_sell, buy_sell, source),
                )

    for p in parsed["gas"]:
        existing = conn.execute(
            "SELECT id FROM gas_rate_schedule WHERE period_start = ? AND period_end = ?",
            (p.period_start, p.period_end),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE gas_rate_schedule SET price_eur_per_m3 = ?, source = ? WHERE id = ?",
                (p.rate, source, existing["id"]),
            )
        else:
            if p.period_end == db.OPEN_ENDED_SENTINEL:  # see the power branch above
                _reconcile_open_ended_period(conn, "gas_rate_schedule", p.period_start)
            if _overlapping_period(conn, "gas_rate_schedule", p.period_start, p.period_end):
                skipped_overlaps += 1
            else:
                conn.execute(
                    "INSERT INTO gas_rate_schedule (period_start, period_end, price_eur_per_m3, source) "
                    "VALUES (?, ?, ?, ?)",
                    (p.period_start, p.period_end, p.rate, source),
                )

    return skipped_overlaps


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(APP_ROOT / "templates"),
        static_folder=str(APP_ROOT / "static"),
    )
    # Largest real upload is a multi-year 15-min CSV export (~5 MB); 32 MB
    # leaves headroom while stopping trivial disk-fill from the LAN.
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

    # A security review found every write endpoint (import, settings) was
    # reachable from any LAN device with no auth at all. Fail closed rather
    # than silently starting with unauthenticated writes if the secret was
    # never provided -- see README's "Write authentication" section for how
    # this gets into the environment for your deployment.
    write_token = default_credential_source().get("OMNIMETER_WRITE_API_TOKEN")
    if not write_token:
        raise RuntimeError(
            "OMNIMETER_WRITE_API_TOKEN not set -- refusing to start with unauthenticated "
            "write endpoints. See README's Write authentication section."
        )

    @app.before_request
    def _require_write_token():
        # GET/HEAD/OPTIONS are read-only by HTTP convention and cover every
        # current read route; checking the method (not a route allowlist)
        # means a future write route is protected by default rather than
        # needing to remember to add it somewhere.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        provided = request.headers.get("X-OmniMeter-Write-Api-Token", "")
        if not hmac.compare_digest(provided, write_token):
            return jsonify({"error": "missing or invalid X-OmniMeter-Write-Api-Token header"}), 401
        return None

    # init_db() (schema script + _migrate() +
    # _migrate_composite_pk(), the latter doing two PRAGMA table_info probes
    # per call) used to run on every single request via get_conn(). It only
    # needs to run once per process: schema/migrations don't change while
    # the process is up, and every migration here is already idempotent
    # (CREATE TABLE IF NOT EXISTS / ALTER-if-column-missing / PK-shape
    # check), so running it once at startup instead of per-request changes
    # nothing about correctness, just cost. (gunicorn runs 2 worker
    # processes, each importing this module once -- so this runs twice at
    # startup, not once system-wide.)
    #
    # This comment used to argue that concurrent startup was safe because
    # "SQLite's own locking makes two processes independently running
    # idempotent CREATE/ALTER against the same file safe", resting on the
    # fact that by then every migration was a read-only check and none
    # emitted DDL. Both halves needed correcting in 1.14.0. SQLite's locking
    # serialises the writes but does NOT make PRAGMA-then-ALTER atomic, and
    # 1.14.0 added ten columns that really do get added at startup. The
    # race is handled where it belongs, in db._add_column_once().
    _startup_conn = db.get_connection()
    db.init_db(_startup_conn)
    _startup_conn.close()

    def get_conn():
        # g-scoped: one connection per request, opened lazily, closed at
        # teardown below -- previously every route opened (and, per the same
        # review's sibling finding, never closed) its own connection via db.get_connection()
        # directly, relying on garbage collection to eventually close it.
        if "db" not in g:
            g.db = db.get_connection()
        return g.db

    @app.teardown_appcontext
    def _close_conn(exception=None):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def date_range_args():
        today = date.today()
        default_from = today - timedelta(days=90)
        f = request.args.get("from", default_from.isoformat())
        t = request.args.get("to", today.isoformat())
        return f, t

    def _today_local() -> str:
        # solar_estimate.estimate_daily_production() has no concept of
        # elapsed time within a day -- it always returns that whole
        # calendar day's projected total. Callers showing today's own row
        # (the "Today" preset, or any range ending today) need to know
        # that figure is a full-day projection, not an actual, so the
        # frontend can label it rather than present it as a settled total.
        return datetime.now(_LOCAL_TZ).date().isoformat()

    @app.route("/")
    def index():
        # Energy-flow diagram's house-node label, configurable per install.
        home_label = os.environ.get("OMNIMETER_HOME_LABEL", "Home")
        # dashboard.js's date/time formatting is driven by the same
        # OMNIMETER_TIMEZONE the backend honors (_LOCAL_TZ above).
        timezone_name = localtime.TIMEZONE_NAME
        return render_template(
            "index.html",
            write_token=write_token,
            home_label=home_label,
            timezone_name=timezone_name,
            app_version=__version__,
        )

    @app.route("/api/version")
    def api_version():
        return jsonify({"version": __version__})

    @app.route("/api/update-check")
    def api_update_check():
        conn = get_conn()
        return jsonify(update_check.get_status(enabled=update_check.is_enabled(conn)))

    def _period_totals(conn, f: str, t: str) -> dict:
        """Power/Gas/Water/Battery sums+avgs for one (f, t) range -- the
        per-category block /api/overview always needed, factored out so
        /api/compare can call it twice (once per period) without
        duplicating the SQL. Deliberately excludes current_soc_pct: that
        figure is "right now," not scoped to any period, so a caller
        showing two periods side by side would just see the same value
        twice -- /api/overview fetches it separately, once, for its own
        period-independent tile."""

        def one(sql, params):
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

        power = one(
            "SELECT COUNT(*) AS days_with_data, "
            "COALESCE(SUM(import_kwh), 0) AS import_kwh, "
            "COALESCE(SUM(export_kwh), 0) AS export_kwh, "
            "COALESCE(SUM(net_kwh), 0) AS net_kwh "
            "FROM power_daily WHERE date >= ? AND date <= ?",
            (f, t),
        )
        gas = one(
            "SELECT COUNT(*) AS days_with_data, COALESCE(SUM(usage_m3), 0) AS usage_m3 "
            "FROM gas_daily WHERE date >= ? AND date <= ?",
            (f, t),
        )
        water = one(
            "SELECT COUNT(*) AS days_with_data, COALESCE(SUM(usage_l), 0) AS usage_l "
            "FROM water_daily WHERE date >= ? AND date <= ?",
            (f, t),
        )
        battery = one(
            "SELECT COUNT(*) AS days_with_data, "
            "COALESCE(SUM(charge_kwh), 0) AS charge_kwh, "
            "COALESCE(SUM(discharge_kwh), 0) AS discharge_kwh, "
            "AVG(avg_soc_pct) AS avg_soc_pct, "
            "(SELECT eod_soc_pct FROM battery_daily WHERE date >= ? AND date <= ? "
            " ORDER BY date DESC LIMIT 1) AS eod_soc_pct "
            "FROM battery_daily WHERE date >= ? AND date <= ?",
            (f, t, f, t),
        )
        return {"power": power, "gas": gas, "water": water, "battery": battery}

    # SolarEdge status (read-only; for any external monitoring tool).
    # Job health, data freshness, the ESTIMATED credit balance and the grant's remaining life --
    # see src/solaredge_status.py. Exposes no credential, token or site id.
    def _solaredge_token_path() -> Path:
        override = os.environ.get("OMNIMETER_SOLAREDGE_TOKEN_FILE")
        return Path(override) if override else db.resolve_db_path().parent / "solaredge_token.json"

    @app.route("/api/solaredge/status")
    def api_solaredge_status():
        reset_day = int(os.environ.get(solaredge_status.RESET_DAY_ENV, solaredge_status.DEFAULT_RESET_DAY))
        limit = int(os.environ.get(solaredge_status.CREDIT_LIMIT_ENV, solaredge_status.DEFAULT_CREDIT_LIMIT))
        return jsonify(
            solaredge_status.build_status(
                get_conn(), _solaredge_token_path(), datetime.now(UTC), _LOCAL_TZ, reset_day=reset_day, credit_limit=limit
            )
        )

    @app.route("/api/solaredge/power")
    def api_solaredge_power():
        hours = request.args.get("hours", default=48, type=int)
        return jsonify(solaredge_status.recent_power(get_conn(), datetime.now(UTC), hours))

    @app.route("/api/overview")
    def api_overview():
        conn = get_conn()
        f, t = date_range_args()
        totals = _period_totals(conn, f, t)
        current_soc_row = conn.execute(
            "SELECT date, eod_soc_pct FROM battery_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if totals["battery"] is not None:
            totals["battery"]["current_soc_pct"] = current_soc_row["eod_soc_pct"] if current_soc_row else None
            # Exposes the tile's actual freshness (the value itself gives
            # no clue it's frozen if both battery ingest paths stop silently).
            totals["battery"]["current_soc_date"] = current_soc_row["date"] if current_soc_row else None

        last_refreshed = None
        raw_latest = conn.execute("SELECT MAX(time) AS t FROM power_readings").fetchone()["t"]
        if raw_latest:
            last_refreshed = (
                datetime.strptime(raw_latest, "%Y-%m-%d %H:%M")
                .replace(tzinfo=_LOCAL_TZ)
                .isoformat()
            )

        return jsonify(
            {
                "from": f,
                "to": t,
                **totals,
                "last_refreshed": last_refreshed,
            }
        )

    @app.route("/api/compare")
    def api_compare():
        conn = get_conn()
        try:
            a_from = _parse_iso_date(request.args.get("a_from"), "a_from")
            a_to = _parse_iso_date(request.args.get("a_to"), "a_to")
            b_from = _parse_iso_date(request.args.get("b_from"), "b_from")
            b_to = _parse_iso_date(request.args.get("b_to"), "b_to")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if a_to < a_from:
            return jsonify({"error": "a_to is before a_from"}), 400
        if b_to < b_from:
            return jsonify({"error": "b_to is before b_from"}), 400

        # Fetched once, shared by both periods below -- occupancy_log is
        # small (a handful of logged ranges), so this is cheap either way,
        # but there's no reason to hit it twice.
        occupancy_rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log").fetchall()]

        def period(f: str, t: str) -> dict:
            totals = _period_totals(conn, f, t)
            by_day = aggregate.expand_occupancy_by_day(occupancy_rows, f, t)
            totals["occupancy"] = {
                "avg_headcount": (sum(by_day.values()) / len(by_day)) if by_day else None,
                "covered_days": len(by_day),
            }
            return {"from": f, "to": t, **totals}

        return jsonify({"period_a": period(a_from, a_to), "period_b": period(b_from, b_to)})

    # ---- Quarters sub-tab (calendar quarters, Q1 = Jan-Mar ...) ----
    # The "today" passed in is the local calendar date, so a quarter is only
    # "in progress" by the household's own clock, not UTC's.
    def _local_today() -> date:
        return datetime.now(_LOCAL_TZ).date()

    @app.route("/api/quarters/available")
    def api_quarters_available():
        return jsonify(quarters.available_quarters(get_conn(), _local_today()))

    @app.route("/api/quarters/compare")
    def api_quarters_compare():
        try:
            a = request.args.get("a")
            b = request.args.get("b")
            quarters.parse_quarter(a)
            quarters.parse_quarter(b)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(quarters.compare(get_conn(), a, b, _local_today()))

    @app.route("/api/quarters/series")
    def api_quarters_series():
        try:
            q = int(request.args.get("q", ""))
            return jsonify(quarters.series(get_conn(), q, _local_today()))
        except ValueError:
            return jsonify({"error": "q must be 1, 2, 3 or 4"}), 400

    def _daily_series(table, columns):
        conn = get_conn()
        f, t = date_range_args()
        cols = ", ".join(["date"] + columns)
        rows = conn.execute(
            f"SELECT {cols} FROM {table} WHERE date >= ? AND date <= ? ORDER BY date",
            (f, t),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/power/daily")
    def api_power_daily():
        return _daily_series(
            "power_daily", ["import_kwh", "export_kwh", "net_kwh", "l1_max_w", "l2_max_w", "l3_max_w"]
        )

    @app.route("/api/gas/daily")
    def api_gas_daily():
        return _daily_series("gas_daily", ["usage_m3"])

    @app.route("/api/water/daily")
    def api_water_daily():
        return _daily_series("water_daily", ["usage_l"])

    @app.route("/api/battery/daily")
    def api_battery_daily():
        return _daily_series(
            "battery_daily", ["charge_kwh", "discharge_kwh", "min_soc_pct", "max_soc_pct", "avg_soc_pct"]
        )

    def _production_by_date(conn, f: str, t: str) -> dict[str, dict] | None:
        """Per-day solar production for [f, t]: MEASURED where an inverter feed has the day (SolarEdge,
        see src/solar_production.py), otherwise ESTIMATED from the panel capacity. Each
        entry carries the kWh, which ``basis`` produced it ("measured" | "weather" | "seasonal"),
        whether it is ``partial`` (today, measured so far) and the day's radiation figure when weather
        is enabled (context for the sun readout, whatever the basis). Shared by /api/self-sufficiency
        and /api/energy-flow so the two can't drift. Returns None when there is neither a kWp rating
        nor any measured day in range (distinct from a range with no power rows, which returns {})."""
        pv = conn.execute("SELECT * FROM pv_config WHERE id = 1").fetchone()
        kwp = pv["kwp_rating"] if pv is not None else None
        measured = solar_production.measured_by_date(conn, f, t, _today_local())
        if kwp is None and not measured:
            return None

        power_rows = conn.execute(
            "SELECT date, import_kwh, export_kwh FROM power_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (f, t),
        ).fetchall()

        # Prefer measured radiation for the ESTIMATE, and keep it as context for measured days too. Falls
        # back to the seasonal curve per-day (not per-request) so a range that straddles the start of
        # weather coverage degrades day by day rather than discarding the good days. `basis` is returned
        # per day so the UI can say which one produced the number.
        radiation: dict[str, float] = {}
        reference_annual = None
        if weather.weather_enabled(conn):
            reference_annual = weather.reference_annual_radiation(conn)
            if reference_annual:
                radiation = weather.radiation_by_date(conn, f, t)

        out: dict[str, dict] = {}
        for r in power_rows:
            rad = radiation.get(r["date"])
            m = measured.get(r["date"])
            if m is not None:
                out[r["date"]] = {"production_kwh": m["kwh"], "basis": "measured", "partial": m["partial"], "radiation_mj": rad}
                continue
            if kwp is None:
                continue  # no measurement and no capacity to estimate from: leave the day out
            day = datetime.strptime(r["date"], "%Y-%m-%d").date()
            if rad is not None and reference_annual:
                est_production = estimate_daily_production_from_radiation(kwp, rad, reference_annual)
                basis = "weather"
            else:
                est_production = estimate_daily_production(kwp, day)
                basis = "seasonal"
            est_production = reconcile_with_export(est_production, r["export_kwh"] or 0.0)
            out[r["date"]] = {"production_kwh": est_production, "basis": basis, "partial": False, "radiation_mj": rad}
        return out

    def _day_flow(prod_kwh: float, power_row, batt) -> dict:
        """One day's energy_flow_matrix with the battery included (zeros before battery data exists)."""
        return aggregate.energy_flow_matrix(
            solar_kwh=prod_kwh,
            discharge_kwh=(batt["discharge_kwh"] if batt else 0.0) or 0.0,
            import_kwh=power_row["import_kwh"] or 0.0,
            charge_kwh=(batt["charge_kwh"] if batt else 0.0) or 0.0,
            export_kwh=power_row["export_kwh"] or 0.0,
        )

    def _battery_by_date(conn, f: str, t: str) -> dict:
        return {
            r["date"]: r
            for r in conn.execute(
                "SELECT date, charge_kwh, discharge_kwh FROM battery_daily WHERE date >= ? AND date <= ?", (f, t)
            ).fetchall()
        }

    @app.route("/api/self-sufficiency")
    def api_self_sufficiency():
        conn = get_conn()
        f, t = date_range_args()
        prod_by_date = _production_by_date(conn, f, t)
        if prod_by_date is None:
            return jsonify(
                {
                    "available": False,
                    "reason": "No solar data yet: connect an inverter feed, "
                    "or add your PV kWp rating under Settings.",
                }
            )

        power_rows = conn.execute(
            "SELECT date, import_kwh, export_kwh FROM power_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (f, t),
        ).fetchall()
        battery_by_date = _battery_by_date(conn, f, t)

        results = []
        for r in power_rows:
            prod = prod_by_date.get(r["date"])
            if prod is None:
                continue
            batt = battery_by_date.get(r["date"])
            sufficiency = solar_production.self_sufficiency(
                prod["production_kwh"],
                r["export_kwh"] or 0.0,
                r["import_kwh"] or 0.0,
                (batt["charge_kwh"] if batt else 0.0) or 0.0,
                (batt["discharge_kwh"] if batt else 0.0) or 0.0,
            )
            results.append(
                {
                    "date": r["date"],
                    "production_kwh": round(prod["production_kwh"], 2),
                    "import_kwh": r["import_kwh"],
                    "export_kwh": r["export_kwh"],
                    "self_sufficiency_pct": round(sufficiency * 100, 1) if sufficiency is not None else None,
                    "basis": prod["basis"],
                    "partial": prod["partial"],
                    "radiation_mj": round(prod["radiation_mj"], 2) if prod["radiation_mj"] is not None else None,
                }
            )
        source = solar_production.production_source(results)
        weather_days = sum(1 for r in results if r["basis"] == "weather")
        radiation_days = sum(1 for r in results if r["radiation_mj"] is not None)
        today_in_progress = any(r["date"] == _today_local() for r in results)

        # How sunny this period actually was, against what that same slice of the calendar normally gets.
        # Compared date-for-date rather than as a flat average, so a range spanning a season boundary is
        # still judged against the right reference for each of its days. Works whatever the production
        # basis: the sun readout is context, not part of the estimate.
        sun_pct = None
        if radiation_days:
            typical = weather.typical_radiation_by_day_of_year(conn)
            actual_sum = sum(r["radiation_mj"] for r in results if r["radiation_mj"] is not None)
            typical_sum = sum(typical.get(r["date"][5:], 0.0) for r in results if r["radiation_mj"] is not None)
            if typical_sum > 0:
                sun_pct = round(actual_sum / typical_sum * 100, 0)
        return jsonify(
            {
                "available": True,
                "production_source": source,
                "estimated": source != "measured",
                "measured_days": sum(1 for r in results if r["basis"] == "measured"),
                "estimated_days": sum(1 for r in results if r["basis"] != "measured"),
                "days": results,
                # Attribution is a CC BY 4.0 obligation wherever weather-derived values are displayed,
                # cached ones included -- surfaced here so the frontend cannot render them without it.
                "weather_attribution": weather.ATTRIBUTION if radiation_days else None,
                "weather_days": weather_days,
                "radiation_days": radiation_days,
                "sun_pct_of_typical": sun_pct,
                "today_in_progress": today_in_progress,
            }
        )

    @app.route("/api/energy-flow")
    def api_energy_flow():
        # Overview's proportional Sankey-style visual, in addition to the existing simplified
        # renderEnergyFlow() diagram. Reuses the same production path as /api/self-sufficiency
        # (_production_by_date) so the two can't drift.
        #
        # The merit-order allocation runs once PER DAY, not once on period totals -- computing it on
        # totals is wrong, not just less precise: a whole
        # period's solar and battery discharge can get fully absorbed by Load in one pass before either
        # has anything left for Grid out, forcing genuinely-exported power through the grid-in-to-grid-out
        # fallback pair instead. Per day, each day's own solar/battery/grid balance stays intact, so a
        # sunny day's real export is attributed to solar on that day. See sum_energy_flow_matrices().
        conn = get_conn()
        f, t = date_range_args()

        power_rows = conn.execute(
            "SELECT date, import_kwh, export_kwh FROM power_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (f, t),
        ).fetchall()
        battery_by_date = _battery_by_date(conn, f, t)

        prod_by_date = _production_by_date(conn, f, t)
        pv_configured = prod_by_date is not None

        daily_matrices = []
        shown = []
        for r in power_rows:
            prod = prod_by_date.get(r["date"]) if prod_by_date else None
            if prod is not None:
                shown.append({"basis": prod["basis"]})
            daily_matrices.append(_day_flow(prod["production_kwh"] if prod else 0.0, r, battery_by_date.get(r["date"])))

        matrix = aggregate.sum_energy_flow_matrices(daily_matrices)
        matrix["pv_configured"] = pv_configured
        matrix["production_source"] = solar_production.production_source(shown)
        matrix["today_in_progress"] = any(r["date"] == _today_local() for r in power_rows)
        if not daily_matrices:
            # sum_energy_flow_matrices([]) omits sources/uses keys entirely (nothing to sum) -- the
            # frontend expects the full shape even for an empty range, so fill in the zeros here.
            matrix["sources"] = {"solar": 0.0, "battery_discharge": 0.0, "grid_in": 0.0}
            matrix["uses"] = {"load": 0.0, "battery_charge": 0.0, "grid_out": 0.0}
        # Weather attribution isn't needed here -- Self-Sufficiency already carries the CC BY 4.0 credit
        # for the radiation context, and this view doesn't expose the radiation figure itself.
        return jsonify(matrix)

    @app.route("/api/solar/summary")
    def api_solar_summary():
        """Solar tab: daily production for [from, to] plus headline stats and the previous period for comparison.
        Measured days (inverter feed) are used when the range has any; otherwise the kWp estimate, labelled as such.
        Unlike Self-Sufficiency this does not need grid-meter rows, so it reaches back as far as the inverter history."""
        conn = get_conn()
        f, t = date_range_args()
        today = _today_local()
        pv = conn.execute("SELECT kwp_rating FROM pv_config WHERE id = 1").fetchone()
        kwp = pv["kwp_rating"] if pv is not None else None

        measured = solar_production.measured_by_date(conn, f, t, today)
        if measured:
            days = [
                {"date": d, "kwh": round(v["kwh"], 3), "partial": v["partial"], "basis": "measured"}
                for d, v in sorted(measured.items())
            ]
            source = "measured"
        else:
            est = _production_by_date(conn, f, t)
            if est is None:
                reason = "No solar data yet: connect an inverter feed, or add your PV kWp rating under Settings."
                return jsonify({"available": False, "reason": reason})
            days = [
                {"date": d, "kwh": round(v["production_kwh"], 3), "partial": False, "basis": v["basis"]}
                for d, v in sorted(est.items())
            ]
            source = solar_production.production_source(days)

        export_by_date = {
            r["date"]: r["export_kwh"] or 0.0
            for r in conn.execute("SELECT date, export_kwh FROM power_daily WHERE date >= ? AND date <= ?", (f, t)).fetchall()
        }
        stats = solar_production.period_stats(days, kwp, export_by_date)

        # Previous period of the same length, for "vs previous". Only offered when it has comparable coverage.
        prior = None
        try:
            d_from, d_to = date.fromisoformat(f), date.fromisoformat(t)
            span = (d_to - d_from).days + 1
            p_to = d_from - timedelta(days=1)
            p_from = p_to - timedelta(days=span - 1)
            prior_days = solar_production.measured_by_date(conn, p_from.isoformat(), p_to.isoformat(), today)
            prior_total = sum(v["kwh"] for v in prior_days.values())
            if source == "measured" and len(prior_days) >= 0.8 * span and prior_total > 0:
                prior = {
                    "from": p_from.isoformat(),
                    "to": p_to.isoformat(),
                    "total_kwh": round(prior_total, 2),
                    "delta_pct": round((stats["total_kwh"] / prior_total - 1) * 100, 1),
                }
        except ValueError:
            prior = None

        return jsonify(
            {
                "available": True,
                "production_source": source,
                "kwp": kwp,
                "days": days,
                "stats": stats,
                "prior": prior,
                "today_in_progress": any(d["date"] == today for d in days),
            }
        )

    @app.route("/api/solar/history")
    def api_solar_history():
        """Solar tab: all-time monthly totals by year (year-over-year comparison) and annual totals."""
        conn = get_conn()
        data = solar_production.monthly_history(conn)
        pv = conn.execute("SELECT kwp_rating FROM pv_config WHERE id = 1").fetchone()
        data["kwp"] = pv["kwp_rating"] if pv is not None else None
        if data.get("available"):
            weather_by_year = solar_production.yearly_weather(conn)
            data["annual"] = solar_production.annual_insights(
                data["annual"], weather_by_year, data["kwp"], datetime.now(_LOCAL_TZ).year
            )
            data["weather_attribution"] = weather.ATTRIBUTION if weather_by_year else None
        return jsonify(data)

    @app.route("/api/costs")
    def api_costs():
        conn = get_conn()
        if not costs.has_rates(conn):
            return jsonify({"available": False, "reason": costs.NO_RATES_REASON})

        f, t = date_range_args()
        result = costs.daily_costs(conn, f, t)
        # Same public shape as always; the per-part stale flags are for the
        # Quarters view and stay out of this response.
        days = [
            {k: d[k] for k in ("date", "power_cost_eur", "gas_cost_eur", "total_cost_eur", "stale")}
            for d in result["days"]
        ]
        return jsonify({"available": True, "days": days, "stale_count": result["stale_count"]})

    @app.route("/api/settings/pv", methods=["GET", "POST"])
    def api_settings_pv():
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            try:
                kwp = _parse_finite(data.get("kwp_rating"), "kwp_rating", 0.0, 100.0)
                installed_date = _parse_iso_date(data["installed_date"], "installed_date") if data.get("installed_date") else None
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            notes = data.get("notes")
            conn.execute(
                "INSERT INTO pv_config (id, kwp_rating, installed_date, notes) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET kwp_rating = excluded.kwp_rating, "
                "installed_date = excluded.installed_date, notes = excluded.notes",
                (kwp, installed_date, notes),
            )
            conn.commit()
        row = conn.execute("SELECT * FROM pv_config WHERE id = 1").fetchone()
        return jsonify(dict(row) if row else {})

    @app.route("/api/settings/rates", methods=["GET", "POST"])
    def api_settings_rates():
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            try:
                start = _parse_iso_date(data.get("period_start"), "period_start")
                # Blank/omitted period_end means "ongoing, no end date yet" --
                # e.g. a supplier's prospective rate sheet rather than a
                # closed historical period.
                end = (
                    _parse_iso_date(data.get("period_end"), "period_end")
                    if data.get("period_end")
                    else db.OPEN_ENDED_SENTINEL
                )
                buy = _parse_finite(data.get("buy_ct_per_kwh"), "buy_ct_per_kwh", 0.0, 1000.0)
                sell = _parse_finite(data.get("sell_ct_per_kwh"), "sell_ct_per_kwh", 0.0, 1000.0)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if end < start:
                return jsonify({"error": "period_end is before period_start"}), 400
            _reconcile_open_ended_period(conn, "rate_schedule", start)
            clash = _overlapping_period(conn, "rate_schedule", start, end)
            if clash:
                return (
                    jsonify({"error": f"period overlaps existing {clash['period_start']} – {clash['period_end']}"}),
                    400,
                )
            conn.execute(
                "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (start, end, buy, sell, data.get("source")),
            )
            conn.commit()
        rows = conn.execute("SELECT * FROM rate_schedule ORDER BY period_start").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/settings/gas-rates", methods=["GET", "POST"])
    def api_settings_gas_rates():
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            try:
                start = _parse_iso_date(data.get("period_start"), "period_start")
                end = (
                    _parse_iso_date(data.get("period_end"), "period_end")
                    if data.get("period_end")
                    else db.OPEN_ENDED_SENTINEL
                )
                price = _parse_finite(data.get("price_eur_per_m3"), "price_eur_per_m3", 0.0, 100.0)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if end < start:
                return jsonify({"error": "period_end is before period_start"}), 400
            _reconcile_open_ended_period(conn, "gas_rate_schedule", start)
            clash = _overlapping_period(conn, "gas_rate_schedule", start, end)
            if clash:
                return (
                    jsonify({"error": f"period overlaps existing {clash['period_start']} – {clash['period_end']}"}),
                    400,
                )
            conn.execute(
                "INSERT INTO gas_rate_schedule (period_start, period_end, price_eur_per_m3, source) "
                "VALUES (?, ?, ?, ?)",
                (start, end, price, data.get("source")),
            )
            conn.commit()
        rows = conn.execute("SELECT * FROM gas_rate_schedule ORDER BY period_start").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/settings/fiscal-years", methods=["GET", "POST"])
    def api_settings_fiscal_years():
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            try:
                power_m, power_d = _parse_fiscal_start(data, "power")
                gas_m, gas_d = _parse_fiscal_start(data, "gas")
                water_m, water_d = _parse_fiscal_start(data, "water")
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            conn.execute(
                "INSERT INTO fiscal_year_config (id, power_fy_start_month, power_fy_start_day, "
                "gas_fy_start_month, gas_fy_start_day, water_fy_start_month, water_fy_start_day) "
                "VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "power_fy_start_month = excluded.power_fy_start_month, power_fy_start_day = excluded.power_fy_start_day, "
                "gas_fy_start_month = excluded.gas_fy_start_month, gas_fy_start_day = excluded.gas_fy_start_day, "
                "water_fy_start_month = excluded.water_fy_start_month, water_fy_start_day = excluded.water_fy_start_day",
                (power_m, power_d, gas_m, gas_d, water_m, water_d),
            )
            conn.commit()
        row = conn.execute("SELECT * FROM fiscal_year_config WHERE id = 1").fetchone()
        return jsonify(dict(row) if row else {})

    @app.route("/api/settings/toggles", methods=["GET", "POST"])
    def api_settings_toggles():
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            values = tuple(1 if data.get(f) else 0 for f in _TOGGLE_FIELDS)
            columns = ", ".join(_TOGGLE_FIELDS)
            placeholders = ", ".join("?" for _ in _TOGGLE_FIELDS)
            updates = ", ".join(f"{f} = excluded.{f}" for f in _TOGGLE_FIELDS)
            conn.execute(
                f"INSERT INTO feature_toggles (id, {columns}) VALUES (1, {placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
            conn.commit()
        # Scoped to _TOGGLE_FIELDS, not SELECT * -- the row also
        # carries the separate show_*_tab visibility columns (same table,
        # different endpoint/form), which have no business leaking into
        # this response.
        row = conn.execute(f"SELECT {', '.join(_TOGGLE_FIELDS)} FROM feature_toggles WHERE id = 1").fetchone()
        return jsonify(dict(row) if row else {})

    @app.route("/api/settings/visibility", methods=["GET", "POST"])
    def api_settings_visibility():
        # Same singleton-row full-replace pattern as toggles above,
        # deliberately a separate endpoint/field set -- see _VISIBILITY_FIELDS.
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            values = tuple(1 if data.get(f) else 0 for f in _VISIBILITY_FIELDS)
            columns = ", ".join(_VISIBILITY_FIELDS)
            placeholders = ", ".join("?" for _ in _VISIBILITY_FIELDS)
            updates = ", ".join(f"{f} = excluded.{f}" for f in _VISIBILITY_FIELDS)
            conn.execute(
                f"INSERT INTO feature_toggles (id, {columns}) VALUES (1, {placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
            conn.commit()
        row = conn.execute(f"SELECT {', '.join(_VISIBILITY_FIELDS)} FROM feature_toggles WHERE id = 1").fetchone()
        return jsonify(dict(row) if row else {})

    @app.route("/api/settings/anomaly", methods=["GET", "POST"])
    def api_settings_anomaly():
        # Chat ID (telegram_config, singleton row like pv_config) and the
        # per-metric runaway thresholds (runaway_detector_config) are
        # DB-editable; the bot token is never returned here --
        # it lives only in the environment (see telegram_notifier.py). GET
        # also reports whether the env vars are set at all, as booleans
        # only, so Settings can tell a self-hoster "token not configured"
        # without ever exposing the value.
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            if "chat_id" in data:
                chat_id = (data.get("chat_id") or "").strip() or None
                conn.execute(
                    "INSERT INTO telegram_config (id, chat_id) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET chat_id = excluded.chat_id",
                    (chat_id,),
                )
            if any(k in data for k in ("home_threshold_l", "away_threshold_l", "away_after_days")):
                try:
                    home_l = _parse_finite(data.get("home_threshold_l"), "home_threshold_l", 0.1, 100000)
                    away_l = _parse_finite(data.get("away_threshold_l"), "away_threshold_l", 0.1, 100000)
                    away_days = _parse_int_range(data.get("away_after_days"), "away_after_days", 1, 30)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
                conn.execute(
                    "INSERT INTO runaway_detector_config (metric, home_threshold_l, away_threshold_l, away_after_days) "
                    "VALUES ('water', ?, ?, ?) "
                    "ON CONFLICT(metric) DO UPDATE SET home_threshold_l = excluded.home_threshold_l, "
                    "away_threshold_l = excluded.away_threshold_l, away_after_days = excluded.away_after_days",
                    (home_l, away_l, away_days),
                )
            conn.commit()
        chat_row = conn.execute("SELECT chat_id FROM telegram_config WHERE id = 1").fetchone()
        thresholds = runaway_detector.get_thresholds(conn, "water")
        # The Tier 1 rate gates and Tier 2 window gates are reported but
        # not accepted back: they are self-tuning through
        # record_dismissal(), which nudges whichever gate actually
        # misfired, and that beats hand-editing a number whose safe range
        # was established empirically. Reported anyway
        # so a self-hoster can see what the detector is actually using.
        gates = runaway_detector.get_gate_config(conn, "water")
        return jsonify(
            {
                "chat_id": chat_row["chat_id"] if chat_row else None,
                "bot_token_configured": bool(default_credential_source().get("OMNIMETER_TELEGRAM_BOT_TOKEN")),
                "chat_id_env_configured": bool(default_credential_source().get("OMNIMETER_TELEGRAM_CHAT_ID")),
                **thresholds,
                **gates,
            }
        )

    @app.route("/api/alerts/test", methods=["POST"])
    def api_alerts_test():
        status, error = telegram_notifier.send_test_message(get_conn())
        return jsonify({"status": status, "error": error})

    @app.route("/api/alerts/active")
    def api_alerts_active():
        # Optional ?kind= filter -- the banner (checkActiveAlerts) wants
        # every kind, but the Leak alerts Settings card's own history only
        # wants 'runaway' rows (a kind='health' row's total_volume means
        # minutes-stale, not litres, and doesn't belong in a "Volume (L)"
        # column -- see health_monitor.py).
        kind = request.args.get("kind")
        if kind:
            rows = get_conn().execute(
                "SELECT * FROM alert_episodes WHERE acknowledged_at IS NULL AND kind = ? ORDER BY started_at DESC",
                (kind,),
            ).fetchall()
        else:
            rows = get_conn().execute(
                "SELECT * FROM alert_episodes WHERE acknowledged_at IS NULL ORDER BY started_at DESC"
            ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/alerts/history")
    def api_alerts_history():
        f, t = date_range_args()
        kind = request.args.get("kind")
        conn = get_conn()
        if kind:
            rows = conn.execute(
                "SELECT * FROM alert_episodes WHERE ended_at IS NOT NULL AND kind = ? "
                "AND date(started_at) >= ? AND date(started_at) <= ? ORDER BY started_at DESC",
                (kind, f, t),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alert_episodes WHERE ended_at IS NOT NULL AND date(started_at) >= ? AND date(started_at) <= ? "
                "ORDER BY started_at DESC",
                (f, t),
            ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/alerts/<int:episode_id>/acknowledge", methods=["POST"])
    def api_alerts_acknowledge(episode_id: int):
        conn = get_conn()
        data = request.get_json(force=True, silent=True) or {}
        row = conn.execute("SELECT id, kind FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        if row is None:
            return jsonify({"error": f"no alert episode with id {episode_id}"}), 404
        # The "not a leak" threshold-nudge is Layer 1-specific -- a
        # kind='health' episode has no threshold_l to nudge, and its
        # metric can be the same value ('water') Layer 1 uses for its own
        # config row, so blindly applying it here would corrupt the wrong
        # setting. Any other kind is always a plain acknowledge.
        not_a_leak = row["kind"] == "runaway" and data.get("reason") == "not_a_leak"
        runaway_detector.record_dismissal(conn, episode_id, not_a_leak=not_a_leak)
        return jsonify({"acknowledged": True})

    @app.route("/api/anomaly/models")
    def api_anomaly_models():
        # Layer 2 training status per metric -- lets a
        # self-hoster (or a monitoring dashboard) see whether/when each
        # metric's context-aware model has actually trained, without a
        # database client. Deliberately does NOT expose intercept/
        # coefficients/feature_means/feature_stds: those are internal model
        # parameters with no meaning to a UI, not a secret, just noise.
        rows = get_conn().execute(
            "SELECT metric, trained_at, training_rows, residual_threshold FROM anomaly_model_state"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/settings/occupancy", methods=["GET", "POST"])
    def api_settings_occupancy():
        conn = get_conn()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            try:
                start = _parse_iso_datetime(data.get("date_from"), "date_from")
                end = _parse_iso_datetime(data.get("date_to"), "date_to")
                count = _parse_int_range(data.get("occupant_count"), "occupant_count", 0, 20)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if end < start:
                return jsonify({"error": "date_to is before date_from"}), 400
            # occupant_count is the TOTAL headcount for [start, end], but
            # unlike rate_schedule this table deliberately allows overlap --
            # e.g. a shorter trip nested inside a longer visit, where the
            # nested entry should override the surrounding one just for the
            # days it covers. Resolved at read time by
            # aggregate.expand_occupancy_by_day() (shortest/most-specific
            # covering entry wins per instant), not rejected here.
            conn.execute(
                "INSERT INTO occupancy_log (date_from, date_to, occupant_count, notes) VALUES (?, ?, ?, ?)",
                (start, end, count, data.get("notes")),
            )
            conn.commit()
        rows = conn.execute("SELECT * FROM occupancy_log ORDER BY date_from").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/occupancy/suggestions")
    def api_occupancy_suggestions():
        # Stretches the meters say were empty but the log doesn't
        # record as empty. Read-only -- proposes, never writes. Logging one is
        # an ordinary POST to /api/settings/occupancy with count 0, so this
        # adds a suggestion surface rather than a second way to write.
        return jsonify(aggregate.suggest_absence_entries(get_conn()))

    @app.route("/api/settings/occupancy/<int:entry_id>", methods=["DELETE"])
    def api_settings_occupancy_delete(entry_id):
        conn = get_conn()
        cur = conn.execute("DELETE FROM occupancy_log WHERE id = ?", (entry_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": f"no occupancy entry with id {entry_id}"}), 404
        rows = conn.execute("SELECT * FROM occupancy_log ORDER BY date_from").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/occupancy-stats")
    def api_occupancy_stats():
        conn = get_conn()
        f, t = date_range_args()
        occupancy_rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log").fetchall()]
        by_day = aggregate.expand_occupancy_by_day(occupancy_rows, f, t)
        if not by_day:
            return jsonify({"available": False, "reason": "No occupancy entries logged for this range yet."})

        def category_stats(table: str, column: str):
            # Only days with BOTH a logged occupancy entry AND a real usage
            # value contribute to any figure below -- keeps every numerator
            # and its person-days denominator consistent, rather than
            # diluting per_person_day with days that have one but not the
            # other.
            usage_by_date = {
                r["date"]: r[column]
                for r in conn.execute(
                    f"SELECT date, {column} FROM {table} WHERE date >= ? AND date <= ?", (f, t)
                ).fetchall()
                if r[column] is not None
            }
            matched = sorted(set(by_day) & set(usage_by_date))
            away_vals = [usage_by_date[d] for d in matched if by_day[d] == 0]
            alone_vals = [usage_by_date[d] for d in matched if by_day[d] == 1]
            occupied_vals = [usage_by_date[d] for d in matched if by_day[d] > 1]
            # per_person_day is deliberately scoped to days with >=1 person --
            # an away day (0 people) can still have real usage (fridge,
            # standby draw, etc.), but that usage isn't attributable to any
            # person, so folding it into this ratio would inflate it with
            # nobody there to divide it among.
            present_days = [d for d in matched if by_day[d] >= 1]
            person_days = sum(by_day[d] for d in present_days)
            total = sum(usage_by_date[d] for d in present_days)
            return {
                "days_with_data": len(matched),
                "avg_away": (sum(away_vals) / len(away_vals)) if away_vals else None,
                "avg_alone": (sum(alone_vals) / len(alone_vals)) if alone_vals else None,
                "avg_occupied": (sum(occupied_vals) / len(occupied_vals)) if occupied_vals else None,
                "per_person_day": (total / person_days) if person_days else None,
            }

        away_days = sum(1 for c in by_day.values() if c == 0)
        alone_days = sum(1 for c in by_day.values() if c == 1)
        occupied_days = sum(1 for c in by_day.values() if c > 1)
        return jsonify(
            {
                "available": True,
                "from": f,
                "to": t,
                "covered_days": len(by_day),
                "away_days": away_days,
                "alone_days": alone_days,
                "occupied_days": occupied_days,
                "power": category_stats("power_daily", "import_kwh"),
                "gas": category_stats("gas_daily", "usage_m3"),
                "water": category_stats("water_daily", "usage_l"),
            }
        )

    @app.route("/api/ingest-status")
    def api_ingest_status():
        conn = get_conn()
        rows = conn.execute(
            "SELECT filename, category, ingested_at, row_count FROM ingested_files ORDER BY ingested_at DESC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/data-freshness")
    def api_data_freshness():
        # power/gas/water/battery: most recent date with real data. costs_power/
        # costs_gas are deliberately NOT "last CSV import" -- Costs isn't
        # imported, it's derived from the rate schedule, so the equivalent
        # "how current is this" figure is the latest rate period's end date --
        # the same boundary /api/costs already uses to decide when a day's
        # cost is stale (see rate_for()'s carry-forward fallback above).
        conn = get_conn()

        def max_col(table: str, column: str):
            row = conn.execute(f"SELECT MAX({column}) AS m FROM {table}").fetchone()
            return row["m"]

        return jsonify(
            {
                "power": max_col("power_daily", "date"),
                "gas": max_col("gas_daily", "date"),
                "water": max_col("water_daily", "date"),
                "battery": max_col("battery_daily", "date"),
                "costs_power": max_col("rate_schedule", "period_end"),
                "costs_gas": max_col("gas_rate_schedule", "period_end"),
            }
        )

    @app.route("/api/data-health")
    def api_data_health():
        # Read-only gap diagnostic, distinct from /api/data-freshness
        # (latest date only) -- reports missing days *within* each category's
        # own tracked history, not just how current it is. Not wired into any
        # chart's gap-handling logic (aggregate.py's compute_daily_deltas
        # already drops gapped intervals there); this is purely a surfaced
        # report for the user to notice a silent ingest failure.
        conn = get_conn()
        acknowledged = _acknowledged_set(conn)
        return jsonify(
            {
                "power": _find_daily_gaps(conn, "power", "power_daily", acknowledged),
                "gas": _find_daily_gaps(conn, "gas", "gas_daily", acknowledged),
                "water": _find_daily_gaps(conn, "water", "water_daily", acknowledged),
                "battery": _find_daily_gaps(conn, "battery", "battery_daily", acknowledged),
            }
        )

    @app.route("/api/data-quality")
    def api_data_quality():
        # "is my data trustworthy" report, distinct from
        # /api/data-health's gap detector -- glitch episodes, cross-source
        # disagreements, negative deltas, and out-of-range gauge values.
        # Purely read-only.
        #
        # Outlier daily totals used to be part of this response and are now
        # served by /api/consumption-notes instead: an unusually low day is a
        # fact about consumption, not about data integrity, and mixing the
        # two buried genuine instrument faults under a large number of
        # consumption flags. The route keeps its /api/data-quality name so
        # that /api/data-quality/acknowledge stays the single acknowledge
        # endpoint for findings from both reports.
        conn = get_conn()
        return jsonify(aggregate.data_integrity_report(conn, _acknowledged_set(conn)))

    @app.route("/api/reconciliation")
    def api_reconciliation():
        # Independently re-derives every stored daily total from the raw
        # cumulative readings (closing minus opening) and compares. Unlike
        # every other check here this is an invariant, not a heuristic --
        # see aggregate.reconcile_daily_totals for why that distinction
        # matters and what it does not cover. Read-only; never corrects.
        conn = get_conn()
        return jsonify(aggregate.reconcile_daily_totals(conn, _acknowledged_set(conn)))

    @app.route("/api/weather/daily")
    def api_weather_daily():
        # Per-day sunshine for the chart rail. Returns
        # pct_of_typical rather than a raw figure, because raw radiation is
        # unreadable without a seasonal reference -- daily radiation swings
        # several-fold between winter and summer, so the same number means
        # opposite things depending on the date.
        conn = get_conn()
        if not weather.weather_enabled(conn):
            return jsonify({"available": False, "days": [], "attribution": None})
        f, t = date_range_args()
        radiation = weather.radiation_by_date(conn, f, t)
        if not radiation:
            return jsonify({"available": False, "days": [], "attribution": None})
        typical = weather.typical_radiation_by_day_of_year(conn)
        days = []
        for d in sorted(radiation):
            ref = typical.get(d[5:])
            days.append(
                {
                    "date": d,
                    "radiation_mj": round(radiation[d], 2),
                    "pct_of_typical": round(radiation[d] / ref * 100, 0) if ref else None,
                }
            )
        return jsonify({"available": True, "days": days, "attribution": weather.ATTRIBUTION})

    @app.route("/api/weather/gas")
    def api_weather_gas():
        # Per-day heating-degree-days for the Gas chart rail -- same shape
        # and same "vs typical for this date" framing as /api/weather/daily
        # above, computed from temperature instead of radiation.
        conn = get_conn()
        if not weather.weather_enabled(conn):
            return jsonify({"available": False, "days": [], "attribution": None})
        f, t = date_range_args()
        hdd = weather.heating_degree_days_by_date(conn, f, t)
        if not hdd:
            return jsonify({"available": False, "days": [], "attribution": None})
        typical = weather.typical_heating_degree_days_by_day_of_year(conn)
        days = []
        for d in sorted(hdd):
            ref = typical.get(d[5:])
            days.append(
                {
                    "date": d,
                    "hdd": round(hdd[d], 1),
                    "pct_of_typical": round(hdd[d] / ref * 100, 0) if ref else None,
                }
            )
        return jsonify(
            {
                "available": True,
                "days": days,
                "attribution": weather.ATTRIBUTION,
                "base_temp_c": weather.DEFAULT_HDD_BASE_C,
            }
        )

    @app.route("/api/consumption-notes")
    def api_consumption_notes():
        # Episodes whose usage sat far outside their own recent baseline,
        # annotated with logged occupancy where it exists. Explicitly NOT a
        # health check -- see aggregate.consumption_notes_report.
        #
        # Now honours the selected date range like every other view.
        # This endpoint used to be the sole exception, which is the actual
        # reason it returned 794 notes while the charts beside it showed 90
        # days' worth. Detection still runs over full history -- only the
        # output is scoped; see consumption_notes_report for why that
        # distinction is not cosmetic.
        conn = get_conn()
        f, t = date_range_args()
        return jsonify(aggregate.consumption_notes_report(conn, _acknowledged_set(conn), f, t))

    _ISSUE_TYPES = {
        "gap",
        "outlier_day",
        "negative_delta",
        "glitch_episode",
        "granularity_disagreement",
        "implausible_value",
        "empty_run",
        "reconciliation_mismatch",
        "reconciliation_unverifiable",
    }

    def _acknowledged_set(conn) -> set[tuple[str, str, str]]:
        return {
            (row["category"], row["issue_type"], row["fingerprint"])
            for row in conn.execute("SELECT category, issue_type, fingerprint FROM acknowledged_issues")
        }

    @app.route("/api/data-quality/acknowledge", methods=["POST", "DELETE"])
    def api_data_quality_acknowledge():
        # A user has reviewed a flagged gap/quality finding and
        # confirmed it's not a real problem (most flags turn out to be
        # explainable, not wrong). Never
        # deletes or edits any actual reading, only records that this
        # specific finding was seen -- reversible by DELETEing the same
        # triple (un-acknowledge), and harmless to leave orphaned if the
        # underlying finding later stops recurring on its own.
        data = request.get_json(force=True, silent=True) or {}
        cat, issue_type, fingerprint = data.get("category"), data.get("issue_type"), data.get("fingerprint")
        if cat not in _READING_TABLES or issue_type not in _ISSUE_TYPES or not fingerprint:
            return jsonify({"error": "category, issue_type, and fingerprint are required and must be valid"}), 400
        conn = get_conn()
        if request.method == "POST":
            conn.execute(
                "INSERT OR IGNORE INTO acknowledged_issues (category, issue_type, fingerprint, acknowledged_at) "
                "VALUES (?, ?, ?, ?)",
                (cat, issue_type, fingerprint, datetime.now(_LOCAL_TZ).isoformat()),
            )
        else:
            conn.execute(
                "DELETE FROM acknowledged_issues WHERE category = ? AND issue_type = ? AND fingerprint = ?",
                (cat, issue_type, fingerprint),
            )
        conn.commit()
        return jsonify({"ok": True})

    # Acknowledging hundreds of findings one at a time is not a workflow
    # anybody will complete -- "users are not going to click each button
    # 794 times." Takes the exact set of findings the user can
    # currently see (the frontend sends the filtered+range-scoped visible
    # rows, not "everything"), so it composes with the filters instead of
    # adding a separate notion of scope.
    #
    # Deliberately one statement rather than a loop of single-finding calls:
    # 794 sequential POSTs is not just slow, it can also half-apply, leaving
    # the user with no idea which findings were actually acknowledged.
    # DELETE is the exact inverse, so a mis-click is one action to undo.
    _BULK_ACK_LIMIT = 5000

    @app.route("/api/data-quality/acknowledge-bulk", methods=["POST", "DELETE"])
    def api_data_quality_acknowledge_bulk():
        data = request.get_json(force=True, silent=True) or {}
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return jsonify({"error": "items must be a non-empty list"}), 400
        if len(items) > _BULK_ACK_LIMIT:
            return jsonify({"error": f"at most {_BULK_ACK_LIMIT} items per request"}), 400

        triples = []
        for item in items:
            if not isinstance(item, dict):
                return jsonify({"error": "each item must be an object"}), 400
            cat, issue_type, fingerprint = item.get("category"), item.get("issue_type"), item.get("fingerprint")
            if cat not in _READING_TABLES or issue_type not in _ISSUE_TYPES or not fingerprint:
                return jsonify({"error": "category, issue_type, and fingerprint are required and must be valid"}), 400
            triples.append((cat, issue_type, fingerprint))

        # Validate every item before writing any of them -- a partially
        # applied bulk action is worse than a rejected one.
        conn = get_conn()
        before = conn.total_changes
        if request.method == "POST":
            now = datetime.now(_LOCAL_TZ).isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO acknowledged_issues (category, issue_type, fingerprint, acknowledged_at) "
                "VALUES (?, ?, ?, ?)",
                [(c, i, f, now) for c, i, f in triples],
            )
        else:
            conn.executemany(
                "DELETE FROM acknowledged_issues WHERE category = ? AND issue_type = ? AND fingerprint = ?",
                triples,
            )
        conn.commit()
        # `changed` counts rows actually written, not rows requested -- an
        # INSERT OR IGNORE over already-acknowledged findings is a no-op, and
        # reporting the request size would overstate what happened.
        return jsonify({"ok": True, "requested": len(triples), "changed": conn.total_changes - before})

    # category -> (table, {nullable value columns}). Never interpolate
    # a category or metric name straight from the request into SQL -- both
    # are checked against this allowlist first.
    _READING_TABLES = {
        "power": (
            "power_readings",
            {
                "import_t1_kwh",
                "import_t2_kwh",
                "import_combined_kwh",
                "export_t1_kwh",
                "export_t2_kwh",
                "export_combined_kwh",
                "l1_max_w",
                "l2_max_w",
                "l3_max_w",
            },
        ),
        "gas": ("gas_readings", {"total_gas_m3"}),
        "water": ("water_readings", {"water_usage_dl"}),
        "battery": ("battery_readings", {"import_kwh", "export_kwh", "soc_pct"}),
    }
    _REBUILD_FNS = {
        "power": aggregate.rebuild_power_daily,
        "gas": aggregate.rebuild_gas_daily,
        "water": aggregate.rebuild_water_daily,
        "battery": aggregate.rebuild_battery_daily,
    }

    @app.route("/api/readings/<category>", methods=["DELETE"])
    def api_delete_reading(category: str):
        # Delete-one-bad-raw-reading. Nulls just the one flagged
        # column, never DELETEs the row -- power_readings/battery_readings
        # are multi-column under one (time, granularity) key, so a row
        # DELETE would collaterally destroy sibling metrics (e.g. export)
        # that weren't flagged at all. `value` is an optimistic-lock check:
        # the request must carry the exact value the finding was computed
        # from, so acting on a stale/already-changed finding is rejected
        # rather than silently nulling the wrong thing.
        if category not in _READING_TABLES:
            return jsonify({"error": f"unknown category '{category}'"}), 400
        table, allowed_columns = _READING_TABLES[category]
        data = request.get_json(force=True, silent=True) or {}
        time_str, granularity, metric, value = (
            data.get("time"),
            data.get("granularity"),
            data.get("metric"),
            data.get("value"),
        )
        if metric not in allowed_columns or not time_str or not granularity or value is None:
            return jsonify({"error": "time, granularity, metric, and value are all required"}), 400

        conn = get_conn()
        try:
            row = conn.execute(
                f"SELECT {metric} AS v FROM {table} WHERE time = ? AND granularity = ?", (time_str, granularity)
            ).fetchone()
            if row is None:
                return jsonify({"error": "reading not found"}), 404
            if row["v"] != value:
                return jsonify({"error": "value has changed since this was flagged -- refresh and try again"}), 409
            conn.execute(f"UPDATE {table} SET {metric} = NULL WHERE time = ? AND granularity = ?", (time_str, granularity))
            conn.commit()
            _REBUILD_FNS[category](conn)
            conn.commit()
        except sqlite3.OperationalError:
            # gunicorn runs 2 worker processes -- a real
            # concurrent writer (another delete, or the poller's own
            # rebuild_all) can legitimately hold the write lock long enough
            # to time out. That's a retry-later condition, not a server bug.
            return jsonify({"error": "database busy, try again shortly"}), 503
        return jsonify({"ok": True, "category": category, "time": time_str, "granularity": granularity, "metric": metric})

    @app.route("/api/import/csv", methods=["POST"])
    def api_import_csv():
        if "file" not in request.files or request.files["file"].filename == "":
            return jsonify({"error": "no file provided"}), 400
        upload = request.files["file"]
        filename = Path(upload.filename).name  # strip any path component from the client
        category = ingest.detect_category(filename)
        if category is None:
            return (
                jsonify(
                    {
                        "error": f"unrecognized filename '{filename}' — expected either a "
                        "Bat-/P1e-/P1g-/Water- prefixed HomeWizard export, or a vendor-neutral "
                        "'omnimeter-<power|gas|water|battery>-<name>.csv' file "
                        "(download a template from /api/import/meter-csv/template?category=power)"
                    }
                ),
                400,
            )

        conn = get_conn()
        if not ingest.category_import_enabled(conn, category):
            return jsonify({"error": f"Import disabled in Settings for category '{category}'"}), 403

        DEFAULT_IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
        dest = DEFAULT_IMPORTS_DIR / filename
        upload.save(dest)

        try:
            rows_ingested = ingest.ingest_file(conn, dest)
        except Exception as e:
            # Quarantine, don't leave it in the dropzone: the file is already
            # saved there, and a file that crashes ingest would otherwise be
            # retried (and fail) on every 15-min timer run from now on.
            conn.rollback()
            failed_dir = DEFAULT_IMPORTS_DIR / "failed"
            failed_dir.mkdir(parents=True, exist_ok=True)
            dest.rename(failed_dir / filename)
            return (
                jsonify(
                    {
                        "error": f"could not parse '{filename}' ({type(e).__name__}: {e}) — "
                        "moved to data/imports/failed/, not retried"
                    }
                ),
                400,
            )
        if rows_ingested:
            aggregate.rebuild_all(conn)
        return jsonify({"filename": filename, "category": category, "rows_ingested": rows_ingested})

    @app.route("/api/import/tariff-pdf", methods=["POST"])
    def api_import_tariff_pdf():
        if "file" not in request.files or request.files["file"].filename == "":
            return jsonify({"error": "no file provided"}), 400
        conn = get_conn()
        toggle_row = conn.execute("SELECT pdf_import_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
        if toggle_row and not toggle_row["v"]:
            return jsonify({"error": "Tariff import (PDF/CSV) disabled in Settings"}), 403

        upload = request.files["file"]
        filename = Path(upload.filename).name

        try:
            parsed = tariff_parser.parse_tariff_pdf(upload.stream)
        except Exception as e:
            return jsonify({"error": f"could not parse PDF: {e}"}), 400

        if not parsed["power"] and not parsed["gas"]:
            known = ", ".join(p.name for p in tariff_parser.REGISTRY)
            return (
                jsonify(
                    {"error": f"no rate periods found — recognised formats: {known}. "
                              f"Use the CSV template for any other supplier."}
                ),
                400,
            )

        source = f"Uploaded PDF: {filename} ({parsed['parser']})"
        skipped_overlaps = _apply_rate_periods(conn, parsed, source)
        conn.commit()
        return jsonify(
            {
                "filename": filename,
                "parser": parsed["parser"],
                "power_periods": len(parsed["power"]),
                "gas_periods": len(parsed["gas"]),
                "skipped_overlaps": skipped_overlaps,
            }
        )

    @app.route("/api/import/meter-csv/template")
    def api_meter_csv_template():
        """Template for the vendor-neutral meter-data CSV (see
        ingest.GENERIC_COLUMNS). This is the documented way to get readings
        from a meter this app has no driver for -- the columns ARE the
        database's own canonical names, so nothing brand-specific is
        involved. No auth: a template download writes nothing, same as the
        tariff template above."""
        category = (request.args.get("category") or "power").lower()
        if category not in ingest.GENERIC_COLUMNS:
            return (
                jsonify(
                    {
                        "error": f"unknown category '{category}' -- "
                        f"expected one of: {', '.join(sorted(ingest.GENERIC_COLUMNS))}"
                    }
                ),
                400,
            )
        columns = ingest.GENERIC_COLUMNS[category]
        csv_text = (
            "time," + ",".join(columns) + "\n"
            f"# Save this file as: omnimeter-{category}-<anything>.csv\n"
            "# The 'omnimeter-<category>-' filename prefix is what selects this generic\n"
            "# format. Without it the file is judged against the HomeWizard export format\n"
            "# instead and will be rejected.\n"
            "#\n"
            "# time: 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD 00:00' for daily readings).\n"
            "#   Rows one day apart are stored as daily; closer together as 15-minute data.\n"
            "# Values are CUMULATIVE METER READINGS, not per-period usage -- this app\n"
            "#   derives usage by differencing consecutive readings.\n"
            "#\n"
            "# Include only the columns your meter actually reports; omit the rest\n"
            "#   entirely. An UNKNOWN column name is rejected rather than ignored, so a\n"
            "#   typo is reported instead of silently dropping that data.\n"
            + (
                "# No dual-tariff meter? Use import_combined_kwh/export_combined_kwh alone\n"
                "#   and leave the t1/t2 columns out.\n"
                if category == "power"
                else ""
            )
            + "# The example row below is commented out on purpose -- the unmodified\n"
            "#   template imports nothing, so it is a safe round-trip test.\n"
            "#1998-01-01 00:00," + ",".join("0" for _ in columns) + "\n"
        )
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=omnimeter-{category}-template.csv"},
        )

    @app.route("/api/import/tariff-csv/template")
    def api_tariff_csv_template():
        # The generic fallback for any supplier with no
        # PDF parser (or none at all -- Eneco/Oxxio/Vandebron/Budget Thuis
        # had no downloadable consumer document in the original research).
        # No auth: a template download writes nothing, same as every other
        # unauthenticated GET route.
        csv_text = (
            "category,period_start,period_end,rate\n"
            "# category: power or gas\n"
            "# period_start/period_end: YYYY-MM-DD. Leave period_end blank for an ongoing "
            "(\"effective from\", no known end) rate -- e.g. a prospective rate sheet rather "
            "than a closed historical period.\n"
            "# rate: EUR per kWh for power (e.g. a bill showing €0,245/kWh -> 0.245), "
            "EUR per m3 for gas. Not ct/kWh.\n"
            "# One row per rate period. The three example rows below are commented out on "
            "purpose -- uncomment and edit them, or just add your own rows. Left as-is they "
            "import nothing, so the unmodified template is a safe round-trip test.\n"
            "#power,1998-01-01,1998-06-30,0.245\n"
            "#power,1998-07-01,,0.258\n"
            "#gas,1998-01-01,,1.35\n"
        )
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=omnimeter-tariff-template.csv"},
        )

    @app.route("/api/import/tariff-csv", methods=["POST"])
    def api_import_tariff_csv():
        if "file" not in request.files or request.files["file"].filename == "":
            return jsonify({"error": "no file provided"}), 400
        conn = get_conn()
        toggle_row = conn.execute("SELECT pdf_import_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
        if toggle_row and not toggle_row["v"]:
            return jsonify({"error": "Tariff import (PDF/CSV) disabled in Settings"}), 403

        upload = request.files["file"]
        filename = Path(upload.filename).name

        # Only genuine bad-input conditions become a 400. Anything else is a
        # defect in our own code and must surface as a 500 rather than being
        # reported to the user as "your file is malformed" -- a bug that
        # classifies itself as user error is a bug nobody investigates.
        # (The PDF route above keeps its broad catch on purpose: pdfplumber
        # raises an open-ended set of exceptions on a corrupt or non-PDF
        # upload, all of which really are bad input.)
        try:
            csv_text = upload.stream.read().decode("utf-8-sig")  # -sig: tolerate Excel's BOM
        except UnicodeDecodeError:
            return jsonify({"error": "could not read the file as UTF-8 text — is it really a CSV?"}), 400
        try:
            parsed = tariff_parser.parse_tariff_csv(csv_text)
        except tariff_parser.TariffCsvError as e:
            return jsonify({"error": f"could not parse CSV: {e}"}), 400

        if not parsed["power"] and not parsed["gas"]:
            return jsonify({"error": "no rate periods found — is the file empty, or only comment/example rows?"}), 400

        source = f"Uploaded CSV: {filename}"
        skipped_overlaps = _apply_rate_periods(conn, parsed, source)
        conn.commit()
        return jsonify(
            {
                "filename": filename,
                "power_periods": len(parsed["power"]),
                "gas_periods": len(parsed["gas"]),
                "skipped_overlaps": skipped_overlaps,
            }
        )

    return app
