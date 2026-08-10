import sqlite3

import pytest

from src import db


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _pk_columns(conn, table):
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in sorted((r for r in info if r["pk"] > 0), key=lambda r: r["pk"])]


class TestFreshSchemaCompositePk:
    def test_power_battery_gas_water_get_composite_pk(self):
        conn = _conn()
        db.init_db(conn)
        assert _pk_columns(conn, "power_readings") == ["time", "granularity"]
        assert _pk_columns(conn, "battery_readings") == ["time", "granularity"]
        assert _pk_columns(conn, "gas_readings") == ["time", "granularity"]
        # water_readings used to keep a bare `time` PK -- HA has no water
        # sensor wired in, so it only ever had one source. The direct
        # local-API poller is about to become a second source (granularity
        # 'api_live' alongside CSV-sourced '15min'/'daily'), so it now gets
        # the same composite PK as the other three tables.
        assert _pk_columns(conn, "water_readings") == ["time", "granularity"]

    def test_two_granularities_at_same_timestamp_both_survive(self):
        conn = _conn()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, granularity) VALUES ('2026-07-01 00:00', 1.0, '15min')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES ('2026-07-01 00:00', 2.0, 'live')"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT granularity FROM power_readings WHERE time = '2026-07-01 00:00' ORDER BY granularity"
        ).fetchall()
        assert [r["granularity"] for r in rows] == ["15min", "live"]

    def test_same_time_and_granularity_still_replaces(self):
        # INSERT OR REPLACE conflict resolution must still work within one
        # source -- only the (time, granularity) pair is now the identity,
        # not (time) alone.
        conn = _conn()
        db.init_db(conn)
        conn.execute(
            "INSERT OR REPLACE INTO power_readings (time, import_t1_kwh, granularity) "
            "VALUES ('2026-07-01 00:00', 1.0, '15min')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO power_readings (time, import_t1_kwh, granularity) "
            "VALUES ('2026-07-01 00:00', 5.0, '15min')"
        )
        conn.commit()
        rows = conn.execute("SELECT import_t1_kwh FROM power_readings WHERE time = '2026-07-01 00:00'").fetchall()
        assert len(rows) == 1
        assert rows[0]["import_t1_kwh"] == pytest.approx(5.0)


class TestOccupancyLogSchema:
    # A brand-new table needs nothing beyond CREATE TABLE IF NOT EXISTS in
    # SCHEMA (every deployed DB is equally missing it) -- no _migrate()/
    # _migrate_composite_pk() entry, unlike the tables above. This just
    # proves init_db() creates it with the expected shape.
    def test_occupancy_log_created_with_expected_columns(self):
        conn = _conn()
        db.init_db(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(occupancy_log)")}
        assert columns == {"id", "date_from", "date_to", "occupant_count", "notes"}
        assert _pk_columns(conn, "occupancy_log") == ["id"]

    def test_occupancy_log_autoincrement_id(self):
        conn = _conn()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-10 00:00', '2026-07-15 23:59', 3)"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM occupancy_log").fetchone()
        assert row["id"] == 1
        assert row["occupant_count"] == 3
        assert row["notes"] is None


class TestOccupancyDatetimeMigration:
    # Simulates a pre-change row logged back when date_from/date_to were
    # date-only (e.g. "guest stay", 2026-07-06 to 2026-07-20) -- inserted
    # directly, bypassing init_db()'s own migration pass, the same way
    # TestCompositePkMigration builds a pre-migration table.
    def _conn_with_date_only_row(self):
        conn = _conn()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count, notes) "
            "VALUES ('2026-07-06', '2026-07-20', 2, 'guest stay')"
        )
        conn.commit()
        return conn

    def test_date_only_row_gets_time_appended(self):
        conn = self._conn_with_date_only_row()
        db._migrate_occupancy_datetime(conn)
        conn.commit()

        row = conn.execute("SELECT * FROM occupancy_log").fetchone()
        assert row["date_from"] == "2026-07-06 00:00"
        assert row["date_to"] == "2026-07-20 23:59"
        assert row["occupant_count"] == 2
        assert row["notes"] == "guest stay"

    def test_row_already_carrying_time_is_untouched(self):
        conn = _conn()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-10 08:00', '2026-07-10 18:00', 0)"
        )
        conn.commit()
        db._migrate_occupancy_datetime(conn)
        conn.commit()

        row = conn.execute("SELECT * FROM occupancy_log").fetchone()
        assert row["date_from"] == "2026-07-10 08:00"
        assert row["date_to"] == "2026-07-10 18:00"

    def test_migration_is_idempotent(self):
        conn = self._conn_with_date_only_row()
        db._migrate_occupancy_datetime(conn)
        db._migrate_occupancy_datetime(conn)  # must not append a second time
        conn.commit()

        row = conn.execute("SELECT * FROM occupancy_log").fetchone()
        assert row["date_from"] == "2026-07-06 00:00"
        assert row["date_to"] == "2026-07-20 23:59"

    def test_init_db_runs_the_migration_automatically(self):
        # init_db() calls _migrate_occupancy_datetime() itself -- a caller
        # shouldn't need to remember to invoke it separately, same as every
        # other migration step.
        conn = self._conn_with_date_only_row()
        db.init_db(conn)

        row = conn.execute("SELECT * FROM occupancy_log").fetchone()
        assert row["date_from"] == "2026-07-06 00:00"
        assert row["date_to"] == "2026-07-20 23:59"


