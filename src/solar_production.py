"""Measured solar production (from the inverter's own readings) and a battery-aware self-sufficiency.

OmniMeter's grid and battery meters never see PV production, so it was ESTIMATED from the panel
capacity (src/solar_estimate.py). When a SolarEdge poller is running (``solaredge_daily`` /
``solaredge_overview``, see src/solaredge_history.py) the real figure is available and is used instead;
the estimate remains the fallback for days without a measurement and for installs with no inverter
feed. The API reports the basis per day so the UI can say which one it is showing.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_EPS = 1e-9


def measured_by_date(conn: sqlite3.Connection, f: str, t: str, today: str) -> dict[str, dict[str, Any]]:
    """{date: {"kwh": float, "partial": bool}} for [f, t].

    Finished days come from solaredge_daily (authoritative /energy DAY totals). *Today* is the latest
    cumulative-since-midnight overview snapshot, flagged ``partial`` (it is "so far", not a projection).
    Tolerates the SolarEdge tables not existing at all (a plain install): returns {}.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        for r in conn.execute("SELECT date, production_wh FROM solaredge_daily WHERE date >= ? AND date <= ?", (f, t)):
            out[r["date"]] = {"kwh": r["production_wh"] / 1000.0, "partial": False}
        if f <= today <= t and today not in out:
            snap = conn.execute(
                "SELECT production_wh FROM solaredge_overview WHERE day = ? AND production_wh IS NOT NULL ORDER BY time DESC LIMIT 1",
                (today,),
            ).fetchone()
            if snap is not None:
                out[today] = {"kwh": snap["production_wh"] / 1000.0, "partial": True}
    except sqlite3.OperationalError:
        return {}
    return out


def production_source(days: list[dict[str, Any]]) -> str:
    """'measured' | 'estimated' | 'mixed' across the returned days (empty -> 'estimated')."""
    basis = {d["basis"] == "measured" for d in days}
    if basis == {True}:
        return "measured"
    if basis == {True, False}:
        return "mixed"
    return "estimated"


def self_sufficiency(production: float, export: float, import_: float, charge: float, discharge: float) -> float | None:
    """Share (0..1) of the energy the house used that was NOT grid energy, for one day. Battery-aware.

    Solar kept on site is ``production - export``; the grid contributes ``import_``. Whatever went into the
    battery is not (yet) consumed, and whatever came out is, so the day's load is
    ``kept + import_ + discharge - charge`` (the same load the Overview Sankey shows).

    The origin of battery energy is an ASSUMPTION, stated rather than hidden: charging takes solar surplus
    first and the grid only for the remainder (a solar-charged battery is the usual case; grid charging is also possible,
    and the remainder rule covers it). Discharge carries the same origin
    mix as the charge that filled it; with no charge today (draining earlier stock) it is attributed in
    proportion to today's solar/grid inputs. With no battery at all this is exactly the old
    ``1 - import / (import + production - export)``.
    """
    kept = max(production - export, 0.0)
    load = kept + import_ + discharge - charge
    if load <= 0.01:
        return None
    if charge > _EPS:
        solar_to_battery = min(charge, kept)
        solar_share_of_battery = solar_to_battery / charge
    else:
        solar_to_battery = 0.0
        solar_share_of_battery = kept / (kept + import_) if kept + import_ > _EPS else 0.0
    solar_origin_load = (kept - solar_to_battery) + discharge * solar_share_of_battery
    return max(0.0, min(1.0, solar_origin_load / load))


# --------------------------------------------------------------------------------------------------
# Solar tab: period statistics and all-time monthly history
# --------------------------------------------------------------------------------------------------


def period_stats(
    days: list[dict[str, Any]],
    kwp: float | None,
    export_by_date: dict[str, float],
) -> dict[str, Any]:
    """Headline numbers for the Solar tab from ``days`` = [{"date", "kwh", "partial"}].

    A *partial* day (today, measured so far) counts toward the total but is excluded from the average, the
    best/worst day and the home/grid split, because it is not a finished day. The home/grid split uses only
    days that have BOTH a production figure and a grid-meter row (grid data starts later than solar history).
    """
    total = sum(d["kwh"] for d in days)
    done = [d for d in days if not d.get("partial")]
    best = max(done, key=lambda d: d["kwh"], default=None)
    worst = min(done, key=lambda d: d["kwh"], default=None)
    both = [d for d in done if d["date"] in export_by_date and d["kwh"] > 0]
    prod_both = sum(d["kwh"] for d in both)
    exported = sum(min(export_by_date[d["date"]], d["kwh"]) for d in both)  # export can't exceed what was produced
    export_pct = round(exported / prod_both * 100, 1) if prod_both > 0 else None
    return {
        "total_kwh": round(total, 2),
        "days": len(days),
        "finished_days": len(done),
        "avg_kwh_per_day": round(sum(d["kwh"] for d in done) / len(done), 2) if done else None,
        "best": {"date": best["date"], "kwh": round(best["kwh"], 2)} if best else None,
        "worst": {"date": worst["date"], "kwh": round(worst["kwh"], 2)} if worst else None,
        "specific_yield_kwh_per_kwp": round(total / kwp, 1) if kwp else None,
        "export_pct": export_pct,
        "home_use_pct": round(100 - export_pct, 1) if export_pct is not None else None,
        "split_days": len(both),
    }


