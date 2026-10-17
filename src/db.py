"""SQLite schema and connection helper for the HomeWizard dashboard.

power_readings.import_combined_kwh / export_combined_kwh (added after the
initial schema, see _migrate): some live sources report a single
combined import/export figure, not split by day/night tariff (T1/T2) the way
the HomeWizard CSV export is. Writing that combined value into
import_t1_kwh/export_t1_kwh caused import_t1_kwh to silently change meaning
(T1-only for 15min-sourced rows vs. T1+T2-combined for 'live'
rows), which produced a one-time phantom delta wherever the
preferred data source crossed from '15min' to 'live' or back -- the two
readings either side of that boundary describe different physical
quantities under the same column name. The combined columns keep that value
in its own place so t1/t2 always mean what they say; aggregate.py sums all
three per-column delta series (t1, t2, combined) when building daily totals,
since a given date's rows are always a single granularity post
filter_preferred_granularity, so at most one of the three is ever non-empty
for any one date.
"""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("/opt/omnimeter/data/omnimeter.db")

# rate_schedule/gas_rate_schedule.period_end for an open-ended ("effective
# from", no known end) rate period -- e.g. a supplier's prospective
# Tarievenblad rather than Vattenfall's retrospective Tarievenspecificatie.
# A real far-future date rather than NULL so every existing period_end
# comparison (rate_for's <=, _overlapping_period's SQL, ORDER BY, MAX())
# keeps working unchanged.
OPEN_ENDED_SENTINEL = "9999-12-31"

