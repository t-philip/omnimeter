"""/api/self-sufficiency and /api/energy-flow with MEASURED production (SolarEdge) vs the estimate.
Needs Flask. The 'today' case uses the real local date by necessity."""

import pytest

flask = pytest.importorskip("flask")

from src import db, localtime  # noqa: E402
from src.app import create_app  # noqa: E402


@pytest.fixture
def client_and_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", "test-write-token")
    monkeypatch.setattr("src.app.DEFAULT_IMPORTS_DIR", tmp_path / "imports")
    app = create_app()
    app.config.update(TESTING=True)
    conn = db.get_connection()
    db.init_db(conn)
    return app.test_client(), conn


def power(conn, date, imp, exp):
    conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES (?,?,?,?)", (date, imp, exp, imp - exp))


def test_no_kwp_and_no_measurements_is_unavailable_with_a_helpful_reason(client_and_conn):
    client, conn = client_and_conn
    power(conn, "1998-07-01", 5.0, 1.0)
    conn.commit()
    body = client.get("/api/self-sufficiency?from=1998-07-01&to=1998-07-31").get_json()
    assert body["available"] is False and "inverter" in body["reason"]


def test_measured_production_is_used_without_any_kwp_and_labelled_measured(client_and_conn):
    client, conn = client_and_conn
    power(conn, "1998-07-01", 6.0, 4.0)  # kept = 10 - 4 = 6, import 6 -> 50%
    conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-01', 10000.0, 'x')")
    conn.commit()
    body = client.get("/api/self-sufficiency?from=1998-07-01&to=1998-07-31").get_json()
    day = body["days"][0]
    assert body["production_source"] == "measured" and body["estimated"] is False
    assert day["basis"] == "measured" and day["production_kwh"] == 10.0 and day["partial"] is False
    assert day["self_sufficiency_pct"] == 50.0


def test_days_without_a_measurement_fall_back_to_the_estimate_and_the_source_is_mixed(client_and_conn):
    client, conn = client_and_conn
    conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
    power(conn, "1998-07-01", 6.0, 4.0)
    power(conn, "1998-07-02", 6.0, 4.0)
    conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-01', 10000.0, 'x')")  # only day 1 measured
    conn.commit()
    body = client.get("/api/self-sufficiency?from=1998-07-01&to=1998-07-31").get_json()
    assert body["production_source"] == "mixed" and body["measured_days"] == 1 and body["estimated_days"] == 1
    assert [d["basis"] for d in body["days"]] == ["measured", "seasonal"]


def test_estimate_only_install_behaves_as_before(client_and_conn):
    client, conn = client_and_conn
    conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
    power(conn, "1998-07-01", 6.0, 0.0)
    conn.commit()
    body = client.get("/api/self-sufficiency?from=1998-07-01&to=1998-07-31").get_json()
    assert body["production_source"] == "estimated" and body["estimated"] is True
    assert body["days"][0]["basis"] == "seasonal" and body["days"][0]["production_kwh"] > 0


def test_battery_data_changes_the_percentage_in_the_expected_direction(client_and_conn):
    client, conn = client_and_conn
    power(conn, "1998-07-01", 5.0, 0.0)
    conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-01', 10000.0, 'x')")
    conn.commit()
    no_batt = client.get("/api/self-sufficiency?from=1998-07-01&to=1998-07-31").get_json()["days"][0]["self_sufficiency_pct"]
    conn.execute("INSERT INTO battery_daily (date, charge_kwh, discharge_kwh) VALUES ('1998-07-01', 4.0, 0.0)")
    conn.commit()
    with_batt = client.get("/api/self-sufficiency?from=1998-07-01&to=1998-07-31").get_json()["days"][0]["self_sufficiency_pct"]
    assert no_batt == pytest.approx(66.7, abs=0.1)  # 10 / 15
    assert with_batt == pytest.approx(54.5, abs=0.1)  # solar stored for later is not consumption today: 6 / 11


