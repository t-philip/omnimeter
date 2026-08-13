"""Daily weather from Open-Meteo's historical archive.

Why this exists at all: solar_estimate.estimate_daily_production() has no
weather input. It is a fixed monthly curve scaled by rated kWp and divided
evenly across the month, so it returns the *identical* figure for every day
of a month -- measured on real data, 16.30 kWh for both 2026-07-08 (65% of
median radiation) and 2026-07-10 (109%). reconcile_with_export() only floors
that at actual export, so an individual day's estimate can be badly wrong
even though the monthly total roughly works out.

`shortwave_radiation_sum` (MJ/m2) is the variable worth having: it is very
nearly proportional to PV output, and unlike grid export it is not a
*residual*. Export is production minus household load minus battery
charging, so a modest dip in production wipes out the exportable surplus
almost entirely -- on 2026-07-08 a 35% drop in radiation produced a 99% drop
in export. Reasoning from export alone is how that day got misread as
"heavily overcast" when it was merely dull.

Licence obligation: the data is CC BY 4.0. Any UI that DISPLAYS these values
must carry the credit "Weather data by Open-Meteo.com" (linked), cached
values included. Phase 1 only stores; the credit lands with the first UI
that shows it.

Verified before this module was written, and both assumptions that would
otherwise have shaped it turned out wrong:
  - There is NO multi-day archive lag; the archive returned data through the
    current day. A "fetch recent days from the forecast endpoint" design was
    therefore unnecessary.
  - The archive and forecast endpoints DISAGREE by up to 60% on the same
    date (2026-07-26: 15.83 vs 6.35 MJ/m2), so they must never be mixed.
    This module talks to the archive only, and records `source` so a future
    change of mind is detectable rather than silent.
"""

import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from . import localtime

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# CC BY 4.0 requires this credit "next to any location Open-Meteo data are
# displayed" -- cached and derived values included. Kept as a constant and
# returned by the API so a frontend physically cannot render weather-derived
# figures without having been handed the credit to show.
ATTRIBUTION = {"text": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com/"}
SOURCE_ARCHIVE = "archive"

_DAILY_FIELDS = (
    "shortwave_radiation_sum",
    "sunshine_duration",
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
)

# Decimal places kept per precision mode. Measured against the real API:
# 52.3080/4.8560, 52.31/4.86 and 52.3/4.9 all return byte-identical values,
# because the reanalysis grid is ~9 km and discards finer precision before
# use. Even a point 40 km away differed by only 0.2%, against a day-to-day
# signal of ~68%. So coarsening is free, and `coarse` is the default: the
# request would otherwise carry a home address, and rounding to a shared grid
# means many users in one region send an identical coordinate.
_PRECISION_DP = {"coarse": 1, "precise": 2, "exact": None}
DEFAULT_PRECISION = "coarse"


class WeatherConfigError(RuntimeError):
    """Latitude/longitude missing or unusable."""


def coarsen(latitude: float, longitude: float, precision: str = DEFAULT_PRECISION) -> tuple[float, float]:
    """Round coordinates according to the configured precision mode.

    Applied in code rather than left to the user's .env, so pasting exact
    coordinates in still yields a coarsened request by default."""
    if precision not in _PRECISION_DP:
        raise WeatherConfigError(
            f"unknown precision {precision!r} -- expected one of {sorted(_PRECISION_DP)}"
        )
    dp = _PRECISION_DP[precision]
    if dp is None:
        return latitude, longitude
    return round(latitude, dp), round(longitude, dp)


def configured_location() -> tuple[float, float]:
    """Coordinates from the environment, already coarsened."""
    lat = os.environ.get("OMNIMETER_WEATHER_LATITUDE")
    lon = os.environ.get("OMNIMETER_WEATHER_LONGITUDE")
    if not lat or not lon:
        raise WeatherConfigError(
            "OMNIMETER_WEATHER_LATITUDE and OMNIMETER_WEATHER_LONGITUDE must both be set"
        )
    try:
        lat_f, lon_f = float(lat), float(lon)
    except ValueError:
        raise WeatherConfigError("latitude/longitude must be numbers") from None
    if not (-90 <= lat_f <= 90) or not (-180 <= lon_f <= 180):
        raise WeatherConfigError("latitude/longitude out of range")
    precision = os.environ.get("OMNIMETER_WEATHER_LOCATION_PRECISION", DEFAULT_PRECISION)
    return coarsen(lat_f, lon_f, precision)


def weather_enabled(conn: sqlite3.Connection) -> bool:
    """Opt-in switch. Defaults to off -- this is the only part of OmniMeter
    that talks to the internet, and the request carries coordinates."""
    row = conn.execute("SELECT weather_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
    return bool(row["v"]) if row else False


def build_url(latitude: float, longitude: float, start_date: str, end_date: str) -> str:
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date,
            "end_date": end_date,
            "daily": ",".join(_DAILY_FIELDS),
            # Open-Meteo buckets its daily aggregates in this zone, so it must
            # be the same one the rest of the app buckets readings in, or a
            # "day" of weather won't line up with a day of consumption.
            "timezone": localtime.TIMEZONE_NAME,
        }
    )
    return f"{ARCHIVE_URL}?{query}"


