"""The weather backfill script must degrade gracefully --
toggle-off and an Open-Meteo outage must both exit cleanly, never crash, and
never write partial/corrupt data."""

import urllib.error

import pytest

from scripts import weather_backfill
from src import weather


@pytest.fixture
def conn():
    import sqlite3

    from src import db

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def _seed_meter_data(conn):
    conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2026-07-08', 4.0)")
    conn.commit()


class TestToggleOff:
    def test_disabled_returns_0_without_network_call(self, conn, monkeypatch):
        monkeypatch.setattr(weather, "fetch_range", lambda *a, **kw: pytest.fail("should not be called"))
        monkeypatch.setattr(weather_backfill.db, "get_connection", lambda: conn)
        assert weather_backfill.main([]) == 0


class TestApiOutage:
    def test_url_error_exits_cleanly_without_writing(self, conn, monkeypatch, capsys):
        conn.execute("UPDATE feature_toggles SET weather_enabled = 1 WHERE id = 1")
        conn.commit()
        _seed_meter_data(conn)
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "52.3")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.9")
        monkeypatch.setattr(weather_backfill.db, "get_connection", lambda: conn)

        def raise_url_error(*a, **kw):
            raise urllib.error.URLError("[Errno -2] Name or service not known")

        monkeypatch.setattr(weather, "fetch_range", raise_url_error)

        assert weather_backfill.main([]) == 1
        assert "weather fetch failed" in capsys.readouterr().out
        assert weather.covered_range(conn) == (None, None)  # nothing written

    def test_timeout_exits_cleanly(self, conn, monkeypatch):
        conn.execute("UPDATE feature_toggles SET weather_enabled = 1 WHERE id = 1")
        conn.commit()
        _seed_meter_data(conn)
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "52.3")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.9")
        monkeypatch.setattr(weather_backfill.db, "get_connection", lambda: conn)
        monkeypatch.setattr(weather, "fetch_range", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("timed out")))

        assert weather_backfill.main([]) == 1

    def test_malformed_response_exits_cleanly(self, conn, monkeypatch):
        conn.execute("UPDATE feature_toggles SET weather_enabled = 1 WHERE id = 1")
        conn.commit()
        _seed_meter_data(conn)
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "52.3")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.9")
        monkeypatch.setattr(weather_backfill.db, "get_connection", lambda: conn)

        def raise_value_error(*a, **kw):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        monkeypatch.setattr(weather, "fetch_range", raise_value_error)

        assert weather_backfill.main([]) == 1

    def test_existing_cached_data_survives_a_failed_run(self, conn, monkeypatch):
        # A prior successful run already wrote weather_daily; today's run
        # hits an outage. The dashboard must still have yesterday's data to
        # fall back on -- confirm the failed run doesn't touch it.
        conn.execute("UPDATE feature_toggles SET weather_enabled = 1 WHERE id = 1")
        conn.commit()
        _seed_meter_data(conn)
        weather.store(
            conn,
            [{"date": "2026-07-07", "shortwave_radiation_sum": 17.08, "sunshine_duration": 33840.0,
              "temperature_2m_max": 21.9, "temperature_2m_min": 13.0, "temperature_2m_mean": 17.2}],
            52.3, 4.9,
        )
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "52.3")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.9")
        monkeypatch.setattr(weather_backfill.db, "get_connection", lambda: conn)
        monkeypatch.setattr(weather, "fetch_range", lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("down")))

        assert weather_backfill.main([]) == 1
        assert weather.covered_range(conn) == ("2026-07-07", "2026-07-07")
