"""One-time (re-)authorisation of the SolarEdge Site Access grant .

Runs on the OmniMeter host itself, so the client secret and the token pair never
reach the operator's workstation. Only the browser step can be remote: the operator
opens the printed URL, signs in as the SolarEdge *owner* (not the developer-portal
login) and approves. When the browser is on another machine, forward the callback
port to this host first (``ssh -L 8888:127.0.0.1:8888 <host>``). In Docker the
listener must bind inside the container -- set ``OMNIMETER_SOLAREDGE_AUTH_BIND=0.0.0.0``
and publish the port on loopback only (see docker-compose.yml, ``solaredge-authorize``).

Why it is shaped this way:

* SolarEdge does **not** echo ``state`` back on the callback -- only ``code``,
  ``site_id`` and ``external_id``. So the anti-forgery nonce rides in ``external_id``.
* Browsers and probes hit the callback port with stray requests (favicon, preconnect).
  Anything without the matching nonce is answered 404 and ignored -- never fatal.
* The callback code is single-use and useless without the client secret, but it is
  still never printed.

Prints statuses only -- never a code, secret or token. Exit 0 = grant stored and
verified; 1 = failed (nothing half-written: the token file is replaced atomically
and only after a successful exchange).
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from src import credentials, db, solaredge_client

AUTHORIZE_URL = "https://connect.solaredge.com/authorize"
DEFAULT_PORT = 8888
DEFAULT_TIMEOUT_S = 600
GRANT_MONTHS = 24  # the vendor's maximum
SCOPES = "SITE_DATA DEVICE_DATA"
TOKEN_FILE_ENV = "OMNIMETER_SOLAREDGE_TOKEN_FILE"
BIND_ENV = "OMNIMETER_SOLAREDGE_AUTH_BIND"


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/callback"


def build_authorize_url(client_id: str, port: int, nonce: str, months: int = GRANT_MONTHS) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": SCOPES,
            "redirect_uri": redirect_uri(port),
            "external_id": nonce,
            "access_duration": months,
        },
        quote_via=urllib.parse.quote,
    )
    return f"{AUTHORIZE_URL}?{query}"


class _Handler(BaseHTTPRequestHandler):
    nonce = ""
    result: tuple[str, int] | None = None
    strays = 0

    def log_message(self, *_args: object) -> None:  # silence: a request line can carry the code
        return

    def _reply(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        code = (q.get("code") or [""])[0]
        site = (q.get("site_id") or [""])[0]
        ok = (
            parsed.path == "/callback"
            and secrets.compare_digest((q.get("external_id") or [""])[0], self.nonce)
            and bool(code)
            and site.isdigit()
        )
        if not ok:
            type(self).strays += 1
            self._reply(404, "not found")
            return
        type(self).result = (code, int(site))
        self._reply(200, "SolarEdge authorisation received. You can close this tab.")


def make_server(port: int, nonce: str) -> HTTPServer:
    """Bind loopback by default (``OMNIMETER_SOLAREDGE_AUTH_BIND`` overrides it for a container,
    whose published port must still be loopback-only on the host). A fresh handler class per
    server keeps state out of globals."""
    handler = type("Handler", (_Handler,), {"nonce": nonce, "result": None, "strays": 0})
    return HTTPServer((os.environ.get(BIND_ENV, "127.0.0.1"), port), handler)


def wait_for_callback(server: HTTPServer, timeout_s: float) -> tuple[str, int] | None:
    """Serve until the genuine callback arrives or the deadline passes."""
    handler = server.RequestHandlerClass
    deadline = time.monotonic() + timeout_s
    while handler.result is None:  # type: ignore[attr-defined]
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        server.timeout = min(left, 1.0)
        server.handle_request()
    return handler.result  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="seconds to wait for the approval")
    ap.add_argument("--open-browser", action="store_true", help="also try to open the URL (usually not useful on a server)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
    credentials.configure(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))
    creds = credentials.default_credential_source()
    client_id, client_secret = creds.get("SOLAREDGE_CLIENT_ID"), creds.get("SOLAREDGE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("SOLAREDGE_CLIENT_ID / SOLAREDGE_CLIENT_SECRET not available -- cannot authorise")
        return 1
    solaredge_client.harden_logging([client_secret])

    override = os.environ.get(TOKEN_FILE_ENV)
    token_path = Path(override) if override else db.resolve_db_path().parent / "solaredge_token.json"
    store = solaredge_client.TokenStore(token_path)
    client = solaredge_client.SolarEdgeClient(client_id=client_id, client_secret=client_secret, store=store)

    nonce = secrets.token_hex(16)
    try:
        server = make_server(args.port, nonce)
    except OSError as exc:
        print(f"cannot listen on port {args.port} ({exc.strerror}); is another authorisation already running?")
        return 1

    url = build_authorize_url(client_id, args.port, nonce)
    print("1. If your browser is on another machine, forward the callback port to this host first (ssh -L).")
    print("2. Open this URL, sign in as the SolarEdge OWNER account, and click Approve:\n")
    print(f"   {url}\n")
    print(f"Waiting up to {args.timeout}s on port {args.port} ...", flush=True)
    if args.open_browser:
        webbrowser.open(url)

    try:
        got = wait_for_callback(server, args.timeout)
    finally:
        server.server_close()
    strays = server.RequestHandlerClass.strays  # type: ignore[attr-defined]
    if got is None:
        print(f"timed out waiting for the callback ({strays} stray request(s) ignored); nothing changed")
        return 1
    code, site_id = got
    print(f"callback received (site_id {site_id}; {strays} stray request(s) ignored)")

    try:
        client.exchange_code(code, redirect_uri(args.port), site_id)
        print("token exchange ok; token pair stored (mode 600)")
        overview = client.get(f"/sites/{site_id}/overview")
    except solaredge_client.SolarEdgeError as exc:
        print(f"FAILED: {exc}")
        return 1
    production = (overview.get("production") or {}).get("total")
    print(f"verified: overview HTTP 200, production.total {'present' if production is not None else 'MISSING'}")

    token_path.with_name("solaredge_auth_alerted").unlink(missing_ok=True)  # re-arm the alert-once flag
    print("done. The next poll (within 30 min) resumes; run python -m scripts.solaredge_poll to poll now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
