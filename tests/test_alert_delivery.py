"""Shared escalating-notification delivery -- the backoff/outcome
logic used by both Layer 1 (runaway_detector) and Layer 3 (health_monitor).
See test_runaway_detector.py's TestMaybeNotify for the fuller integration
coverage of the actual backoff schedule; this file covers alert_delivery's
own gating in isolation."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from src import alert_delivery, db, localtime, telegram_notifier

_TZ = localtime.LOCAL_TZ
_START = datetime(1998, 1, 1, 12, 0, 0, tzinfo=_TZ)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def _make_episode(conn, alert_count=0, last_alerted_at=None, acknowledged_at=None, ended_at=None):
    conn.execute(
        "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
        "threshold_used, alert_count, last_alerted_at, acknowledged_at, ended_at) VALUES "
        "('health', 'water', ?, ?, 10, 'min', 90, ?, ?, ?, ?)",
        (
            _START.strftime("%Y-%m-%d %H:%M:%S"),
            _START.strftime("%Y-%m-%d %H:%M:%S"),
            alert_count,
            last_alerted_at,
            acknowledged_at,
            ended_at,
        ),
    )
    conn.commit()
    return conn.execute("SELECT id FROM alert_episodes").fetchone()["id"]


class TestIsDue:
    def test_never_alerted_is_always_due(self):
        assert alert_delivery.is_due(0, None, _START, _TZ) is True

    def test_too_soon_is_not_due(self):
        last = _START.strftime("%Y-%m-%d %H:%M:%S")
        assert alert_delivery.is_due(1, last, _START + timedelta(minutes=5), _TZ) is False

    def test_past_the_interval_is_due(self):
        last = _START.strftime("%Y-%m-%d %H:%M:%S")
        assert alert_delivery.is_due(1, last, _START + timedelta(minutes=16), _TZ) is True

    def test_unknown_high_count_falls_back_to_hourly(self):
        last = _START.strftime("%Y-%m-%d %H:%M:%S")
        assert alert_delivery.is_due(9, last, _START + timedelta(minutes=59), _TZ) is False
        assert alert_delivery.is_due(9, last, _START + timedelta(minutes=61), _TZ) is True


class TestNotify:
    def test_skips_acknowledged(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(1) or ("sent", None)))
        episode_id = _make_episode(conn, acknowledged_at=_START.strftime("%Y-%m-%d %H:%M:%S"))
        alert_delivery.notify(conn, episode_id, "test", _START)
        assert calls == []

    def test_skips_ended(self, conn, monkeypatch):
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: (calls.append(1) or ("sent", None)))
        episode_id = _make_episode(conn, ended_at=_START.strftime("%Y-%m-%d %H:%M:%S"))
        alert_delivery.notify(conn, episode_id, "test", _START)
        assert calls == []

    def test_missing_episode_does_not_raise(self, conn):
        alert_delivery.notify(conn, 999, "test", _START)  # must not raise

    def test_successful_send_advances_alert_count(self, conn, monkeypatch):
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: ("sent", None))
        episode_id = _make_episode(conn)
        alert_delivery.notify(conn, episode_id, "test", _START)
        row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["alert_count"] == 1
        assert row["last_alerted_at"] is not None

    def test_failed_send_does_not_advance_alert_count(self, conn, monkeypatch):
        monkeypatch.setattr(telegram_notifier, "send_message", lambda conn, text: ("failed", "boom"))
        episode_id = _make_episode(conn)
        alert_delivery.notify(conn, episode_id, "test", _START)
        row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
        assert row["alert_count"] == 0
        assert row["notify_status"] == "failed"
