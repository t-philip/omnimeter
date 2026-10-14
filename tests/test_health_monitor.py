"""Layer 3 health/staleness monitor."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from src import db, health_monitor, telegram_notifier

_TZ = health_monitor._LOCAL_TZ
_START = datetime(1998, 1, 1, 12, 0, 0, tzinfo=_TZ)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def _seed_reading(conn, table: str, t: datetime) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO {table} (time, granularity) VALUES (?, 'api_live')",
        (t.strftime("%Y-%m-%d %H:%M"),),
    )
    conn.commit()


class TestNeverConfigured:
    def test_metric_with_no_live_history_is_never_flagged(self, conn):
        # An empty water_readings table (no watermeter ever paired) must not
        # be treated as "stale" -- there's nothing to be stale relative to.
        active = health_monitor.check_health(conn, now=_START)
        assert active == []


class TestFreshData:
    def test_recent_reading_is_not_stale(self, conn):
        _seed_reading(conn, "water_readings", _START - timedelta(minutes=5))
        active = health_monitor.check_health(conn, now=_START)
        assert active == []


class TestStaleData:
    def test_gap_beyond_threshold_opens_a_health_episode(self, conn):
        last_seen = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", last_seen)
        active = health_monitor.check_health(conn, now=_START)
        assert len(active) == 1
        ep = active[0]
        assert ep["kind"] == "health"
        assert ep["metric"] == "water"
        assert ep["ended_at"] is None
        assert ep["total_volume"] == pytest.approx(120, abs=1)

    def test_repeated_checks_update_the_same_episode_not_duplicate(self, conn):
        last_seen = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", last_seen)
        health_monitor.check_health(conn, now=_START)
        health_monitor.check_health(conn, now=_START + timedelta(hours=1))
        rows = conn.execute("SELECT * FROM alert_episodes WHERE kind = 'health' AND metric = 'water'").fetchall()
        assert len(rows) == 1
        assert rows[0]["ended_at"] is None

    def test_multiple_stale_metrics_each_get_their_own_episode(self, conn):
        last_seen = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", last_seen)
        _seed_reading(conn, "gas_readings", last_seen)
        active = health_monitor.check_health(conn, now=_START)
        assert {ep["metric"] for ep in active} == {"water", "gas"}


class TestRecovery:
    def test_fresh_data_closes_an_open_health_episode(self, conn):
        stale_time = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", stale_time)
        health_monitor.check_health(conn, now=_START)
        row = conn.execute("SELECT * FROM alert_episodes WHERE kind = 'health' AND metric = 'water'").fetchone()
        assert row["ended_at"] is None

        # Data resumes.
        _seed_reading(conn, "water_readings", _START + timedelta(minutes=1))
        active = health_monitor.check_health(conn, now=_START + timedelta(minutes=2))
        assert active == []
        row = conn.execute("SELECT * FROM alert_episodes WHERE kind = 'health' AND metric = 'water'").fetchone()
        assert row["ended_at"] is not None

    def test_recovery_then_a_new_stall_opens_a_second_episode(self, conn):
        stale_time = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", stale_time)
        health_monitor.check_health(conn, now=_START)
        _seed_reading(conn, "water_readings", _START + timedelta(minutes=1))
        health_monitor.check_health(conn, now=_START + timedelta(minutes=2))

        later = _START + timedelta(hours=3)
        active = health_monitor.check_health(conn, now=later)
        assert len(active) == 1
        rows = conn.execute(
            "SELECT * FROM alert_episodes WHERE kind = 'health' AND metric = 'water' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["ended_at"] is not None
        assert rows[1]["ended_at"] is None


class TestMaybeNotify:
    def test_sends_immediately_and_message_names_the_metric(self, conn, monkeypatch):
        conn.execute("UPDATE feature_toggles SET anomaly_alerts_enabled = 1 WHERE id = 1")
        conn.commit()
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(text) or ("sent", None)))

        last_seen = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", last_seen)
        active = health_monitor.check_health(conn, now=_START)
        health_monitor.maybe_notify(conn, active[0]["id"], now=_START)

        assert len(calls) == 1
        assert "water" in calls[0].lower()
        assert "alert #1" in calls[0]

    def test_acknowledged_episode_is_never_renotified(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(1) or ("sent", None)))
        last_seen = _START - timedelta(minutes=health_monitor.STALE_THRESHOLD_MINUTES + 30)
        _seed_reading(conn, "water_readings", last_seen)
        active = health_monitor.check_health(conn, now=_START)
        conn.execute(
            "UPDATE alert_episodes SET acknowledged_at = ? WHERE id = ?",
            (_START.strftime("%Y-%m-%d %H:%M:%S"), active[0]["id"]),
        )
        conn.commit()
        health_monitor.maybe_notify(conn, active[0]["id"], now=_START + timedelta(hours=2))
        assert calls == []
