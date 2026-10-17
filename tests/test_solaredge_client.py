"""SolarEdge client: token rotation safety, error mapping, and the no-secret-in-output guarantees.

No real calendar dates here (clock is a plain float) and every credential is an
obvious placeholder, per the fixture-data rules.
"""

import json
import logging
import os
import stat

import pytest
import requests

from src import solaredge_client as se

CLIENT_ID = "test-data-client-id"
CLIENT_SECRET = "test-data-client-secret-VALUE"
ACCESS_1, REFRESH_1 = "test-data-access-1", "test-data-refresh-1"
ACCESS_2, REFRESH_2 = "test-data-access-2", "test-data-refresh-2"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text_body=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._text_body = text_body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, posts=(), gets=()):
        self.posts, self.gets = list(posts), list(gets)
        self.post_calls, self.get_calls = [], []

    def post(self, url, json=None, timeout=None):
        self.post_calls.append((url, json))
        item = self.posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append((url, params, headers))
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def token_ok(access, refresh, expires_in=7200):
    return FakeResponse(200, {"access_token": access, "token_type": "Bearer", "expires_in": expires_in, "refresh_token": refresh})


def make(tmp_path, session, clock=None, seed=True, expires_at=1000.0 + 7200):
    store = se.TokenStore(tmp_path / "solaredge_token.json")
    if seed:
        store.save(se.TokenPair(ACCESS_1, REFRESH_1, expires_at, 12345))
    client = se.SolarEdgeClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, store=store, session=session, clock=clock or Clock())
    return client, store


