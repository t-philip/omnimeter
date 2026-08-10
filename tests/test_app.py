import io
from datetime import date, timedelta

import flask.testing
import pytest

from src.app import create_app

TEST_WRITE_TOKEN = "test-write-token"


class _AuthedFlaskClient(flask.testing.FlaskClient):
    """Every existing test in this file predates the write-token check
    and asserts on business-logic status codes (200/400), not auth. Rather
    than hand-editing every client.post(...) call, this client attaches the
    valid token to every request by default -- the auth layer itself is
    tested explicitly in TestWriteTokenAuth below, including what happens
    when the header is absent or wrong."""

    def open(self, *args, **kwargs):
        headers = kwargs.pop("headers", None) or {}
        if not isinstance(headers, dict):
            headers = dict(headers)
        headers.setdefault("X-OmniMeter-Write-Api-Token", TEST_WRITE_TOKEN)
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", TEST_WRITE_TOKEN)
    monkeypatch.setattr("src.app.DEFAULT_IMPORTS_DIR", tmp_path / "imports")
    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    flask_app.test_client_class = _AuthedFlaskClient
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class TestCosts:
    def test_carries_forward_last_known_rate_and_flags_stale(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
            "VALUES ('2026-01-01', '2026-04-27', 20.0, 20.0, 'test')"
        )
        conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-05-01', 10.0, 2.0, 8.0)")
        conn.commit()
        conn.close()

        resp = client.get("/api/costs?from=2026-05-01&to=2026-05-01")
        body = resp.get_json()
        assert body["available"] is True
        assert body["stale_count"] == 1
        day = body["days"][0]
        assert day["stale"] is True
        # (10.0 * 0.20) - (2.0 * 0.20) = 1.60, using the carried-forward rate
        assert day["power_cost_eur"] == pytest.approx(1.60)

    def test_no_stale_flag_when_rate_covers_the_date(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
            "VALUES ('2026-01-01', '2026-12-31', 20.0, 20.0, 'test')"
        )
        conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-05-01', 10.0, 2.0, 8.0)")
        conn.commit()
        conn.close()

        resp = client.get("/api/costs?from=2026-05-01&to=2026-05-01")
        body = resp.get_json()
        assert body["stale_count"] == 0
        assert body["days"][0]["stale"] is False


class TestOverviewBatterySoc:
    def test_returns_both_average_and_end_of_day_soc(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh, avg_soc_pct, eod_soc_pct) "
            "VALUES ('2026-07-08', 0.4, 0.9, 1.68, 1.0)"
        )
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh, avg_soc_pct, eod_soc_pct) "
            "VALUES ('2026-07-09', 2.9, 1.8, 45.2, 60.0)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/overview?from=2026-07-08&to=2026-07-09")
        body = resp.get_json()
        # avg_soc_pct averages the two days' own daily averages; eod_soc_pct
        # takes the *last date in range*'s end-of-day value, not an average.
        assert body["battery"]["avg_soc_pct"] == pytest.approx((1.68 + 45.2) / 2)
        assert body["battery"]["eod_soc_pct"] == pytest.approx(60.0)


class TestOverviewCurrentSoc:
    # current_soc_pct is "right now" (latest battery_daily row,
    # independent of the from/to range) -- current_soc_date is its
    # freshness, so the UI can flag a frozen tile instead of showing a
    # possibly-ancient reading with no indication it's stale.
    def test_current_soc_paired_with_its_date(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh, avg_soc_pct, eod_soc_pct) "
            "VALUES ('2026-07-08', 0.4, 0.9, 1.68, 1.0)"
        )
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh, avg_soc_pct, eod_soc_pct) "
            "VALUES ('2026-07-25', 2.9, 1.8, 45.2, 72.0)"
        )
        conn.commit()
        conn.close()

        # from/to deliberately doesn't cover the latest row -- current_soc_pct
        # must still reflect the true latest date, not whatever the selected
        # range happens to include (that's eod_soc_pct's job).
        resp = client.get("/api/overview?from=2026-07-08&to=2026-07-08")
        body = resp.get_json()
        assert body["battery"]["current_soc_pct"] == pytest.approx(72.0)
        assert body["battery"]["current_soc_date"] == "2026-07-25"

    def test_current_soc_null_when_no_battery_data(self, client):
        resp = client.get("/api/overview?from=2026-07-01&to=2026-07-02")
        body = resp.get_json()
        assert body["battery"]["current_soc_pct"] is None
        assert body["battery"]["current_soc_date"] is None


class TestCompareEndpoint:
    def test_rejects_missing_params(self, client):
        resp = client.get("/api/compare?a_from=2026-07-01&a_to=2026-07-05&b_from=2026-07-06")
        assert resp.status_code == 400

    def test_rejects_a_to_before_a_from(self, client):
        resp = client.get(
            "/api/compare?a_from=2026-07-05&a_to=2026-07-01&b_from=2026-07-06&b_to=2026-07-10"
        )
        assert resp.status_code == 400

    def test_two_periods_totalled_independently(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        for d, kwh in [("2026-07-01", 10.0), ("2026-07-02", 20.0), ("2026-08-01", 5.0), ("2026-08-02", 5.0)]:
            conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES (?, ?)", (d, kwh))
        conn.commit()
        conn.close()

        resp = client.get(
            "/api/compare?a_from=2026-07-01&a_to=2026-07-02&b_from=2026-08-01&b_to=2026-08-02"
        )
        body = resp.get_json()
        assert body["period_a"]["power"]["import_kwh"] == pytest.approx(30.0)
        assert body["period_b"]["power"]["import_kwh"] == pytest.approx(10.0)

    def test_single_day_period(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2026-07-01', 7.5)")
        conn.commit()
        conn.close()

        resp = client.get(
            "/api/compare?a_from=2026-07-01&a_to=2026-07-01&b_from=2026-07-02&b_to=2026-07-02"
        )
        body = resp.get_json()
        assert body["period_a"]["power"]["import_kwh"] == pytest.approx(7.5)
        assert body["period_b"]["power"]["import_kwh"] == pytest.approx(0.0)

    def test_occupancy_average_headcount_per_period(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-01 00:00', '2026-07-02 23:59', 1)"
        )
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-08-01 00:00', '2026-08-02 23:59', 4)"
        )
        conn.commit()
        conn.close()

        resp = client.get(
            "/api/compare?a_from=2026-07-01&a_to=2026-07-02&b_from=2026-08-01&b_to=2026-08-02"
        )
        body = resp.get_json()
        assert body["period_a"]["occupancy"]["avg_headcount"] == pytest.approx(1.0)
        assert body["period_b"]["occupancy"]["avg_headcount"] == pytest.approx(4.0)
        assert body["period_a"]["occupancy"]["covered_days"] == 2

    def test_occupancy_average_headcount_includes_away_days_as_zero(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # 1 away day (0) + 1 alone day (1) -> avg headcount 0.5, not 1.0
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-01 00:00', '2026-07-01 23:59', 0)"
        )
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-02 00:00', '2026-07-02 23:59', 1)"
        )
        conn.commit()
        conn.close()

        resp = client.get(
            "/api/compare?a_from=2026-07-01&a_to=2026-07-02&b_from=2026-08-01&b_to=2026-08-02"
        )
        body = resp.get_json()
        assert body["period_a"]["occupancy"]["avg_headcount"] == pytest.approx(0.5)

    def test_occupancy_avg_headcount_null_when_unlogged(self, client):
        resp = client.get(
            "/api/compare?a_from=2026-07-01&a_to=2026-07-02&b_from=2026-08-01&b_to=2026-08-02"
        )
        body = resp.get_json()
        assert body["period_a"]["occupancy"]["avg_headcount"] is None


class TestOverviewLastRefreshed:
    def test_returns_latest_power_reading_as_amsterdam_offset_iso(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, granularity) VALUES ('2026-07-16 22:00', 100.0, 'live')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, granularity) VALUES ('2026-07-16 21:45', 99.9, 'live')"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/overview?from=2026-07-08&to=2026-07-16")
        body = resp.get_json()
        # July is CEST (UTC+2); picks the later of the two rows, not the first/last inserted.
        assert body["last_refreshed"] == "2026-07-16T22:00:00+02:00"

    def test_null_when_no_power_readings_yet(self, client, app):
        resp = client.get("/api/overview?from=2026-07-08&to=2026-07-16")
        body = resp.get_json()
        assert body["last_refreshed"] is None


