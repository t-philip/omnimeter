"""Calendar-quarter comparison (Comparison tab -> Quarters sub-tab).

Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec. Two views share one
metric builder:

* :func:`compare` -- any two quarters side by side (for example Q1 of one year vs Q3 of another).
* :func:`series`  -- one quarter across every year that has data, with the
  change from each year to the previous one.

Every metric carries its own completeness record, so a quarter with gaps is
tagged with exactly which parameters are incomplete and why, instead of a gap
silently reading as a low total. Nothing here fabricates data: a metric with
no data at all is ``None`` ("no data"), never 0.

Totals are returned together with a per-day figure (total / days that actually
have data), because quarters differ in length (90-92 days) and an in-progress
quarter is shorter still.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

from . import aggregate, costs

#: Heating-degree-day base temperature (deg C). 18 is the usual Dutch/KNMI
#: convention: a day's heating demand is ``max(0, base - mean temperature)``.
HDD_BASE_C = 18.0

_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")


# --------------------------------------------------------------------------
# Quarter arithmetic
# --------------------------------------------------------------------------

def parse_quarter(value: str) -> tuple[int, int]:
    """``"1998-Q1"`` -> ``(1998, 1)``. Raises ``ValueError`` on anything else."""
    m = _QUARTER_RE.match(value or "")
    if not m:
        raise ValueError(f"invalid quarter {value!r}, expected YYYY-Qn (e.g. 1998-Q1)")
    year = int(m.group(1))
    if not 1900 <= year <= 2200:
        raise ValueError(f"invalid quarter {value!r}: year out of range")
    return year, int(m.group(2))


def quarter_id(year: int, q: int) -> str:
    return f"{year:04d}-Q{q}"


def quarter_bounds(year: int, q: int) -> tuple[str, str]:
    """First and last ISO date of a calendar quarter."""
    first_month = 3 * (q - 1) + 1
    last_month = first_month + 2
    last_day = calendar.monthrange(year, last_month)[1]
    return f"{year:04d}-{first_month:02d}-01", f"{year:04d}-{last_month:02d}-{last_day:02d}"


def quarter_of(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _days_between(first: str, last: str) -> int:
    return (date.fromisoformat(last) - date.fromisoformat(first)).days + 1


# --------------------------------------------------------------------------
# Metric catalogue
# --------------------------------------------------------------------------

#: key, label, unit, group, decimals, sum_like. ``sum_like`` metrics have a
#: meaningful per-day figure (total / days with data); the rest are already
#: averages or ratios, so their per-day figure equals the value.
METRICS = [
    ("power_import_kwh", "power import", "kWh", "Power", 2, True),
    ("power_export_kwh", "power export", "kWh", "Power", 2, True),
    ("power_net_kwh", "power net (import - export)", "kWh", "Power", 2, True),
    ("gas_m3", "Gas", "m³", "Gas", 2, True),
    ("water_l", "water", "L", "Water", 0, True),
    ("solar_kwh", "solar production", "kWh", "Solar", 2, True),
    ("cost_power_eur", "cost - power (net of export)", "€", "Cost", 2, True),
    ("cost_gas_eur", "cost - gas", "€", "Cost", 2, True),
    ("cost_total_eur", "cost - total", "€", "Cost", 2, True),
    ("battery_charge_kwh", "battery charge", "kWh", "Battery", 2, True),
    ("battery_discharge_kwh", "battery discharge", "kWh", "Battery", 2, True),
    ("avg_headcount", "average headcount", "people", "Household", 1, False),
    ("avg_temp_c", "average temperature", "°C", "Weather", 1, False),
    ("hdd", "heating degree days", "HDD", "Weather", 0, True),
    ("gas_per_hdd", "gas per heating degree day", "m³/HDD", "Weather", 3, False),
]
# Labels are stored lowercase and capitalised once here.
_META = {
    k: dict(key=k, label=lab[:1].upper() + lab[1:], unit=u, group=g, decimals=d, sum_like=s)
    for k, lab, u, g, d, s in METRICS
}


def _table_exists(conn, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _metric(key: str, *, value, days_with_data: int, days_in_quarter: int, in_progress: bool,
            elapsed_days: int, extra_issues=None, unavailable_reason=None) -> dict:
    """Assemble one metric record, including its completeness tag."""
    meta = _META[key]
    base = {
        "key": key,
        "label": meta["label"],
        "unit": meta["unit"],
        "group": meta["group"],
        "decimals": meta["decimals"],
    }
    if unavailable_reason is not None:
        return {
            **base,
            "available": False,
            "value": None,
            "per_day": None,
            "days_with_data": 0,
            "days_in_quarter": days_in_quarter,
            "complete": False,
            "issues": [unavailable_reason],
        }
    issues = list(extra_issues or [])
    if days_with_data == 0:
        issues.insert(0, "no data for this quarter")
        value = None
    else:
        # Compare against days that have *happened* so far: an in-progress
        # quarter is flagged once, as "in progress", not also as N missing days.
        missing = max(0, elapsed_days - days_with_data)
        if missing:
            issues.insert(0, f"{missing} of {elapsed_days} days have no data")
    if in_progress:
        issues.append(f"quarter still in progress ({elapsed_days} of {days_in_quarter} days elapsed)")
    if value is None:
        per_day = None
    elif meta["sum_like"]:
        per_day = value / days_with_data if days_with_data else None
    else:
        per_day = value
    return {
        **base,
        "available": True,
        "value": value,
        "per_day": per_day,
        "days_with_data": days_with_data,
        "days_in_quarter": days_in_quarter,
        "complete": not issues,
        "issues": issues,
    }


def quarter_metrics(conn, year: int, q: int, today: date) -> dict:
    """Every metric for one calendar quarter, each tagged complete/incomplete."""
    first, last = quarter_bounds(year, q)
    days_in_quarter = _days_between(first, last)
    # "Elapsed" = days up to and including *yesterday*: today's rollup is a
    # partial day, so it is not held against a metric's completeness.
    through = min(date.fromisoformat(last), today - timedelta(days=1))
    elapsed_days = max(0, (through - date.fromisoformat(first)).days + 1)
    in_progress = date.fromisoformat(last) >= today
    common: dict[str, Any] = dict(
        days_in_quarter=days_in_quarter, in_progress=in_progress, elapsed_days=elapsed_days
    )

    def sums(sql, params=(first, last)):
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else {}

    out: dict[str, dict] = {}

    p = sums(
        "SELECT COUNT(*) AS n, SUM(import_kwh) AS i, SUM(export_kwh) AS e, SUM(net_kwh) AS net "
        "FROM power_daily WHERE date >= ? AND date <= ?"
    )
    n = p.get("n") or 0
    out["power_import_kwh"] = _metric("power_import_kwh", value=p.get("i"), days_with_data=n, **common)
    out["power_export_kwh"] = _metric("power_export_kwh", value=p.get("e"), days_with_data=n, **common)
    out["power_net_kwh"] = _metric("power_net_kwh", value=p.get("net"), days_with_data=n, **common)

    g = sums("SELECT COUNT(*) AS n, SUM(usage_m3) AS v FROM gas_daily WHERE date >= ? AND date <= ?")
    out["gas_m3"] = _metric("gas_m3", value=g.get("v"), days_with_data=g.get("n") or 0, **common)

    w = sums("SELECT COUNT(*) AS n, SUM(usage_l) AS v FROM water_daily WHERE date >= ? AND date <= ?")
    out["water_l"] = _metric("water_l", value=w.get("v"), days_with_data=w.get("n") or 0, **common)

    # Solar exists only where an optional solar-production table is present. A missing
    # table is "not available here", which is different from "no data yet".
    if _table_exists(conn, "solaredge_daily"):
        s = sums(
            "SELECT COUNT(*) AS n, SUM(production_wh) AS v FROM solaredge_daily WHERE date >= ? AND date <= ?"
        )
        wh = s.get("v")
        out["solar_kwh"] = _metric(
            "solar_kwh", value=(wh / 1000.0) if wh is not None else None,
            days_with_data=s.get("n") or 0, **common,
        )
    else:
        out["solar_kwh"] = _metric(
            "solar_kwh", value=None, days_with_data=0, unavailable_reason="solar data is not set up on this install",
            **common,
        )

    out.update(_cost_metrics(conn, first, last, common))

    b = sums(
        "SELECT COUNT(*) AS n, SUM(charge_kwh) AS c, SUM(discharge_kwh) AS d "
        "FROM battery_daily WHERE date >= ? AND date <= ?"
    )
    bn = b.get("n") or 0
    out["battery_charge_kwh"] = _metric("battery_charge_kwh", value=b.get("c"), days_with_data=bn, **common)
    out["battery_discharge_kwh"] = _metric("battery_discharge_kwh", value=b.get("d"), days_with_data=bn, **common)

    # Occupancy: manual log. Away days count as 0, unlogged days are excluded
    # (not assumed), exactly as the existing Comparison tab treats them.
    occupancy_rows = [dict(r) for r in conn.execute("SELECT * FROM occupancy_log").fetchall()]
    by_day = aggregate.expand_occupancy_by_day(occupancy_rows, first, last)
    out["avg_headcount"] = _metric(
        "avg_headcount",
        value=(sum(by_day.values()) / len(by_day)) if by_day else None,
        days_with_data=len(by_day),
        **common,
    )

    out.update(_weather_metrics(conn, first, last, common))

    return {
        "quarter": quarter_id(year, q),
        "year": year,
        "q": q,
        "from": first,
        "to": last,
        "days_in_quarter": days_in_quarter,
        "elapsed_days": elapsed_days,
        "in_progress": in_progress,
        "metrics": out,
        "incomplete": [m["label"] for m in out.values() if not m["complete"]],
    }


def _cost_metrics(conn, first: str, last: str, common: dict[str, Any]) -> dict:
    result = costs.daily_costs(conn, first, last)
    keys = ("cost_power_eur", "cost_gas_eur", "cost_total_eur")
    if not result["available"]:
        return {
            k: _metric(k, value=None, days_with_data=0, unavailable_reason="no rate schedule configured", **common)
            for k in keys
        }
    days = result["days"]
    out = {}
    for key, field, stale_field in (
        ("cost_power_eur", "power_cost_eur", "power_stale"),
        ("cost_gas_eur", "gas_cost_eur", "gas_stale"),
        ("cost_total_eur", "total_cost_eur", None),
    ):
        priced = [d for d in days if d[field] is not None]
        stale = sum(1 for d in priced if (d["stale"] if stale_field is None else d[stale_field]))
        extra = []
        if stale:
            extra.append(f"{stale} days priced with an expired rate (renewal not loaded yet)")
        out[key] = _metric(
            key,
            value=sum(d[field] for d in priced) if priced else None,
            days_with_data=len(priced),
            extra_issues=extra,
            **common,
        )
    return out


def _weather_metrics(conn, first: str, last: str, common: dict[str, Any]) -> dict:
    rows = conn.execute(
        "SELECT date, temperature_2m_mean AS t FROM weather_daily "
        "WHERE date >= ? AND date <= ? AND temperature_2m_mean IS NOT NULL",
        (first, last),
    ).fetchall()
    temps = {r["date"]: r["t"] for r in rows}
    n = len(temps)
    avg_t = (sum(temps.values()) / n) if n else None
    hdd = sum(max(0.0, HDD_BASE_C - t) for t in temps.values()) if n else None

    # Gas per HDD uses only days that have BOTH gas and a temperature, so a gap
    # in one series cannot skew the ratio.
    gas_by_day = {
        r["date"]: r["v"]
        for r in conn.execute(
            "SELECT date, usage_m3 AS v FROM gas_daily WHERE date >= ? AND date <= ? AND usage_m3 IS NOT NULL",
            (first, last),
        ).fetchall()
    }
    both = [d for d in gas_by_day if d in temps]
    joint_hdd = sum(max(0.0, HDD_BASE_C - temps[d]) for d in both)
    joint_gas = sum(gas_by_day[d] for d in both)
    ratio = (joint_gas / joint_hdd) if both and joint_hdd > 0 else None
    ratio_issues = []
    if both and joint_hdd == 0:
        ratio_issues.append("no heating degree days in this quarter, ratio undefined")

    return {
        "avg_temp_c": _metric("avg_temp_c", value=avg_t, days_with_data=n, **common),
        "hdd": _metric("hdd", value=hdd, days_with_data=n, **common),
        "gas_per_hdd": _metric("gas_per_hdd", value=ratio, days_with_data=len(both), extra_issues=ratio_issues, **common),
    }


# --------------------------------------------------------------------------
# Deltas
# --------------------------------------------------------------------------

def _delta(a, b):
    """``(b - a, % change)``. % is ``None`` when it is undefined (a missing or 0)."""
    if a is None or b is None:
        return None, None
    d = b - a
    pct = (d / abs(a) * 100.0) if a != 0 else None
    return d, pct


def _row(a_metric: dict, b_metric: dict) -> dict:
    d, dp = _delta(a_metric["value"], b_metric["value"])
    dd, ddp = _delta(a_metric["per_day"], b_metric["per_day"])
    return {
        "key": a_metric["key"],
        "label": a_metric["label"],
        "unit": a_metric["unit"],
        "group": a_metric["group"],
        "decimals": a_metric["decimals"],
        "sum_like": _META[a_metric["key"]]["sum_like"],
        "a": a_metric,
        "b": b_metric,
        "delta": d,
        "delta_pct": dp,
        "delta_per_day": dd,
        "delta_pct_per_day": ddp,
    }


# --------------------------------------------------------------------------
# Public views
# --------------------------------------------------------------------------

def compare(conn, a: str, b: str, today: date) -> dict:
    """Quarter ``a`` against quarter ``b`` (both ``YYYY-Qn``)."""
    qa = quarter_metrics(conn, *parse_quarter(a), today)
    qb = quarter_metrics(conn, *parse_quarter(b), today)
    rows = [_row(qa["metrics"][k], qb["metrics"][k]) for k, *_ in METRICS]
    return {
        "a": {k: v for k, v in qa.items() if k != "metrics"},
        "b": {k: v for k, v in qb.items() if k != "metrics"},
        "rows": rows,
    }


def _data_span(conn) -> tuple[str, str] | None:
    """Earliest and latest date across every daily source, or None if empty."""
    tables = ["power_daily", "gas_daily", "water_daily", "battery_daily", "weather_daily"]
    if _table_exists(conn, "solaredge_daily"):
        tables.append("solaredge_daily")
    lows, highs = [], []
    for t in tables:
        row = conn.execute(f"SELECT MIN(date) AS lo, MAX(date) AS hi FROM {t}").fetchone()
        if row and row["lo"]:
            lows.append(row["lo"])
            highs.append(row["hi"])
    return (min(lows), max(highs)) if lows else None


def available_quarters(conn, today: date) -> dict:
    """Every quarter from the first data to the current one, newest first."""
    span = _data_span(conn)
    if span is None:
        return {"quarters": [], "years": []}
    y, q = quarter_of(date.fromisoformat(span[0]))
    end_y, end_q = quarter_of(date.fromisoformat(span[1]))
    out = []
    while (y, q) <= (end_y, end_q):
        first, last = quarter_bounds(y, q)
        out.append(
            {
                "quarter": quarter_id(y, q),
                "year": y,
                "q": q,
                "from": first,
                "to": last,
                "in_progress": date.fromisoformat(last) >= today,
            }
        )
        q += 1
        if q == 5:
            y, q = y + 1, 1
    out.reverse()
    return {"quarters": out, "years": sorted({x["year"] for x in out}, reverse=True)}


def series(conn, q: int, today: date) -> dict:
    """Quarter ``q`` for every year with data, plus the change vs the previous year."""
    if q not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1, 2, 3 or 4")
    years = sorted({x["year"] for x in available_quarters(conn, today)["quarters"] if x["q"] == q})
    quarters = [quarter_metrics(conn, y, q, today) for y in years]
    rows = []
    d: float | None
    dp: float | None
    dd: float | None
    ddp: float | None
    for key, *_ in METRICS:
        cells = []
        prev = None
        for qm in quarters:
            m = qm["metrics"][key]
            cell = {"year": qm["year"], **{k: m[k] for k in
                    ("value", "per_day", "available", "complete", "issues", "days_with_data", "days_in_quarter")}}
            if prev is not None:
                d, dp = _delta(prev["value"], m["value"])
                dd, ddp = _delta(prev["per_day"], m["per_day"])
            else:
                d = dp = dd = ddp = None
            cell.update(delta_vs_prev=d, delta_pct_vs_prev=dp,
                        delta_per_day_vs_prev=dd, delta_pct_per_day_vs_prev=ddp)
            cells.append(cell)
            prev = m
        meta = _META[key]
        rows.append({k: meta[k] for k in ("key", "label", "unit", "group", "decimals", "sum_like")} | {"years": cells})
    return {
        "q": q,
        "years": [
            {k: qm[k] for k in ("quarter", "year", "from", "to", "in_progress", "incomplete")} for qm in quarters
        ],
        "rows": rows,
    }
