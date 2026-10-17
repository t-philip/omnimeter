"""One-time SolarEdge history backfill: daily PV production from installation to yesterday.

    python -m scripts.solaredge_backfill [--from YYYY-MM-DD] [--dry-run] [--yes]

Runs on the OmniMeter host with the poller's credentials. ~1 API call per calendar month
since the install date (about 12 per year of history), one call every ~7 s to respect the free tier's
10 calls/minute. Idempotent and resumable: months already stored are skipped, so it is safe to rerun.

Credit guard: refuses to start if the planned calls would take this billing cycle's ESTIMATED usage
past the limit (minus a safety margin). Every call is logged to solaredge_runs (status 'backfill') so the
dashboard's credit estimate includes it.

Prints statuses and counts only -- never a credential, token or any site-identifying detail.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from src import credentials, db, localtime, solaredge_client, solaredge_history, solaredge_status

SAFETY_MARGIN = 150  # credits kept free for the normal poller for the rest of the cycle
TOKEN_FILE_ENV = "OMNIMETER_SOLAREDGE_TOKEN_FILE"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="first", help="first day to fetch (default: the site's installation date)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and exit; makes no API calls")
    ap.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    ap.add_argument("--pause", type=float, default=solaredge_history.DEFAULT_PAUSE_S, help="seconds between calls")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
    credentials.configure(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))
    creds = credentials.default_credential_source()
    client_id, client_secret = creds.get("SOLAREDGE_CLIENT_ID"), creds.get("SOLAREDGE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("SOLAREDGE_CLIENT_ID / SOLAREDGE_CLIENT_SECRET not available -- cannot run")
        return 1
    solaredge_client.harden_logging([client_secret])

    override = os.environ.get(TOKEN_FILE_ENV)
    token_path = Path(override) if override else db.resolve_db_path().parent / "solaredge_token.json"
    store = solaredge_client.TokenStore(token_path)
    pair = store.load()
    if pair is None or pair.site_id is None:
        print("no token/site on file; run the one-time authorisation first")
        return 1
    client = solaredge_client.SolarEdgeClient(client_id=client_id, client_secret=client_secret, store=store)

    conn = db.get_connection()
    db.init_db(conn)
    tz = localtime.LOCAL_TZ
    now = datetime.now(UTC)
    today = now.astimezone(tz).date()

    try:
        if args.first:
            first = date.fromisoformat(args.first)
        else:
            details = client.get(f"/sites/{pair.site_id}")  # 1 call; only installationDate is used or shown
            first = date.fromisoformat(str(details["installationDate"])[:10])
        print(f"range: {first.isoformat()} .. {(today).isoformat()} (exclusive; today is never stored here)")

        todo = [
            w for w in solaredge_history.month_windows(first, today) if solaredge_history.stored_days_in(conn, *w) < (w[1] - w[0]).days
        ]
        planned = len(todo) + 1  # + the final yearly verification call
        credits = solaredge_status.build_status(conn, token_path, now, tz)["credits"]
        print(
            f"months to fetch: {len(todo)} (+1 verification call); "
            f"estimated credits used so far: {credits['used_estimate']} / {credits['limit']}"
        )
        if credits["used_estimate"] + planned > credits["limit"] - SAFETY_MARGIN:
            print(f"REFUSING: {planned} calls would leave under {SAFETY_MARGIN} credits free this cycle")
            return 1
        if args.dry_run:
            print("dry run: nothing fetched")
            return 0
        if not args.yes:
            print(f"about to make ~{planned} API calls (~{planned * args.pause / 60:.0f} min). Re-run with --yes to proceed.")
            return 0

        print("fetching ...", flush=True)
        result = solaredge_history.backfill(conn, client, pair.site_id, first, today, tz, pause_s=args.pause, now_utc=now)
        print(f"backfill: {result}")
        rows = solaredge_history.yearly_check(conn, client, pair.site_id, first, today, tz)
    except solaredge_client.SolarEdgeRateLimitError as exc:
        print(f"STOPPED by a SolarEdge limit ({exc}); progress is saved -- rerun later to resume")
        return 1
    except solaredge_client.SolarEdgeError as exc:
        print(f"FAILED: {exc}; progress is saved -- rerun to resume")
        return 1
    finally:
        solaredge_status.record_run(conn, now, "backfill", client.requests_made, client.requests_by_endpoint)

    bad = [r for r in rows if not r["ok"]]
    for r in rows:
        flag = "ok " if r["ok"] else "MISMATCH"
        print(
            f"  {r['year']}: daily sum {r['daily_sum_kwh']:8.1f} kWh vs SolarEdge yearly "
            f"{r['api_kwh']:8.1f} kWh  ({r['days']} days)  {flag}"
        )
    total_days = conn.execute("SELECT COUNT(*) AS n FROM solaredge_daily").fetchone()["n"]
    print(f"done: {total_days} daily rows stored; {client.requests_made} API calls this run")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