class TestDataFreshness:
    def test_returns_most_recent_date_per_category(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-07-17', 1, 1, 0)")
        conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-07-10', 1, 1, 0)")
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('2026-07-15', 1.0)")
        conn.execute("INSERT INTO water_daily (date, usage_l) VALUES ('2026-07-14', 100)")
        conn.execute("INSERT INTO battery_daily (date, charge_kwh, discharge_kwh) VALUES ('2026-07-16', 1, 1)")
        conn.commit()
        conn.close()

        resp = client.get("/api/data-freshness")
        body = resp.get_json()
        assert body["power"] == "2026-07-17"
        assert body["gas"] == "2026-07-15"
        assert body["water"] == "2026-07-14"
        assert body["battery"] == "2026-07-16"

    def test_costs_use_latest_rate_period_end_not_a_daily_table(self, client, app):
        # Costs isn't "imported" -- it's derived from the rate schedule, so
        # its freshness is the latest period_end, the same boundary /api/costs
        # already uses to decide when a day's cost is stale.
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh) "
            "VALUES ('2026-01-01', '2026-04-27', 20.0, 20.0)"
        )
        conn.execute(
            "INSERT INTO gas_rate_schedule (period_start, period_end, price_eur_per_m3) "
            "VALUES ('2026-01-01', '2026-05-15', 1.3)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/data-freshness")
        body = resp.get_json()
        assert body["costs_power"] == "2026-04-27"
        assert body["costs_gas"] == "2026-05-15"

    def test_null_when_no_data_at_all(self, client):
        resp = client.get("/api/data-freshness")
        body = resp.get_json()
        assert body["power"] is None
        assert body["costs_power"] is None


class TestDataHealth:
    # Distinct from TestDataFreshness above -- this checks for
    # missing days *within* a category's own tracked history, not just the
    # most recent date.
    def test_no_data_at_all_reports_null_bounds_no_gaps(self, client):
        resp = client.get("/api/data-health")
        body = resp.get_json()
        assert body["power"] == {"first_date": None, "last_date": None, "gaps": []}

    def test_contiguous_data_reports_no_gaps(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, 1.0)", (d,))
        conn.commit()
        conn.close()

        resp = client.get("/api/data-health")
        body = resp.get_json()
        assert body["gas"] == {"first_date": "2026-07-01", "last_date": "2026-07-03", "gaps": []}

    def test_single_missing_day_reported_as_one_day_range(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # 2026-07-02 deliberately absent.
        for d in ("2026-07-01", "2026-07-03"):
            conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, 100)", (d,))
        conn.commit()
        conn.close()

        resp = client.get("/api/data-health")
        body = resp.get_json()
        assert body["water"]["gaps"] == [
            {"start": "2026-07-02", "end": "2026-07-02", "fingerprint": "2026-07-02|2026-07-02", "acknowledged": False}
        ]

    def test_multi_day_gap_grouped_into_one_range(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # 2026-07-02 through 2026-07-04 deliberately absent.
        for d in ("2026-07-01", "2026-07-05"):
            conn.execute(
                "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES (?, 1, 1, 0)", (d,)
            )
        conn.commit()
        conn.close()

        resp = client.get("/api/data-health")
        body = resp.get_json()
        assert body["power"]["gaps"] == [
            {"start": "2026-07-02", "end": "2026-07-04", "fingerprint": "2026-07-02|2026-07-04", "acknowledged": False}
        ]

    def test_two_separate_gaps_reported_as_two_ranges(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # Missing: 07-02 (single day), 07-04 to 07-05 (two days).
        for d in ("2026-07-01", "2026-07-03", "2026-07-06"):
            conn.execute("INSERT INTO battery_daily (date, charge_kwh, discharge_kwh) VALUES (?, 1, 1)", (d,))
        conn.commit()
        conn.close()

        resp = client.get("/api/data-health")
        body = resp.get_json()
        assert body["battery"]["gaps"] == [
            {"start": "2026-07-02", "end": "2026-07-02", "fingerprint": "2026-07-02|2026-07-02", "acknowledged": False},
            {"start": "2026-07-04", "end": "2026-07-05", "fingerprint": "2026-07-04|2026-07-05", "acknowledged": False},
        ]

    def test_bounded_to_own_coverage_not_flagged_before_first_or_after_last(self, client, app):
        # A device added mid-history must not have the entire period before
        # it existed reported as one giant "gap" -- that's "not tracked yet,"
        # a different thing from a real ingest failure. Similarly the most
        # recent date is never itself "missing" -- staleness is
        # /api/data-freshness's job, not this endpoint's.
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('2026-07-20', 1.0)")
        conn.commit()
        conn.close()

        resp = client.get("/api/data-health")
        body = resp.get_json()
        assert body["gas"] == {"first_date": "2026-07-20", "last_date": "2026-07-20", "gaps": []}


class TestDataQuality:
    # "is my data trustworthy" report, distinct from TestDataHealth
    # above -- glitch counts, cross-source disagreements, negative deltas,
    # and (battery) out-of-range gauge values. Outlier days moved to
    # /api/consumption-notes (see TestConsumptionNotes). The underlying
    # checks are unit-tested directly in test_aggregate.py; this only
    # confirms the endpoint wires them up and returns the expected shape.
    def test_no_data_returns_well_formed_report(self, client):
        resp = client.get("/api/data-quality")
        body = resp.get_json()
        for category in ("power", "gas", "water", "battery"):
            entry = body[category]
            assert entry["negative_deltas"] == {"count": 0, "items": []}
            assert entry["glitch_episodes"] == {"count": 0, "items": []}
            assert entry["granularity_disagreements"] == []
            assert entry["implausible_values"] == []
            # Outlier days are served by /api/consumption-notes now, and must
            # not leak back into the integrity report.
            assert "outlier_days" not in entry

    def test_consumption_notes_endpoint_returns_well_formed_report(self, client):
        resp = client.get("/api/consumption-notes")
        assert resp.status_code == 200
        body = resp.get_json()
        # Battery is absent by design -- see the aggregate-level test.
        assert set(body) == {"categories", "events"}
        assert set(body["categories"]) == {"power", "gas", "water"}
        for category in ("power", "gas", "water"):
            assert body["categories"][category]["outlier_days"] == []
        assert body["events"] == []

    def test_consumption_notes_endpoint_honours_the_date_range(self, client, app):
        # This endpoint used to ignore from/to entirely -- the only
        # view in the app that did, and the reason it returned five years of
        # notes while every chart beside it showed 90 days.
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # 20 flat days, then a 5-day near-zero run (2026-06-21..06-25), then 3
        # flat days -- shaped like a real absence.
        start = date(2026, 6, 1)
        for i in range(28):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES (?, ?, 0, 0)",
                (d.isoformat(), 0.2 if 20 <= i <= 24 else 4.0),
            )
        conn.commit()
        conn.close()

        def import_notes(query):
            body = client.get(f"/api/consumption-notes{query}").get_json()
            return [o for o in body["categories"]["power"]["outlier_days"] if o["metric"] == "import_kwh"]

        # The seeded absence is one 5-day episode, not five notes.
        whole = import_notes("?from=2026-01-01&to=2026-12-31")
        assert len(whole) == 1
        assert (whole[0]["start"], whole[0]["end"], whole[0]["days"]) == ("2026-06-21", "2026-06-25", 5)

        assert import_notes("?from=2026-07-01&to=2026-07-31") == []
        # A range narrower than the detector's history requirement must still
        # surface the episode -- see consumption_notes_report for why.
        assert import_notes("?from=2026-06-20&to=2026-06-26") == whole

    def test_reconciliation_endpoint_returns_well_formed_report(self, client):
        resp = client.get("/api/reconciliation")
        assert resp.status_code == 200
        body = resp.get_json()
        for category in ("power", "gas", "water", "battery"):
            assert body[category] == {"verified": 0, "mismatches": [], "unverifiable": []}

    def test_reconciliation_mismatch_can_be_acknowledged(self, client):
        # The two reconciliation issue types must be in the acknowledge
        # allowlist, or the Acknowledge button 400s on every mismatch.
        for issue_type in ("reconciliation_mismatch", "reconciliation_unverifiable"):
            resp = client.post(
                "/api/data-quality/acknowledge",
                json={"category": "power", "issue_type": issue_type, "fingerprint": "2026-07-02|import_kwh"},
            )
            assert resp.status_code == 200, issue_type

    # ---- bulk acknowledge -------------------------------------------------

    def _ack_count(self):
        from src import db

        conn = db.get_connection()
        n = conn.execute("SELECT COUNT(*) FROM acknowledged_issues").fetchone()[0]
        conn.close()
        return n

    def _items(self, n, category="power"):
        return [
            {"category": category, "issue_type": "outlier_day", "fingerprint": f"2026-07-{d:02d}|import_kwh"}
            for d in range(1, n + 1)
        ]

    def test_bulk_acknowledge_writes_every_item_in_one_request(self, client):
        resp = client.post("/api/data-quality/acknowledge-bulk", json={"items": self._items(20)})
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "requested": 20, "changed": 20}
        assert self._ack_count() == 20

    def test_bulk_unacknowledge_is_the_exact_inverse(self, client):
        # A bulk action nobody can undo would be worse than no bulk action.
        client.post("/api/data-quality/acknowledge-bulk", json={"items": self._items(20)})
        resp = client.delete("/api/data-quality/acknowledge-bulk", json={"items": self._items(20)})
        assert resp.status_code == 200
        assert resp.get_json()["changed"] == 20
        assert self._ack_count() == 0

    def test_bulk_acknowledge_reports_rows_actually_written(self, client):
        # `changed` counts real writes, not request size -- re-acknowledging
        # already-acknowledged findings is a no-op, and saying "20" again
        # would overstate what happened.
        client.post("/api/data-quality/acknowledge-bulk", json={"items": self._items(20)})
        resp = client.post("/api/data-quality/acknowledge-bulk", json={"items": self._items(20)})
        assert resp.get_json() == {"ok": True, "requested": 20, "changed": 0}
        assert self._ack_count() == 20

    def test_bulk_acknowledge_is_all_or_nothing(self, client):
        # Every item is validated before any is written. A partially applied
        # bulk action leaves the user with no idea what actually happened.
        items = [*self._items(5), {"category": "nonsense", "issue_type": "outlier_day", "fingerprint": "x"}]
        resp = client.post("/api/data-quality/acknowledge-bulk", json={"items": items})
        assert resp.status_code == 400
        assert self._ack_count() == 0

    def test_bulk_acknowledge_rejects_bad_issue_type_and_missing_fingerprint(self, client):
        for bad in (
            {"category": "power", "issue_type": "not_a_type", "fingerprint": "x"},
            {"category": "power", "issue_type": "outlier_day"},
            {"category": "power", "issue_type": "outlier_day", "fingerprint": ""},
            "not an object",
        ):
            resp = client.post("/api/data-quality/acknowledge-bulk", json={"items": [bad]})
            assert resp.status_code == 400, bad
        assert self._ack_count() == 0

    def test_bulk_acknowledge_rejects_empty_or_missing_items(self, client):
        for body in ({"items": []}, {}, {"items": "everything"}):
            resp = client.post("/api/data-quality/acknowledge-bulk", json=body)
            assert resp.status_code == 400, body

    def test_bulk_acknowledge_has_an_upper_bound(self, client):
        resp = client.post("/api/data-quality/acknowledge-bulk", json={"items": self._items(5001)})
        assert resp.status_code == 400
        assert self._ack_count() == 0

    def test_bulk_acknowledge_shares_storage_with_single_acknowledge(self, client):
        # Both paths write the same triple, so a bulk unacknowledge must clear
        # something acknowledged individually, and vice versa.
        one = {"category": "gas", "issue_type": "outlier_day", "fingerprint": "2026-07-01|usage_m3"}
        client.post("/api/data-quality/acknowledge", json=one)
        assert self._ack_count() == 1
        resp = client.delete("/api/data-quality/acknowledge-bulk", json={"items": [one]})
        assert resp.get_json()["changed"] == 1
        assert self._ack_count() == 0

    def test_negative_delta_surfaced_for_gas(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-01-01 00:00', 100.0, 'api_live')"
        )
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-01-01 00:15', 50.0, 'api_live')"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/data-quality")
        body = resp.get_json()
        assert body["gas"]["negative_deltas"]["count"] == 1
        item = body["gas"]["negative_deltas"]["items"][0]
        assert item["delta"] == pytest.approx(-50.0)
        assert item["fingerprint"] == "2026-01-01 00:15|total_gas_m3"
        assert item["acknowledged"] is False

    def test_battery_soc_out_of_range_surfaced(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh, min_soc_pct, max_soc_pct) "
            "VALUES ('2026-07-01', 1.0, 1.0, -5.0, 50.0)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/data-quality")
        body = resp.get_json()
        assert len(body["battery"]["implausible_values"]) == 1
        finding = body["battery"]["implausible_values"][0]
        assert finding["date"] == "2026-07-01"
        assert finding["value"] == -5.0
        assert finding["metric"] == "min_soc_pct"


