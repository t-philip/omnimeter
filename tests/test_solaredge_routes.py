"""Read-only SolarEdge status routes (for any external monitoring tool). Needs Flask
(or any env with the app's requirements installed)."""

import json

import pytest

flask = pytest.importorskip("flask")

from src import db  # noqa: E402
from src import solaredge_status as ss  # noqa: E402
from src.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", "test-write-token")
    monkeypatch.setenv("OMNIMETER_SOLAREDGE_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.setattr("src.app.DEFAULT_IMPORTS_DIR", tmp_path / "imports")
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_status_on_an_empty_install_is_a_clean_200(client):
    body = client.get("/api/solaredge/status").get_json()
    assert body["enabled"] is False and body["last_run"] is None
    assert body["credits"]["limit"] == 2000 and body["credits"]["used_estimate"] == 0
    assert body["grant"]["known"] is False


def test_status_reports_runs_and_never_exposes_a_credential(client, tmp_path):
    from datetime import UTC, datetime

    conn = db.get_connection()
    db.init_db(conn)
    ss.record_run(conn, datetime.now(UTC), "ok", 2)
    (tmp_path / "tok.json").write_text(
        json.dumps(
            {
                "access_token": "test-data-access",
                "refresh_token": "test-data-refresh",
                "expires_at": 9e12,
                "site_id": 424242,
                "granted_at": 1.0,
            }
        )
    )
    resp = client.get("/api/solaredge/status")
    text = resp.get_data(as_text=True)
    assert resp.get_json()["credits"]["used_estimate"] == 2
    for needle in ("test-data-access", "test-data-refresh", "424242"):
        assert needle not in text


def test_reset_day_and_limit_are_overridable_from_the_environment(client, monkeypatch):
    monkeypatch.setenv(ss.CREDIT_LIMIT_ENV, "5000")
    assert client.get("/api/solaredge/status").get_json()["credits"]["limit"] == 5000


def test_power_endpoint_returns_recent_buckets_oldest_first(client):
    from datetime import UTC, datetime, timedelta

    conn = db.get_connection()
    db.init_db(conn)
    now = datetime.now(UTC)
    for mins in (60, 15):
        t = (now - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("INSERT INTO solaredge_power VALUES (?, ?)", (t, float(mins)))
    conn.commit()
    got = client.get("/api/solaredge/power?hours=2").get_json()
    assert [r["power_w"] for r in got] == [60.0, 15.0]
