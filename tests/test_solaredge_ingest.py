"""SolarEdge ingest: unit normalisation, storage, the daylight gate and the power-window rule.

All calendar dates are 1990s fixtures; no real readings.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src import solaredge_ingest as si

TZ = ZoneInfo("Europe/Amsterdam")
NOON = datetime(1998, 7, 28, 10, 0, tzinfo=UTC)  # 12:00 local (CEST)
NIGHT = datetime(1998, 7, 28, 1, 0, tzinfo=UTC)  # 03:00 local


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


class FakeClient:
    def __init__(self, overview=None, power=None):
        self.overview = overview or {
            "production": {"total": 12.5, "unit": "KWH", "toGrid": 2.5, "toSelfConsumption": 10.0, "toStorage": 0.0},
            "consumption": {"total": None, "unit": "WH"},
        }
        self.power = power or {"unit": "W", "values": []}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.overview if path.endswith("/overview") else self.power


def test_to_wh_units_and_none():
    assert si.to_wh(1.5, "KWH") == 1500.0
    assert si.to_wh(2, "MWH") == 2_000_000.0
    assert si.to_wh(7, None) == 7.0
    assert si.to_wh(None, "KWH") is None
    with pytest.raises(ValueError):
        si.to_wh(1, "BTU")


def test_toggle_defaults_off_and_migration_adds_column(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE feature_toggles (id INTEGER PRIMARY KEY CHECK (id = 1)); INSERT INTO feature_toggles (id) VALUES (1);"
    )
    db._migrate_add_solaredge_toggle(c)
    db._migrate_add_solaredge_toggle(c)  # idempotent
    assert si.solaredge_enabled(c) is False


def test_daylight_gate():
    assert si.in_daylight(NOON, TZ) is True
    assert si.in_daylight(NIGHT, TZ) is False


def test_night_poll_makes_no_calls(conn):
    client = FakeClient()
    out = si.poll_once(conn, client, 1, NIGHT, TZ)
    assert out["calls"] == 0 and client.calls == []


def test_overview_stored_in_wh_with_local_day_and_null_consumption(conn):
    si.poll_once(conn, FakeClient(), 1, NOON, TZ)
    row = conn.execute("SELECT * FROM solaredge_overview").fetchone()
    assert row["production_wh"] == 12500.0 and row["to_grid_wh"] == 2500.0
    assert row["consumption_wh"] is None
    assert row["day"] == "1998-07-28" and row["time"] == "1998-07-28T10:00:00Z"


def _seed_yesterday(conn):
    conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-27', 1.0, 'x')")


def test_first_poll_fetches_power_for_the_last_12h_window(conn):
    _seed_yesterday(conn)
    client = FakeClient(power={"unit": "W", "values": [{"timestamp": "1998-07-28T11:45:00+02:00", "value": 400.0}]})
    out = si.poll_once(conn, client, 1, NOON, TZ)
    assert out["calls"] == 2 and out["power_rows"] == 1
    path, params = client.calls[1]
    assert path == "/sites/1/power" and params["resolution"] == "QUARTER_HOUR" and params["unit"] == "W"
    span = datetime.fromisoformat(params["to"].replace("Z", "+00:00")) - datetime.fromisoformat(params["from"].replace("Z", "+00:00"))
    assert span <= timedelta(hours=12)
    row = conn.execute("SELECT * FROM solaredge_power").fetchone()
    assert row["time"] == "1998-07-28T09:45:00Z" and row["power_w"] == 400.0  # +02:00 -> UTC


def test_fresh_power_means_overview_only(conn):
    _seed_yesterday(conn)
    conn.execute("INSERT INTO solaredge_power VALUES (?, ?)", ("1998-07-28T09:30:00Z", 100.0))  # 30 min old
    client = FakeClient()
    out = si.poll_once(conn, client, 1, NOON, TZ)
    assert out["calls"] == 1 and [p for p, _ in client.calls] == ["/sites/1/overview"]


def test_stale_power_refetches_from_newest_bucket_and_upserts(conn):
    conn.execute("INSERT INTO solaredge_power VALUES (?, ?)", ("1998-07-28T08:00:00Z", 100.0))  # 2 h old
    client = FakeClient(
        power={
            "unit": "KW",
            "values": [
                {"timestamp": "1998-07-28T10:00:00+02:00", "value": 0.25},  # same bucket as stored -> replaced, KW -> W
                {"timestamp": "1998-07-28T10:15:00+02:00", "value": None},  # skipped
            ],
        }
    )
    si.poll_once(conn, client, 1, NOON, TZ)
    assert client.calls[1][1]["from"] == "1998-07-28T08:00:00Z"
    rows = conn.execute("SELECT time, power_w FROM solaredge_power ORDER BY time").fetchall()
    assert [(r["time"], r["power_w"]) for r in rows] == [("1998-07-28T08:00:00Z", 250.0)]


def test_long_outage_window_is_capped_below_the_api_limit(conn):
    conn.execute("INSERT INTO solaredge_power VALUES (?, ?)", ("1998-07-20T08:00:00Z", 1.0))  # days ago
    frm, to = si.power_window(conn, NOON)
    assert to - frm <= timedelta(hours=12)


def test_client_errors_propagate_and_nothing_partial_is_committed(conn):
    class Boom(FakeClient):
        def get(self, path, params=None):
            if path.endswith("/power"):
                raise RuntimeError("api down")
            return super().get(path, params)

    with pytest.raises(RuntimeError):
        si.poll_once(conn, Boom(), 1, NOON, TZ)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM solaredge_overview").fetchone()[0] == 0


def test_first_daylight_run_of_a_day_also_finalises_yesterday_once(conn):
    class C(FakeClient):
        def get(self, path, params=None):
            if path.endswith("/energy"):
                self.calls.append((path, params))
                return {"values": [{"timestamp": "1998-07-27T00:00:00+02:00", "value": 5000.0}]}
            return super().get(path, params)

    c = C(power={"unit": "W", "values": []})
    out = si.poll_once(conn, c, 1, NOON, TZ)
    assert out["daily_calls"] == 1 and any(p.endswith("/energy") for p, _ in c.calls)
    assert conn.execute("SELECT production_wh FROM solaredge_daily WHERE date = '1998-07-27'").fetchone()[0] == 5000.0
    again = si.poll_once(conn, C(power={"unit": "W", "values": []}), 1, NOON, TZ)
    assert again["daily_calls"] == 0  # already stored -> no repeat