class TestAcknowledge:
    # Acknowledging never edits or deletes any reading -- only
    # records that a specific flagged finding was reviewed.
    def test_acknowledge_then_visible_in_report(self, client):
        resp = client.post(
            "/api/data-quality/acknowledge",
            json={"category": "gas", "issue_type": "negative_delta", "fingerprint": "2026-01-01 00:15|total_gas_m3"},
        )
        assert resp.status_code == 200

        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-01-01 00:00', 100.0, 'api_live')"
        )
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-01-01 00:15', 50.0, 'api_live')"
        )
        conn.commit()
        conn.close()

        body = client.get("/api/data-quality").get_json()
        item = body["gas"]["negative_deltas"]["items"][0]
        assert item["acknowledged"] is True
        # Tag-don't-filter: still present, count unchanged.
        assert body["gas"]["negative_deltas"]["count"] == 1

    def test_double_acknowledge_is_a_no_op(self, client):
        body = {"category": "power", "issue_type": "outlier_day", "fingerprint": "2026-01-01|import_kwh"}
        assert client.post("/api/data-quality/acknowledge", json=body).status_code == 200
        assert client.post("/api/data-quality/acknowledge", json=body).status_code == 200  # no UNIQUE-constraint error

    def test_unacknowledge_never_acked_is_a_no_op(self, client):
        resp = client.delete(
            "/api/data-quality/acknowledge",
            json={"category": "power", "issue_type": "outlier_day", "fingerprint": "never-acked"},
        )
        assert resp.status_code == 200

    def test_acknowledge_then_unacknowledge_round_trips(self, client):
        body = {"category": "water", "issue_type": "gap", "fingerprint": "2026-01-01|2026-01-02"}
        client.post("/api/data-quality/acknowledge", json=body)
        health_before = client.get("/api/data-health").get_json()
        assert health_before  # sanity: endpoint still works with an ack present

        resp = client.delete("/api/data-quality/acknowledge", json=body)
        assert resp.status_code == 200
        # Re-acknowledging after un-ack must succeed cleanly.
        assert client.post("/api/data-quality/acknowledge", json=body).status_code == 200

    def test_invalid_category_rejected(self, client):
        resp = client.post(
            "/api/data-quality/acknowledge",
            json={"category": "not-a-real-category", "issue_type": "gap", "fingerprint": "x"},
        )
        assert resp.status_code == 400

    def test_invalid_issue_type_rejected(self, client):
        resp = client.post(
            "/api/data-quality/acknowledge",
            json={"category": "power", "issue_type": "not-a-real-type", "fingerprint": "x"},
        )
        assert resp.status_code == 400

    def test_gap_acknowledged_flag_reflected_in_data_health(self, client):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        for d in ("2026-07-01", "2026-07-03"):
            conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, 100)", (d,))
        conn.commit()
        conn.close()

        gaps = client.get("/api/data-health").get_json()["water"]["gaps"]
        assert gaps == [{"start": "2026-07-02", "end": "2026-07-02", "fingerprint": "2026-07-02|2026-07-02", "acknowledged": False}]

        client.post(
            "/api/data-quality/acknowledge",
            json={"category": "water", "issue_type": "gap", "fingerprint": "2026-07-02|2026-07-02"},
        )
        gaps_after = client.get("/api/data-health").get_json()["water"]["gaps"]
        assert gaps_after[0]["acknowledged"] is True
        assert len(gaps_after) == 1  # still present, not filtered out


class TestDeleteReading:
    # Nulls just the one flagged column, never DELETEs the row --
    # power_readings/battery_readings carry other metrics under the same
    # (time, granularity) key that must survive untouched.
    def _seed_power_row(self, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, export_combined_kwh, granularity) "
            "VALUES ('2026-01-01 00:15', 50.0, 999.0, 'api_live')"
        )
        conn.commit()
        conn.close()

    def test_delete_nulls_only_the_flagged_column(self, client, app):
        self._seed_power_row(app)
        resp = client.delete(
            "/api/readings/power",
            json={"time": "2026-01-01 00:15", "granularity": "api_live", "metric": "import_combined_kwh", "value": 50.0},
        )
        assert resp.status_code == 200

        from src import db

        conn = db.get_connection()
        row = conn.execute(
            "SELECT import_combined_kwh, export_combined_kwh FROM power_readings "
            "WHERE time = '2026-01-01 00:15' AND granularity = 'api_live'"
        ).fetchone()
        conn.close()
        assert row["import_combined_kwh"] is None
        assert row["export_combined_kwh"] == pytest.approx(999.0)  # sibling column untouched

    def test_delete_triggers_immediate_rollup_rebuild(self, client, app):
        self._seed_power_row(app)
        client.delete(
            "/api/readings/power",
            json={"time": "2026-01-01 00:15", "granularity": "api_live", "metric": "import_combined_kwh", "value": 50.0},
        )
        before = client.get("/api/data-quality").get_json()
        assert before["power"]["negative_deltas"]["count"] == 0

    def test_stale_value_rejected_with_409(self, client, app):
        self._seed_power_row(app)
        resp = client.delete(
            "/api/readings/power",
            json={"time": "2026-01-01 00:15", "granularity": "api_live", "metric": "import_combined_kwh", "value": 999.0},
        )
        assert resp.status_code == 409

    def test_nonexistent_reading_returns_404(self, client, app):
        resp = client.delete(
            "/api/readings/power",
            json={"time": "2099-01-01 00:15", "granularity": "api_live", "metric": "import_combined_kwh", "value": 1.0},
        )
        assert resp.status_code == 404

    def test_unknown_category_rejected_with_400(self, client):
        resp = client.delete(
            "/api/readings/not-a-category",
            json={"time": "2026-01-01 00:15", "granularity": "api_live", "metric": "value", "value": 1.0},
        )
        assert resp.status_code == 400

    def test_unknown_metric_for_category_rejected_with_400(self, client, app):
        self._seed_power_row(app)
        resp = client.delete(
            "/api/readings/power",
            json={"time": "2026-01-01 00:15", "granularity": "api_live", "metric": "soc_pct", "value": 50.0},
        )
        assert resp.status_code == 400


