"""GitHub release version-check: toggle, version comparison, caching, and
fetch-failure handling."""

import io
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from src import db, update_check


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


@pytest.fixture(autouse=True)
def _reset_cache():
    # The module-level cache is process-global by design (see
    # update_check.py's docstring) -- reset it around every test so one
    # test's cached result can never leak into the next.
    update_check._cache["checked_at"] = None
    update_check._cache["latest_version"] = None
    yield
    update_check._cache["checked_at"] = None
    update_check._cache["latest_version"] = None


class TestToggle:
    def test_disabled_by_default(self, conn):
        # Same opt-in reasoning as weather_enabled -- an already-deployed DB
        # must never start phoning out to GitHub on upgrade.
        assert update_check.is_enabled(conn) is False

    def test_enabled_when_set(self, conn):
        conn.execute("UPDATE feature_toggles SET update_check_enabled = 1 WHERE id = 1")
        conn.commit()
        assert update_check.is_enabled(conn) is True


class TestIsNewer:
    def test_newer_patch(self):
        assert update_check.is_newer("v1.1.0", "1.0.6") is True

    def test_newer_minor(self):
        assert update_check.is_newer("v1.1.0", "1.0.9") is True

    def test_newer_major(self):
        assert update_check.is_newer("v2.0.0", "1.9.9") is True

    def test_equal_is_not_newer(self):
        assert update_check.is_newer("v1.1.0", "1.1.0") is False

    def test_older_is_not_newer(self):
        assert update_check.is_newer("v1.0.0", "1.1.0") is False

    def test_v_prefix_optional(self):
        assert update_check.is_newer("1.1.0", "1.0.6") is True

    def test_malformed_latest_never_claims_newer(self):
        # A hand-edited or pre-release tag ("v1.1.0-rc1") must never trigger
        # a false "update available" banner just because it fails to parse.
        assert update_check.is_newer("v1.1.0-rc1", "1.0.6") is False

    def test_malformed_current_never_claims_newer(self):
        assert update_check.is_newer("v1.1.0", "dev") is False


class TestGetStatus:
    def test_disabled_returns_immediately_without_network_call(self, monkeypatch):
        called = False

        def _boom(*a, **kw):
            nonlocal called
            called = True
            raise AssertionError("must not fetch when disabled")

        monkeypatch.setattr(update_check, "_fetch_latest_tag", _boom)
        result = update_check.get_status(enabled=False)
        assert called is False
        assert result == {
            "enabled": False,
            "current_version": update_check.__version__,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
        }

    def test_enabled_reports_update_available(self, monkeypatch):
        monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda timeout=5: "v99.0.0")
        result = update_check.get_status(enabled=True)
        assert result["update_available"] is True
        assert result["latest_version"] == "v99.0.0"
        assert result["release_url"] == update_check.RELEASES_PAGE_URL

    def test_enabled_up_to_date_reports_no_update(self, monkeypatch):
        monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda timeout=5: f"v{update_check.__version__}")
        result = update_check.get_status(enabled=True)
        assert result["update_available"] is False

    def test_fetch_failure_degrades_to_no_update_info(self, monkeypatch):
        # A GitHub outage/rate-limit must never surface as an error to the
        # dashboard -- _fetch_latest_tag already swallows failures and
        # returns None; get_status must pass that through cleanly.
        monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda timeout=5: None)
        result = update_check.get_status(enabled=True)
        assert result["latest_version"] is None
        assert result["update_available"] is False
        assert result["release_url"] is None

    def test_cache_avoids_refetch_within_interval(self, monkeypatch):
        calls = []
        monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda timeout=5: calls.append(1) or "v99.0.0")
        now = datetime(2026, 8, 11, tzinfo=UTC)
        update_check.get_status(enabled=True, now=now)
        update_check.get_status(enabled=True, now=now + timedelta(hours=1))
        assert len(calls) == 1

    def test_cache_refetches_after_interval(self, monkeypatch):
        calls = []
        monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda timeout=5: calls.append(1) or "v99.0.0")
        now = datetime(2026, 8, 11, tzinfo=UTC)
        update_check.get_status(enabled=True, now=now)
        update_check.get_status(enabled=True, now=now + update_check.CHECK_INTERVAL + timedelta(seconds=1))
        assert len(calls) == 2


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def __enter__(self):
        return self._buf

    def __exit__(self, *exc):
        return False


class TestFetchLatestTag:
    def test_parses_tag_name(self, monkeypatch):
        monkeypatch.setattr(
            update_check.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(b'{"tag_name": "v1.2.3"}')
        )
        assert update_check._fetch_latest_tag() == "v1.2.3"

    def test_missing_tag_name_returns_none(self, monkeypatch):
        monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(b"{}"))
        assert update_check._fetch_latest_tag() is None

    def test_malformed_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(b"not json"))
        assert update_check._fetch_latest_tag() is None

    def test_network_error_returns_none(self, monkeypatch):
        def _raise(*a, **kw):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(update_check.urllib.request, "urlopen", _raise)
        assert update_check._fetch_latest_tag() is None

    def test_timeout_returns_none(self, monkeypatch):
        def _raise(*a, **kw):
            raise TimeoutError("timed out")

        monkeypatch.setattr(update_check.urllib.request, "urlopen", _raise)
        assert update_check._fetch_latest_tag() is None
