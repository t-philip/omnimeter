"""Layer 3 -- health & staleness monitoring: meter silent, ingest stalled,
device unreachable. A silent meter means data loss accruing.
Rule-based, no ML.

For each metric that has ever produced a live (api_live) reading, checks
whether the gap since the most recent one exceeds STALE_THRESHOLD_MINUTES.
A metric that has never produced a live reading (no device configured for
it, or a household with no gas connection at all) is never checked --
there is nothing to be "stale" relative to, and device configuration is
deliberately not consulted directly (devices.json can change; the readings
table is the ground truth for "has this ever actually worked").

Runs on its own hourly timer (scripts/health_check.py), unlike Layer 1
which runs inside meter_poller's own loop -- a stalled poller is exactly
the failure this exists to catch, so it cannot depend on that same poller
to run it.
"""

import sqlite3
from datetime import datetime

from . import alert_delivery, localtime

_LOCAL_TZ = localtime.LOCAL_TZ

# Comfortably more than a handful of missed ~20s polls (same reasoning as
# runaway_detector.STALE_GAP_SECONDS), while still catching a genuine stall
# same-day rather than waiting out a full hourly cycle before noticing.
STALE_THRESHOLD_MINUTES = 90

_METRIC_TABLES = {
    "power": "power_readings",
    "gas": "gas_readings",
    "water": "water_readings",
    "battery": "battery_readings",
}


def _now() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _normalize(time_str: str) -> str:
    # power_readings.time etc. are written at minute precision
    # ("YYYY-MM-DD HH:MM", see meter_ingest._now_local()); alert_episodes
    # timestamps elsewhere in this feature carry seconds. Padded here so
    # started_at is consistently comparable/parseable across both kinds.
    return time_str if len(time_str) > 16 else f"{time_str}:00"


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(_normalize(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_LOCAL_TZ)


def check_health(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Checks every metric that has ever produced a live reading, opening/
    updating/closing a kind='health' alert_episodes row per metric as
    needed. Returns the currently-active (still stale) episodes as dicts --
    callers (scripts/health_check.py) should pass each id to maybe_notify()."""
    now = now or _now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    active: list[dict] = []

    for metric, table in _METRIC_TABLES.items():
        row = conn.execute(f"SELECT MAX(time) AS t FROM {table} WHERE granularity = 'api_live'").fetchone()
        last_seen = row["t"] if row and row["t"] is not None else None
        if last_seen is None:
            continue
        last_seen = _normalize(last_seen)

        open_episode = conn.execute(
            "SELECT * FROM alert_episodes WHERE kind = 'health' AND metric = ? AND ended_at IS NULL",
            (metric,),
        ).fetchone()

        gap_minutes = (now - _parse_dt(last_seen)).total_seconds() / 60.0

        if gap_minutes <= STALE_THRESHOLD_MINUTES:
            if open_episode is not None:
                conn.execute(
                    "UPDATE alert_episodes SET ended_at = ? WHERE id = ?", (now_str, open_episode["id"])
                )
                conn.commit()
            continue

        if open_episode is not None and open_episode["started_at"] == last_seen:
            conn.execute(
                "UPDATE alert_episodes SET last_seen_at = ?, total_volume = ? WHERE id = ?",
                (now_str, round(gap_minutes), open_episode["id"]),
            )
            episode_id = open_episode["id"]
        else:
            # The metric has gone stale relative to a *different* last-known-
            # good reading than any currently-open episode tracks (or there
            # is no open episode yet) -- close the stale one, if any, and
            # open a fresh one keyed on this reading. started_at = the last
            # known-good reading itself, so re-running this check while
            # still stale against the same reading always resolves to the
            # branch above (upsert, never a duplicate row).
            if open_episode is not None:
                conn.execute(
                    "UPDATE alert_episodes SET ended_at = ? WHERE id = ?", (now_str, open_episode["id"])
                )
            conn.execute(
                "INSERT INTO alert_episodes (kind, metric, started_at, last_seen_at, total_volume, unit, "
                "threshold_used, alert_count) VALUES ('health', ?, ?, ?, ?, 'min', ?, 0)",
                (metric, last_seen, now_str, round(gap_minutes), STALE_THRESHOLD_MINUTES),
            )
            episode_id = conn.execute(
                "SELECT id FROM alert_episodes WHERE kind = 'health' AND metric = ? AND started_at = ?",
                (metric, last_seen),
            ).fetchone()["id"]
        conn.commit()
        active.append(dict(conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()))

    return active


def maybe_notify(conn: sqlite3.Connection, episode_id: int, now: datetime | None = None) -> None:
    """Escalating-backoff Telegram notification for an active health
    episode -- see alert_delivery.notify() for the shared logic (also used
    by Layer 1)."""
    now = now or _now()
    row = conn.execute("SELECT * FROM alert_episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return
    gap_minutes = round((now - _parse_dt(row["started_at"])).total_seconds() / 60.0)
    text = (
        f"⚠️ {row['metric'].capitalize()} data has gone stale — no new reading in "
        f"{gap_minutes} min (alert #{row['alert_count'] + 1}). Check the meter/poller."
    )
    alert_delivery.notify(conn, episode_id, text, now)
