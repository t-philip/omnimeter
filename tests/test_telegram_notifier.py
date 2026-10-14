"""Telegram delivery: enable/token/chat-id gating, and the actual send call
(mocked at urlopen, same pattern as test_update_check.py's TestFetchLatestTag)."""

import io
import sqlite3
import urllib.error

import pytest

from src import db, telegram_notifier


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


def _enable(conn):
    conn.execute("UPDATE feature_toggles SET anomaly_alerts_enabled = 1 WHERE id = 1")
    conn.commit()


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def __enter__(self):
        return self._buf

    def __exit__(self, *exc):
        return False


class TestGating:
    def test_disabled_toggle_never_calls_network(self, conn, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("must not send when the toggle is off")

        monkeypatch.setattr(telegram_notifier.urllib.request, "urlopen", _boom)
        status, error = telegram_notifier.send_message(conn, "hello")
        assert status == "disabled"
        assert "anomaly_alerts_enabled" in error

    def test_no_token_is_disabled(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.delenv("OMNIMETER_TELEGRAM_BOT_TOKEN", raising=False)
        status, error = telegram_notifier.send_message(conn, "hello")
        assert status == "disabled"
        assert "TOKEN" in error

    def test_no_chat_id_is_disabled(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.delenv("OMNIMETER_TELEGRAM_CHAT_ID", raising=False)
        status, error = telegram_notifier.send_message(conn, "hello")
        assert status == "disabled"
        assert "chat ID" in error

    def test_env_chat_id_used_as_default(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setattr(
            telegram_notifier.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(b'{"ok": true}')
        )
        status, error = telegram_notifier.send_message(conn, "hello")
        assert (status, error) == ("sent", None)

    def test_db_chat_id_overrides_env_default(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "should-not-be-used")
        conn.execute("INSERT INTO telegram_config (id, chat_id) VALUES (1, '999')")
        conn.commit()

        seen_urls = []

        def fake_urlopen(req, *a, **kw):
            seen_urls.append(req.data)
            return _FakeResponse(b'{"ok": true}')

        monkeypatch.setattr(telegram_notifier.urllib.request, "urlopen", fake_urlopen)
        telegram_notifier.send_message(conn, "hello")
        assert b'"999"' in seen_urls[0]


class TestSend:
    def test_ok_response_is_sent(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setattr(
            telegram_notifier.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(b'{"ok": true}')
        )
        assert telegram_notifier.send_message(conn, "hello") == ("sent", None)

    def test_api_ok_false_is_failed_not_raised(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "bad-chat-id")
        monkeypatch.setattr(
            telegram_notifier.urllib.request,
            "urlopen",
            lambda *a, **kw: _FakeResponse(b'{"ok": false, "description": "chat not found"}'),
        )
        status, error = telegram_notifier.send_message(conn, "hello")
        assert status == "failed"
        assert "chat not found" in error

    def test_network_error_is_failed_not_raised(self, conn, monkeypatch):
        # The poll loop (meter_poller.py) must never crash on a Telegram
        # outage.
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "12345")

        def _raise(*a, **kw):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(telegram_notifier.urllib.request, "urlopen", _raise)
        status, error = telegram_notifier.send_message(conn, "hello")
        assert status == "failed"
        assert error

    def test_timeout_is_failed_not_raised(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "12345")

        def _raise(*a, **kw):
            raise TimeoutError("timed out")

        monkeypatch.setattr(telegram_notifier.urllib.request, "urlopen", _raise)
        status, error = telegram_notifier.send_message(conn, "hello")
        assert status == "failed"
        assert error

    def test_send_test_message(self, conn, monkeypatch):
        _enable(conn)
        monkeypatch.setenv("OMNIMETER_TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("OMNIMETER_TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setattr(
            telegram_notifier.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(b'{"ok": true}')
        )
        assert telegram_notifier.send_test_message(conn) == ("sent", None)