class TestCompositePkMigration:
    def _old_schema_conn_with_data(self):
        """Simulates a pre-migration DB: bare `time` PRIMARY KEY, one row
        already present (the old schema physically can't hold more than one
        row per timestamp, which is exactly the bug being fixed)."""
        conn = _conn()
        conn.executescript(
            """
            CREATE TABLE power_readings (
                time TEXT PRIMARY KEY,
                import_t1_kwh REAL,
                import_t2_kwh REAL,
                import_combined_kwh REAL,
                export_t1_kwh REAL,
                export_t2_kwh REAL,
                export_combined_kwh REAL,
                l1_max_w REAL,
                l2_max_w REAL,
                l3_max_w REAL,
                granularity TEXT NOT NULL
            );
            CREATE TABLE battery_readings (
                time TEXT PRIMARY KEY,
                import_kwh REAL,
                export_kwh REAL,
                soc_pct REAL,
                granularity TEXT NOT NULL
            );
            CREATE TABLE gas_readings (
                time TEXT PRIMARY KEY,
                total_gas_m3 REAL,
                granularity TEXT NOT NULL
            );
            CREATE TABLE water_readings (
                time TEXT PRIMARY KEY,
                water_usage_dl REAL,
                granularity TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, granularity) VALUES ('2026-06-01 00:00', 100.0, '15min')"
        )
        conn.execute(
            "INSERT INTO battery_readings (time, import_kwh, export_kwh, soc_pct, granularity) "
            "VALUES ('2026-06-01 00:00', 1.0, 0.0, 55.0, '15min')"
        )
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-06-01 00:00', 10.0, '15min')"
        )
        conn.execute(
            "INSERT INTO water_readings (time, water_usage_dl, granularity) VALUES ('2026-06-01 00:00', 5.0, '15min')"
        )
        conn.commit()
        return conn

    def test_existing_data_preserved_across_migration(self):
        conn = self._old_schema_conn_with_data()
        db._migrate_composite_pk(conn)

        power = conn.execute("SELECT * FROM power_readings").fetchone()
        assert power["import_t1_kwh"] == pytest.approx(100.0)
        battery = conn.execute("SELECT * FROM battery_readings").fetchone()
        assert battery["soc_pct"] == pytest.approx(55.0)
        gas = conn.execute("SELECT * FROM gas_readings").fetchone()
        assert gas["total_gas_m3"] == pytest.approx(10.0)
        water = conn.execute("SELECT * FROM water_readings").fetchone()
        assert water["water_usage_dl"] == pytest.approx(5.0)

    def test_migrated_tables_all_get_composite_pk(self):
        conn = self._old_schema_conn_with_data()
        db._migrate_composite_pk(conn)

        assert _pk_columns(conn, "power_readings") == ["time", "granularity"]
        assert _pk_columns(conn, "battery_readings") == ["time", "granularity"]
        assert _pk_columns(conn, "gas_readings") == ["time", "granularity"]
        assert _pk_columns(conn, "water_readings") == ["time", "granularity"]

    def test_migration_unblocks_second_granularity_at_same_timestamp(self):
        # Before the fix, this second INSERT would have silently destroyed
        # the first row (same `time`, different `granularity`).
        conn = self._old_schema_conn_with_data()
        db._migrate_composite_pk(conn)

        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, granularity) "
            "VALUES ('2026-06-01 00:00', 999.0, 'live')"
        )
        conn.commit()
        rows = conn.execute("SELECT granularity FROM power_readings WHERE time = '2026-06-01 00:00'").fetchall()
        assert {r["granularity"] for r in rows} == {"15min", "live"}

    def test_migration_is_idempotent(self):
        conn = self._old_schema_conn_with_data()
        db._migrate_composite_pk(conn)
        db._migrate_composite_pk(conn)  # must not raise or duplicate/lose data

        rows = conn.execute("SELECT * FROM power_readings").fetchall()
        assert len(rows) == 1
        assert rows[0]["import_t1_kwh"] == pytest.approx(100.0)

    def test_init_db_runs_the_migration_end_to_end(self):
        # init_db() is what every real entry point actually calls -- prove
        # the migration fires through that path, not just when called directly.
        conn = self._old_schema_conn_with_data()
        db.init_db(conn)
        assert _pk_columns(conn, "power_readings") == ["time", "granularity"]
        assert conn.execute("SELECT import_t1_kwh FROM power_readings").fetchone()["import_t1_kwh"] == pytest.approx(
            100.0
        )