class TestImportCsv:
    def test_valid_upload_ingests_rows(self, client):
        content = b"time,Total gas used\n2026-01-01 00:00,100.0\n2026-01-01 00:15,100.5\n"
        data = {"file": (io.BytesIO(content), "P1g-2026-1-01-2026-7-14.csv")}
        resp = client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["category"] == "gas"
        assert body["rows_ingested"] == 2

    def test_reingest_same_file_reports_zero_rows(self, client):
        content = b"time,water usage dl\n2026-01-01 00:00,100\n2026-01-01 00:15,101\n"
        data = {"file": (io.BytesIO(content), "Water-2026-1-01-2026-7-14.csv")}
        client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        data2 = {"file": (io.BytesIO(content), "Water-2026-1-01-2026-7-14.csv")}
        resp = client.post("/api/import/csv", data=data2, content_type="multipart/form-data")
        assert resp.get_json()["rows_ingested"] == 0

    def test_unrecognized_filename_rejected(self, client):
        data = {"file": (io.BytesIO(b"a,b\n1,2\n"), "random.csv")}
        resp = client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_no_file_rejected(self, client):
        resp = client.post("/api/import/csv", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_malformed_file_quarantined_not_left_in_dropzone(self, client, app, tmp_path):
        # A code review found an upload that crashes ingest_file used to stay
        # in the dropzone, so omnimeter-ingest.timer would retry (and fail on)
        # it every 15 minutes forever. It must be moved aside instead.
        content = b"time,Import kWh\nnot-a-timestamp,1.0\nalso-bad,2.0\n"
        data = {"file": (io.BytesIO(content), "Bat-bad.csv")}
        resp = client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "error" in resp.get_json()

        # app's DEFAULT_IMPORTS_DIR is patched to tmp_path/imports by the
        # `app` fixture (monkeypatch.setattr in conftest above).
        from src.app import DEFAULT_IMPORTS_DIR

        assert not (DEFAULT_IMPORTS_DIR / "Bat-bad.csv").exists()
        assert (DEFAULT_IMPORTS_DIR / "failed" / "Bat-bad.csv").exists()


class TestSettingsValidation:
    def test_pv_rejects_nan(self, client):
        # NaN must never reach the DB -- it would silently poison every
        # downstream average/self-sufficiency estimate that reads kwp_rating.
        resp = client.post("/api/settings/pv", json={"kwp_rating": "nan"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_pv_rejects_out_of_range(self, client):
        resp = client.post("/api/settings/pv", json={"kwp_rating": 99999})
        assert resp.status_code == 400

    def test_pv_accepts_valid_value(self, client):
        resp = client.post("/api/settings/pv", json={"kwp_rating": 6.5})
        assert resp.status_code == 200
        assert resp.get_json()["kwp_rating"] == pytest.approx(6.5)

    def test_rate_rejects_missing_field(self, client):
        resp = client.post(
            "/api/settings/rates",
            json={"period_start": "2026-01-01", "period_end": "2026-06-30", "buy_ct_per_kwh": 20.0},
        )
        assert resp.status_code == 400

    def test_rate_rejects_end_before_start(self, client):
        resp = client.post(
            "/api/settings/rates",
            json={
                "period_start": "2026-06-30",
                "period_end": "2026-01-01",
                "buy_ct_per_kwh": 20.0,
                "sell_ct_per_kwh": 20.0,
            },
        )
        assert resp.status_code == 400

    def test_rate_rejects_overlap_with_existing_period(self, client):
        # A code review found overlapping periods made rate_for()'s
        # first-match lookup order-dependent with no signal that anything
        # was wrong.
        first = {
            "period_start": "2026-01-01",
            "period_end": "2026-06-30",
            "buy_ct_per_kwh": 20.0,
            "sell_ct_per_kwh": 20.0,
        }
        assert client.post("/api/settings/rates", json=first).status_code == 200

        overlapping = {
            "period_start": "2026-04-01",
            "period_end": "2026-09-30",
            "buy_ct_per_kwh": 22.0,
            "sell_ct_per_kwh": 22.0,
        }
        resp = client.post("/api/settings/rates", json=overlapping)
        assert resp.status_code == 400
        assert "overlap" in resp.get_json()["error"]

    def test_rate_allows_adjacent_non_overlapping_period(self, client):
        first = {
            "period_start": "2026-01-01",
            "period_end": "2026-06-30",
            "buy_ct_per_kwh": 20.0,
            "sell_ct_per_kwh": 20.0,
        }
        adjacent = {
            "period_start": "2026-07-01",
            "period_end": "2026-12-31",
            "buy_ct_per_kwh": 21.0,
            "sell_ct_per_kwh": 21.0,
        }
        assert client.post("/api/settings/rates", json=first).status_code == 200
        assert client.post("/api/settings/rates", json=adjacent).status_code == 200

    def test_rate_omitted_period_end_is_open_ended(self, client):
        # A prospective rate sheet (e.g. a supplier's
        # Tarievenblad) has no known end date yet.
        resp = client.post(
            "/api/settings/rates",
            json={"period_start": "2026-01-01", "buy_ct_per_kwh": 20.0, "sell_ct_per_kwh": 20.0},
        )
        assert resp.status_code == 200
        rows = resp.get_json()
        assert rows[0]["period_end"] == "9999-12-31"

    def test_rate_new_open_ended_period_shrinks_previous_open_period(self, client):
        first = client.post(
            "/api/settings/rates",
            json={"period_start": "2026-01-01", "buy_ct_per_kwh": 20.0, "sell_ct_per_kwh": 20.0},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/settings/rates",
            json={"period_start": "2026-07-01", "buy_ct_per_kwh": 22.0, "sell_ct_per_kwh": 22.0},
        )
        assert second.status_code == 200
        rows = {r["period_start"]: r for r in second.get_json()}
        assert rows["2026-01-01"]["period_end"] == "2026-06-30"
        assert rows["2026-07-01"]["period_end"] == "9999-12-31"

    def test_rate_closed_period_also_shrinks_previous_open_period(self, client):
        # A backfilled historical (closed) period superseding an existing
        # open-ended one should reconcile the same way an open-ended
        # successor does.
        open_ended = client.post(
            "/api/settings/rates",
            json={"period_start": "2026-01-01", "buy_ct_per_kwh": 20.0, "sell_ct_per_kwh": 20.0},
        )
        assert open_ended.status_code == 200

        closed = client.post(
            "/api/settings/rates",
            json={
                "period_start": "2026-07-01",
                "period_end": "2026-12-31",
                "buy_ct_per_kwh": 22.0,
                "sell_ct_per_kwh": 22.0,
            },
        )
        assert closed.status_code == 200
        rows = {r["period_start"]: r for r in closed.get_json()}
        assert rows["2026-01-01"]["period_end"] == "2026-06-30"

    def test_rate_backfilled_historical_period_before_open_period_untouched(self, client):
        # A new period starting *before* the currently-open row's start is a
        # historical backfill, not a successor -- reconciliation must not
        # touch the open row for this.
        open_ended = client.post(
            "/api/settings/rates",
            json={"period_start": "2026-07-01", "buy_ct_per_kwh": 22.0, "sell_ct_per_kwh": 22.0},
        )
        assert open_ended.status_code == 200

        backfill = client.post(
            "/api/settings/rates",
            json={
                "period_start": "2026-01-01",
                "period_end": "2026-06-30",
                "buy_ct_per_kwh": 20.0,
                "sell_ct_per_kwh": 20.0,
            },
        )
        assert backfill.status_code == 200
        rows = {r["period_start"]: r for r in backfill.get_json()}
        assert rows["2026-07-01"]["period_end"] == "9999-12-31"

    def test_gas_rate_omitted_period_end_is_open_ended_and_reconciles(self, client):
        first = client.post(
            "/api/settings/gas-rates",
            json={"period_start": "2026-01-01", "price_eur_per_m3": 1.5},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/settings/gas-rates",
            json={"period_start": "2026-07-01", "price_eur_per_m3": 1.6},
        )
        assert second.status_code == 200
        rows = {r["period_start"]: r for r in second.get_json()}
        assert rows["2026-01-01"]["period_end"] == "2026-06-30"
        assert rows["2026-07-01"]["period_end"] == "9999-12-31"


class TestOccupancySettings:
    def test_rejects_missing_field(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59"},
        )
        assert resp.status_code == 400

    def test_rejects_end_before_start(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-15T00:00", "date_to": "2026-07-10T23:59", "occupant_count": 2},
        )
        assert resp.status_code == 400

    def test_rejects_non_integer_count(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59", "occupant_count": 2.5},
        )
        assert resp.status_code == 400

    def test_rejects_out_of_range_count(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59", "occupant_count": 21},
        )
        assert resp.status_code == 400

    def test_rejects_negative_count(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59", "occupant_count": -1},
        )
        assert resp.status_code == 400

    def test_rejects_malformed_datetime(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "not-a-datetime", "date_to": "2026-07-15T23:59", "occupant_count": 1},
        )
        assert resp.status_code == 400

    def test_accepts_zero_count_for_away_periods(self, client):
        # 0 = nobody home (e.g. travelling) -- a real, common case, not an
        # invalid one.
        resp = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59", "occupant_count": 0},
        )
        assert resp.status_code == 200
        assert resp.get_json()[0]["occupant_count"] == 0

    def test_create_and_list(self, client):
        resp = client.post(
            "/api/settings/occupancy",
            json={
                "date_from": "2026-07-10T00:00",
                "date_to": "2026-07-15T23:59",
                "occupant_count": 3,
                "notes": "friends visiting",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body) == 1
        assert body[0]["occupant_count"] == 3
        assert body[0]["notes"] == "friends visiting"
        # Normalized to the app's storage convention, 'YYYY-MM-DD HH:MM'.
        assert body[0]["date_from"] == "2026-07-10 00:00"
        assert body[0]["date_to"] == "2026-07-15 23:59"

    def test_allows_overlap_with_existing_period(self, client):
        # Unlike rate_schedule, occupancy_log deliberately allows overlap --
        # e.g. a shorter trip nested inside a longer visit -- resolved at
        # read time by aggregate.expand_occupancy_by_day(), not rejected here.
        first = {"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59", "occupant_count": 3}
        assert client.post("/api/settings/occupancy", json=first).status_code == 200

        overlapping = {"date_from": "2026-07-13T00:00", "date_to": "2026-07-20T23:59", "occupant_count": 2}
        resp = client.post("/api/settings/occupancy", json=overlapping)
        assert resp.status_code == 200
        assert len(resp.get_json()) == 2

    def test_allows_adjacent_non_overlapping_period(self, client):
        first = {"date_from": "2026-07-01T00:00", "date_to": "2026-07-10T23:59", "occupant_count": 1}
        adjacent = {"date_from": "2026-07-11T00:00", "date_to": "2026-07-15T23:59", "occupant_count": 3}
        assert client.post("/api/settings/occupancy", json=first).status_code == 200
        assert client.post("/api/settings/occupancy", json=adjacent).status_code == 200

    def test_allows_two_entries_same_calendar_day_different_times(self, client):
        # The whole point of the CR: a morning departure and an evening
        # return on the same date, as two non-overlapping entries.
        morning = {"date_from": "2026-07-10T00:00", "date_to": "2026-07-10T08:00", "occupant_count": 2}
        away = {"date_from": "2026-07-10T08:00", "date_to": "2026-07-10T18:00", "occupant_count": 0}
        evening = {"date_from": "2026-07-10T18:00", "date_to": "2026-07-10T23:59", "occupant_count": 2}
        assert client.post("/api/settings/occupancy", json=morning).status_code == 200
        assert client.post("/api/settings/occupancy", json=away).status_code == 200
        resp = client.post("/api/settings/occupancy", json=evening)
        assert resp.status_code == 200
        assert len(resp.get_json()) == 3

    def test_allows_overlap_within_same_calendar_day(self, client):
        first = {"date_from": "2026-07-10T08:00", "date_to": "2026-07-10T18:00", "occupant_count": 0}
        assert client.post("/api/settings/occupancy", json=first).status_code == 200

        overlapping = {"date_from": "2026-07-10T12:00", "date_to": "2026-07-10T20:00", "occupant_count": 2}
        resp = client.post("/api/settings/occupancy", json=overlapping)
        assert resp.status_code == 200
        assert len(resp.get_json()) == 2

    def test_delete_removes_entry(self, client):
        create = client.post(
            "/api/settings/occupancy",
            json={"date_from": "2026-07-10T00:00", "date_to": "2026-07-15T23:59", "occupant_count": 3},
        )
        entry_id = create.get_json()[0]["id"]

        resp = client.delete(f"/api/settings/occupancy/{entry_id}")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/api/settings/occupancy/999")
        assert resp.status_code == 404


class TestOccupancyStats:
    def test_unavailable_with_no_entries(self, client):
        resp = client.get("/api/occupancy-stats?from=2026-07-01&to=2026-07-31")
        body = resp.get_json()
        assert body["available"] is False

    def test_alone_vs_occupied_averages(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # 2 alone days (1 person), then 2 occupied days (4 people)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-01 00:00', '2026-07-02 23:59', 1)"
        )
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-03 00:00', '2026-07-04 23:59', 4)"
        )
        for d, kwh in [
            ("2026-07-01", 10.0),
            ("2026-07-02", 12.0),
            ("2026-07-03", 30.0),
            ("2026-07-04", 34.0),
        ]:
            conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES (?, ?)", (d, kwh))
        conn.commit()
        conn.close()

        resp = client.get("/api/occupancy-stats?from=2026-07-01&to=2026-07-04")
        body = resp.get_json()
        assert body["available"] is True
        assert body["covered_days"] == 4
        assert body["alone_days"] == 2
        assert body["occupied_days"] == 2
        assert body["power"]["avg_alone"] == pytest.approx(11.0)
        assert body["power"]["avg_occupied"] == pytest.approx(32.0)
        # person_days = 1+1+4+4 = 10, total = 10+12+30+34 = 86
        assert body["power"]["per_person_day"] == pytest.approx(8.6)

    def test_away_days_tracked_separately_and_excluded_from_per_person_day(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        # 1 away day (0 people), 1 alone day (1 person)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-01 00:00', '2026-07-01 23:59', 0)"
        )
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-02 00:00', '2026-07-02 23:59', 1)"
        )
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2026-07-01', 2.0)")  # standby draw, nobody home
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2026-07-02', 10.0)")
        conn.commit()
        conn.close()

        resp = client.get("/api/occupancy-stats?from=2026-07-01&to=2026-07-02")
        body = resp.get_json()
        assert body["away_days"] == 1
        assert body["alone_days"] == 1
        assert body["power"]["avg_away"] == pytest.approx(2.0)
        assert body["power"]["avg_alone"] == pytest.approx(10.0)
        # The away day's usage (2.0 kWh) must NOT be folded into
        # per_person_day -- only the alone day (1 person, 10.0 kWh) counts,
        # since there's nobody on the away day to attribute usage to.
        assert body["power"]["per_person_day"] == pytest.approx(10.0)

    def test_days_outside_range_excluded_from_stats(self, client, app):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-01 00:00', '2026-07-01 23:59', 1)"
        )
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2026-07-01', 10.0)")
        # Usage exists for a day with no occupancy entry -- must not leak in.
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2026-07-15', 999.0)")
        conn.commit()
        conn.close()

        resp = client.get("/api/occupancy-stats?from=2026-07-01&to=2026-07-31")
        body = resp.get_json()
        assert body["power"]["days_with_data"] == 1
        assert body["power"]["avg_alone"] == pytest.approx(10.0)


