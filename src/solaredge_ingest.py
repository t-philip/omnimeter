"""SolarEdge poller logic: fetch -> normalise -> store. No I/O of its own
beyond the injected client and SQLite connection, so it is fully unit-testable.

Two calls, deliberately unequal in frequency (the free tier is 2,000 credits/month):

* ``/sites/{id}/overview`` every run -- cumulative production since local midnight,
  so a day's total is exact whenever it is read.
* ``/sites/{id}/power`` (15-minute buckets, up to 12 h per call) only when the newest
  stored bucket is older than ``POWER_STALE_AFTER`` -- one call refills the gap, so an
  outage heals itself and steady state costs ~1 extra call per hour.

Only daylight runs call out at all (``DAYLIGHT_START_HOUR``..``DAYLIGHT_END_HOUR`` local).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from . import solaredge_history

DAYLIGHT_START_HOUR = 6  # inclusive, local time
DAYLIGHT_END_HOUR = 21  # exclusive, local time
POWER_STALE_AFTER = timedelta(minutes=60)
POWER_MAX_SPAN = timedelta(hours=11, minutes=45)  # API allows 12 h at QUARTER_HOUR; stay inside it
_UNIT_TO_WH = {"WH": 1.0, "KWH": 1_000.0, "MWH": 1_000_000.0, "GWH": 1_000_000_000.0}


class _Client(Protocol):
    requests_made: int

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]: ...


def solaredge_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT solaredge_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
    return bool(row["v"]) if row else False


def in_daylight(now_utc: datetime, tz: ZoneInfo) -> bool:
    hour = now_utc.astimezone(tz).hour
    return DAYLIGHT_START_HOUR <= hour < DAYLIGHT_END_HOUR


def to_wh(value: Any, unit: str | None) -> float | None:
    """Normalise an API energy figure to Wh. ``None`` stays ``None`` (no meter)."""
    if value is None:
        return None
    factor = _UNIT_TO_WH.get((unit or "WH").upper())
    if factor is None:
        raise ValueError(f"unknown energy unit {unit!r}")
    return float(value) * factor


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_overview(conn: sqlite3.Connection, now_utc: datetime, tz: ZoneInfo, body: dict[str, Any]) -> None:
    prod = body.get("production") or {}
    cons = body.get("consumption") or {}
    pu, cu = prod.get("unit"), cons.get("unit")
    conn.execute(
        "INSERT OR REPLACE INTO solaredge_overview "
        "(time, day, production_wh, to_self_wh, to_storage_wh, to_grid_wh, "
        " consumption_wh, from_pv_wh, from_storage_wh, from_grid_wh) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            _iso_utc(now_utc),
            now_utc.astimezone(tz).date().isoformat(),
            to_wh(prod.get("total"), pu),
            to_wh(prod.get("toSelfConsumption"), pu),
            to_wh(prod.get("toStorage"), pu),
            to_wh(prod.get("toGrid"), pu),
            to_wh(cons.get("total"), cu),
            to_wh(cons.get("fromPv"), cu),
            to_wh(cons.get("fromStorage"), cu),
            to_wh(cons.get("fromGrid"), cu),
        ),
    )


def newest_power_bucket(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(time) AS t FROM solaredge_power").fetchone()
    if not row or row["t"] is None:
        return None
    return datetime.strptime(row["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def power_window(conn: sqlite3.Connection, now_utc: datetime) -> tuple[datetime, datetime] | None:
    """The (from, to) to fetch, or None if the stored buckets are fresh enough."""
    newest = newest_power_bucket(conn)
    earliest = now_utc - POWER_MAX_SPAN
    if newest is None:
        return earliest, now_utc
    if now_utc - newest < POWER_STALE_AFTER:
        return None
    # Re-fetch the newest bucket too: it may have been partial when stored.
    return max(newest, earliest), now_utc


def store_power(conn: sqlite3.Connection, body: dict[str, Any]) -> int:
    unit = (body.get("unit") or "W").upper()
    scale = {"W": 1.0, "KW": 1_000.0, "MW": 1_000_000.0}.get(unit)
    if scale is None:
        raise ValueError(f"unknown power unit {unit!r}")
    n = 0
    for v in body.get("values") or []:
        if v.get("value") is None or not v.get("timestamp"):
            continue
        ts = datetime.fromisoformat(str(v["timestamp"]))
        conn.execute(
            "INSERT OR REPLACE INTO solaredge_power (time, power_w) VALUES (?, ?)",
            (_iso_utc(ts), float(v["value"]) * scale),
        )
        n += 1
    return n


def poll_once(
    conn: sqlite3.Connection,
    client: _Client,
    site_id: int,
    now_utc: datetime,
    tz: ZoneInfo,
) -> dict[str, Any]:
    """One poll. Returns a status dict (no secrets) for the caller to print.

    Raises the client's own exceptions unchanged -- the caller decides what an
    auth failure or a rate limit means for the process exit code.
    """
    if not in_daylight(now_utc, tz):
        return {"status": "skipped: outside daylight window", "calls": 0}

    calls = 0
    overview = client.get(f"/sites/{site_id}/overview")
    calls += 1
    store_overview(conn, now_utc, tz, overview)

    power_rows = 0
    window = power_window(conn, now_utc)
    if window is not None:
        frm, to = window
        power = client.get(
            f"/sites/{site_id}/power",
            {"from": _iso_utc(frm), "to": _iso_utc(to), "resolution": "QUARTER_HOUR", "unit": "W"},
        )
        calls += 1
        power_rows = store_power(conn, power)

    # Once a day (first daylight run after midnight): store yesterday's authoritative total, and heal any
    # gap since the newest stored day. Zero calls when already up to date.
    daily_calls = solaredge_history.finalise_recent(conn, client, site_id, now_utc, tz)
    calls += daily_calls
    conn.commit()
    return {"status": "ok", "calls": calls, "power_rows": power_rows, "daily_calls": daily_calls}