SCHEMA = """
CREATE TABLE IF NOT EXISTS power_readings (
    time TEXT NOT NULL,
    import_t1_kwh REAL,
    import_t2_kwh REAL,
    import_combined_kwh REAL,
    export_t1_kwh REAL,
    export_t2_kwh REAL,
    export_combined_kwh REAL,
    l1_max_w REAL,
    l2_max_w REAL,
    l3_max_w REAL,
    granularity TEXT NOT NULL,
    PRIMARY KEY (time, granularity)
);

CREATE TABLE IF NOT EXISTS gas_readings (
    time TEXT NOT NULL,
    total_gas_m3 REAL,
    granularity TEXT NOT NULL,
    PRIMARY KEY (time, granularity)
);

CREATE TABLE IF NOT EXISTS water_readings (
    time TEXT NOT NULL,
    water_usage_dl REAL,
    granularity TEXT NOT NULL,
    PRIMARY KEY (time, granularity)
);

CREATE TABLE IF NOT EXISTS battery_readings (
    time TEXT NOT NULL,
    import_kwh REAL,
    export_kwh REAL,
    soc_pct REAL,
    granularity TEXT NOT NULL,
    PRIMARY KEY (time, granularity)
);

CREATE TABLE IF NOT EXISTS ingested_files (
    filename TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    ingested_at TEXT NOT NULL,
    row_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    buy_ct_per_kwh REAL NOT NULL,
    sell_ct_per_kwh REAL NOT NULL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS gas_rate_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    price_eur_per_m3 REAL NOT NULL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS pv_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kwp_rating REAL,
    installed_date TEXT,
    notes TEXT
);

-- Per-category "1y" preset anchor (dashboard.js rangeParams()). Power/Gas
-- default to 1 May (NL utility billing year); Water defaults to 1 Jan
-- (calendar year). INSERT OR IGNORE below seeds row id=1 with
-- these column defaults on both fresh installs and already-deployed DBs.
CREATE TABLE IF NOT EXISTS fiscal_year_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    power_fy_start_month INTEGER NOT NULL DEFAULT 5,
    power_fy_start_day INTEGER NOT NULL DEFAULT 1,
    gas_fy_start_month INTEGER NOT NULL DEFAULT 5,
    gas_fy_start_day INTEGER NOT NULL DEFAULT 1,
    water_fy_start_month INTEGER NOT NULL DEFAULT 1,
    water_fy_start_day INTEGER NOT NULL DEFAULT 1
);
INSERT OR IGNORE INTO fiscal_year_config (id) VALUES (1);

-- Per-source enable/disable switches, read by app.py (imports),
-- meter_poller.py (local API poller), ingest.py (CSV ingest),
-- and scripts/backup.py (nightly backup). An earlier live-data sync path
-- was removed entirely (its toggle is dropped, see _migrate_drop_ha_toggle
-- below); meter_poller.py's direct local-API poller is now the
-- sole live-data source.
-- show_*_tab are UI-visibility switches, deliberately separate in
-- meaning from the ingest toggles above even though they share this table
-- (same singleton-row precedent) -- not everyone running OmniMeter has a
-- gas/water/battery device or solar panels (matters most for a self-hosted
-- fork), and these let a user hide an empty/inapplicable tab rather
-- than see it perpetually blank. Power has no such toggle: a P1 meter is the
-- one device every OmniMeter install has by definition. Default 1 (visible)
-- so an existing dashboard with all categories configured is unaffected.
CREATE TABLE IF NOT EXISTS feature_toggles (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    homewizard_api_enabled INTEGER NOT NULL DEFAULT 1,
    import_power_enabled INTEGER NOT NULL DEFAULT 1,
    import_gas_enabled INTEGER NOT NULL DEFAULT 1,
    import_water_enabled INTEGER NOT NULL DEFAULT 1,
    pdf_import_enabled INTEGER NOT NULL DEFAULT 1,
    nightly_backup_enabled INTEGER NOT NULL DEFAULT 1,
    show_gas_tab INTEGER NOT NULL DEFAULT 1,
    show_water_tab INTEGER NOT NULL DEFAULT 1,
    show_battery_tab INTEGER NOT NULL DEFAULT 1,
    show_sufficiency_tab INTEGER NOT NULL DEFAULT 1,
    show_solar_tab INTEGER NOT NULL DEFAULT 1,
    -- OmniMeter's first outbound internet call. Defaults
    -- to 0 so a self-hosting user opts in knowingly rather than
    -- discovering afterwards that the box talks to the internet and that
    -- the request carries their coordinates.
    weather_enabled INTEGER NOT NULL DEFAULT 0,
    -- OmniMeter's second outbound internet call (a GitHub releases lookup,
    -- see src/update_check.py) -- same opt-in reasoning as weather_enabled
    -- above, defaults to 0 for the same reason.
    update_check_enabled INTEGER NOT NULL DEFAULT 0,
    -- SolarEdge cloud poller (src/solaredge_ingest.py). Opt-in like the
    -- other outbound-internet features: an already-deployed DB must not start
    -- calling SolarEdge on upgrade.
    solaredge_enabled INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO feature_toggles (id) VALUES (1);

-- SolarEdge Monitoring API V2. `solaredge_overview` holds one
-- snapshot per poll of the site's cumulative-since-local-midnight totals, so a
-- day's figure is exact whenever it is read and the interval only sets the
-- intraday resolution. `solaredge_power` holds the 15-minute PV production
-- buckets (watts) the time-series endpoint returns, keyed by bucket start in
-- UTC so a re-fetch of an overlapping window is an idempotent upsert.
-- Consumption columns are NULL on a site with no consumption meter.
CREATE TABLE IF NOT EXISTS solaredge_overview (
    time TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    production_wh REAL,
    to_self_wh REAL,
    to_storage_wh REAL,
    to_grid_wh REAL,
    consumption_wh REAL,
    from_pv_wh REAL,
    from_storage_wh REAL,
    from_grid_wh REAL
);

CREATE TABLE IF NOT EXISTS solaredge_power (
    time TEXT PRIMARY KEY,
    power_w REAL NOT NULL
);

-- Authoritative daily PV production from SolarEdge's /energy (DAY resolution), keyed by the
-- site-local date. Filled once by scripts/solaredge_backfill.py (history back to the install
-- date) and then topped up by the poller (one call per day finalises yesterday). Today is NOT
-- here: it is only known as the latest cumulative `solaredge_overview` snapshot.
CREATE TABLE IF NOT EXISTS solaredge_daily (
    date TEXT PRIMARY KEY,
    production_wh REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

-- One row per poller run (any outcome), so job health and the monthly API-call
-- tally can be shown. SolarEdge exposes no credit-balance header, so the
-- balance is estimated from `calls` (every HTTP request the client sent).
-- status: ok | skipped_night | auth_error | rate_limited | api_error
CREATE TABLE IF NOT EXISTS solaredge_runs (
    time TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    detail TEXT  -- JSON {endpoint: calls}; NULL on rows written before this column existed
);

-- One row per site-local day on which the daily top-up already asked SolarEdge to fill a gap. A day with no
-- value (the inverter was offline from the cloud) stays absent from solaredge_daily, so without this the
-- top-up would re-ask on EVERY 30-minute run -- one wasted credit each -- until data finally arrived.
CREATE TABLE IF NOT EXISTS solaredge_gap_attempts (
    date TEXT PRIMARY KEY,
    attempted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS power_daily (
    date TEXT PRIMARY KEY,
    import_kwh REAL,
    export_kwh REAL,
    net_kwh REAL,
    l1_max_w REAL,
    l2_max_w REAL,
    l3_max_w REAL
);

CREATE TABLE IF NOT EXISTS gas_daily (
    date TEXT PRIMARY KEY,
    usage_m3 REAL
);

CREATE TABLE IF NOT EXISTS water_daily (
    date TEXT PRIMARY KEY,
    usage_l REAL
);

CREATE TABLE IF NOT EXISTS battery_daily (
    date TEXT PRIMARY KEY,
    charge_kwh REAL,
    discharge_kwh REAL,
    min_soc_pct REAL,
    max_soc_pct REAL,
    avg_soc_pct REAL,
    eod_soc_pct REAL
);

-- Headcount per date/time range, manually logged by the user (time
-- precision added in a follow-up change). occupant_count is the TOTAL people in
-- the house for [date_from, date_to], not an incremental add-on, so entries
-- must never overlap (enforced in app.py the same way as
-- rate_schedule/gas_rate_schedule). Solo = 1. date_from/date_to are naive
-- local-timezone strings (see src/localtime.py), "YYYY-MM-DD HH:MM" (same convention as
-- power_readings.time) -- not date-only, so a departure/return that doesn't
-- align with midnight (e.g. "left 8am, back 6pm") can be logged as its own
-- entry rather than forcing a whole day to one guessed state. Used to
-- correlate consumption with who's actually home.
CREATE TABLE IF NOT EXISTS occupancy_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    occupant_count INTEGER NOT NULL,
    notes TEXT
);

-- A user has reviewed a flagged gap/quality finding and confirmed
-- it's not actually a problem (see aggregate.data_quality_report) -- kept
-- as its own table, not a column on the readings/daily tables, since a
-- finding is a derived/recomputed-every-call fact, not a stored one; this
-- table is the only part of it that's actually persisted.
CREATE TABLE IF NOT EXISTS acknowledged_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    UNIQUE (category, issue_type, fingerprint)
);

-- Daily weather, cached once per date and kept forever
-- (history does not change). Fetched from Open-Meteo's archive API, whose
-- data is CC BY 4.0 -- any UI that DISPLAYS these values must carry the
-- credit "Weather data by Open-Meteo.com", cached values included.
--
-- shortwave_radiation_sum (MJ/m2) is the reason this table exists: it is
-- very nearly proportional to PV output, and estimate_daily_production()
-- currently has no weather input at all, returning the identical figure for
-- every day of a month (measured: 16.30 kWh for both a dull and a bright
-- July day). temperature_2m_* is stored in the same call at no extra cost --
-- heating-degree-days explain gas far better than any threshold could.
--
-- `source` records which endpoint the row came from. The archive and
-- forecast endpoints were measured to disagree by up to 60% on the same
-- date, so they must never be mixed silently; `fetched_at` exists so a
-- recent date can be re-fetched later and any revision detected rather than
-- assumed (whether the archive revises recent days is deliberately left as
-- an observation, not a design assumption).
CREATE TABLE IF NOT EXISTS weather_daily (
    date TEXT PRIMARY KEY,
    shortwave_radiation_sum REAL,
    sunshine_duration_s REAL,
    temperature_2m_max REAL,
    temperature_2m_min REAL,
    temperature_2m_mean REAL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Layer 1 runaway detector -- in-progress continuous-run
-- state, one row per metric. Upserted every poll from meter_poller.py;
-- survives a restart so a leak mid-run doesn't silently restart its
-- counter (see runaway_detector.check_water). last_meter_value is the
-- cumulative reading last seen, needed to compute the next delta across a
-- restart without re-reading raw history.
CREATE TABLE IF NOT EXISTS detector_state (
    metric           TEXT PRIMARY KEY,   -- 'water' (gas/power in later phases)
    run_started_at   TEXT,               -- NULL when idle
    run_last_at      TEXT,               -- last non-idle sample in the run
    run_volume       REAL NOT NULL DEFAULT 0,
    last_meter_value REAL
);

-- Anything that crossed a runaway threshold. One row per continuous-run
-- episode, identified by (kind, metric, started_at) -- it can't reuse
-- acknowledged_issues' daily-episode
-- fingerprint. threshold_used/occupancy_state are stored so a past alert
-- can be audited or the threshold retuned later, since both change over
-- time. From 1.14.0 threshold_used holds whichever gate's operative value
-- actually fired the episode (litres for G1, L/min for G2/G3, hours for
-- the Tier 2 gates), so it is only interpretable together with fired_via
-- -- which is also what record_dismissal() nudges.
CREATE TABLE IF NOT EXISTS alert_episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,      -- 'runaway' (health/context in later layers)
    metric           TEXT NOT NULL,
    started_at       TEXT NOT NULL,      -- first non-idle poll of the run
    last_seen_at     TEXT NOT NULL,
    ended_at         TEXT,               -- NULL while still active
    peak_rate        REAL,
    total_volume     REAL,
    unit             TEXT NOT NULL,      -- 'L' | 'm3' | 'kWh'
    threshold_used   REAL NOT NULL,      -- what actually fired it
    occupancy_state  TEXT,               -- 'home' | 'away' | 'unknown'
    fired_via        TEXT,               -- 'G1'|'G2'|'G3'|'T2-night'|'T2-away'; NULL on pre-1.14 rows
    alert_count      INTEGER NOT NULL DEFAULT 0,
    last_alerted_at  TEXT,
    notify_status    TEXT,               -- 'sent' | 'failed' | 'disabled'
    notify_error     TEXT,
    acknowledged_at  TEXT,
    UNIQUE (kind, metric, started_at)
);

-- Telegram chat ID is not a secret (unlike the bot token, which stays in
-- the environment -- see telegram_notifier.py), so it lives here and is
-- editable in Settings, overriding the OMNIMETER_TELEGRAM_CHAT_ID env
-- default. Singleton-row pattern, same as pv_config.
CREATE TABLE IF NOT EXISTS telegram_config (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    chat_id TEXT
);

-- Per-metric runaway thresholds, editable in Settings -- deliberately a DB
-- value, not a Python constant, matching this app's standing principle
-- that every such number is relative to the household's own history
-- rather than a hardcoded default (see aggregate.py's outlier-detector
-- constants). 250/10/2 are the defaults for a fresh install with no
-- history; an already-tuned household overrides these via the Settings
-- UI, not by editing code. home_threshold_l seeds at G1's designed value
-- rather than the 150 L this shipped with before 1.14.0: G1 is only the
-- backstop now -- the rate gates below do the actual discriminating -- and
-- a volume gate low enough to trip on a single bath is exactly the false
-- positive the three-signal design exists to remove.
CREATE TABLE IF NOT EXISTS runaway_detector_config (
    metric           TEXT PRIMARY KEY,
    home_threshold_l REAL NOT NULL,          -- G1: run volume, home mode
    away_threshold_l REAL NOT NULL,          -- Tier 1 volume gate, away mode
    away_after_days  INTEGER NOT NULL,
    -- Tier 1 rate gates. A run is described by three signals -- duration,
    -- volume and the trailing-5-minute mean rate -- OR'd together, because
    -- a bath and a burst pipe share a flow rate and are separated only by
    -- how long each one keeps it up.
    g2_min_minutes    INTEGER NOT NULL DEFAULT 15,  -- fixture left running
    g2_rate_l_min     REAL    NOT NULL DEFAULT 10.0,
    g3_min_minutes    INTEGER NOT NULL DEFAULT 5,   -- burst pipe / failed valve
    g3_rate_l_min     REAL    NOT NULL DEFAULT 20.0,
    -- Tier 2 background-leak gates, in 15-minute buckets.
    t2_night_hours    REAL    NOT NULL DEFAULT 2.5,
    t2_night_start    INTEGER NOT NULL DEFAULT 1,   -- local hour, inclusive
    t2_night_end      INTEGER NOT NULL DEFAULT 6,   -- local hour, exclusive
    t2_away_hours     REAL    NOT NULL DEFAULT 2.0,
    t2_zero_tolerance INTEGER NOT NULL DEFAULT 1    -- idle buckets bridged
);
INSERT OR IGNORE INTO runaway_detector_config (metric, home_threshold_l, away_threshold_l, away_after_days)
VALUES ('water', 250.0, 10.0, 2);

-- Layer 2 context-aware model -- one fitted ridge-regression
-- model per metric, fully replaced on every nightly retrain (src/
-- anomaly_model.py: train_model). feature_names/feature_means/feature_stds/
-- coefficients are JSON arrays, all in the same fixed order, so prediction
-- never has to guess it back from a dict. residual_threshold is the Nth
-- percentile (RESIDUAL_PERCENTILE) of that fit's own training residuals, in
-- the metric's native unit -- an absolute number, so anomaly detection at
-- read time only ever needs one comparison, not a re-derivation.
CREATE TABLE IF NOT EXISTS anomaly_model_state (
    metric             TEXT PRIMARY KEY,
    trained_at         TEXT NOT NULL,
    training_rows      INTEGER NOT NULL,
    feature_names      TEXT NOT NULL,
    feature_means      TEXT NOT NULL,
    feature_stds       TEXT NOT NULL,
    intercept          REAL NOT NULL,
    coefficients       TEXT NOT NULL,
    residual_threshold REAL NOT NULL
);
"""


