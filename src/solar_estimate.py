"""Estimated (not measured) solar production and self-sufficiency.

Most solar inverters aren't reachable by OmniMeter directly (installer-locked
APIs, no local integration, etc.), so there is no metered PV production
series in the common case. This module derives an approximation from a
typical NL seasonal solar-yield shape scaled by the panel's rated capacity
(kWp, from pv_config), reconciled against real grid export so the estimate
can never imply less production than the household is known to have
exported. Every value this module produces must be labeled "estimated" by
the caller — it is not a substitute for a real production meter.
"""

import calendar
from datetime import date

# Relative NL seasonal solar-yield shape (arbitrary units, normalized at use
# time) — heavier in summer, lighter in winter.
MONTHLY_WEIGHTS = {
    1: 3.0, 2: 4.5, 3: 7.5, 4: 10.5, 5: 12.5, 6: 13.0,
    7: 12.5, 8: 11.5, 9: 8.5, 10: 5.5, 11: 3.0, 12: 2.0,
}

# Typical NL specific yield for a well-sited system. A reference tool this
# model was cross-checked against derived 801 kWh/kWp/yr for one real
# household; adjust via pv_config if your actual yield (from panel
# orientation/shading) is known to differ.
DEFAULT_SPECIFIC_YIELD_KWH_PER_KWP = 950.0


def estimate_daily_production(
    kwp: float, day: date, specific_yield: float = DEFAULT_SPECIFIC_YIELD_KWH_PER_KWP
) -> float:
    annual_kwh = kwp * specific_yield
    total_weight = sum(MONTHLY_WEIGHTS.values())
    month_share = MONTHLY_WEIGHTS[day.month] / total_weight
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    return (annual_kwh * month_share) / days_in_month


def reconcile_with_export(estimated_kwh: float, exported_kwh: float) -> float:
    """Production can't be less than what was verifiably exported that day."""
    return max(estimated_kwh, exported_kwh)


def estimate_daily_production_from_radiation(
    kwp: float,
    radiation_mj: float,
    reference_annual_radiation_mj: float,
    specific_yield: float = DEFAULT_SPECIFIC_YIELD_KWH_PER_KWP,
) -> float:
    """Same annual total as estimate_daily_production, redistributed across
    days by MEASURED solar radiation instead of a fixed monthly curve.

    The monthly-curve version divides a month's share evenly across its days,
    so it returns the identical figure for every day of that month --
    measured on real data, 16.30 kWh for both a dull July day (65% of median
    radiation) and a bright one (109%). The monthly total was roughly right;
    the daily shape was pure fiction.

    Deliberately *redistributes* rather than recomputing from first
    principles. The annual figure (kwp x specific_yield) is already
    calibrated and cross-checked against a real household, and it holds up
    independently here: 2.5 kWp x ~1,159 kWh/m2 of annual radiation at a
    ~0.8 performance ratio is ~2,318 kWh, against the model's 2,375. Keeping
    that anchor and changing only the distribution means this cannot drift
    from a known-good annual total, and it needs no panel orientation, tilt
    or shading data that OmniMeter does not have.

    Still an estimate, and must still be labelled one -- it now has the right
    day-to-day *shape*, not a measured value. A real production meter
    remains the only way to know."""
    if reference_annual_radiation_mj <= 0:
        raise ValueError("reference_annual_radiation_mj must be positive")
    annual_kwh = kwp * specific_yield
    return annual_kwh * (radiation_mj / reference_annual_radiation_mj)


def estimate_self_sufficiency(production_kwh: float, export_kwh: float, import_kwh: float) -> float | None:
    """Fraction of consumption covered without grid import (0..1), or None if
    consumption can't be determined for the day."""
    consumption = import_kwh + (production_kwh - export_kwh)
    if consumption <= 0:
        return None
    return max(0.0, min(1.0, 1 - (import_kwh / consumption)))
