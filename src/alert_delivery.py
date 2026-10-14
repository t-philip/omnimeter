"""Shared escalating-notification delivery for anomaly_alerts --
the same "immediate, then +15/+30/hourly, only a successful send advances
the schedule" logic that both Layer 1 (src/runaway_detector.py) and Layer 3
(src/health_monitor.py) need for their own alert_episodes rows, extracted
here rather than duplicated a second time now that a second caller exists.
Layer 2's digest (a future phase) is expected to want the same "did this
send actually succeed" bookkeeping too.

Callers own their own message text and their own episode lifecycle
(when a row is opened/closed); this module only owns "is a notification
due right now, and how do we record the outcome."
"""

import sqlite3
from datetime import datetime

# Escalating backoff: immediate, then +15 min, +30 min, then hourly.
# Keyed by the episode's alert_count *before* the send being considered.
BACKOFF_MINUTES = {0: 0, 1: 15, 2: 30}
BACKOFF_DEFAULT_MINUTES = 60


def parse_dt(s: str, tz) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)


def is_due(alert_count: int, last_alerted_at: str | None, now: datetime, tz) -> bool:
    """True if enough time has passed since the last successful send for
    this episode's current alert_count to warrant another attempt.
    alert_count == 0 (never successfully sent) is always due."""
    if alert_count == 0:
        return True
    if last_alerted_at is None:
        return True
    interval = BACKOFF_MINUTES.get(alert_count, BACKOFF_DEFAULT_MINUTES)
    return (now - parse_dt(last_alerted_at, tz)).total_seconds() >= interval * 60


def notify(conn: sqlite3.Connection, episode_id: int, text: str, now: datetime) -> None:
    """Sends `text` for the given alert_episodes row if it is active,
    unacknowledged, and due per is_due() -- a no-op otherwise. Only a
    successful send advances alert_count/last_alerted_at (and so the
    backoff schedule); a failed attempt leaves both untouched so the very
    next caller invocation re-evaluates the same still-due notification and
    retries immediately, rather than waiting out the backoff meant for
    successful sends (a failure is retried on the
    next cycle, not lost)."""
    from . import telegram_notifier  # deferred: avoids a hard dependency for callers that only check thresholds

    row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None or row["acknowledged_at"] is not None or row["ended_at"] is not None:
        return
    if not is_due(row["alert_count"], row["last_alerted_at"], now, now.tzinfo):
        return

    status, error = telegram_notifier.send_message(conn, text)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    if status == "sent":
        conn.execute(
            "UPDATE alert_episodes SET alert_count = alert_count + 1, last_alerted_at = ?, "
            "notify_status = ?, notify_error = ? WHERE id = ?",
            (now_str, status, error, episode_id),
        )
    else:
        conn.execute(
            "UPDATE alert_episodes SET notify_status = ?, notify_error = ? WHERE id = ?",
            (status, error, episode_id),
        )
    conn.commit()
