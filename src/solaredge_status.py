"""Read-only SolarEdge status for external monitoring: job health, data freshness, the
estimated API-credit balance and the grant's remaining life.

The credit figure is an ESTIMATE: SolarEdge exposes no balance header, so it is the sum of
the HTTP requests this poller logged since the start of the credit month (``solaredge_runs``).
Other callers of the same app (testing, the one-time authorisation) are not in it, so the
real portal figure can be a little higher.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import solaredge_client

DEFAULT_CREDIT_LIMIT = 2000
# The credit cycle is anchored to the developer account, so the reset day differs per account: set
# OMNIMETER_SOLAREDGE_CREDIT_RESET_DAY (and _CREDIT_LIMIT) from your own portal usage page. The balance
# shown is an estimate built from this app's own request count, not a figure the vendor reports.
DEFAULT_RESET_DAY = 1
RESET_DAY_ENV = "OMNIMETER_SOLAREDGE_CREDIT_RESET_DAY"
CREDIT_LIMIT_ENV = "OMNIMETER_SOLAREDGE_CREDIT_LIMIT"
GRANT_LIFETIME = timedelta(days=730)  # 24 months, the vendor maximum, in whole days
EXPIRY_WARN_DAYS = 30


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def credit_month_start(now_utc: datetime, tz: ZoneInfo, reset_day: int) -> datetime:
    """Start (UTC) of the current credit month: the most recent ``reset_day`` at local midnight."""
    reset_day = max(1, min(int(reset_day), 28))
    local = now_utc.astimezone(tz)
    year, month = local.year, local.month
    if local.day < reset_day:
        month, year = (12, year - 1) if month == 1 else (month - 1, year)
    return datetime.combine(date(year, month, reset_day), datetime.min.time(), tzinfo=tz).astimezone(UTC)


def month_length_days(start_utc: datetime, tz: ZoneInfo, reset_day: int) -> int:
    local = start_utc.astimezone(tz)
    month, year = (1, local.year + 1) if local.month == 12 else (local.month + 1, local.year)
    nxt = datetime.combine(date(year, month, max(1, min(int(reset_day), 28))), datetime.min.time(), tzinfo=tz)
    return (nxt - local).days


def record_run(conn: sqlite3.Connection, now_utc: datetime, status: str, calls: int, detail: dict[str, int] | None = None) -> None:
    """``detail``: calls per endpoint for this run (JSON-encoded); None when the caller has no breakdown."""
    conn.execute(
        "INSERT OR REPLACE INTO solaredge_runs (time, status, calls, detail) VALUES (?, ?, ?, ?)",
        (now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), status, int(calls), json.dumps(detail, sort_keys=True) if detail else None),
    )
    conn.commit()


def grant_info(token_path: Path, now_utc: datetime) -> dict[str, Any]:
    try:
        pair = solaredge_client.TokenStore(token_path).load()
    except solaredge_client.SolarEdgeError:
        pair = None
    if pair is None or pair.granted_at is None:
        return {"known": False, "days_left": None}
    granted = datetime.fromtimestamp(pair.granted_at, UTC)
    expires = granted + GRANT_LIFETIME
    return {
        "known": True,
        "granted_at": granted.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_left": (expires - now_utc).days,
    }


def build_status(
    conn: sqlite3.Connection,
    token_path: Path,
    now_utc: datetime,
    tz: ZoneInfo,
    *,
    reset_day: int = DEFAULT_RESET_DAY,
    credit_limit: int = DEFAULT_CREDIT_LIMIT,
) -> dict[str, Any]:
    enabled_row = conn.execute("SELECT solaredge_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
    # 'baseline' / 'backfill' rows only feed the credit tally; they are not poller runs.
    last_run = conn.execute(
        "SELECT time, status FROM solaredge_runs WHERE status NOT IN ('baseline', 'backfill') ORDER BY time DESC LIMIT 1"
    ).fetchone()
    last_ok = conn.execute("SELECT MAX(time) AS t FROM solaredge_runs WHERE status = 'ok'").fetchone()["t"]
    start = credit_month_start(now_utc, tz, reset_day)
    used = conn.execute(
        "SELECT COALESCE(SUM(calls), 0) AS n FROM solaredge_runs WHERE time >= ?", (start.strftime("%Y-%m-%dT%H:%M:%SZ"),)
    ).fetchone()["n"]
    # Projection = what is already used + the STEADY rate over the rest of the cycle. One-time bursts
    # (the history backfill, the manual baseline) are counted once, never extrapolated.
    one_off = conn.execute(
        "SELECT COALESCE(SUM(calls), 0) AS n FROM solaredge_runs WHERE time >= ? AND status IN ('baseline', 'backfill')",
        (start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchone()["n"]
    elapsed_days = max((now_utc - start).total_seconds() / 86400.0, 0.5)
    cycle_days = month_length_days(start, tz, reset_day)
    steady_per_day = (used - one_off) / elapsed_days
    projected = round(used + steady_per_day * max(cycle_days - elapsed_days, 0.0))
    overview = conn.execute("SELECT time, day, production_wh FROM solaredge_overview ORDER BY time DESC LIMIT 1").fetchone()
    power = conn.execute("SELECT MAX(time) AS t FROM solaredge_power").fetchone()["t"]
    today = now_utc.astimezone(tz).date().isoformat()
    return {
        "enabled": bool(enabled_row["v"]) if enabled_row else False,
        "last_run": {"time": last_run["time"], "status": last_run["status"]} if last_run else None,
        # Job health: age of the latest run of ANY outcome (night runs are 'skipped_night'), so it stays
        # fresh around the clock. minutes_since_ok below is data freshness and grows overnight by design.
        "minutes_since_run": round((now_utc - _parse(last_run["time"])).total_seconds() / 60.0, 1) if last_run else None,
        "last_ok_time": last_ok,
        "minutes_since_ok": round((now_utc - _parse(last_ok)).total_seconds() / 60.0, 1) if last_ok else None,
        "credits": {
            "limit": int(credit_limit),
            "used_estimate": int(used),
            "left_estimate": max(int(credit_limit) - int(used), 0),
            "month_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "projected_month_total": int(projected),
        },
        "data": {
            "newest_overview_time": overview["time"] if overview else None,
            "newest_power_time": power,
            "today_production_wh": overview["production_wh"] if overview and overview["day"] == today else None,
        },
        "grant": grant_info(token_path, now_utc),
    }


def recent_power(conn: sqlite3.Connection, now_utc: datetime, hours: int = 48) -> list[dict[str, Any]]:
    since = (now_utc - timedelta(hours=max(1, min(int(hours), 24 * 14)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT time, power_w FROM solaredge_power WHERE time >= ? ORDER BY time", (since,)).fetchall()
    return [{"time": r["time"], "power_w": r["power_w"]} for r in rows]
