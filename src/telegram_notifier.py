"""Telegram delivery for OmniMeter's runaway-leak alerts.

Deliberately does NOT follow update_check.py's "degrade silently" precedent
-- a silently dropped leak notification is
indistinguishable from "no leak," which is the one failure mode this
feature exists to avoid. Every send attempt's outcome is returned to the
caller (runaway_detector.maybe_notify), which records it on the episode
row so a failure is visible and retried, never just lost.

The bot token is a secret and never touches the database -- it is read
from the environment only, the same token_env indirection pattern
meter_poller.py already uses for device credentials (e.g. from
.env via docker-compose's env_file:). The chat ID is not a secret, so
it lives in the DB and is editable in Settings, falling back to an env
default for a self-hoster who configures everything from .env without
opening the UI.
"""

import json
import sqlite3
import urllib.error
import urllib.request

from .credentials import default_credential_source

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_SECONDS = 5

TEST_MESSAGE = "✅ OmniMeter test message — leak alerts are configured correctly."


def _bot_token() -> str | None:
    return default_credential_source().get("OMNIMETER_TELEGRAM_BOT_TOKEN") or None


def _chat_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT chat_id FROM telegram_config WHERE id = 1").fetchone()
    if row and row["chat_id"]:
        return row["chat_id"]
    return default_credential_source().get("OMNIMETER_TELEGRAM_CHAT_ID") or None


def is_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT anomaly_alerts_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
    return bool(row["v"]) if row else False


def send_message(conn: sqlite3.Connection, text: str) -> tuple[str, str | None]:
    """Returns (status, error) where status is 'sent' | 'failed' | 'disabled'.
    Never raises -- the poll loop must never stall on network, and
    the test-message endpoint needs a normal response either way, not an
    exception."""
    if not is_enabled(conn):
        return "disabled", "anomaly_alerts_enabled is off"
    token = _bot_token()
    if not token:
        return "disabled", "OMNIMETER_TELEGRAM_BOT_TOKEN is not set"
    chat_id = _chat_id(conn)
    if not chat_id:
        return "disabled", "no chat ID configured (Settings, or OMNIMETER_TELEGRAM_CHAT_ID)"

    url = TELEGRAM_API_URL.format(token=token)
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https host
            body = json.load(response)
        if not body.get("ok"):
            return "failed", str(body.get("description") or "Telegram API returned ok=false")
        return "sent", None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return "failed", str(exc)


def send_test_message(conn: sqlite3.Connection) -> tuple[str, str | None]:
    return send_message(conn, TEST_MESSAGE)
