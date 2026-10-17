"""scripts.solaredge_poll.main(): toggle gate, auth-failure alert-once, flag reset, exit codes.

Credentials here are placeholders; no real dates; no network (client + notifier are faked).
"""

import sqlite3

import pytest

from scripts import solaredge_poll as sp
from src import db
from src import solaredge_client as se

TEST_ID, TEST_SECRET = "test-data-client-id", "test-data-client-secret"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "om.db"))
    monkeypatch.setenv("OMNIMETER_SOLAREDGE_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.setenv("SOLAREDGE_CLIENT_ID", TEST_ID)
    monkeypatch.setenv("SOLAREDGE_CLIENT_SECRET", TEST_SECRET)
    monkeypatch.delenv("OMNIMETER_CREDENTIALS_DIR", raising=False)
    conn = db.get_connection()
    db.init_db(conn)
    return tmp_path, conn


def _enable(conn):
    conn.execute("UPDATE feature_toggles SET solaredge_enabled = 1 WHERE id = 1")
    conn.commit()


def _seed_token(tmp_path):
    se.TokenStore(tmp_path / "tok.json").save(se.TokenPair("a", "r", 9e12, 1))


def test_disabled_is_a_clean_noop(env, capsys):
    assert sp.main() == 0
    assert "disabled" in capsys.readouterr().out


def test_missing_credentials_fails_without_printing_any(env, monkeypatch, capsys):
    _tmp, conn = env
    _enable(conn)
    monkeypatch.delenv("SOLAREDGE_CLIENT_SECRET")
    assert sp.main() == 1
    assert TEST_ID not in capsys.readouterr().out


def test_no_token_file_alerts_once_then_stays_quiet(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    sent = []
    monkeypatch.setattr(sp.telegram_notifier, "send_message", lambda c, t: (sent.append(t), ("sent", None))[1])
    assert sp.main() == 1
    assert sp.main() == 1  # still failing, but the flag suppresses a second alert
    assert len(sent) == 1
    assert (tmp / "solaredge_auth_alerted").exists()
    assert TEST_SECRET not in sent[0]


def test_alert_not_flagged_if_delivery_failed(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    monkeypatch.setattr(sp.telegram_notifier, "send_message", lambda c, t: ("failed", "net down"))
    sp.main()
    assert not (tmp / "solaredge_auth_alerted").exists()  # will try again next run


def test_success_clears_the_flag_and_exits_zero(env, monkeypatch, capsys):
    tmp, conn = env
    _enable(conn)
    _seed_token(tmp)
    (tmp / "solaredge_auth_alerted").touch()
    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", lambda *a, **k: {"status": "ok", "calls": 2})
    assert sp.main() == 0
    assert not (tmp / "solaredge_auth_alerted").exists()
    assert "ok" in capsys.readouterr().out


def test_success_prints_usage_headers_when_the_client_saw_any(env, monkeypatch, capsys):
    tmp, conn = env
    _enable(conn)
    _seed_token(tmp)

    def fake_poll(c, client, *a, **k):
        client.usage_headers, client.header_names = {"x-ratelimit-remaining-minute": "9"}, ["x-ratelimit-remaining-minute"]
        return {"status": "ok", "calls": 1}

    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", fake_poll)
    assert sp.main() == 0
    out = capsys.readouterr().out
    assert "usage headers {'x-ratelimit-remaining-minute': '9'}" in out


def test_rate_limit_is_exit_zero_api_error_is_exit_one(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    _seed_token(tmp)

    def limited(*a, **k):
        raise se.SolarEdgeRateLimitError("429", credit_exhausted=True, retry_after_s=None)

    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", limited)
    assert sp.main() == 0

    def broken(*a, **k):
        raise se.SolarEdgeApiError("HTTP 503")

    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", broken)
    assert sp.main() == 1


def test_failed_poll_leaves_no_partial_rows(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    _seed_token(tmp)

    def broken(c, *a, **k):
        c.execute("INSERT INTO solaredge_power VALUES ('1998-07-28T00:00:00Z', 1.0)")
        raise se.SolarEdgeApiError("HTTP 500")

    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", broken)
    assert sp.main() == 1
    check = sqlite3.connect(tmp / "om.db")
    assert check.execute("SELECT COUNT(*) FROM solaredge_power").fetchone()[0] == 0


def _runs(tmp):
    c = sqlite3.connect(tmp / "om.db")
    return c.execute("SELECT status, calls FROM solaredge_runs ORDER BY time").fetchall()


def test_every_outcome_is_logged_to_the_run_table(env, monkeypatch):
    from datetime import UTC, datetime, timedelta

    tmp, conn = env
    _enable(conn)
    _seed_token(tmp)

    ticks = iter(datetime(1998, 7, 28, 10, 0, tzinfo=UTC) + timedelta(minutes=30 * i) for i in range(10))

    class FakeDatetime:
        @staticmethod
        def now(_tz=None):
            return next(ticks)

    monkeypatch.setattr(sp, "datetime", FakeDatetime)

    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", lambda *a, **k: {"status": "ok", "calls": 2})
    sp.main()
    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", lambda *a, **k: {"status": "skipped", "calls": 0})
    sp.main()

    def broken(*a, **k):
        raise se.SolarEdgeApiError("HTTP 503")

    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", broken)
    sp.main()
    assert [s for s, _ in _runs(tmp)] == ["ok", "skipped_night", "api_error"]


def test_auth_failure_is_logged_as_auth_error(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    monkeypatch.setattr(sp.telegram_notifier, "send_message", lambda c, t: ("sent", None))
    sp.main()  # no token file -> auth error
    assert ("auth_error", 0) in _runs(tmp)


def test_disabled_run_is_not_logged(env):
    tmp, _conn = env
    sp.main()
    assert _runs(tmp) == []


def test_grant_expiry_warning_sent_once_then_cleared_by_a_fresh_grant(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    sent = []
    monkeypatch.setattr(sp.telegram_notifier, "send_message", lambda c, t: (sent.append(t), ("sent", None))[1])
    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", lambda *a, **k: {"status": "ok", "calls": 1})
    import time as _t

    # granted 710 days ago -> ~20 days left -> warn once
    se.TokenStore(tmp / "tok.json").save(se.TokenPair("a", "r", 9e12, 1, granted_at=_t.time() - 710 * 86400))
    assert sp.main() == 0 and sp.main() == 0
    assert len(sent) == 1 and "ends in about" in sent[0] and "scripts.solaredge_authorize" in sent[0]
    assert (tmp / "solaredge_expiry_warned").exists()
    # re-authorised -> ~730 days left -> flag cleared, no new warning
    se.TokenStore(tmp / "tok.json").save(se.TokenPair("a", "r", 9e12, 1, granted_at=_t.time()))
    assert sp.main() == 0
    assert not (tmp / "solaredge_expiry_warned").exists() and len(sent) == 1


def test_unknown_grant_date_never_warns(env, monkeypatch):
    tmp, conn = env
    _enable(conn)
    _seed_token(tmp)  # no granted_at
    sent = []
    monkeypatch.setattr(sp.telegram_notifier, "send_message", lambda c, t: (sent.append(t), ("sent", None))[1])
    monkeypatch.setattr(sp.solaredge_ingest, "poll_once", lambda *a, **k: {"status": "ok", "calls": 1})
    assert sp.main() == 0 and sent == []
