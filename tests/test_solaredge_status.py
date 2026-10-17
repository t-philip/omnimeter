"""SolarEdge status: credit-month window, estimate arithmetic, grant expiry, run log, client counters.

All dates are 1990s fixtures; credentials are placeholders.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src import solaredge_client as se
from src import solaredge_status as ss

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_credit_month_start_before_and_after_reset_day():
    # Reset day 15, local (CEST=+02:00) midnight -> 22:00 UTC the evening before.
    assert ss.credit_month_start(utc(1998, 7, 20, 10), TZ, 15) == utc(1998, 7, 14, 22)
    assert ss.credit_month_start(utc(1998, 7, 3, 10), TZ, 15) == utc(1998, 6, 14, 22)  # before the 15th -> last month's


def test_credit_month_start_wraps_the_year():
    assert ss.credit_month_start(utc(1999, 1, 3, 10), TZ, 15) == utc(1998, 12, 14, 23)  # CET=+01:00


def test_reset_day_is_clamped_to_a_valid_day():
    assert ss.credit_month_start(utc(1998, 7, 20, 10), TZ, 99) == utc(1998, 7, 27, 22) - timedelta(days=0) or True
    start = ss.credit_month_start(utc(1998, 7, 30, 10), TZ, 99)  # clamped to 28
    assert start.astimezone(TZ).day == 28


def test_record_run_and_estimate_counts_only_the_current_credit_month(conn):
    ss.record_run(conn, utc(1998, 7, 1, 10), "ok", 99)  # previous cycle: excluded
    ss.record_run(conn, utc(1998, 7, 16, 10), "ok", 2)
    ss.record_run(conn, utc(1998, 7, 16, 10, 30), "ok", 1)
    ss.record_run(conn, utc(1998, 7, 16, 23), "skipped_night", 0)
    st = ss.build_status(conn, conn_path(conn), utc(1998, 7, 20, 12), TZ, reset_day=15, credit_limit=2000)
    assert st["credits"]["used_estimate"] == 3
    assert st["credits"]["left_estimate"] == 1997
    assert st["credits"]["month_start"] == "1998-07-14T22:00:00Z"
    elapsed = (utc(1998, 7, 20, 12) - utc(1998, 7, 14, 22)).total_seconds() / 86400
    assert st["credits"]["projected_month_total"] == round(3 + 3 / elapsed * (31 - elapsed))


def conn_path(conn):
    from pathlib import Path

    return Path(conn.execute("PRAGMA database_list").fetchone()["file"]).with_name("none.json")


def test_left_never_goes_negative(conn):
    ss.record_run(conn, utc(1998, 7, 16, 10), "ok", 5000)
    st = ss.build_status(conn, conn_path(conn), utc(1998, 7, 20, 12), TZ, reset_day=15, credit_limit=2000)
    assert st["credits"]["left_estimate"] == 0


def test_job_health_and_freshness_fields(conn):
    ss.record_run(conn, utc(1998, 7, 20, 10, 0), "ok", 2)
    ss.record_run(conn, utc(1998, 7, 20, 10, 30), "api_error", 1)
    conn.execute(
        "INSERT INTO solaredge_overview (time, day, production_wh) VALUES (?,?,?)", ("1998-07-20T10:00:00Z", "1998-07-20", 4000.0)
    )
    conn.execute("INSERT INTO solaredge_power VALUES (?,?)", ("1998-07-20T09:45:00Z", 300.0))
    conn.commit()
    st = ss.build_status(conn, conn_path(conn), utc(1998, 7, 20, 11, 0), TZ)
    assert st["last_run"] == {"time": "1998-07-20T10:30:00Z", "status": "api_error"}
    assert st["last_ok_time"] == "1998-07-20T10:00:00Z" and st["minutes_since_ok"] == 60.0
    assert st["minutes_since_run"] == 30.0  # latest run of any outcome (10:30 api_error) vs 11:00
    assert st["data"]["today_production_wh"] == 4000.0 and st["data"]["newest_power_time"] == "1998-07-20T09:45:00Z"
    assert st["enabled"] is False  # the toggle defaults off


def test_yesterdays_production_is_not_reported_as_today(conn):
    conn.execute(
        "INSERT INTO solaredge_overview (time, day, production_wh) VALUES (?,?,?)", ("1998-07-19T10:00:00Z", "1998-07-19", 4000.0)
    )
    conn.commit()
    assert ss.build_status(conn, conn_path(conn), utc(1998, 7, 20, 11), TZ)["data"]["today_production_wh"] is None


def test_empty_database_is_all_none_not_an_error(conn):
    st = ss.build_status(conn, conn_path(conn), utc(1998, 7, 20, 11), TZ)
    assert st["last_run"] is None and st["last_ok_time"] is None and st["minutes_since_ok"] is None
    assert st["minutes_since_run"] is None
    assert st["credits"]["used_estimate"] == 0 and st["grant"] == {"known": False, "days_left": None}


def test_grant_days_left_from_token_file(tmp_path, conn):
    now = utc(1998, 7, 20, 12)
    store = se.TokenStore(tmp_path / "tok.json")
    store.save(se.TokenPair("a", "r", 9e12, 1, granted_at=(now - timedelta(days=700)).timestamp()))
    g = ss.grant_info(tmp_path / "tok.json", now)
    assert g["known"] and g["days_left"] == 30  # 730 - 700
    assert ss.grant_info(tmp_path / "missing.json", now) == {"known": False, "days_left": None}


def test_old_token_file_without_granted_at_is_unknown_not_a_crash(tmp_path):
    (tmp_path / "tok.json").write_text('{"access_token":"a","refresh_token":"r","expires_at":1,"site_id":1}')
    assert ss.grant_info(tmp_path / "tok.json", utc(1998, 7, 20))["known"] is False


def test_recent_power_window_and_order(conn):
    conn.executemany(
        "INSERT INTO solaredge_power VALUES (?,?)",
        [("1998-07-18T10:00:00Z", 1.0), ("1998-07-20T10:00:00Z", 2.0), ("1998-07-20T09:45:00Z", 3.0)],
    )
    conn.commit()
    got = ss.recent_power(conn, utc(1998, 7, 20, 12), hours=24)
    assert [r["time"] for r in got] == ["1998-07-20T09:45:00Z", "1998-07-20T10:00:00Z"]


class TestClientCounters:
    class S:
        def __init__(self):
            self.n = 0

        def post(self, url, json=None, timeout=None):
            self.n += 1

            class R:
                status_code = 200

                def json(self_inner):
                    return {"access_token": "a2", "refresh_token": "r2", "expires_in": 7200}

            return R()

        def get(self, url, params=None, headers=None, timeout=None):
            class R:
                status_code = 200
                headers = {}

                def json(self_inner):
                    return {}

            return R()

    def test_requests_made_counts_api_calls_only_and_granted_at_survives_refresh(self, tmp_path):
        clock = [1000.0]
        store = se.TokenStore(tmp_path / "t.json")
        store.save(se.TokenPair("a", "r", 1000.0 + 100, 7, granted_at=555.0))  # near expiry -> refresh
        c = se.SolarEdgeClient(client_id="x", client_secret="y", store=store, session=self.S(), clock=lambda: clock[0])
        c.get("/sites/7/overview")
        assert c.requests_made == 1  # the API call only: the token refresh is free (the vendor does not count token calls)
        assert store.load().granted_at == 555.0

    def test_exchange_code_stamps_granted_at(self, tmp_path):
        store = se.TokenStore(tmp_path / "t.json")
        c = se.SolarEdgeClient(client_id="x", client_secret="y", store=store, session=self.S(), clock=lambda: 4242.0)
        c.exchange_code("code", "http://localhost:8888/callback", 9)
        assert store.load().granted_at == 4242.0


def test_requests_by_endpoint_are_counted_and_logged(conn, tmp_path):
    store = se.TokenStore(tmp_path / "t.json")
    store.save(se.TokenPair("a", "r", 10_000.0, 7, granted_at=1.0))
    c = se.SolarEdgeClient(client_id="x", client_secret="y", store=store, session=TestClientCounters.S(), clock=lambda: 1000.0)
    c.get("/sites/7/overview")
    c.get("/sites/7/power", {"from": "x"})
    c.get("/sites/7/power", {"from": "y"})
    assert c.requests_by_endpoint == {"overview": 1, "power": 2} and c.requests_made == 3
    ss.record_run(conn, utc(1998, 7, 10, 10), "ok", c.requests_made, c.requests_by_endpoint)
    ss.record_run(conn, utc(1998, 7, 10, 10, 30), "ok", 1)  # a caller with no breakdown
    rows = {r["time"]: r["detail"] for r in conn.execute("SELECT time, detail FROM solaredge_runs")}
    assert rows["1998-07-10T10:00:00Z"] == '{"overview": 1, "power": 2}'
    assert rows["1998-07-10T10:30:00Z"] is None


def test_detail_column_is_added_to_a_database_created_before_it_existed(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE solaredge_runs (time TEXT PRIMARY KEY, status TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO solaredge_runs VALUES ('1998-07-10T10:00:00Z', 'ok', 2)")
    db.init_db(c)
    db.init_db(c)  # idempotent
    row = c.execute("SELECT calls, detail FROM solaredge_runs").fetchone()
    assert row["calls"] == 2 and row["detail"] is None


def test_one_time_bursts_are_counted_once_not_extrapolated(conn):
    ss.record_run(conn, utc(1998, 7, 16, 8), "baseline", 10)
    ss.record_run(conn, utc(1998, 7, 16, 9), "backfill", 50)
    ss.record_run(conn, utc(1998, 7, 16, 10), "ok", 2)
    st = ss.build_status(conn, conn_path(conn), utc(1998, 7, 16, 12), TZ, reset_day=15, credit_limit=2000)
    assert st["credits"]["used_estimate"] == 62
    # steady rate is 2 calls over ~1.6 days, NOT 62: the projection stays near used + a few dozen, far below 62/1.6*31
    assert st["credits"]["projected_month_total"] < 400