def test_today_is_the_latest_snapshot_and_flagged_partial(client_and_conn):
    from datetime import datetime

    client, conn = client_and_conn
    today = datetime.now(localtime.LOCAL_TZ).date().isoformat()
    power(conn, today, 1.0, 0.0)
    conn.execute("INSERT INTO solaredge_overview (time, day, production_wh) VALUES (?,?,?)", (today + "T08:00:00Z", today, 3000.0))
    conn.commit()
    body = client.get(f"/api/self-sufficiency?from={today}&to={today}").get_json()
    assert body["today_in_progress"] is True and body["days"][0]["partial"] is True and body["days"][0]["production_kwh"] == 3.0


def test_energy_flow_uses_measured_solar_and_reports_the_source(client_and_conn):
    client, conn = client_and_conn
    power(conn, "1998-07-01", 2.0, 4.0)
    conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-01', 10000.0, 'x')")
    conn.commit()
    body = client.get("/api/energy-flow?from=1998-07-01&to=1998-07-31").get_json()
    assert body["pv_configured"] is True and body["production_source"] == "measured"
    assert body["sources"]["solar"] == 10.0


def test_energy_flow_without_any_solar_data_keeps_its_shape(client_and_conn):
    client, conn = client_and_conn
    power(conn, "1998-07-01", 2.0, 0.0)
    conn.commit()
    body = client.get("/api/energy-flow?from=1998-07-01&to=1998-07-31").get_json()
    assert body["pv_configured"] is False and body["sources"]["solar"] == 0.0 and "flows" in body