def resolve_db_path() -> Path:
    env_path = os.environ.get("OMNIMETER_DB_PATH")
    return Path(env_path) if env_path else DEFAULT_DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout: the web workers and the two ingest timers share this file; a
    # rollup rebuild holds the write lock for seconds, so waiting beats
    # surfacing "database is locked" to whoever else touches it meanwhile.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the web app keep reading while a rebuild transaction writes.
    # Persistent in the DB file, but cheap and idempotent to re-issue.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds columns to already-deployed databases that predate them.
    CREATE TABLE IF NOT EXISTS only helps fresh installs -- tables that
    already exist on a deployed DB need an explicit, idempotent ALTER TABLE."""
    added_columns = {
        "power_readings": ("import_combined_kwh", "export_combined_kwh"),
        "battery_daily": ("eod_soc_pct",),
    }
    for table, columns in added_columns.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col in columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")


def _migrate_occupancy_datetime(conn: sqlite3.Connection) -> None:
    """occupancy_log predates time-of-day precision -- date_from/date_to were
    plain 'YYYY-MM-DD' (10 chars). Rewrites any row still in that shape to
    '{date} 00:00'/'{date} 23:59', preserving the exact same whole-day
    coverage every already-logged entry had, so historical data keeps
    meaning what it always meant. Idempotent: a row
    already carrying a time component (16 chars) is left untouched."""
    conn.execute(
        "UPDATE occupancy_log SET date_from = date_from || ' 00:00' WHERE length(date_from) = 10"
    )
    conn.execute("UPDATE occupancy_log SET date_to = date_to || ' 23:59' WHERE length(date_to) = 10")


# power_readings/battery_readings/gas_readings used to have a bare `time`
# PRIMARY KEY, so a 'live' row and a CSV row at the same timestamp
# silently overwrote each other via INSERT OR REPLACE -- whichever source
# wrote second destroyed the other's raw data with no way to recover it.
# water_readings was excluded at the time: the earlier live source had no
# water sensor, so it only ever had one source. The
# direct-local-API poller (meter_ingest.py) is about to become
# water's second source (granularity='api_live', alongside CSV-sourced
# '15min'/'daily' rows) -- migrated here too now, before that second source
# exists, to avoid reintroducing the exact clobbering bug this dict already
# fixed for the other three tables. ingest.py already writes both columns to
# its INSERT statements -- INSERT OR REPLACE naturally starts matching on the
# new composite key with no code change on the write side.
_COMPOSITE_PK_COLUMNS = {
    "power_readings": (
        "time", "import_t1_kwh", "import_t2_kwh", "import_combined_kwh",
        "export_t1_kwh", "export_t2_kwh", "export_combined_kwh",
        "l1_max_w", "l2_max_w", "l3_max_w", "granularity",
    ),
    "battery_readings": ("time", "import_kwh", "export_kwh", "soc_pct", "granularity"),
    "gas_readings": ("time", "total_gas_m3", "granularity"),
    "water_readings": ("time", "water_usage_dl", "granularity"),
}


def _has_composite_time_granularity_pk(conn: sqlite3.Connection, table: str) -> bool:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pk_cols = [row["name"] for row in sorted((r for r in info if r["pk"] > 0), key=lambda r: r["pk"])]
    return pk_cols == ["time", "granularity"]


def _migrate_composite_pk(conn: sqlite3.Connection) -> None:
    """Rebuilds power_readings/battery_readings/gas_readings under the
    composite (time, granularity) PK from SCHEMA, for DBs created before
    this change. SQLite can't ALTER a PRIMARY KEY in place, so each table is
    recreated: copy every row into a same-shape table with the new PK, drop
    the old one, rename the new one in. Existing data has at most one row
    per timestamp today (that's the bug this fixes), so the copy can never
    violate the new composite key -- there is nothing to deduplicate.
    Idempotent: a table already on the composite PK is left untouched."""
    for table, columns in _COMPOSITE_PK_COLUMNS.items():
        if _has_composite_time_granularity_pk(conn, table):
            continue
        col_defs = ", ".join(f"{c} TEXT NOT NULL" if c in ("time", "granularity") else f"{c} REAL" for c in columns)
        col_list = ", ".join(columns)
        tmp = f"{table}_pk_migration"
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")
            conn.execute(f"CREATE TABLE {tmp} ({col_defs}, PRIMARY KEY (time, granularity))")
            conn.execute(f"INSERT INTO {tmp} ({col_list}) SELECT {col_list} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _migrate_drop_ha_toggle(conn: sqlite3.Connection) -> None:
    """The earlier live-data sync was removed entirely -- feature_toggles.ha_api_enabled is
    dead on any DB created before this change. DROP COLUMN needs SQLite
    3.35+; idempotent via the same PRAGMA table_info
    check _migrate() uses."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(feature_toggles)")}
    if "ha_api_enabled" in existing:
        conn.execute("ALTER TABLE feature_toggles DROP COLUMN ha_api_enabled")


