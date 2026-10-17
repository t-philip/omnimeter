"""SolarEdge one-time authorisation tool: URL, callback server, end-to-end main().

Real loopback sockets (port 0) -- the stray-request behaviour is the point. Credentials are
placeholders; no real dates.
"""

import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from scripts import solaredge_authorize as sa
from src import solaredge_client as se

NONCE = "test-data-nonce"
TEST_ID, TEST_SECRET = "test-data-client-id", "test-data-client-secret"


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _serve(timeout_s=10):
    server = sa.make_server(0, NONCE)
    out = {}
    t = threading.Thread(target=lambda: out.update(got=sa.wait_for_callback(server, timeout_s)))
    t.start()
    return server, server.server_address[1], t, out


def test_authorize_url_carries_nonce_in_external_id_and_max_grant():
    url = sa.build_authorize_url(TEST_ID, 8888, NONCE)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://connect.solaredge.com/authorize?")
    assert q["external_id"] == [NONCE] and q["access_duration"] == ["24"]
    assert q["scope"] == ["SITE_DATA DEVICE_DATA"] and q["redirect_uri"] == ["http://localhost:8888/callback"]
    assert "state" not in q  # SolarEdge does not echo it
    assert TEST_SECRET not in url


def test_strays_are_ignored_and_the_real_callback_is_accepted():
    server, port, t, out = _serve()
    assert _get(port, "/favicon.ico") == 404
    assert _get(port, "/callback?code=abc&site_id=12&external_id=wrong") == 404  # wrong nonce
    assert _get(port, "/callback?code=abc&site_id=12") == 404  # no nonce (a forged/vendor-style callback)
    assert _get(port, f"/callback?code=&site_id=12&external_id={NONCE}") == 404  # no code
    assert _get(port, f"/callback?code=abc&site_id=notanumber&external_id={NONCE}") == 404
    assert _get(port, f"/callback?code=abc&site_id=12&external_id={NONCE}") == 200
    t.join(5)
    server.server_close()
    assert out["got"] == ("abc", 12)
    assert server.RequestHandlerClass.strays == 5


def test_wrong_path_with_valid_nonce_is_not_accepted():
    server, port, t, out = _serve(timeout_s=1.5)
    assert _get(port, f"/other?code=abc&site_id=12&external_id={NONCE}") == 404
    t.join(5)
    server.server_close()
    assert out["got"] is None


def test_timeout_returns_none():
    server = sa.make_server(0, NONCE)
    assert sa.wait_for_callback(server, 0.3) is None
    server.server_close()


def test_server_binds_loopback_only():
    server = sa.make_server(0, NONCE)
    assert server.server_address[0] == "127.0.0.1"
    server.server_close()


class FakeClient:
    def __init__(self, store, fail=None):
        self.store, self.fail, self.exchanged = store, fail, None

    def exchange_code(self, code, redirect, site_id):
        if self.fail:
            raise self.fail
        self.exchanged = (code, redirect, site_id)
        self.store.save(se.TokenPair("test-data-access", "test-data-refresh", 9e12, site_id))

    def get(self, path, params=None):
        return {"production": {"total": 1.0}}


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "om.db"))
    monkeypatch.setenv("OMNIMETER_SOLAREDGE_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.setenv("SOLAREDGE_CLIENT_ID", TEST_ID)
    monkeypatch.setenv("SOLAREDGE_CLIENT_SECRET", TEST_SECRET)
    monkeypatch.delenv("OMNIMETER_CREDENTIALS_DIR", raising=False)
    return tmp_path


def _patch_flow(monkeypatch, fail=None):
    holder = {}

    def fake_client(*, client_id, client_secret, store, **_):
        holder["c"] = FakeClient(store, fail)
        return holder["c"]

    monkeypatch.setattr(sa.solaredge_client, "SolarEdgeClient", fake_client)
    monkeypatch.setattr(sa.secrets, "token_hex", lambda _n: NONCE)
    return holder


def _run_main_with_callback(argv, path):
    """Run main() in a thread on a free port, deliver one callback to it, return (exit code)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out = {}
    t = threading.Thread(target=lambda: out.update(rc=sa.main(["--port", str(port), "--timeout", "10", *argv])))
    t.start()
    for _ in range(100):  # wait until the listener is up
        try:
            _get(port, "/favicon.ico")
            break
        except OSError:
            threading.Event().wait(0.05)
    _get(port, path)
    t.join(15)
    return out["rc"]


def test_main_success_stores_token_clears_flag_and_prints_no_secret(run_env, monkeypatch, capsys):
    holder = _patch_flow(monkeypatch)
    (run_env / "solaredge_auth_alerted").touch()
    rc = _run_main_with_callback([], f"/callback?code=test-data-code&site_id=77&external_id={NONCE}")
    out = capsys.readouterr().out
    assert rc == 0
    assert holder["c"].exchanged[0] == "test-data-code" and holder["c"].exchanged[2] == 77
    assert se.TokenStore(run_env / "tok.json").load().site_id == 77
    assert not (run_env / "solaredge_auth_alerted").exists()
    for secret in (TEST_SECRET, "test-data-code", "test-data-access", "test-data-refresh"):
        assert secret not in out
    assert "verified: overview HTTP 200" in out


def test_main_exchange_failure_exits_one_and_leaves_old_token_alone(run_env, monkeypatch, capsys):
    se.TokenStore(run_env / "tok.json").save(se.TokenPair("old-a", "old-r", 9e12, 5))
    (run_env / "solaredge_auth_alerted").touch()
    _patch_flow(monkeypatch, fail=se.SolarEdgeAuthError("token endpoint rejected the request: invalid_client"))
    rc = _run_main_with_callback([], f"/callback?code=test-data-code&site_id=77&external_id={NONCE}")
    assert rc == 1
    assert se.TokenStore(run_env / "tok.json").load().refresh_token == "old-r"
    assert (run_env / "solaredge_auth_alerted").exists()  # flag only cleared on success
    assert "invalid_client" in capsys.readouterr().out


def test_main_without_credentials_exits_one_and_prints_none(run_env, monkeypatch, capsys):
    monkeypatch.delenv("SOLAREDGE_CLIENT_SECRET")
    assert sa.main(["--timeout", "1"]) == 1
    assert TEST_ID not in capsys.readouterr().out
