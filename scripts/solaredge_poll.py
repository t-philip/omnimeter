"""One SolarEdge poll. Run every 30 minutes by the ``solaredge`` compose service (or any scheduler).

    python -m scripts.solaredge_poll

Exit codes: 0 = ok / nothing to do / transient rate limit; 1 = failure the operator
should look at. An authorisation failure (revoked or rotated-away
grant) sends ONE Telegram alert -- a flag file suppresses repeats until a poll
succeeds again -- because retrying cannot fix it and only a human can
re-authorise (``python -m scripts.solaredge_authorize``, see the README's SolarEdge section).
A second one-shot warning goes out when the 24-month grant is within
EXPIRY_WARN_DAYS of ending.

Every run (any outcome) is logged to solaredge_runs with the number of HTTP
requests it sent, which is what the monitoring dashboard's job-health and
credit-balance estimate in /api/solaredge/status are built on.

Secrets: client id/secret come from src.credentials (credential files first, then the
environment). Nothing here prints or logs a credential; only statuses.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from src import (
    credentials,
    db,
    localtime,
    solaredge_client,
    solaredge_ingest,
    solaredge_status,
    telegram_notifier,
)

log = logging.getLogger("omnimeter.solaredge")

TOKEN_FILE_ENV = "OMNIMETER_SOLAREDGE_TOKEN_FILE"


def _token_path() -> Path:
    override = os.environ.get(TOKEN_FILE_ENV)
    return Path(override) if override else db.resolve_db_path().parent / "solaredge_token.json"


def _alert_flag(token_path: Path) -> Path:
    return token_path.with_name("solaredge_auth_alerted")


def _expiry_flag(token_path: Path) -> Path:
    return token_path.with_name("solaredge_expiry_warned")


def _warn_if_grant_ending(conn, token_path: Path, now: datetime) -> None:
    """One Telegram warning when the grant has <= EXPIRY_WARN_DAYS left. The flag is set only after a
    successful send (so a failed delivery is retried next run) and cleared by a fresh authorisation
    (the new grant's days_left is back near 730)."""
    flag = _expiry_flag(token_path)
    days_left = solaredge_status.grant_info(token_path, now).get("days_left")
    if days_left is None:
        return
    if days_left > solaredge_status.EXPIRY_WARN_DAYS:
        flag.unlink(missing_ok=True)
        return
    if flag.exists():
        return
    status, _err = telegram_notifier.send_message(
        conn,
        f"OmniMeter: the SolarEdge access grant ends in about {max(days_left, 0)} day(s). "
        "Re-authorise before then: run python -m scripts.solaredge_authorize on the OmniMeter host.",
    )
    if status == "sent":
        flag.touch(mode=0o600)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    credentials.configure(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))
    creds = credentials.default_credential_source()

    conn = db.get_connection()
    db.init_db(conn)
    if not solaredge_ingest.solaredge_enabled(conn):
        print("solaredge is disabled (feature_toggles.solaredge_enabled = 0) -- nothing to do")
        return 0

    now = datetime.now(UTC)
    client_id, client_secret = creds.get("SOLAREDGE_CLIENT_ID"), creds.get("SOLAREDGE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("SOLAREDGE_CLIENT_ID / SOLAREDGE_CLIENT_SECRET not available -- cannot poll")
        solaredge_status.record_run(conn, now, "api_error", 0)
        return 1

    solaredge_client.harden_logging([client_secret])
    token_path = _token_path()
    store = solaredge_client.TokenStore(token_path)
    client = solaredge_client.SolarEdgeClient(client_id=client_id, client_secret=client_secret, store=store)
    flag = _alert_flag(token_path)

    def finish(status: str, code: int) -> int:
        conn.rollback()
        solaredge_status.record_run(conn, now, status, client.requests_made, client.requests_by_endpoint)
        return code

    try:
        pair = store.load()
        if pair is None or pair.site_id is None:
            raise solaredge_client.SolarEdgeAuthError("no token/site on file; run the one-time authorisation")
        result = solaredge_ingest.poll_once(conn, client, pair.site_id, now, localtime.LOCAL_TZ)
    except solaredge_client.SolarEdgeAuthError as exc:
        log.error("solaredge authorisation needed: %s", exc)
        if not flag.exists():
            status, _err = telegram_notifier.send_message(
                conn, "OmniMeter: SolarEdge access needs re-authorising (token refused). Polling is paused until then."
            )
            if status == "sent":
                flag.touch(mode=0o600)
        return finish("auth_error", 1)
    except solaredge_client.SolarEdgeRateLimitError as exc:
        log.warning("solaredge rate/credit limit (%s); credit_exhausted=%s", exc, exc.credit_exhausted)
        return finish("rate_limited", 0)
    except (solaredge_client.SolarEdgeApiError, ValueError) as exc:
        log.error("solaredge poll failed: %s", exc)
        return finish("api_error", 1)

    flag.unlink(missing_ok=True)
    solaredge_status.record_run(
        conn, now, "ok" if result.get("calls") else "skipped_night", client.requests_made, client.requests_by_endpoint
    )
    _warn_if_grant_ending(conn, token_path, now)
    print(f"solaredge: {result}")
    if client.header_names:  # record any usage-related response headers (diagnostics)
        print(f"solaredge: usage headers {client.usage_headers or 'none'}; all response headers {client.header_names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