def _migrate_add_visibility_toggles(conn: sqlite3.Connection) -> None:
    """show_*_tab columns are new -- add them to any DB created
    before this change. Not folded into _migrate()'s added_columns dict:
    that helper hardcodes REAL as the column type, these need
    INTEGER NOT NULL DEFAULT 1. Default 1 (visible) preserves current
    behavior exactly for an already-deployed DB, same reasoning as the
    table's own DEFAULT."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(feature_toggles)")}
    for col in ("show_gas_tab", "show_water_tab", "show_battery_tab", "show_sufficiency_tab", "show_solar_tab"):
        if col not in existing:
            conn.execute(f"ALTER TABLE feature_toggles ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
    # Default 0, not 1 -- see the column comment in SCHEMA. An
    # already-deployed DB must not start making outbound calls on upgrade.
    if "weather_enabled" not in existing:
        conn.execute("ALTER TABLE feature_toggles ADD COLUMN weather_enabled INTEGER NOT NULL DEFAULT 0")


def _migrate_add_update_check_toggle(conn: sqlite3.Connection) -> None:
    """update_check_enabled is new -- add it to any DB created before this
    change. Own function rather than folded into
    _migrate_add_visibility_toggles above: that one is about show_*_tab
    columns (weather_enabled riding along there was already a slight
    mismatch); this is a second, unrelated opt-in outbound-call toggle, not
    a visibility switch. Default 0, same opt-in reasoning as weather_enabled
    -- an already-deployed DB must not start making outbound calls on
    upgrade."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(feature_toggles)")}
    if "update_check_enabled" not in existing:
        conn.execute("ALTER TABLE feature_toggles ADD COLUMN update_check_enabled INTEGER NOT NULL DEFAULT 0")


