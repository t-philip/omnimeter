"""SolarEdge daily production history: the one-time backfill and the daily top-up.

``/sites/{id}/energy`` with ``resolution=DAY`` returns one value per site-local day (timestamp at
local midnight with its UTC offset, end exclusive) and accepts at most one month per call, so
history since installation costs about one call per month. All logic here takes an injected client
and a ``sleep`` so it is unit-testable without a network or real delays.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from . import solaredge_client

#: The API allows up to 1 month of DAY data per call; stay inside it.
MAX_DAYS_PER_CALL = 31
#: Free tier is 10 calls/minute; one call per 7 s leaves headroom.
DEFAULT_PAUSE_S = 7.0


class _Client(Protocol):
    requests_made: int

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]: ...


def _local_midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=tz)


def month_windows(first: date, end_exclusive: date) -> list[tuple[date, date]]:
    """Calendar-month windows [start, end) covering first..end_exclusive-1, clipped at both ends."""
    out: list[tuple[date, date]] = []
    cur = first
    while cur < end_exclusive:
        nxt = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        out.append((cur, min(nxt, end_exclusive)))
        cur = nxt
    return out


def fetch_days(client: _Client, site_id: int, start: date, end_exclusive: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    if (end_exclusive - start).days > MAX_DAYS_PER_CALL:
        raise ValueError("window exceeds the API's one-month limit for DAY resolution")
    body = client.get(
        f"/sites/{site_id}/energy",
        {
            "from": _local_midnight(start, tz).isoformat(),
            "to": _local_midnight(end_exclusive, tz).isoformat(),
            "resolution": "DAY",
            "unit": "WH",
        },
    )
    return list(body.get("values") or [])


def fetch_days_retrying(
    client: _Client,
    site_id: int,
    start: date,
    end_exclusive: date,
    tz: ZoneInfo,
    *,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    attempts: int = 3,
) -> list[dict[str, Any]]:
    """fetch_days, riding out the PER-MINUTE rate limit (another caller -- e.g. the 30-minute poller -- can share the
    same 10 calls/minute). The monthly CREDIT cap is never retried: waiting cannot help."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch_days(client, site_id, start, end_exclusive, tz)
        except solaredge_client.SolarEdgeRateLimitError as exc:
            if exc.credit_exhausted or attempt == attempts:
                raise
            wait = float(exc.retry_after_s or 30)
            log(f"  rate-limited (per-minute); waiting {wait:.0f}s then retrying ({attempt}/{attempts - 1})")
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def store_daily(conn: sqlite3.Connection, values: list[dict[str, Any]], now_utc: datetime) -> int:
    """Upsert one row per day. Days with no value (before installation / no data) are skipped."""
    stamp = now_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for v in values:
        if v.get("value") is None or not v.get("timestamp"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO solaredge_daily (date, production_wh, fetched_at) VALUES (?, ?, ?)",
            (str(v["timestamp"])[:10], float(v["value"]), stamp),
        )
        n += 1
    return n


def stored_days_in(conn: sqlite3.Connection, start: date, end_exclusive: date) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM solaredge_daily WHERE date >= ? AND date < ?", (start.isoformat(), end_exclusive.isoformat())
    ).fetchone()["n"]


def newest_daily(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MAX(date) AS d FROM solaredge_daily").fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def finalise_recent(conn: sqlite3.Connection, client: _Client, site_id: int, now_utc: datetime, tz: ZoneInfo) -> int:
    """Daily top-up: make sure yesterday (and any gap since the newest stored day, up to a month) is stored.
    Costs one call, once per day, and zero when already up to date. Returns the calls made.

    A gap that the API cannot fill (no value for those days) is asked about at most ONCE per local day:
    ``solaredge_gap_attempts`` remembers the attempt. Without it every 30-minute run re-asked and burned
    a credit each time until data arrived."""
    today = now_utc.astimezone(tz).date()
    yesterday = today - timedelta(days=1)
    newest = newest_daily(conn)
    if newest is not None and newest >= yesterday:
        return 0
    if conn.execute("SELECT 1 FROM solaredge_gap_attempts WHERE date = ?", (today.isoformat(),)).fetchone():
        return 0
    start = yesterday if newest is None else max(newest + timedelta(days=1), today - timedelta(days=MAX_DAYS_PER_CALL))
    store_daily(conn, fetch_days(client, site_id, start, today, tz), now_utc)
    # Recorded only after the call succeeded: an API/rate-limit error propagates above and is retried next run.
    conn.execute(
        "INSERT OR REPLACE INTO solaredge_gap_attempts (date, attempted_at) VALUES (?, ?)",
        (today.isoformat(), now_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.execute("DELETE FROM solaredge_gap_attempts WHERE date < ?", ((today - timedelta(days=7)).isoformat(),))
    return 1


def backfill(
    conn: sqlite3.Connection,
    client: _Client,
    site_id: int,
    first: date,
    today: date,
    tz: ZoneInfo,
    *,
    pause_s: float = DEFAULT_PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    now_utc: datetime | None = None,
) -> dict[str, int]:
    """Fill ``first .. yesterday`` month by month. Idempotent and resumable: a month whose days are all
    stored is skipped (no call). Commits after every month so an interruption keeps what was fetched."""
    now_utc = now_utc or datetime.now(UTC)
    windows = month_windows(first, today)  # today is exclusive: the in-progress day is never stored here
    calls = rows = skipped = 0
    for start, end in windows:
        if stored_days_in(conn, start, end) >= (end - start).days:
            skipped += 1
            continue
        if calls:
            sleep(pause_s)
        values = fetch_days_retrying(client, site_id, start, end, tz, sleep=sleep, log=log)
        calls += 1
        rows += store_daily(conn, values, now_utc)
        conn.commit()
        log(f"  {start.isoformat()[:7]}: {len(values)} days returned")
    return {"windows": len(windows), "calls": calls, "rows": rows, "skipped_months": skipped}


def yearly_check(
    conn: sqlite3.Connection, client: _Client, site_id: int, first: date, today: date, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """One call: compare each COMPLETE calendar year's stored daily sum with SolarEdge's own yearly total."""
    body = client.get(
        f"/sites/{site_id}/energy",
        {
            "from": _local_midnight(date(first.year, 1, 1), tz).isoformat(),
            "to": _local_midnight(today, tz).isoformat(),
            "resolution": "YEAR",
            "unit": "KWH",
        },
    )
    out = []
    for v in body.get("values") or []:
        if v.get("value") is None:
            continue
        year = int(str(v["timestamp"])[:4])
        s = conn.execute(
            "SELECT COALESCE(SUM(production_wh), 0) / 1000.0 AS kwh, COUNT(*) AS n FROM solaredge_daily WHERE date >= ? AND date < ?",
            (f"{year}-01-01", f"{year + 1}-01-01"),
        ).fetchone()
        api_kwh = float(v["value"])
        out.append(
            {
                "year": year,
                "daily_sum_kwh": round(s["kwh"], 1),
                "api_kwh": round(api_kwh, 1),
                "days": s["n"],
                "complete_year": year < today.year,
                "ok": abs(s["kwh"] - api_kwh) <= max(5.0, 0.01 * api_kwh),
            }
        )
    return out
