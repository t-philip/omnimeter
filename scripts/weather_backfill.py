"""Fetch daily weather for the range the meters actually cover.

Safe to re-run: dates are upserted by date, so a second run refreshes rather
than duplicates. Fetches only the dates not already stored unless --refresh
is passed.

    python -m scripts.weather_backfill            # fill gaps only
    python -m scripts.weather_backfill --refresh  # re-fetch the whole range
    python -m scripts.weather_backfill --dry-run  # show what would happen
"""

import sys
from datetime import date, timedelta

from src import db, weather


def main(argv: list[str]) -> int:
    refresh = "--refresh" in argv
    dry_run = "--dry-run" in argv

    conn = db.get_connection()
    db.init_db(conn)

    if not weather.weather_enabled(conn):
        print("weather is disabled (feature_toggles.weather_enabled = 0) -- nothing to do")
        return 0

    try:
        lat, lon = weather.configured_location()
    except weather.WeatherConfigError as exc:
        print(f"configuration error: {exc}")
        return 1

    # Both halves of each pair are tested, not just the first. They come from
    # one MIN()/MAX() row apiece, so in practice they are None together and
    # testing one implied the other -- but nothing enforces that, and the
    # partner stayed Optional to every reader and type-checker, leaving the
    # uses further down (fromisoformat, the > compare, fetch_range) formally
    # able to receive None.
    first, last = weather.meter_data_range(conn)
    if first is None or last is None:
        print("no meter data yet -- nothing to correlate weather against")
        return 0

    have_first, have_last = weather.covered_range(conn)
    if refresh or have_first is None or have_last is None:
        start, end = first, last
    else:
        # Only the uncovered tail/head. History never changes, so the common
        # case is a single new day.
        start = first if first < have_first else (date.fromisoformat(have_last) + timedelta(days=1)).isoformat()
        end = last
        if start > end:
            print(f"weather already covers {have_first}..{have_last} -- nothing to fetch")
            return 0

    print(f"location  : {lat}, {lon}  (coarsened per OMNIMETER_WEATHER_LOCATION_PRECISION)")
    print(f"meter data: {first} .. {last}")
    print(f"stored    : {have_first} .. {have_last}")
    print(f"fetching  : {start} .. {end}")

    if dry_run:
        print("\nDRY RUN -- no request made, nothing written.")
        return 0

    try:
        rows = weather.fetch_range(lat, lon, start, end)
    except (OSError, ValueError) as exc:
        # OSError covers URLError/HTTPError/timeouts; ValueError covers a
        # malformed (non-JSON) response. Either way: fail cleanly, touch
        # nothing, and let the next scheduled run retry -- a public
        # self-hoster's box must not crash just because Open-Meteo is down,
        # and the dashboard already degrades gracefully per-day using
        # whatever weather_daily already holds (app.py's basis fallback).
        print(f"weather fetch failed: {exc} -- leaving existing weather_daily data untouched, will retry next run")
        return 1

    written = weather.store(conn, rows, lat, lon)
    new_first, new_last = weather.covered_range(conn)
    print(f"stored {written} day(s); weather_daily now covers {new_first} .. {new_last}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