class TestFiscalYearSettings:
    def test_defaults_match_db_schema(self, client):
        # Power/Gas = 1 May (NL utility billing year), Water = 1 Jan.
        body = client.get("/api/settings/fiscal-years").get_json()
        assert (body["power_fy_start_month"], body["power_fy_start_day"]) == (5, 1)
        assert (body["gas_fy_start_month"], body["gas_fy_start_day"]) == (5, 1)
        assert (body["water_fy_start_month"], body["water_fy_start_day"]) == (1, 1)

    def test_round_trips_a_saved_value(self, client):
        resp = client.post(
            "/api/settings/fiscal-years",
            json={
                "power_fy_start_month": 5,
                "power_fy_start_day": 1,
                "gas_fy_start_month": 4,
                "gas_fy_start_day": 15,
                "water_fy_start_month": 1,
                "water_fy_start_day": 1,
            },
        )
        assert resp.status_code == 200
        body = client.get("/api/settings/fiscal-years").get_json()
        assert (body["gas_fy_start_month"], body["gas_fy_start_day"]) == (4, 15)

    def test_rejects_invalid_day_for_month(self, client):
        # 31 April doesn't exist -- must be rejected, not silently stored as
        # something SQLite happens to accept.
        resp = client.post(
            "/api/settings/fiscal-years",
            json={
                "power_fy_start_month": 4,
                "power_fy_start_day": 31,
                "gas_fy_start_month": 5,
                "gas_fy_start_day": 1,
                "water_fy_start_month": 1,
                "water_fy_start_day": 1,
            },
        )
        assert resp.status_code == 400

    def test_rejects_feb_29_as_a_non_leap_safe_anchor(self, client):
        resp = client.post(
            "/api/settings/fiscal-years",
            json={
                "power_fy_start_month": 2,
                "power_fy_start_day": 29,
                "gas_fy_start_month": 5,
                "gas_fy_start_day": 1,
                "water_fy_start_month": 1,
                "water_fy_start_day": 1,
            },
        )
        assert resp.status_code == 400

    def test_rejects_month_out_of_range(self, client):
        resp = client.post(
            "/api/settings/fiscal-years",
            json={
                "power_fy_start_month": 13,
                "power_fy_start_day": 1,
                "gas_fy_start_month": 5,
                "gas_fy_start_day": 1,
                "water_fy_start_month": 1,
                "water_fy_start_day": 1,
            },
        )
        assert resp.status_code == 400

    def test_rejects_missing_field(self, client):
        resp = client.post("/api/settings/fiscal-years", json={"power_fy_start_month": 5})
        assert resp.status_code == 400


