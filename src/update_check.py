"""Checks GitHub for a newer OmniMeter release than the one currently running.

Opt-in via feature_toggles.update_check_enabled -- same consent pattern as
weather.weather_enabled (see that module's docstring): this is the second
thing in OmniMeter that talks to the internet, unrelated to the household's
own meters, and a self-hosting user should decide that for themselves
rather than discover it. Defaults off.

Cached in-process rather than in the database: the only two things this
needs to remember are "what did GitHub last say" and "when did we last
ask", and neither has to survive a restart -- the worst case after a
restart is one extra GitHub call, not a wrong answer. A DB-backed cache
would need its own migration for two fields that exist purely to avoid
hitting GitHub's rate limit, which is unearned complexity for what this is.
"""

import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

from .__version__ import __version__

RELEASES_API_URL = "https://api.github.com/repos/t-philip/omnimeter/releases/latest"
RELEASES_PAGE_URL = "https://github.com/t-philip/omnimeter/releases/latest"
# GitHub's unauthenticated rate limit is 60 requests/hour per IP -- a
# self-hosted single-instance app checking once a day is nowhere near that,
# so a longer interval only trades staleness for no real benefit.
CHECK_INTERVAL = timedelta(hours=24)
REQUEST_TIMEOUT_SECONDS = 5

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# Process-local cache -- see module docstring for why this isn't in the DB.
# Each gunicorn worker keeps its own copy, so a multi-worker deployment may
# make this call more than once a day in the worst case; still well inside
# GitHub's rate limit, and not worth a shared cache for a version string.
_cache: dict = {"checked_at": None, "latest_version": None}


def is_enabled(conn: sqlite3.Connection) -> bool:
    """Opt-in switch. Same reasoning and default as weather.weather_enabled
    -- this is OmniMeter's other outbound internet call."""
    row = conn.execute("SELECT update_check_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
    return bool(row["v"]) if row else False


def _parse_version(v: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    a, b, c = (int(g) for g in m.groups())
    return (a, b, c)


def is_newer(latest: str, current: str) -> bool:
    """True if latest is a strictly newer release than current. Either
    version failing to parse returns False -- a malformed tag must never
    trigger a false "update available" banner."""
    lp, cp = _parse_version(latest), _parse_version(current)
    if lp is None or cp is None:
        return False
    return lp > cp


def _fetch_latest_tag(timeout: int = REQUEST_TIMEOUT_SECONDS) -> str | None:
    """One unauthenticated call to GitHub's public releases API. Every
    failure mode (network down, rate-limited, GitHub outage, malformed
    response) is swallowed and reported as "don't know" -- an update check
    must never break the dashboard it's decorating."""
    req = urllib.request.Request(
        RELEASES_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "OmniMeter-update-check"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - fixed https host
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    tag = payload.get("tag_name")
    return tag if isinstance(tag, str) else None


def get_status(*, enabled: bool, now: datetime | None = None) -> dict:
    """Returns the payload the /api/update-check route hands to the
    frontend. Never raises and never blocks longer than
    REQUEST_TIMEOUT_SECONDS -- called from a normal page-load request path,
    not a background job, so a GitHub outage must degrade to "no update
    info" rather than a failed page load.

    `now` is only ever overridden by tests -- production callers get the
    real clock.
    """
    if not enabled:
        return {
            "enabled": False,
            "current_version": __version__,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
        }

    now = now or datetime.now(UTC)
    stale = _cache["checked_at"] is None or now - _cache["checked_at"] > CHECK_INTERVAL
    if stale:
        _cache["latest_version"] = _fetch_latest_tag()
        _cache["checked_at"] = now

    latest = _cache["latest_version"]
    return {
        "enabled": True,
        "current_version": __version__,
        "latest_version": latest,
        "update_available": bool(latest) and is_newer(latest, __version__),
        "release_url": RELEASES_PAGE_URL if latest else None,
    }