class TestTokenStore:
    def test_roundtrip_and_mode_600(self, tmp_path):
        store = se.TokenStore(tmp_path / "t.json")
        store.save(se.TokenPair(ACCESS_1, REFRESH_1, 5.0, 7))
        assert store.load() == se.TokenPair(ACCESS_1, REFRESH_1, 5.0, 7)
        if os.name == "posix":
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    def test_missing_file_is_none(self, tmp_path):
        assert se.TokenStore(tmp_path / "nope.json").load() is None

    def test_corrupt_file_error_does_not_leak_content(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text("{not json test-data-refresh-1", encoding="utf-8")
        with pytest.raises(se.SolarEdgeAuthError) as exc:
            se.TokenStore(p).load()
        assert "refresh-1" not in str(exc.value)

    def test_failed_write_leaves_old_file_intact_and_no_tmp(self, tmp_path, monkeypatch):
        store = se.TokenStore(tmp_path / "t.json")
        store.save(se.TokenPair(ACCESS_1, REFRESH_1, 5.0, 7))

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(se.os, "replace", boom)
        with pytest.raises(OSError):
            store.save(se.TokenPair(ACCESS_2, REFRESH_2, 9.0, 7))
        assert store.load().refresh_token == REFRESH_1  # the old pair survived
        assert not list(tmp_path.glob("*.tmp"))


class TestRefresh:
    def test_valid_token_no_refresh(self, tmp_path):
        session = FakeSession()
        client, _ = make(tmp_path, session)
        assert client.access_token().access_token == ACCESS_1
        assert session.post_calls == []

    def test_near_expiry_refreshes_and_persists_new_pair(self, tmp_path):
        session = FakeSession(posts=[token_ok(ACCESS_2, REFRESH_2)])
        clock = Clock(1000.0)
        client, store = make(tmp_path, session, clock, expires_at=1000.0 + 100)  # < margin
        pair = client.access_token()
        assert (pair.access_token, pair.refresh_token) == (ACCESS_2, REFRESH_2)
        assert store.load().refresh_token == REFRESH_2  # rotated pair is on disk
        url, body = session.post_calls[0]
        assert url == se.TOKEN_URL
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == REFRESH_1
        assert pair.site_id == 12345  # site id carried across the rotation

    def test_new_pair_is_persisted_before_the_access_token_is_used(self, tmp_path):
        order = []
        session = FakeSession(posts=[token_ok(ACCESS_2, REFRESH_2)])
        client, store = make(tmp_path, session, expires_at=1000.0 + 100)
        real_save = store.save
        store.save = lambda p: (order.append("save"), real_save(p))[1]
        client.access_token()
        assert order == ["save"]

    def test_invalid_grant_is_auth_error_with_code_only(self, tmp_path):
        session = FakeSession(posts=[FakeResponse(400, {"error": "invalid_grant", "error_description": "x " + REFRESH_1})])
        client, store = make(tmp_path, session, expires_at=1000.0 + 100)
        with pytest.raises(se.SolarEdgeAuthError) as exc:
            client.access_token()
        assert "invalid_grant" in str(exc.value)
        assert REFRESH_1 not in str(exc.value)  # description (which could echo a token) is dropped
        assert store.load().refresh_token == REFRESH_1  # nothing overwritten on failure

    def test_transport_failure_message_has_no_secret(self, tmp_path):
        boom = requests.ConnectionError("POST body " + CLIENT_SECRET)
        client, _ = make(tmp_path, FakeSession(posts=[boom]), expires_at=1000.0 + 100)
        with pytest.raises(se.SolarEdgeApiError) as exc:
            client.access_token()
        assert CLIENT_SECRET not in str(exc.value)
        assert "ConnectionError" in str(exc.value)

    def test_no_token_file_requires_authorisation(self, tmp_path):
        client, _ = make(tmp_path, FakeSession(), seed=False)
        with pytest.raises(se.SolarEdgeAuthError):
            client.access_token()


class TestExchangeCode:
    def test_first_authorisation_persists_pair(self, tmp_path):
        session = FakeSession(posts=[token_ok(ACCESS_1, REFRESH_1)])
        client, store = make(tmp_path, session, seed=False)
        client.exchange_code("test-data-code", "http://localhost:8888/callback", 12345)
        assert store.load().refresh_token == REFRESH_1
        body = session.post_calls[0][1]
        assert body["grant_type"] == "authorization_code" and body["redirect_uri"] == "http://localhost:8888/callback"


class TestGet:
    def test_sends_bearer_header_and_never_a_secret_in_url(self, tmp_path):
        session = FakeSession(gets=[FakeResponse(200, {"ok": 1})])
        client, _ = make(tmp_path, session)
        assert client.get("/sites/1/overview", {"a": "b"}) == {"ok": 1}
        url, params, headers = session.get_calls[0]
        assert headers["Authorization"] == f"Bearer {ACCESS_1}"
        assert ACCESS_1 not in url and CLIENT_SECRET not in url and not any(ACCESS_1 in str(v) for v in (params or {}).values())

    def test_401_forces_one_refresh_then_retries(self, tmp_path):
        session = FakeSession(posts=[token_ok(ACCESS_2, REFRESH_2)], gets=[FakeResponse(401), FakeResponse(200, {"ok": 2})])
        client, store = make(tmp_path, session)
        assert client.get("/x") == {"ok": 2}
        assert session.get_calls[1][2]["Authorization"] == f"Bearer {ACCESS_2}"
        assert store.load().refresh_token == REFRESH_2

    def test_second_401_is_auth_error(self, tmp_path):
        session = FakeSession(posts=[token_ok(ACCESS_2, REFRESH_2)], gets=[FakeResponse(401), FakeResponse(401)])
        client, _ = make(tmp_path, session)
        with pytest.raises(se.SolarEdgeAuthError):
            client.get("/x")

    def test_429_rate_limit_vs_credit_cap(self, tmp_path):
        rate = FakeResponse(429, {}, {"x-ratelimit-remaining-minute": "0", "retry-after": "12"})
        credit = FakeResponse(429, {}, {"x-ratelimit-remaining-minute": "7"})
        client, _ = make(tmp_path, FakeSession(gets=[rate, credit]))
        with pytest.raises(se.SolarEdgeRateLimitError) as e1:
            client.get("/x")
        assert e1.value.credit_exhausted is False and e1.value.retry_after_s == 12
        with pytest.raises(se.SolarEdgeRateLimitError) as e2:
            client.get("/x")
        assert e2.value.credit_exhausted is True

    def test_usage_headers_are_captured_lowercased_and_others_only_by_name(self, tmp_path):
        hdrs = {"X-RateLimit-Remaining-Minute": "9", "Content-Type": "application/json", "Set-Cookie": "test-data"}
        client, _ = make(tmp_path, FakeSession(gets=[FakeResponse(200, {"ok": 1}, headers=hdrs)]))
        client.get("/x")
        assert client.usage_headers == {"x-ratelimit-remaining-minute": "9"}
        assert client.header_names == ["content-type", "set-cookie", "x-ratelimit-remaining-minute"]
        assert "test-data" not in str(client.usage_headers)  # non-usage values are never kept

    def test_no_headers_means_empty_capture(self, tmp_path):
        client, _ = make(tmp_path, FakeSession(gets=[FakeResponse(200, {"ok": 1})]))
        client.get("/x")
        assert client.usage_headers == {} and client.header_names == []

    def test_other_status_is_api_error(self, tmp_path):
        client, _ = make(tmp_path, FakeSession(gets=[FakeResponse(503)]))
        with pytest.raises(se.SolarEdgeApiError):
            client.get("/x")


class TestLoggingHygiene:
    def test_secret_values_are_scrubbed_from_log_output(self, tmp_path, caplog):
        handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            se.harden_logging([CLIENT_SECRET, REFRESH_1])
            captured = []
            handler.emit = lambda r: captured.append(r.getMessage())  # type: ignore[method-assign]
            # Filters on a handler run in Handler.handle(), so go through it.
            rec = logging.LogRecord("x", logging.ERROR, __file__, 1, f"boom {CLIENT_SECRET} and {REFRESH_1}", None, None)
            handler.handle(rec)
        finally:
            root.removeHandler(handler)
        assert captured and CLIENT_SECRET not in captured[0] and REFRESH_1 not in captured[0]
        assert "«redacted»" in captured[0]

    def test_http_client_loggers_pinned_to_warning(self):
        se.harden_logging([])
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("requests").level == logging.WARNING


def test_token_file_is_valid_json_with_expected_keys(tmp_path):
    store = se.TokenStore(tmp_path / "t.json")
    store.save(se.TokenPair(ACCESS_1, REFRESH_1, 5.0, 7))
    assert set(json.loads(store.path.read_text())) == {"access_token", "refresh_token", "expires_at", "site_id", "granted_at"}
