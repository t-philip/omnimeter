"""SQLite schema and connection helper for the HomeWizard dashboard.

power_readings.import_combined_kwh / export_combined_kwh (added after the
initial schema, see _migrate): Home Assistant's P1 sensors report a single
combined import/export figure, not split by day/night tariff (T1/T2) the way
the HomeWizard CSV export is. Writing that combined value into
import_t1_kwh/export_t1_kwh caused import_t1_kwh to silently change meaning
(T1-only for 15min-sourced rows vs. T1+T2-combined for HA-sourced 'live'
rows), which produced a one-time ~10,373 kWh phantom delta wherever the
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
-- and scripts/backup.py (nightly backup). A later change removed the
-- HA-sync path entirely (ha_api_enabled dropped, see _migrate_drop_ha_toggle
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
    -- OmniMeter's first outbound internet call. Defaults
    -- to 0 so a self-hosting user opts in knowingly rather than
    -- discovering afterwards that the box talks to the internet and that
    -- the request carries their coordinates.
    weather_enabled INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO feature_toggles (id) VALUES (1);

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
    coverage every already-logged entry had, so historical data (e.g.
    "guest stay") keeps meaning what it always meant. Idempotent: a row
    already carrying a time component (16 chars) is left untouched."""
    conn.execute(
        "UPDATE occupancy_log SET date_from = date_from || ' 00:00' WHERE length(date_from) = 10"
    )
    conn.execute("UPDATE occupancy_log SET date_to = date_to || ' 23:59' WHERE length(date_to) = 10")


# power_readings/battery_readings/gas_readings used to have a bare `time`
# PRIMARY KEY, so an HA 'live' row and a CSV row at the same timestamp
# silently overwrote each other via INSERT OR REPLACE -- whichever source
# wrote second destroyed the other's raw data with no way to recover it.
# water_readings was excluded at the time: HA had no water sensor wired in
# (the now-removed ha_ingest.py's ENTITY_ID_MAP), so it only
# ever had one source. The
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
    """HA sync removed entirely -- feature_toggles.ha_api_enabled is
    dead on any DB created before this change. DROP COLUMN needs SQLite
    3.35+ (the reference deployment runs 3.45.1); idempotent via the same PRAGMA table_info
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
    for col in ("show_gas_tab", "show_water_tab", "show_battery_tab", "show_sufficiency_tab"):
        if col not in existing:
            conn.execute(f"ALTER TABLE feature_toggles ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
    # Default 0, not 1 -- see the column comment in SCHEMA. An
    # already-deployed DB must not start making outbound calls on upgrade.
    if "weather_enabled" not in existing:
        conn.execute("ALTER TABLE feature_toggles ADD COLUMN weather_enabled INTEGER NOT NULL DEFAULT 0")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _migrate_composite_pk(conn)
    _migrate_occupancy_datetime(conn)
    _migrate_drop_ha_toggle(conn)
    _migrate_add_visibility_toggles(conn)
    conn.commit()