def _migrate_add_anomaly_toggle(conn: sqlite3.Connection) -> None:
    """anomaly_alerts_enabled is new -- add it to any DB created before this
    change (Layer 1). Own function, same reasoning as
    _migrate_add_update_check_toggle: an unrelated opt-in switch, not a
    visibility column. Default 0 -- an already-deployed DB must not start
    sending Telegram messages on upgrade."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(feature_toggles)")}
    if "anomaly_alerts_enabled" not in existing:
        conn.execute("ALTER TABLE feature_toggles ADD COLUMN anomaly_alerts_enabled INTEGER NOT NULL DEFAULT 0")


def _migrate_add_solaredge_toggle(conn: sqlite3.Connection) -> None:
    """solaredge_enabled is new -- add it to any DB created before the SolarEdge
    poller. Default 0: an already-deployed DB must not start calling out."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(feature_toggles)")}
    if "solaredge_enabled" not in existing:
        conn.execute("ALTER TABLE feature_toggles ADD COLUMN solaredge_enabled INTEGER NOT NULL DEFAULT 0")


def _migrate_add_solaredge_run_detail(conn: sqlite3.Connection) -> None:
    """solaredge_runs.detail (per-endpoint call counts) is new. Nullable, no default: rows written
    before it existed simply have no breakdown, and saying otherwise would be a guess."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(solaredge_runs)")}
    if existing and "detail" not in existing:
        _add_column_once(conn, "ALTER TABLE solaredge_runs ADD COLUMN detail TEXT")


def _add_column_once(conn: sqlite3.Connection, ddl: str) -> None:
    """Run one ADD COLUMN, tolerating the process that got there first.

    Every migration here is check-then-act: read PRAGMA table_info, then
    ALTER if the column is missing. That is not atomic across processes, and
    this app starts several at once -- gunicorn runs two workers, each
    calling init_db() from create_app(), and pull-deploy.sh restarts
    omnimeter-web and omnimeter-api-ingest back to back. get_connection()
    sets timeout=30, so the loser of the race does not fail on the lock; it
    waits, then runs its ALTER against a table that now already has the
    column and raises "duplicate column name".

    That was harmless while no migration emitted real DDL at startup -- the
    condition app.py's own init_db() comment relied on. 1.14.0 ends it: ten
    new columns actually get added on the first restart after deploy.
    """
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _migrate_add_fired_via(conn: sqlite3.Connection) -> None:
    """fired_via records which gate opened an episode (1.14.0). Own function
    rather than folded into _migrate()'s added_columns dict: that helper
    hardcodes REAL as the column type and this one is TEXT. Deliberately
    nullable with no default -- an episode written before the multi-gate
    detector existed was fired by the single volume threshold, but saying so
    retroactively would be a guess dressed up as a record, so those rows
    stay NULL and every consumer treats NULL as "legacy volume gate"."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(alert_episodes)")}
    if "fired_via" not in existing:
        _add_column_once(conn, "ALTER TABLE alert_episodes ADD COLUMN fired_via TEXT")