def monthly_history(conn: sqlite3.Connection) -> dict[str, Any]:
    """All-time monthly production from the stored daily totals, shaped for a year-over-year comparison.

    ``years`` maps "1999" -> 12 month totals (Jan..Dec, None where there is no data); ``months`` lists every
    month with its day count so the UI can mark a partial month; ``annual`` is the per-year total with the
    number of days covered. Returns ``available: False`` when there is no stored history.
    """
    try:
        rows = conn.execute(
            "SELECT substr(date, 1, 7) AS m, SUM(production_wh) / 1000.0 AS kwh, COUNT(*) AS n "
            "FROM solaredge_daily GROUP BY m ORDER BY m"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False}
    if not rows:
        return {"available": False}
    years: dict[str, list[float | None]] = {}
    months = []
    annual: dict[str, dict[str, Any]] = {}
    for r in rows:
        year, month = r["m"][:4], int(r["m"][5:7])
        years.setdefault(year, [None] * 12)[month - 1] = round(r["kwh"], 1)
        months.append({"month": r["m"], "kwh": round(r["kwh"], 1), "days": r["n"]})
        a = annual.setdefault(year, {"year": int(year), "kwh": 0.0, "days": 0})
        a["kwh"] += r["kwh"]
        a["days"] += r["n"]
    return {
        "available": True,
        "years": years,
        "months": months,
        "annual": [{"year": a["year"], "kwh": round(a["kwh"], 1), "days": a["days"]} for a in annual.values()],
    }


def yearly_weather(conn: sqlite3.Connection) -> dict[int, dict[str, float]]:
    """{year: {"days": n, "irradiation_kwh_m2": total}} from weather_daily.

    Radiation is stored as MJ/m2 per day; 3.6 MJ = 1 kWh. Days with no value are not counted (a partial year of
    weather is reported as such through ``days``, never scaled up). Sunshine *hours* are deliberately not used: the
    reanalysis' sunshine_duration is its own model definition (about 16 h on a June day, ~1.8x what weather stations
    report), so energy -- which is physical -- is what gets compared."""
    try:
        rows = conn.execute(
            "SELECT substr(date, 1, 4) AS y, COUNT(*) AS n, SUM(shortwave_radiation_sum) / 3.6 AS irr "
            "FROM weather_daily WHERE shortwave_radiation_sum IS NOT NULL GROUP BY y"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r["y"]): {"days": r["n"], "irradiation_kwh_m2": r["irr"]} for r in rows}


def annual_insights(
    annual: list[dict[str, Any]],
    weather: dict[int, dict[str, float]],
    kwp: float | None,
    current_year: int,
) -> list[dict[str, Any]]:
    """Annotate each annual row with yield, sun energy and comparisons that let years be judged fairly.

    A year is *complete* (comparable) when it is a past calendar year with >= 360 production days. Within the complete
    years: ``yield_vs_avg_pct`` compares each year's kWh/kWp to their mean, ``best``/``lowest`` flag the extremes, and
    ``sun_adjusted_index`` divides yield by that year's irradiation and scales so the average complete year = 100 (above
    100 = the panels did better than the sun alone explains); ``sun_vs_avg_pct`` is the year's irradiation against the
    mean of the complete years. The irradiation itself is shown for any year with weather data covering at least 95% of its
    production days; the comparisons additionally need the year to be complete.
    """
    out: list[dict[str, Any]] = []
    complete = [a for a in annual if a["year"] < current_year and a["days"] >= 360 and kwp]
    for a in annual:
        w = weather.get(a["year"]) or {}
        covered = bool(w) and w["days"] >= 0.95 * a["days"]
        out.append(
            {
                **a,
                "complete": a in complete,
                "yield_kwh_per_kwp": round(a["kwh"] / kwp, 1) if kwp and a in complete else None,
                "irradiation_kwh_m2": round(w["irradiation_kwh_m2"], 0) if covered else None,
                "sun_vs_avg_pct": None,
                "yield_vs_avg_pct": None,
                "best": False,
                "lowest": False,
                "sun_adjusted_index": None,
            }
        )
    done = [r for r in out if r["complete"]]
    if len(done) >= 2:
        mean_yield = sum(r["yield_kwh_per_kwp"] for r in done) / len(done)
        for r in done:
            r["yield_vs_avg_pct"] = round((r["yield_kwh_per_kwp"] / mean_yield - 1) * 100, 1)
        top = max(done, key=lambda r: r["yield_kwh_per_kwp"])
        low = min(done, key=lambda r: r["yield_kwh_per_kwp"])
        top["best"], low["lowest"] = True, True
        sunny = [r for r in done if r["irradiation_kwh_m2"]]
        if len(sunny) >= 2:
            perf = {r["year"]: r["yield_kwh_per_kwp"] / r["irradiation_kwh_m2"] for r in sunny}
            mean_perf = sum(perf.values()) / len(perf)
            mean_irr = sum(r["irradiation_kwh_m2"] for r in sunny) / len(sunny)
            for r in sunny:
                r["sun_adjusted_index"] = round(perf[r["year"]] / mean_perf * 100, 0)
                r["sun_vs_avg_pct"] = round((r["irradiation_kwh_m2"] / mean_irr - 1) * 100, 1)
    return out