class TestFeatureToggles:
    def test_defaults_all_enabled(self, client):
        body = client.get("/api/settings/toggles").get_json()
        for field in (
            "homewizard_api_enabled",
            "import_power_enabled",
            "import_gas_enabled",
            "import_water_enabled",
            "pdf_import_enabled",
            "nightly_backup_enabled",
        ):
            assert body[field] == 1

    def test_round_trips_disabled_state(self, client):
        resp = client.post("/api/settings/toggles", json={"import_water_enabled": False})
        assert resp.status_code == 200
        body = client.get("/api/settings/toggles").get_json()
        assert body["import_water_enabled"] == 0
        # Fields omitted from a POST are *not* left at their prior value --
        # this is a full-replace singleton config (same as form-pv), and the
        # checkbox form always submits every field, so a partial POST here
        # correctly resets everything else to disabled too.
        assert body["import_power_enabled"] == 0

    def test_re_enabling_a_toggle(self, client):
        client.post("/api/settings/toggles", json={"homewizard_api_enabled": False})
        client.post("/api/settings/toggles", json={"homewizard_api_enabled": True})
        body = client.get("/api/settings/toggles").get_json()
        assert body["homewizard_api_enabled"] == 1


class TestVisibilityToggles:
    # Separate endpoint/table-columns from TestFeatureToggles above,
    # even though both back onto the same feature_toggles row.
    def test_defaults_all_visible(self, client):
        body = client.get("/api/settings/visibility").get_json()
        assert body == {
            "show_gas_tab": 1,
            "show_water_tab": 1,
            "show_battery_tab": 1,
            "show_sufficiency_tab": 1,
        }

    def test_sufficiency_toggle_round_trips(self, client):
        # Solar/self-sufficiency is the 5th toggleable
        # category, same full-replace singleton mechanism as gas/water/battery.
        resp = client.post("/api/settings/visibility", json={"show_sufficiency_tab": False})
        assert resp.status_code == 200
        body = client.get("/api/settings/visibility").get_json()
        assert body["show_sufficiency_tab"] == 0
        client.post("/api/settings/visibility", json={"show_sufficiency_tab": True})
        body = client.get("/api/settings/visibility").get_json()
        assert body["show_sufficiency_tab"] == 1

    def test_round_trips_disabled_state(self, client):
        resp = client.post("/api/settings/visibility", json={"show_water_tab": False})
        assert resp.status_code == 200
        body = client.get("/api/settings/visibility").get_json()
        assert body["show_water_tab"] == 0
        # Full-replace singleton, same as toggles -- fields omitted from the
        # POST reset to disabled too, not left at their prior value.
        assert body["show_gas_tab"] == 0

    def test_re_enabling_a_toggle(self, client):
        client.post("/api/settings/visibility", json={"show_battery_tab": False})
        client.post("/api/settings/visibility", json={"show_battery_tab": True})
        body = client.get("/api/settings/visibility").get_json()
        assert body["show_battery_tab"] == 1

    def test_independent_of_feature_toggles_form(self, client):
        # The exact risk this split endpoint design was chosen to avoid:
        # saving one form must never reset the *other* form's fields, since
        # both are full-replace singletons sharing one underlying table row.
        # (Each form still resets its *own* omitted fields -- that's the
        # existing, already-tested full-replace behavior, not what this
        # test is about.)
        client.post("/api/settings/toggles", json={"import_water_enabled": False})
        toggles_before = client.get("/api/settings/toggles").get_json()

        client.post("/api/settings/visibility", json={"show_gas_tab": False})

        toggles_after = client.get("/api/settings/toggles").get_json()
        visibility = client.get("/api/settings/visibility").get_json()
        assert toggles_after == toggles_before  # the visibility POST touched nothing here
        assert visibility["show_gas_tab"] == 0
        assert visibility["show_water_tab"] == 0  # full-replace within its own form -- expected


