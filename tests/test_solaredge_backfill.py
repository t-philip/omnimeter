"""scripts.solaredge_backfill.main(): dry-run makes no calls, the credit guard refuses, a real run stores
rows and logs its calls. Placeholder credentials; 1990s fixture dates; no network."""

from datetime import UTC, datetime

import pytest

from scripts import solaredge_backfill as sb
from src import db
from src import solaredge_client as se
from src import solaredge_status as ss


class FakeClient:
    instances = []

    def __init__(self, *, client_id, client_secret, store, **_):
        self.requests_made, self.calls = 0, []
        self.requests_by_endpoint = {}
        FakeClient.instances.append(self)

    def get(self, path, params=None):
        self.calls.append((path, params))
        self.requests_made += 1
        if params and params["resolution"] == "YEAR":
            return {"values": []}
        if params:
            return {"values": [{"timestamp": params["from"][:10] + "T00:00:00+02:00", "value": 1000.0}]}
        return {"installationDate": "1998-06-01T00:00:00Z"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "om.db"))
    monkeypatch.setenv("OMNIMETER_SOLAREDGE_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.setenv("SOLAREDGE_CLIENT_ID", "test-data-id")
    monkeypatch.setenv("SOLAREDGE_CLIENT_SECRET", "test-data-secret")
    monkeypatch.delenv("OMNIMETER_CREDENTIALS_DIR", raising=False)
    se.TokenStore(tmp_path / "tok.json").save(se.TokenPair("a", "r", 9e12, 1))
    FakeClient.instances.clear()
    monkeypatch.setattr(sb.solaredge_client, "SolarEdgeClient", FakeClient)
    conn = db.get_connection()
    db.init_db(conn)
    return tmp_path, conn


def test_dry_run_makes_no_api_calls_and_stores_nothing(env, capsys):
    _tmp, conn = env
    assert sb.main(["--from", "1998-06-01", "--dry-run"]) == 0
    assert FakeClient.instances[0].calls == []
    assert conn.execute("SELECT COUNT(*) FROM solaredge_daily").fetchone()[0] == 0
    assert "dry run" in capsys.readouterr().out


def test_without_yes_it_only_prints_the_plan(env, capsys):
    assert sb.main(["--from", "1998-06-01"]) == 0
    assert FakeClient.instances[0].calls == [] and "--yes" in capsys.readouterr().out


def test_credit_guard_refuses_when_the_cycle_is_nearly_spent(env, capsys):
    _tmp, conn = env
    ss.record_run(conn, datetime.now(UTC), "baseline", 1990)
    assert sb.main(["--from", "1998-06-01", "--yes"]) == 1
    assert FakeClient.instances[0].calls == [] and "REFUSING" in capsys.readouterr().out


def test_real_run_stores_rows_logs_its_calls_and_never_prints_secrets(env, capsys, monkeypatch):
    _tmp, conn = env
    monkeypatch.setattr(sb.solaredge_history.time, "sleep", lambda s: None)
    rc = sb.main(["--from", "1998-06-01", "--yes", "--pause", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert conn.execute("SELECT COUNT(*) FROM solaredge_daily").fetchone()[0] > 0
    row = conn.execute("SELECT status, calls FROM solaredge_runs WHERE status = 'backfill'").fetchone()
    assert row is not None and row["calls"] == FakeClient.instances[0].requests_made
    assert "test-data-secret" not in out and "installationDate" not in out
    # backfill rows must not masquerade as the poller's last run
    assert (
        ss.build_status(
            conn,
            _tmp / "tok.json",
            datetime.now(UTC),
            db.localtime.LOCAL_TZ if hasattr(db, "localtime") else __import__("zoneinfo").ZoneInfo("UTC"),
        )["last_run"]
        is None
    )