def _migrate_add_gate_config(conn: sqlite3.Connection) -> None:
    """The Tier 1 rate gates and the Tier 2 background-leak gates are new
    columns on runaway_detector_config (1.14.0). Mixed INTEGER/REAL types
    and every one NOT NULL DEFAULT, so an already-deployed row silently
    acquires the validated defaults rather than a NULL the detector would
    then have to defend against on every poll. home_threshold_l keeps its
    name and becomes G1's volume gate -- no data migration, an installation
    that already tuned it keeps its tuning."""
    defaults = (
        ("g2_min_minutes", "INTEGER", "15"),
        ("g2_rate_l_min", "REAL", "10.0"),
        ("g3_min_minutes", "INTEGER", "5"),
        ("g3_rate_l_min", "REAL", "20.0"),
        ("t2_night_hours", "REAL", "2.5"),
        ("t2_night_start", "INTEGER", "1"),
        ("t2_night_end", "INTEGER", "6"),
        ("t2_away_hours", "REAL", "2.0"),
        ("t2_zero_tolerance", "INTEGER", "1"),
    )
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(runaway_detector_config)")}
    for col, sql_type, default in defaults:
        if col not in existing:
            _add_column_once(
                conn,
                f"ALTER TABLE runaway_detector_config ADD COLUMN {col} {sql_type} NOT NULL DEFAULT {default}",
            )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _migrate_composite_pk(conn)
    _migrate_occupancy_datetime(conn)
    _migrate_drop_ha_toggle(conn)
    _migrate_add_visibility_toggles(conn)
    _migrate_add_update_check_toggle(conn)
    _migrate_add_anomaly_toggle(conn)
    _migrate_add_solaredge_toggle(conn)
    _migrate_add_solaredge_run_detail(conn)
    _migrate_add_fired_via(conn)
    _migrate_add_gate_config(conn)
    conn.commit()