def fetch_range(latitude: float, longitude: float, start_date: str, end_date: str, timeout: int = 60) -> list[dict]:
    """One request covering a whole date range -- a five-year backfill is a
    single call, which is why the free tier's 10,000/day limit is never a
    consideration here. Days the archive has no value for are skipped rather
    than stored as nulls that would later be indistinguishable from a real
    zero."""
    url = build_url(latitude, longitude, start_date, end_date)
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed https host
        payload = json.load(response)

    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    out: list[dict] = []
    for i, day in enumerate(dates):
        values = {field: (daily.get(field) or [None] * len(dates))[i] for field in _DAILY_FIELDS}
        if all(v is None for v in values.values()):
            continue
        out.append({"date": day, **values})
    return out


def store(conn: sqlite3.Connection, rows: list[dict], latitude: float, longitude: float) -> int:
    """Upsert by date. Re-fetching an existing date overwrites it and stamps a
    new fetched_at, so a later revision is visible rather than silently
    merged."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO weather_daily "
        "(date, shortwave_radiation_sum, sunshine_duration_s, temperature_2m_max, "
        " temperature_2m_min, temperature_2m_mean, latitude, longitude, source, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r["date"],
                r["shortwave_radiation_sum"],
                r["sunshine_duration"],
                r["temperature_2m_max"],
                r["temperature_2m_min"],
                r["temperature_2m_mean"],
                latitude,
                longitude,
                SOURCE_ARCHIVE,
                now,
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def covered_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT MIN(date) AS f, MAX(date) AS l FROM weather_daily").fetchone()
    return (row["f"], row["l"]) if row else (None, None)


def meter_data_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Earliest and latest date any category actually has data for -- the
    range worth fetching weather over. Nothing is gained by holding weather
    for dates with no consumption to correlate it against."""
    firsts, lasts = [], []
    for table in ("power_daily", "gas_daily", "water_daily", "battery_daily"):
        row = conn.execute(f"SELECT MIN(date) AS f, MAX(date) AS l FROM {table}").fetchone()
        if row and row["f"]:
            firsts.append(row["f"])
            lasts.append(row["l"])
    if not firsts:
        return None, None
    return min(firsts), max(lasts)


DAYS_PER_YEAR = 365.0
# Half-width of the calendar window used to decide what is "typical" for a
# date. +/-10 days pools ~21 days per year of history, enough to be stable
# without smearing across a season -- late October must not be averaged with
# mid-November when the heating curve is moving fast.
TYPICAL_WINDOW_DAYS = 10


def reference_annual_radiation(conn: sqlite3.Connection) -> float | None:
    """Mean daily radiation scaled to a year -- the denominator that turns a
    day's radiation into a share of the annual production budget.

    Mean-times-365 rather than an actual calendar-year sum, so a partial year
    (the current one) cannot inflate every day's share."""
    row = conn.execute(
        "SELECT AVG(shortwave_radiation_sum) AS m FROM weather_daily "
        "WHERE shortwave_radiation_sum IS NOT NULL"
    ).fetchone()
    if row is None or row["m"] is None:
        return None
    return float(row["m"]) * DAYS_PER_YEAR


def radiation_by_date(conn: sqlite3.Connection, date_from: str, date_to: str) -> dict[str, float]:
    return {
        r["date"]: r["shortwave_radiation_sum"]
        for r in conn.execute(
            "SELECT date, shortwave_radiation_sum FROM weather_daily "
            "WHERE date >= ? AND date <= ? AND shortwave_radiation_sum IS NOT NULL",
            (date_from, date_to),
        )
    }