class TestSolarTabRoutes:
    def seed_two_years(self, conn):
        # 1997: 3 days in July; 1998: 3 days in July; grid rows only for 1998 (grid data starts later than solar).
        for d, wh in [
            ("1997-07-01", 10000.0),
            ("1997-07-02", 8000.0),
            ("1997-07-03", 6000.0),
            ("1998-07-01", 12000.0),
            ("1998-07-02", 9000.0),
            ("1998-07-03", 3000.0),
        ]:
            conn.execute("INSERT INTO solaredge_daily VALUES (?, ?, 'x')", (d, wh))
        for d, imp, exp in [("1998-07-01", 3.0, 4.0), ("1998-07-02", 5.0, 3.0), ("1998-07-03", 6.0, 1.0)]:
            power(conn, d, imp, exp)
        conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
        conn.commit()

    def test_summary_totals_extremes_yield_and_the_home_export_split(self, client_and_conn):
        client, conn = client_and_conn
        self.seed_two_years(conn)
        b = client.get("/api/solar/summary?from=1998-07-01&to=1998-07-03").get_json()
        assert b["available"] and b["production_source"] == "measured" and b["kwp"] == 2.5
        s = b["stats"]
        assert s["total_kwh"] == 24.0 and s["avg_kwh_per_day"] == 8.0
        assert s["best"] == {"date": "1998-07-01", "kwh": 12.0} and s["worst"]["kwh"] == 3.0
        assert s["specific_yield_kwh_per_kwp"] == 9.6
        assert s["export_pct"] == pytest.approx(8 / 24 * 100, abs=0.1)  # exported 4+3+1 = 8 of 24
        assert [d["basis"] for d in b["days"]] == ["measured"] * 3

    def test_summary_reaches_back_before_the_grid_meter_data_starts(self, client_and_conn):
        client, conn = client_and_conn
        self.seed_two_years(conn)  # 1997 has no power_daily rows at all
        b = client.get("/api/solar/summary?from=1997-07-01&to=1997-07-03").get_json()
        assert b["available"] and b["stats"]["total_kwh"] == 24.0 and b["stats"]["export_pct"] is None

    def test_previous_period_comparison_when_coverage_is_comparable(self, client_and_conn):
        client, conn = client_and_conn
        for i in range(1, 11):
            conn.execute("INSERT INTO solaredge_daily VALUES (?, ?, 'x')", (f"1998-06-{i:02d}", 10000.0))
            conn.execute("INSERT INTO solaredge_daily VALUES (?, ?, 'x')", (f"1998-06-{i + 10:02d}", 5000.0))
        conn.commit()
        b = client.get("/api/solar/summary?from=1998-06-11&to=1998-06-20").get_json()
        assert b["prior"]["total_kwh"] == 100.0 and b["prior"]["delta_pct"] == -50.0

    def test_no_previous_period_when_history_is_missing(self, client_and_conn):
        client, conn = client_and_conn
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-10', 5000.0, 'x')")
        conn.commit()
        assert client.get("/api/solar/summary?from=1998-07-10&to=1998-07-10").get_json()["prior"] is None

    def test_estimate_only_install_is_labelled_estimated(self, client_and_conn):
        client, conn = client_and_conn
        conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
        power(conn, "1998-07-01", 6.0, 0.0)
        conn.commit()
        b = client.get("/api/solar/summary?from=1998-07-01&to=1998-07-31").get_json()
        assert b["production_source"] == "estimated" and b["prior"] is None and b["days"][0]["basis"] in ("seasonal", "weather")

    def test_nothing_configured_is_unavailable_with_a_reason(self, client_and_conn):
        client, _conn = client_and_conn
        b = client.get("/api/solar/summary?from=1998-07-01&to=1998-07-31").get_json()
        assert b["available"] is False and "inverter" in b["reason"]

    def test_history_has_year_over_year_months_and_annual_totals(self, client_and_conn):
        client, conn = client_and_conn
        self.seed_two_years(conn)
        h = client.get("/api/solar/history").get_json()
        assert h["available"] and h["kwp"] == 2.5
        assert h["years"]["1997"][6] == 24.0 and h["years"]["1998"][6] == 24.0 and h["years"]["1998"][0] is None
        assert [a["year"] for a in h["annual"]] == [1997, 1998]

    def test_history_unavailable_without_stored_days(self, client_and_conn):
        client, _conn = client_and_conn
        assert client.get("/api/solar/history").get_json()["available"] is False

    def test_the_solar_tab_toggle_defaults_on_and_round_trips(self, client_and_conn):
        client, _conn = client_and_conn
        assert client.get("/api/settings/visibility").get_json()["show_solar_tab"] == 1
        client.post(
            "/api/settings/visibility", json={"show_solar_tab": False}, headers={"X-OmniMeter-Write-Api-Token": "test-write-token"}
        )
        assert client.get("/api/settings/visibility").get_json()["show_solar_tab"] == 0

    def test_history_annual_rows_carry_yield_and_sunshine_fields(self, client_and_conn):
        client, conn = client_and_conn
        for year, wh in [(1996, 2500000.0), (1997, 2250000.0)]:
            conn.execute("INSERT INTO solaredge_daily VALUES (?, ?, 'x')", (f"{year}-06-01", wh))
        # make both years 'complete' enough: 360+ days each
        for year in (1996, 1997):
            for d in range(1, 361):  # 360 distinct days (the June-1 row above is one of them) -> 'complete'
                conn.execute(
                    "INSERT OR IGNORE INTO solaredge_daily VALUES (?, ?, 'x')",
                    (f"{year}-{(d - 1) // 30 + 1:02d}-{(d - 1) % 30 + 1:02d}", 0.0),
                )
        conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
        conn.commit()
        h = client.get("/api/solar/history").get_json()
        by = {a["year"]: a for a in h["annual"]}
        assert by[1996]["complete"] is True and by[1996]["best"] is True and by[1997]["lowest"] is True
        assert by[1996]["yield_kwh_per_kwp"] == 1000.0 and by[1996]["irradiation_kwh_m2"] is None  # no weather rows seeded
        assert h["weather_attribution"] is None