class TestImportGating:
    def test_disabled_category_rejected_with_clear_error(self, client):
        client.post("/api/settings/toggles", json={"import_water_enabled": False})
        content = b"time,water usage dl\n2026-01-01 00:00,100\n"
        data = {"file": (io.BytesIO(content), "Water-2026-1-01-2026-7-14.csv")}
        resp = client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 403
        assert "disabled" in resp.get_json()["error"].lower()

    def test_other_categories_unaffected_by_one_disabled_category(self, client):
        # /api/settings/toggles is a full-replace singleton (see
        # TestFeatureToggles.test_round_trips_disabled_state) -- every other
        # field must be explicitly kept enabled here, or this test would
        # incorrectly disable gas import too and mask the real behavior
        # being checked (found by running this suite: posting only
        # import_water_enabled initially zeroed every other toggle as well).
        client.post(
            "/api/settings/toggles",
            json={
                "homewizard_api_enabled": True,
                "import_power_enabled": True,
                "import_gas_enabled": True,
                "import_water_enabled": False,
                "pdf_import_enabled": True,
                "nightly_backup_enabled": True,
            },
        )
        content = b"time,Total gas used\n2026-01-01 00:00,100.0\n"
        data = {"file": (io.BytesIO(content), "P1g-2026-1-01-2026-7-14.csv")}
        resp = client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["rows_ingested"] == 1

    def test_pdf_import_disabled_rejected(self, client):
        client.post("/api/settings/toggles", json={"pdf_import_enabled": False})
        data = {"file": (io.BytesIO(b"not a real pdf"), "tarieven.pdf")}
        resp = client.post("/api/import/tariff-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 403
        assert "disabled" in resp.get_json()["error"].lower()


class TestTariffPdfImport:
    """The import route now goes through tariff_parser's
    registry (not a single hardcoded parser) and reconciles open-ended rows
    the same way the Settings-UI manual-entry route already does. PDF text
    extraction itself (pdfplumber) isn't exercised here -- parse_tariff_pdf
    is monkeypatched, same as the HomeWizard tests fake the network layer."""

    def _period(self, start, end, rate):
        from src.tariff_parser import RatePeriod

        return RatePeriod(start, end, rate)

    def test_success_reports_which_parser_matched(self, client, monkeypatch):
        from src import tariff_parser

        monkeypatch.setattr(
            tariff_parser,
            "parse_tariff_pdf",
            lambda f: {
                "power": [self._period("2026-01-01", "2026-06-30", 0.25)],
                "gas": [self._period("2026-01-01", "2026-06-30", 1.30)],
                "parser": "Vattenfall Tarievenspecificatie",
            },
        )
        data = {"file": (io.BytesIO(b"fake"), "tarieven.pdf")}
        resp = client.post("/api/import/tariff-pdf", data=data, content_type="multipart/form-data")
        body = resp.get_json()
        assert resp.status_code == 200
        assert body["parser"] == "Vattenfall Tarievenspecificatie"
        assert body["power_periods"] == 1
        assert body["gas_periods"] == 1

    def test_no_recognized_parser_lists_known_formats(self, client, monkeypatch):
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "parse_tariff_pdf", lambda f: {"power": [], "gas": [], "parser": None})
        data = {"file": (io.BytesIO(b"fake"), "unknown.pdf")}
        resp = client.post("/api/import/tariff-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "Vattenfall Tarievenspecificatie" in resp.get_json()["error"]
        assert "CSV template" in resp.get_json()["error"]

    def _seed_open_ended_row(self):
        """An existing open-ended power rate -- the steady state, since 8 of
        the 9 parsers emit OPEN_ENDED_SENTINEL rows."""
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
            "VALUES ('2025-01-01', ?, 24.0, 24.0, 'prior open-ended import')",
            (db.OPEN_ENDED_SENTINEL,),
        )
        conn.commit()
        return conn

    def test_open_ended_row_shrunk_before_pdf_period_inserted(self, client, app, monkeypatch):
        # Seed an open-ended row the same way the Settings-UI route would
        # (period_end = OPEN_ENDED_SENTINEL), then import a PDF period that
        # is ITSELF open-ended and starts after it -- without reconciliation
        # this would always be rejected as an overlap against the sentinel
        # end date (the whole point of open-ended reconciliation, now wired
        # into the PDF path too). A successor rate sheet supersedes its predecessor.
        from src import db, tariff_parser

        conn = self._seed_open_ended_row()

        monkeypatch.setattr(
            tariff_parser,
            "parse_tariff_pdf",
            lambda f: {
                "power": [self._period("2026-06-01", db.OPEN_ENDED_SENTINEL, 0.28)],
                "gas": [],
                "parser": "Vattenfall Tarievenblad",
            },
        )
        data = {"file": (io.BytesIO(b"fake"), "tarieven.pdf")}
        resp = client.post("/api/import/tariff-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["skipped_overlaps"] == 0

        rows = conn.execute("SELECT period_start, period_end FROM rate_schedule ORDER BY period_start").fetchall()
        assert [(r["period_start"], r["period_end"]) for r in rows] == [
            ("2025-01-01", "2026-05-31"),  # shrunk, no longer open-ended
            ("2026-06-01", db.OPEN_ENDED_SENTINEL),  # the new current rate
        ]

    def test_closed_historical_pdf_period_does_not_truncate_the_live_open_row(self, client, app, monkeypatch):
        # A Vattenfall Tarievenspecificatie is a closed
        # historical document by nature. Importing one must NOT shrink the
        # currently-open rate: doing so leaves every date after the closed
        # period's end uncovered, and rate_for() then silently falls back to
        # the old bill's rate marked merely "stale" -- a wrong money figure
        # with no error surfaced. It must be skipped as an overlap instead,
        # which is what the code did before a later regression broke it.
        from src import db, tariff_parser

        conn = self._seed_open_ended_row()

        monkeypatch.setattr(
            tariff_parser,
            "parse_tariff_pdf",
            lambda f: {
                "power": [self._period("2025-03-01", "2025-05-31", 0.24)],
                "gas": [],
                "parser": "Vattenfall Tarievenspecificatie",
            },
        )
        data = {"file": (io.BytesIO(b"fake"), "specificatie.pdf")}
        resp = client.post("/api/import/tariff-pdf", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        # Skipped, and *reported* -- the user is told, not silently ignored.
        assert resp.get_json()["skipped_overlaps"] == 1

        rows = conn.execute("SELECT period_start, period_end FROM rate_schedule ORDER BY period_start").fetchall()
        assert [(r["period_start"], r["period_end"]) for r in rows] == [
            ("2025-01-01", db.OPEN_ENDED_SENTINEL),  # untouched, still open
        ]


class TestTariffCsvImport:
    """The generic fallback for suppliers with no PDF
    parser. Uses real CSV bodies through the actual route (not a
    monkeypatched parser) -- the format itself is the thing under test
    here, unlike TestTariffPdfImport where the PDF extraction step is
    faked."""

    def test_template_download_parses_cleanly_and_is_a_real_attachment(self, client):
        from src.tariff_parser import parse_tariff_csv

        resp = client.get("/api/import/tariff-csv/template")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert "attachment" in resp.headers["Content-Disposition"]
        # The example rows are commented out: downloading the
        # template and uploading it straight back must import NOTHING, so a
        # user who forgets to delete them doesn't silently acquire three
        # fabricated tariffs -- including a live open-ended power rate.
        parsed = parse_tariff_csv(resp.get_data(as_text=True))
        assert parsed["power"] == []
        assert parsed["gas"] == []

    def test_template_download_requires_no_auth(self, app):
        # Deliberately uses the bare (unauthenticated) test client -- a
        # template download writes nothing, so it must work without the
        # write token like every other GET route.
        app.test_client_class = flask.testing.FlaskClient
        plain_client = app.test_client()
        resp = plain_client.get("/api/import/tariff-csv/template")
        assert resp.status_code == 200

    def test_valid_csv_creates_rows(self, client):
        csv_text = "power,2026-01-01,2026-06-30,0.245\ngas,2026-01-01,,1.35\n"
        data = {"file": (io.BytesIO(csv_text.encode()), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        body = resp.get_json()
        assert resp.status_code == 200
        assert body["power_periods"] == 1
        assert body["gas_periods"] == 1

        rates = client.get("/api/settings/rates").get_json()
        assert rates[0]["buy_ct_per_kwh"] == pytest.approx(24.5)
        assert rates[0]["source"] == "Uploaded CSV: rates.csv"

    def test_malformed_csv_reports_the_bad_row_not_a_generic_error(self, client):
        data = {"file": (io.BytesIO(b"power,2026-01-01,,not-a-number\n"), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "line 1" in resp.get_json()["error"]

    def test_empty_file_after_comments_rejected(self, client):
        data = {"file": (io.BytesIO(b"# nothing but a comment\n"), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_csv_import_disabled_rejected(self, client):
        client.post("/api/settings/toggles", json={"pdf_import_enabled": False})
        data = {"file": (io.BytesIO(b"power,2026-01-01,,0.25\n"), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 403
        assert "disabled" in resp.get_json()["error"].lower()

    def test_no_file_provided_rejected(self, client):
        resp = client.post("/api/import/tariff-csv", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_write_token_required(self, app):
        app.test_client_class = flask.testing.FlaskClient
        plain_client = app.test_client()
        data = {"file": (io.BytesIO(b"power,2026-01-01,,0.25\n"), "rates.csv")}
        resp = plain_client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 401

    def _seed_open_ended_row(self):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
            "VALUES ('2025-01-01', ?, 24.0, 24.0, 'prior open-ended import')",
            (db.OPEN_ENDED_SENTINEL,),
        )
        conn.commit()
        return conn

    def test_open_ended_row_shrunk_before_csv_period_inserted(self, client, app):
        # Same reconciliation guarantee TestTariffPdfImport already checks
        # for the PDF path -- both routes now share _apply_rate_periods, but
        # a shared helper is only as good as both callers actually using it.
        # Blank period_end in the CSV == open-ended, so this is a successor.
        from src import db

        conn = self._seed_open_ended_row()

        data = {"file": (io.BytesIO(b"power,2026-06-01,,0.28\n"), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["skipped_overlaps"] == 0

        rows = conn.execute("SELECT period_start, period_end FROM rate_schedule ORDER BY period_start").fetchall()
        assert [(r["period_start"], r["period_end"]) for r in rows] == [
            ("2025-01-01", "2026-05-31"),
            ("2026-06-01", db.OPEN_ENDED_SENTINEL),
        ]

    def test_closed_csv_period_does_not_truncate_the_live_open_row(self, client, app):
        # The same regression, CSV side. The UI text for this form explicitly
        # invites "fill in your rate periods by hand from your bill" -- and a
        # bill is a closed historical period by definition, making this the
        # likeliest way a user hits the bug rather than an exotic edge case.
        from src import db

        conn = self._seed_open_ended_row()

        data = {"file": (io.BytesIO(b"power,2025-03-01,2025-05-31,0.24\n"), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["skipped_overlaps"] == 1

        rows = conn.execute("SELECT period_start, period_end FROM rate_schedule ORDER BY period_start").fetchall()
        assert [(r["period_start"], r["period_end"]) for r in rows] == [
            ("2025-01-01", db.OPEN_ENDED_SENTINEL),
        ]

    def test_closed_gas_period_does_not_truncate_the_live_open_gas_row(self, client, app):
        # The same fix applies to both schedules; the gas branch is a separate
        # call site and was fixed separately, so it needs its own proof.
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO gas_rate_schedule (period_start, period_end, price_eur_per_m3, source) "
            "VALUES ('2025-01-01', ?, 1.30, 'prior open-ended import')",
            (db.OPEN_ENDED_SENTINEL,),
        )
        conn.commit()

        data = {"file": (io.BytesIO(b"gas,2025-03-01,2025-05-31,1.42\n"), "rates.csv")}
        resp = client.post("/api/import/tariff-csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["skipped_overlaps"] == 1

        rows = conn.execute("SELECT period_start, period_end FROM gas_rate_schedule ORDER BY period_start").fetchall()
        assert [(r["period_start"], r["period_end"]) for r in rows] == [
            ("2025-01-01", db.OPEN_ENDED_SENTINEL),
        ]


class TestDbConnectionLifecycle:
    """A code review found init_db() used to run on every request via get_conn(); each
    request also opened its own connection that was never explicitly closed.
    These construct create_app() themselves (rather than using the shared
    `app` fixture) so monkeypatching is in place *before* create_app() runs
    its one startup call -- the thing being proven only happens once."""

    def test_init_db_runs_once_at_startup_not_per_request(self, tmp_path, monkeypatch):
        from src import db as db_module

        monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", TEST_WRITE_TOKEN)

        call_count = {"n": 0}
        real_init_db = db_module.init_db

        def counting_init_db(conn):
            call_count["n"] += 1
            real_init_db(conn)

        monkeypatch.setattr(db_module, "init_db", counting_init_db)

        flask_app = create_app()
        assert call_count["n"] == 1  # the one startup call

        client = flask_app.test_client()
        client.get("/api/overview")
        client.get("/api/power/daily")
        client.get("/api/ingest-status")

        assert call_count["n"] == 1  # still just the startup call -- not one per request

    def test_connection_closed_after_request_teardown(self, tmp_path, monkeypatch):
        import sqlite3

        from src import db as db_module

        monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", TEST_WRITE_TOKEN)

        opened = []
        real_get_connection = db_module.get_connection

        def tracking_get_connection(*args, **kwargs):
            conn = real_get_connection(*args, **kwargs)
            opened.append(conn)
            return conn

        monkeypatch.setattr(db_module, "get_connection", tracking_get_connection)

        flask_app = create_app()
        opened.clear()  # drop the startup connection -- already closed by create_app() itself

        client = flask_app.test_client()
        client.get("/api/overview")

        assert len(opened) == 1  # exactly one connection for the one request, not leaked/duplicated
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")  # closed at teardown, not left for GC


class TestWriteTokenAuth:
    def test_post_without_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient  # bypass the auto-injecting client
        client = app.test_client()
        resp = client.post("/api/settings/pv", json={"kwp_rating": 5.0})
        assert resp.status_code == 401
        assert "error" in resp.get_json()

    def test_post_with_wrong_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.post(
            "/api/settings/pv", json={"kwp_rating": 5.0}, headers={"X-OmniMeter-Write-Api-Token": "wrong-token"}
        )
        assert resp.status_code == 401

    def test_post_with_correct_token_accepted(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.post(
            "/api/settings/pv",
            json={"kwp_rating": 5.0},
            headers={"X-OmniMeter-Write-Api-Token": TEST_WRITE_TOKEN},
        )
        assert resp.status_code == 200

    def test_get_requires_no_token(self, app):
        # Reads were never the problem the write-token check was about -- must stay open.
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.get("/api/overview")
        assert resp.status_code == 200

    def test_import_csv_without_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        data = {"file": (io.BytesIO(b"time,Total gas used\n2026-01-01 00:00,100.0\n"), "P1g-x.csv")}
        resp = client.post("/api/import/csv", data=data, content_type="multipart/form-data")
        assert resp.status_code == 401

    def test_create_app_raises_without_write_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.delenv("OMNIMETER_WRITE_API_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="OMNIMETER_WRITE_API_TOKEN"):
            create_app()

    # The three new write endpoints, same no-token/wrong-token/
    # correct-token coverage as every other write endpoint above.
    _ACK_BODY = {"category": "power", "issue_type": "gap", "fingerprint": "x"}
    _DELETE_BODY = {"time": "2026-01-01 00:00", "granularity": "api_live", "metric": "import_combined_kwh", "value": 1.0}

    def test_acknowledge_post_without_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.post("/api/data-quality/acknowledge", json=self._ACK_BODY)
        assert resp.status_code == 401

    def test_acknowledge_post_with_wrong_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.post(
            "/api/data-quality/acknowledge", json=self._ACK_BODY, headers={"X-OmniMeter-Write-Api-Token": "wrong"}
        )
        assert resp.status_code == 401

    def test_acknowledge_post_with_correct_token_accepted(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.post(
            "/api/data-quality/acknowledge",
            json=self._ACK_BODY,
            headers={"X-OmniMeter-Write-Api-Token": TEST_WRITE_TOKEN},
        )
        assert resp.status_code == 200

    def test_acknowledge_delete_without_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.delete("/api/data-quality/acknowledge", json=self._ACK_BODY)
        assert resp.status_code == 401

    def test_delete_reading_without_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.delete("/api/readings/power", json=self._DELETE_BODY)
        assert resp.status_code == 401

    def test_delete_reading_with_wrong_token_rejected(self, app):
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.delete(
            "/api/readings/power", json=self._DELETE_BODY, headers={"X-OmniMeter-Write-Api-Token": "wrong"}
        )
        assert resp.status_code == 401

    def test_delete_reading_with_correct_token_accepted(self, app):
        # No matching row exists -- correct token still gets past auth to the
        # route's own 404, not blocked at the auth layer.
        app.test_client_class = flask.testing.FlaskClient
        client = app.test_client()
        resp = client.delete(
            "/api/readings/power", json=self._DELETE_BODY, headers={"X-OmniMeter-Write-Api-Token": TEST_WRITE_TOKEN}
        )
        assert resp.status_code == 404


class TestEnergyFlow:
    def test_happy_path_with_seeded_data(self, client):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-05-01', 8.0, 3.0, 5.0)"
        )
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh) VALUES ('2026-05-01', 2.0, 1.5)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/energy-flow?from=2026-05-01&to=2026-05-01")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["pv_configured"] is True
        assert body["sources"]["grid_in"] == 8.0
        assert body["uses"]["grid_out"] == 3.0
        assert body["uses"]["battery_charge"] == 2.0
        assert body["sources"]["battery_discharge"] == 1.5
        assert body["sources"]["solar"] >= 0.0
        assert isinstance(body["flows"], list)
        assert "unbalanced_kwh" in body

    def test_no_pv_configured(self, client):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-05-01', 8.0, 3.0, 5.0)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/energy-flow?from=2026-05-01&to=2026-05-01")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["pv_configured"] is False
        assert body["sources"]["solar"] == 0.0

    def test_empty_range_returns_all_zero_no_error(self, client):
        resp = client.get("/api/energy-flow?from=2026-05-01&to=2026-05-01")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["sources"]["grid_in"] == 0.0
        assert body["uses"]["load"] == 0.0
        assert body["flows"] == []

    def test_response_shape_matches_frontend_expectations(self, client):
        resp = client.get("/api/energy-flow")
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body["sources"].keys()) == {"solar", "battery_discharge", "grid_in"}
        assert set(body["uses"].keys()) == {"load", "battery_charge", "grid_out"}
        assert "flows" in body and "unbalanced_kwh" in body and "pv_configured" in body

    def test_self_sufficiency_unaffected_by_the_shared_helper_refactor(self, client):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("INSERT INTO pv_config (id, kwp_rating) VALUES (1, 2.5)")
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-05-01', 8.0, 3.0, 5.0)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/self-sufficiency?from=2026-05-01&to=2026-05-01")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is True
        assert len(body["days"]) == 1
        assert body["days"][0]["date"] == "2026-05-01"
        assert body["days"][0]["basis"] in ("weather", "seasonal")

    def test_per_day_allocation_not_period_totals(self, client):
        # No PV needed to reproduce this -- battery + grid alone show it.
        # Day 1: battery discharge (15) covers that day's own load (10) and
        # still has 5 left to export -- a real, internally-balanced day.
        # Day 2: grid import (10) covers that day's load (10) exactly, no
        # export. Computed on the 2-day TOTALS in one pass (discharge=15,
        # import=10, export=5, load=20), grid import would fully cover load
        # first and have nothing left except to force the remaining 5 kWh
        # of real export through the grid-in-to-grid-out fallback -- exactly
        # the bug found against real production data. Per-day computation must
        # attribute the export to battery_discharge (where it actually
        # happened) instead.
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES "
            "('2026-05-01', 0.0, 5.0, -5.0), ('2026-05-02', 10.0, 0.0, 10.0)"
        )
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh) VALUES "
            "('2026-05-01', 0.0, 15.0), ('2026-05-02', 0.0, 0.0)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/energy-flow?from=2026-05-01&to=2026-05-02")
        assert resp.status_code == 200
        body = resp.get_json()
        flows = body["flows"]
        assert any(
            f["from"] == "battery_discharge" and f["to"] == "grid_out" and f["kwh"] == pytest.approx(5.0)
            for f in flows
        )
        assert not any(f["from"] == "grid_in" and f["to"] == "grid_out" for f in flows)

    def test_fallback_kwh_reaches_the_response(self, client):
        from src import db

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2026-05-01', 19.7, 16.4, 3.3)"
        )
        conn.execute(
            "INSERT INTO battery_daily (date, charge_kwh, discharge_kwh) VALUES ('2026-05-01', 23.4, 26.1)"
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/energy-flow?from=2026-05-01&to=2026-05-01")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "fallback_kwh" in body
        assert body["fallback_kwh"] > 0


class TestVersion:
    def test_index_shows_version_in_footer(self, client):
        from src.__version__ import __version__

        resp = client.get("/")
        assert resp.status_code == 200
        assert f"OmniMeter v{__version__}".encode() in resp.data

    def test_api_version_returns_current_version(self, client):
        from src.__version__ import __version__

        resp = client.get("/api/version")
        assert resp.status_code == 200
        assert resp.get_json() == {"version": __version__}

    def test_version_constant_is_semver(self):
        import re

        from src.__version__ import __version__

        assert re.match(r"^\d+\.\d+\.\d+$", __version__), __version__


class TestMeterCsvTemplate:
    """Vendor-neutral meter-CSV template download (GET, unauthenticated --
    it writes nothing, same as the tariff template)."""

    def test_default_category_is_power(self, client):
        resp = client.get("/api/import/meter-csv/template")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert body.startswith("time,import_t1_kwh")
        assert "import_combined_kwh" in body

    def test_each_category_lists_its_own_columns(self, client):
        from src import ingest

        for category, columns in ingest.GENERIC_COLUMNS.items():
            resp = client.get(f"/api/import/meter-csv/template?category={category}")
            assert resp.status_code == 200
            header = resp.get_data(as_text=True).splitlines()[0]
            assert header == "time," + ",".join(columns)

    def test_unknown_category_rejected(self, client):
        resp = client.get("/api/import/meter-csv/template?category=nonsense")
        assert resp.status_code == 400

    def test_template_round_trips_as_a_valid_empty_import(self, client, tmp_path):
        # The unmodified template must be safe to upload: every example row
        # is commented out, so it imports nothing rather than erroring.
        from src import ingest

        body = client.get("/api/import/meter-csv/template?category=gas").get_data(as_text=True)
        p = tmp_path / "omnimeter-gas-template.csv"
        p.write_text(body, encoding="utf-8")
        rows = ingest.read_csv_rows(p)
        # Only comment rows survive; none carry a usable time value.
        assert all(r["time"].startswith("#") for r in rows)

    def test_upload_error_names_both_supported_formats(self, client):
        import io

        resp = client.post(
            "/api/import/csv",
            data={"file": (io.BytesIO(b"time,x\n"), "some-random-name.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        error = resp.get_json()["error"]
        assert "omnimeter-" in error  # generic format offered
        assert "HomeWizard" in error  # legacy format still named
