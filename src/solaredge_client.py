"""SolarEdge Monitoring API V2 client (OAuth 2.0 "Site Access", authorization-code grant).

Why this module is shaped the way it is -- the vendor facts it has to survive:

* The client secret travels in a JSON POST body to ``/v2/oauth2/token``; calls
  carry ``Authorization: Bearer <access_token>``. Nothing secret is ever put in
  a URL, so there is no URL-in-log leak by construction.
* The access token lives 2 hours. The refresh token is valid 30 days and
  **rotates on every refresh -- the old one dies immediately**. A crash between
  "the token endpoint replied" and "the new pair is on disk" loses *both*
  tokens and forces a manual re-authorisation. So the new pair is persisted
  atomically (tmp file + fsync + rename, mode 600) *before* the new access token
  is used for anything, and the whole read-refresh-write sequence runs under an
  exclusive file lock so two processes can never refresh from the same stale
  refresh token.
* ``invalid_grant`` / ``invalid_client`` from the token endpoint are
  deterministic: retrying cannot help, so they raise :class:`SolarEdgeAuthError`
  and the caller stops and alerts once.

Secrets are never put in an exception message or a log line: errors carry the
HTTP status and, for the token endpoint, only the RFC 6749 ``error`` code.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:  # fcntl is POSIX-only; where it is missing the lock is a no-op.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

BASE_URL = "https://monitoringapi.solaredge.com/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"
DEFAULT_TIMEOUT = 20

#: Response headers whose NAME suggests usage/limit information (their values are safe to log).
_USAGE_HEADER_RE = re.compile(r"ratelimit|rate-limit|credit|quota|usage|remaining|retry-after", re.IGNORECASE)

#: Refresh when the access token has less than this many seconds left.
REFRESH_MARGIN_S = 300

_log = logging.getLogger("omnimeter.solaredge")


class SolarEdgeError(Exception):
    """Base class. Messages never contain a credential."""


class SolarEdgeAuthError(SolarEdgeError):
    """The grant is unusable (revoked/expired/rotated away). Human must re-authorise."""


class SolarEdgeRateLimitError(SolarEdgeError):
    """HTTP 429. ``credit_exhausted`` distinguishes the monthly credit cap from the per-minute limit."""

    def __init__(self, message: str, *, credit_exhausted: bool, retry_after_s: int | None) -> None:
        super().__init__(message)
        self.credit_exhausted = credit_exhausted
        self.retry_after_s = retry_after_s


class SolarEdgeApiError(SolarEdgeError):
    """Any other non-2xx or transport failure."""


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds when the access token stops being valid
    site_id: int | None = None
    #: epoch seconds of the original authorisation (the vendor grant lasts <= 24 months from here).
    #: None for a token file written before this field existed.
    granted_at: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "site_id": self.site_id,
                "granted_at": self.granted_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> TokenPair:
        d = json.loads(raw)
        return cls(
            access_token=str(d["access_token"]),
            refresh_token=str(d["refresh_token"]),
            expires_at=float(d["expires_at"]),
            site_id=int(d["site_id"]) if d.get("site_id") is not None else None,
            granted_at=float(d["granted_at"]) if d.get("granted_at") is not None else None,
        )


class TokenStore:
    """The on-disk token pair: mode 600, atomically replaced, guarded by a lock file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive cross-process lock. Held across read -> refresh -> write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def load(self) -> TokenPair | None:
        try:
            return TokenPair.from_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError) as exc:
            # Never include the file content in the message.
            raise SolarEdgeAuthError("token file is unreadable; re-authorise") from exc

    def save(self, pair: TokenPair) -> None:
        """Atomic, durable replace: write tmp (0600) -> fsync -> rename -> fsync dir."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(pair.to_json())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        if hasattr(os, "O_DIRECTORY"):  # POSIX: make the rename itself durable
            dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)


class SolarEdgeClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        store: TokenStore,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._store = store
        self._session = session or requests.Session()
        self._clock = clock
        #: API requests (credit-bearing) sent by this client; the poller logs it because the API exposes
        #: no credit-balance header. The vendor does not count token-endpoint calls as credits.
        self.requests_made = 0
        #: The same tally split by endpoint (``overview`` / ``power`` / ``energy``...), logged per run so the
        #: breakdown is useful for diagnostics.
        self.requests_by_endpoint: dict[str, int] = {}
        #: Headers of the most recent API response whose name looks usage-related (rate limit, credit, quota,
        #: retry-after), lowercased. Recorded for diagnostics. Never auth headers.
        self.usage_headers: dict[str, str] = {}
        #: Names (never values) of every header on the most recent API response, for diagnostics.
        self.header_names: list[str] = []

    # -- token lifecycle -------------------------------------------------

    def _post_token(self, body: dict[str, str]) -> dict[str, Any]:
        try:
            resp = self._session.post(TOKEN_URL, json=body, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            # exc text can echo the request; report only the exception class.
            raise SolarEdgeApiError(f"token endpoint unreachable ({type(exc).__name__})") from None
        if resp.status_code == 200:
            try:
                return dict(resp.json())
            except ValueError:
                raise SolarEdgeApiError("token endpoint returned non-JSON") from None
        code = "unknown"
        if resp.status_code == 400:
            with contextlib.suppress(ValueError, AttributeError):
                code = str(resp.json().get("error", "unknown"))
            raise SolarEdgeAuthError(f"token endpoint rejected the request: {code}")
        raise SolarEdgeApiError(f"token endpoint HTTP {resp.status_code}")

    def _pair_from_response(self, data: dict[str, Any], site_id: int | None, granted_at: float | None) -> TokenPair:
        try:
            return TokenPair(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=self._clock() + float(data.get("expires_in", 7200)),
                site_id=site_id,
                granted_at=granted_at,
            )
        except (KeyError, TypeError, ValueError):
            raise SolarEdgeApiError("token endpoint response missing fields") from None

    def exchange_code(self, code: str, redirect_uri: str, site_id: int | None) -> TokenPair:
        """One-time authorisation: trade the callback code for the first pair and persist it."""
        data = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
            }
        )
        pair = self._pair_from_response(data, site_id, self._clock())
        with self._store.locked():
            self._store.save(pair)
        return pair

    def _refresh_locked(self, current: TokenPair) -> TokenPair:
        data = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        )
        new = self._pair_from_response(data, current.site_id, current.granted_at)
        # The old refresh token is already dead. Persist the new pair NOW,
        # before anything else can fail.
        self._store.save(new)
        return new

    def access_token(self, *, force_refresh: bool = False) -> TokenPair:
        """A pair whose access token is good for at least REFRESH_MARGIN_S more seconds."""
        with self._store.locked():
            pair = self._store.load()
            if pair is None:
                raise SolarEdgeAuthError("no token file; run the one-time authorisation")
            if force_refresh or pair.expires_at - self._clock() < REFRESH_MARGIN_S:
                pair = self._refresh_locked(pair)
            return pair

    # -- API calls -------------------------------------------------------

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        pair = self.access_token()
        resp = self._request(path, params, pair)
        if resp.status_code == 401:  # expired/revoked early: one forced refresh, then give up
            pair = self.access_token(force_refresh=True)
            resp = self._request(path, params, pair)
        return self._parse(resp, path)

    def _request(self, path: str, params: dict[str, str] | None, pair: TokenPair) -> requests.Response:
        self.requests_made += 1
        endpoint = path.rstrip("/").rsplit("/", 1)[-1] or "?"
        self.requests_by_endpoint[endpoint] = self.requests_by_endpoint.get(endpoint, 0) + 1
        try:
            resp = self._session.get(
                f"{BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {pair.access_token}", "Accept": "application/json"},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SolarEdgeApiError(f"{path}: unreachable ({type(exc).__name__})") from None
        self._note_headers(resp)
        return resp

    def _note_headers(self, resp: requests.Response) -> None:
        """Remember usage-looking headers + all header names of the latest response (values only for the former)."""
        lowered = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
        self.header_names = sorted(lowered)
        self.usage_headers = {k: v for k, v in lowered.items() if _USAGE_HEADER_RE.search(k)}

    @staticmethod
    def _parse(resp: requests.Response, path: str) -> dict[str, Any]:
        if resp.status_code == 200:
            try:
                return dict(resp.json())
            except ValueError:
                raise SolarEdgeApiError(f"{path}: non-JSON body") from None
        if resp.status_code == 401:
            raise SolarEdgeAuthError(f"{path}: HTTP 401 after refresh")
        if resp.status_code == 429:
            # Docs: remaining-minute == 0 -> per-minute rate limit; > 0 -> monthly credit cap.
            remaining = resp.headers.get("x-ratelimit-remaining-minute")
            retry = resp.headers.get("retry-after")
            raise SolarEdgeRateLimitError(
                f"{path}: HTTP 429",
                credit_exhausted=remaining not in (None, "0"),
                retry_after_s=int(retry) if retry and retry.isdigit() else None,
            )
        raise SolarEdgeApiError(f"{path}: HTTP {resp.status_code}")


class _RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Longest first so a secret that contains another is scrubbed whole.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            text = record.getMessage()
            scrubbed = text
            for s in self._secrets:
                scrubbed = scrubbed.replace(s, "«redacted»")
            if scrubbed != text:
                record.msg, record.args = scrubbed, None
            if record.exc_text:
                for s in self._secrets:
                    record.exc_text = record.exc_text.replace(s, "«redacted»")
        return True


def harden_logging(secrets: list[str]) -> None:
    """Hop 2: HTTP client loggers off DEBUG/INFO, plus a value-redaction filter on the root logger
    and its handlers. Call once at startup, after the credential values are known."""
    for name in ("requests", "urllib3", "urllib3.connectionpool"):
        logging.getLogger(name).setLevel(logging.WARNING)
    filt = _RedactingFilter(secrets)
    root = logging.getLogger()
    root.addFilter(filt)
    for handler in root.handlers:
        handler.addFilter(filt)
