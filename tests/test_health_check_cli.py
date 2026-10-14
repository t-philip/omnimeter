"""scripts/health_check.py -- the Layer 3 timer entry point. Mirrors
test_weather_backfill.py's pattern of monkeypatching db.get_connection so
the script runs against an in-memory test DB."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from scripts import health_check
from src import db, health_monitor, localtime, telegram_notifier

_TZ = localtime.LOCAL_TZ


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


class TestMain:
    def test_no_prior_data_exits_cleanly(self, conn, monkeypatch, capsys):
        monkeypatch.setattr(health_check.db, "get_connection", lambda: conn)
        assert health_check.main() == 0
        assert "fresh" in capsys.readouterr().out

    def test_stale_metric_notifies_and_reports(self, conn, monkeypatch, capsys):
        monkeypatch.setattr(health_check.db, "get_connection", lambda: conn)
        stale_time = datetime.now(_TZ) - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        conn.execute(
            "INSERT INTO water_readings (time, granularity) VALUES (?, 'api_live')",
            (stale_time.strftime("%Y-%m-%d %H:%M"),),
        )
        conn.commit()
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(text) or ("sent", None)))

        assert health_check.main() == 0
        assert "water" in capsys.readouterr().out
        assert len(calls) == 1