def _typical_by_day_of_year(values_by_date: dict[str, float]) -> dict[str, float]:
    """{MM-DD: median value within +/-TYPICAL_WINDOW_DAYS across all years
    held}. Shared pooling core for both typical_radiation_by_day_of_year and
    typical_heating_degree_days_by_day_of_year below -- same "what's normal
    for this date" question, asked of two different daily metrics.

    "Typical" has to be seasonal, not a flat average: this location's
    radiation swings from ~2.0 MJ/m2 in December to ~22.6 in June, a factor
    of ~11 -- an absolute number carries no meaning without that context,
    which is exactly why the UI shows a percentage of typical rather than a
    raw figure. Median, not mean, so one freak day cannot move the
    reference."""
    if not values_by_date:
        return {}

    by_doy: dict[int, list[float]] = {}
    for date_str, v in values_by_date.items():
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        by_doy.setdefault(d.timetuple().tm_yday, []).append(v)

    typical: dict[str, float] = {}
    for doy in range(1, 367):
        pooled: list[float] = []
        for offset in range(-TYPICAL_WINDOW_DAYS, TYPICAL_WINDOW_DAYS + 1):
            # Wrap around the year end so 1 January pools with late December.
            neighbour = (doy - 1 + offset) % 366 + 1
            pooled.extend(by_doy.get(neighbour, []))
        if not pooled:
            continue
        pooled.sort()
        n = len(pooled)
        median = pooled[n // 2] if n % 2 else (pooled[n // 2 - 1] + pooled[n // 2]) / 2.0
        # Key by MM-DD via a leap year, so 29 February has its own entry.
        key = (datetime(2024, 1, 1) + timedelta(days=doy - 1)).strftime("%m-%d")
        typical[key] = median
    return typical


def typical_radiation_by_day_of_year(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        "SELECT date, shortwave_radiation_sum AS v FROM weather_daily "
        "WHERE shortwave_radiation_sum IS NOT NULL"
    ).fetchall()
    return _typical_by_day_of_year({r["date"]: r["v"] for r in rows})


# Base temperature for heating-degree-days: the widely-used NOAA/international
# convention (65 F = 18.3 C, commonly rounded to 18 C) -- below this, a
# household is assumed to be running its heating. Deliberately not exposed as
# an env var, same reasoning as solar_estimate.py's DEFAULT_SPECIFIC_YIELD:
# one documented default beats a config knob nobody has grounds to tune
# without their own gas-vs-temperature data to calibrate against.
DEFAULT_HDD_BASE_C = 18.0


def heating_degree_days(mean_temp_c: float | None, base_c: float = DEFAULT_HDD_BASE_C) -> float | None:
    """Degrees the day's mean temperature fell below base_c, floored at 0 --
    zero on a day mild enough that heating wouldn't be running. This is the
    reason temperature_2m_mean is stored at all (see db.py's schema
    comment): it explains gas usage far better than a flat seasonal curve,
    the same problem shortwave_radiation_sum solves for solar above."""
    if mean_temp_c is None:
        return None
    return max(0.0, base_c - mean_temp_c)


def heating_degree_days_by_date(
    conn: sqlite3.Connection, date_from: str, date_to: str, base_c: float = DEFAULT_HDD_BASE_C
) -> dict[str, float]:
    # heating_degree_days() is Optional-in/Optional-out (a single reading may
    # be missing), but the WHERE clause here already excludes NULL means --
    # narrowed explicitly below so the dict stays float-only for callers.
    result: dict[str, float] = {}
    for r in conn.execute(
        "SELECT date, temperature_2m_mean FROM weather_daily "
        "WHERE date >= ? AND date <= ? AND temperature_2m_mean IS NOT NULL",
        (date_from, date_to),
    ):
        hdd = heating_degree_days(r["temperature_2m_mean"], base_c)
        if hdd is not None:
            result[r["date"]] = hdd
    return result


def typical_heating_degree_days_by_day_of_year(
    conn: sqlite3.Connection, base_c: float = DEFAULT_HDD_BASE_C
) -> dict[str, float]:
    """Same seasonal-median reasoning as typical_radiation_by_day_of_year,
    applied to heating-degree-days instead of radiation -- so a cold day in
    October (unusual, heating season hasn't started) and an equally cold day
    in January (routine) read differently, the same way an equally sunny day
    in June and December already do."""
    rows = conn.execute(
        "SELECT date, temperature_2m_mean AS v FROM weather_daily WHERE temperature_2m_mean IS NOT NULL"
    ).fetchall()
    values_by_date: dict[str, float] = {}
    for r in rows:
        hdd = heating_degree_days(r["v"], base_c)
        if hdd is not None:
            values_by_date[r["date"]] = hdd
    return _typical_by_day_of_year(values_by_date)
