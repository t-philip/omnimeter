"""Per-day energy cost from the rate schedules.

Shared by ``/api/costs`` (the Costs tab) and the Quarters comparison, so a
quarter's cost can never disagree with what the Costs tab shows for the same
days -- there is exactly one place that turns kWh / m³ into euros.

Cost is energy only: power is ``import * buy - export * sell`` and gas is
``m3 * price``. Fixed/standing fees are not modelled and water has no cost.
"""

from __future__ import annotations


def rate_for(rates, d: str):
    """Exact period match, or the most recent expired period as a stale
    fallback -- better than showing nothing when the latest rate renewal
    hasn't been uploaded yet. Returns ``(rate_row_or_None, is_stale)``."""
    for r in rates:
        if r["period_start"] <= d <= r["period_end"]:
            return r, False
    candidates = [r for r in rates if r["period_end"] < d]
    if not candidates:
        return None, False
    return max(candidates, key=lambda r: r["period_end"]), True


NO_RATES_REASON = "No rate schedule configured yet."


def has_rates(conn) -> bool:
    """True when at least one power or gas rate period is configured."""
    power = conn.execute("SELECT COUNT(*) AS c FROM rate_schedule").fetchone()["c"]
    gas = conn.execute("SELECT COUNT(*) AS c FROM gas_rate_schedule").fetchone()["c"]
    return power > 0 or gas > 0


def daily_costs(conn, f: str, t: str) -> dict:
    """Cost per day for every date in ``[f, t]`` that has power or gas data.

    Returns ``{"available": False, "reason": ...}`` when no rate schedule is
    configured at all, otherwise ``{"available": True, "days": [...],
    "stale_count": n}``. Each day carries ``power_cost_eur`` / ``gas_cost_eur``
    / ``total_cost_eur`` (``None`` when that part could not be priced), a
    combined ``stale`` flag, and ``power_stale`` / ``gas_stale`` so callers
    can say which part used an expired rate.
    """
    if not has_rates(conn):
        return {"available": False, "reason": NO_RATES_REASON}

    power_rows = {
        r["date"]: r
        for r in conn.execute(
            "SELECT date, import_kwh, export_kwh FROM power_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (f, t),
        ).fetchall()
    }
    gas_rows = {
        r["date"]: r
        for r in conn.execute(
            "SELECT date, usage_m3 FROM gas_daily WHERE date >= ? AND date <= ? ORDER BY date", (f, t)
        ).fetchall()
    }
    power_rates = conn.execute("SELECT * FROM rate_schedule ORDER BY period_start").fetchall()
    gas_rates = conn.execute("SELECT * FROM gas_rate_schedule ORDER BY period_start").fetchall()

    all_dates = sorted(set(power_rows) | set(gas_rows))
    results = []
    stale_count = 0
    for d in all_dates:
        power_cost = None
        gas_cost = None
        power_stale = False
        gas_stale = False
        if d in power_rows:
            rate, is_stale = rate_for(power_rates, d)
            if rate is not None:
                buy = (power_rows[d]["import_kwh"] or 0.0) * rate["buy_ct_per_kwh"] / 100.0
                sell = (power_rows[d]["export_kwh"] or 0.0) * rate["sell_ct_per_kwh"] / 100.0
                power_cost = round(buy - sell, 2)
                power_stale = is_stale
        if d in gas_rows:
            rate, is_stale = rate_for(gas_rates, d)
            if rate is not None:
                gas_cost = round((gas_rows[d]["usage_m3"] or 0.0) * rate["price_eur_per_m3"], 2)
                gas_stale = is_stale
        total = (
            round((power_cost or 0.0) + (gas_cost or 0.0), 2)
            if (power_cost is not None or gas_cost is not None)
            else None
        )
        stale = power_stale or gas_stale
        if stale:
            stale_count += 1
        results.append(
            {
                "date": d,
                "power_cost_eur": power_cost,
                "gas_cost_eur": gas_cost,
                "total_cost_eur": total,
                "stale": stale,
                "power_stale": power_stale,
                "gas_stale": gas_stale,
            }
        )
    return {"available": True, "days": results, "stale_count": stale_count}
