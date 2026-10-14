"""Quarters comparison (Comparison tab -> Quarters sub-tab).

All dates are fictitious (1998-2001) and free text is placeholder, per the
project's test-data rules. Module functions are called with an explicit
``today`` so "in progress" is deterministic; the route tests only use
quarters long past, so the real clock cannot change their result.
"""

from datetime import date, timedelta

import pytest

from src import db, quarters
from src.app import create_app

AFTER_ALL = date(2001, 6, 1)  # a "today" after every fixture quarter has ended


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", "test-write-token")  # app refuses to start without one
    monkeypatch.setattr("src.app.DEFAULT_IMPORTS_DIR", tmp_path / "imports")
    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def conn(app):
    c = db.get_connection()
    db.init_db(c)
    yield c
    c.close()


def _days(first: str, last: str):
    d = date.fromisoformat(first)
    end = date.fromisoformat(last)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def _fill_power(conn, first, last, import_kwh=10.0, export_kwh=2.0):
    for d in _days(first, last):
        conn.execute(
            "INSERT OR REPLACE INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES (?, ?, ?, ?)",
            (d, import_kwh, export_kwh, import_kwh - export_kwh),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Quarter arithmetic
# --------------------------------------------------------------------------

class TestQuarterMaths:
    def test_bounds_are_calendar_quarters(self):
        assert quarters.quarter_bounds(1998, 1) == ("1998-01-01", "1998-03-31")
        assert quarters.quarter_bounds(1998, 2) == ("1998-04-01", "1998-06-30")
        assert quarters.quarter_bounds(1998, 3) == ("1998-07-01", "1998-09-30")
        assert quarters.quarter_bounds(1998, 4) == ("1998-10-01", "1998-12-31")

    def test_leap_year_lengthens_q1(self):
        assert quarters._days_between(*quarters.quarter_bounds(1999, 1)) == 90
        assert quarters._days_between(*quarters.quarter_bounds(2000, 1)) == 91  # 2000 is a leap year

    def test_parse_valid_and_invalid(self):
        assert quarters.parse_quarter("1998-Q3") == (1998, 3)
        for bad in ("1998-Q5", "1998-Q0", "98-Q1", "1998Q1", "1998-q1", "", None):
            with pytest.raises(ValueError):
                quarters.parse_quarter(bad)

    def test_quarter_of(self):
        assert quarters.quarter_of(date(1998, 1, 1)) == (1998, 1)
        assert quarters.quarter_of(date(1998, 6, 30)) == (1998, 2)
        assert quarters.quarter_of(date(1998, 12, 31)) == (1998, 4)


# --------------------------------------------------------------------------
# Totals, per-day figure and deltas
# --------------------------------------------------------------------------

class TestCompareTotals:
    def test_two_quarters_in_different_years_with_delta(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31", import_kwh=10.0)  # 90 days -> 900
        _fill_power(conn, "1999-01-01", "1999-03-31", import_kwh=15.0)  # 90 days -> 1350
        res = quarters.compare(conn, "1998-Q1", "1999-Q1", AFTER_ALL)
        row = next(r for r in res["rows"] if r["key"] == "power_import_kwh")
        assert row["a"]["value"] == pytest.approx(900.0)
        assert row["b"]["value"] == pytest.approx(1350.0)
        assert row["delta"] == pytest.approx(450.0)
        assert row["delta_pct"] == pytest.approx(50.0)
        assert row["a"]["complete"] and row["b"]["complete"]

    def test_per_day_uses_days_that_have_data(self, conn):
        _fill_power(conn, "1998-01-10", "1998-01-11", import_kwh=15.0)  # 2 days, 30 total
        res = quarters.compare(conn, "1998-Q1", "1998-Q2", AFTER_ALL)
        a = next(r for r in res["rows"] if r["key"] == "power_import_kwh")["a"]
        assert a["value"] == pytest.approx(30.0)
        assert a["days_with_data"] == 2
        assert a["per_day"] == pytest.approx(15.0)  # not 30 / 90

    def test_zero_baseline_has_no_percentage(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31", import_kwh=0.0)
        _fill_power(conn, "1999-01-01", "1999-03-31", import_kwh=5.0)
        row = next(r for r in quarters.compare(conn, "1998-Q1", "1999-Q1", AFTER_ALL)["rows"]
                   if r["key"] == "power_import_kwh")
        assert row["delta"] == pytest.approx(450.0)
        assert row["delta_pct"] is None

    def test_metric_with_no_data_is_none_not_zero(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31")
        res = quarters.compare(conn, "1998-Q1", "1998-Q2", AFTER_ALL)
        row = next(r for r in res["rows"] if r["key"] == "power_import_kwh")
        assert row["b"]["value"] is None
        assert "no data for this quarter" in row["b"]["issues"]
        assert row["delta"] is None and row["delta_pct"] is None


# --------------------------------------------------------------------------
# The "incomplete" tag
# --------------------------------------------------------------------------

class TestCompleteness:
    def test_full_quarter_is_complete(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31")
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["power_import_kwh"]
        assert m["complete"] is True
        assert m["issues"] == []

    def test_gaps_name_how_many_days_are_missing(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-20")  # 79 of 90 days
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["power_import_kwh"]
        assert m["complete"] is False
        assert "11 of 90 days have no data" in m["issues"]

    def test_in_progress_quarter_is_flagged_once_not_as_missing_days(self, conn):
        # "today" is 10 Feb: days up to and including 9 Feb (40 days) have happened.
        _fill_power(conn, "1998-01-01", "1998-02-09")
        q = quarters.quarter_metrics(conn, 1998, 1, date(1998, 2, 10))
        m = q["metrics"]["power_import_kwh"]
        assert q["in_progress"] is True and q["elapsed_days"] == 40
        assert m["issues"] == ["quarter still in progress (40 of 90 days elapsed)"]
        assert m["complete"] is False

    def test_in_progress_quarter_with_a_gap_reports_both(self, conn):
        _fill_power(conn, "1998-01-01", "1998-02-04")  # 35 of the 40 elapsed days
        m = quarters.quarter_metrics(conn, 1998, 1, date(1998, 2, 10))["metrics"]["power_import_kwh"]
        assert "5 of 40 days have no data" in m["issues"]
        assert any("in progress" in i for i in m["issues"])

    def test_incomplete_list_names_the_parameters(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31")
        q = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)
        label = {k: quarters._META[k]["label"] for k in ("power_import_kwh", "gas_m3", "water_l")}
        assert label["power_import_kwh"] not in q["incomplete"]
        assert label["gas_m3"] in q["incomplete"]  # no gas rows at all
        assert label["water_l"] in q["incomplete"]


# --------------------------------------------------------------------------
# Solar, cost, weather
# --------------------------------------------------------------------------

class TestSolar:
    def test_solar_unavailable_when_not_set_up(self, conn):
        conn.execute("DROP TABLE IF EXISTS solaredge_daily")
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["solar_kwh"]
        assert m["available"] is False
        assert m["value"] is None
        assert m["complete"] is False
        assert "not set up" in m["issues"][0]

    def test_solar_sums_to_kwh(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS solaredge_daily "
            "(date TEXT PRIMARY KEY, production_wh REAL NOT NULL, fetched_at TEXT NOT NULL)"
        )
        for d in _days("1998-07-01", "1998-07-02"):
            conn.execute("INSERT OR REPLACE INTO solaredge_daily VALUES (?, ?, ?)", (d, 12500.0, "1998-07-03"))
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 3, AFTER_ALL)["metrics"]["solar_kwh"]
        assert m["available"] is True
        assert m["value"] == pytest.approx(25.0)
        assert m["days_with_data"] == 2


class TestCost:
    def _rates(self, conn, start="1998-01-01", end="1998-12-31"):
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
            "VALUES (?, ?, 20.0, 10.0, 'Test Data')",
            (start, end),
        )
        conn.execute(
            "INSERT INTO gas_rate_schedule (period_start, period_end, price_eur_per_m3, source) "
            "VALUES (?, ?, 1.5, 'Test Data')",
            (start, end),
        )
        conn.commit()

    def test_cost_unavailable_without_a_rate_schedule(self, conn):
        _fill_power(conn, "1998-01-01", "1998-01-02")
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["cost_total_eur"]
        assert m["available"] is False
        assert "no rate schedule" in m["issues"][0]

    def test_cost_matches_rates(self, conn):
        self._rates(conn)
        _fill_power(conn, "1998-01-01", "1998-01-02", import_kwh=10.0, export_kwh=4.0)
        for d in _days("1998-01-01", "1998-01-02"):
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, 2.0)", (d,))
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]
        # per day power: 10*0.20 - 4*0.10 = 1.60 ; gas: 2*1.5 = 3.00
        assert m["cost_power_eur"]["value"] == pytest.approx(3.20)
        assert m["cost_gas_eur"]["value"] == pytest.approx(6.00)
        assert m["cost_total_eur"]["value"] == pytest.approx(9.20)

    def test_expired_rate_is_tagged_incomplete(self, conn):
        self._rates(conn, start="1998-01-01", end="1998-01-31")  # rates stop after January
        _fill_power(conn, "1998-01-30", "1998-02-02")  # 2 days inside, 2 days after the rates end
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["cost_power_eur"]
        assert any("2 days priced with an expired rate" in i for i in m["issues"])

    def test_quarter_cost_equals_costs_tab_total(self, client, conn):
        # One source of truth: the Quarters total must equal what /api/costs shows.
        self._rates(conn)
        _fill_power(conn, "1998-01-01", "1998-03-31", import_kwh=7.0, export_kwh=1.0)
        for d in _days("1998-01-01", "1998-03-31"):
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, 3.0)", (d,))
        conn.commit()
        body = client.get("/api/costs?from=1998-01-01&to=1998-03-31").get_json()
        tab_total = sum(d["total_cost_eur"] for d in body["days"])
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["cost_total_eur"]
        assert m["value"] == pytest.approx(tab_total)


class TestWeather:
    def _weather(self, conn, d, mean):
        conn.execute(
            "INSERT OR REPLACE INTO weather_daily "
            "(date, temperature_2m_mean, latitude, longitude, source, fetched_at) VALUES (?, ?, 0.0, 0.0, 'Test Data', ?)",
            (d, mean, d),
        )

    def test_heating_degree_days_use_18_degree_base(self, conn):
        self._weather(conn, "1998-01-01", 8.0)   # 10 HDD
        self._weather(conn, "1998-01-02", 20.0)  # warmer than base -> 0
        self._weather(conn, "1998-01-03", 0.0)   # 18 HDD
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]
        assert m["hdd"]["value"] == pytest.approx(28.0)
        assert m["avg_temp_c"]["value"] == pytest.approx((8.0 + 20.0 + 0.0) / 3)

    def test_gas_per_hdd_uses_only_days_with_both(self, conn):
        self._weather(conn, "1998-01-01", 8.0)   # 10 HDD
        self._weather(conn, "1998-01-02", 8.0)   # 10 HDD, but no gas this day
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('1998-01-01', 20.0)")
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('1998-01-05', 99.0)")  # no weather
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["gas_per_hdd"]
        assert m["value"] == pytest.approx(2.0)  # 20 m3 / 10 HDD, ignoring the unmatched days
        assert m["days_with_data"] == 1

    def test_gas_per_hdd_undefined_without_heating_days(self, conn):
        self._weather(conn, "1998-07-01", 25.0)
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('1998-07-01', 1.0)")
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 3, AFTER_ALL)["metrics"]["gas_per_hdd"]
        assert m["value"] is None


class TestOccupancy:
    def test_headcount_average_and_unlogged_days_excluded(self, conn):
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count, notes) "
            "VALUES ('1998-01-01 00:00', '1998-01-02 23:59', 2, 'Test Data')"
        )
        conn.commit()
        m = quarters.quarter_metrics(conn, 1998, 1, AFTER_ALL)["metrics"]["avg_headcount"]
        assert m["value"] == pytest.approx(2.0)
        assert m["days_with_data"] == 2
        assert "88 of 90 days have no data" in m["issues"]


# --------------------------------------------------------------------------
# Available quarters and the quarter-across-years series
# --------------------------------------------------------------------------

class TestAvailableAndSeries:
    def test_available_spans_first_data_to_last_newest_first(self, conn):
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('1998-02-01', 1.0)")
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('1999-08-01', 1.0)")
        conn.commit()
        res = quarters.available_quarters(conn, AFTER_ALL)
        ids = [q["quarter"] for q in res["quarters"]]
        assert ids[0] == "1999-Q3" and ids[-1] == "1998-Q1"
        assert len(ids) == 7  # 1998 Q1-Q4 + 1999 Q1-Q3
        assert res["years"] == [1999, 1998]

    def test_available_empty_database(self, conn):
        assert quarters.available_quarters(conn, AFTER_ALL) == {"quarters": [], "years": []}

    def test_series_one_quarter_across_years_with_yoy_delta(self, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31", import_kwh=10.0)
        _fill_power(conn, "1999-01-01", "1999-03-31", import_kwh=12.0)
        _fill_power(conn, "2000-01-01", "2000-03-31", import_kwh=9.0)
        res = quarters.series(conn, 1, AFTER_ALL)
        assert [y["year"] for y in res["years"]] == [1998, 1999, 2000]
        row = next(r for r in res["rows"] if r["key"] == "power_import_kwh")
        cells = row["years"]
        assert cells[0]["delta_vs_prev"] is None  # nothing to compare the first year against
        assert cells[1]["delta_vs_prev"] == pytest.approx(180.0)  # 1080 - 900
        assert cells[1]["delta_pct_vs_prev"] == pytest.approx(20.0)
        assert cells[2]["value"] == pytest.approx(91 * 9.0)  # 2000 is a leap year: 91 days -> 819
        assert cells[2]["delta_vs_prev"] == pytest.approx(819.0 - 1080.0)
        assert cells[2]["delta_pct_vs_prev"] == pytest.approx((819.0 - 1080.0) / 1080.0 * 100.0)
        # Quarters differ in length, so the per-day change is the fairer figure:
        assert cells[2]["delta_per_day_vs_prev"] == pytest.approx(9.0 - 12.0)

    def test_series_rejects_bad_quarter(self, conn):
        with pytest.raises(ValueError):
            quarters.series(conn, 5, AFTER_ALL)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

class TestRoutes:
    def test_compare_requires_valid_quarters(self, client):
        assert client.get("/api/quarters/compare").status_code == 400
        assert client.get("/api/quarters/compare?a=1998-Q1").status_code == 400
        assert client.get("/api/quarters/compare?a=1998-Q1&b=1998-Q9").status_code == 400

    def test_compare_returns_rows_and_both_quarters(self, client, conn):
        _fill_power(conn, "1998-01-01", "1998-03-31", import_kwh=10.0)
        _fill_power(conn, "1999-01-01", "1999-03-31", import_kwh=12.0)
        body = client.get("/api/quarters/compare?a=1998-Q1&b=1999-Q1").get_json()
        assert body["a"]["quarter"] == "1998-Q1" and body["b"]["quarter"] == "1999-Q1"
        keys = [r["key"] for r in body["rows"]]
        for expected in ("power_import_kwh", "gas_m3", "water_l", "solar_kwh", "cost_total_eur", "hdd"):
            assert expected in keys
        row = next(r for r in body["rows"] if r["key"] == "power_import_kwh")
        assert row["delta"] == pytest.approx(180.0)

    def test_series_requires_valid_q(self, client):
        assert client.get("/api/quarters/series").status_code == 400
        assert client.get("/api/quarters/series?q=7").status_code == 400
        assert client.get("/api/quarters/series?q=abc").status_code == 400

    def test_available_route(self, client, conn):
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('1998-02-01', 1.0)")
        conn.commit()
        body = client.get("/api/quarters/available").get_json()
        assert body["quarters"][-1]["quarter"] == "1998-Q1"


class TestCostsEndpointShapeUnchanged:
    def test_public_day_shape_has_no_internal_fields(self, client, conn):
        conn.execute(
            "INSERT INTO rate_schedule (period_start, period_end, buy_ct_per_kwh, sell_ct_per_kwh, source) "
            "VALUES ('1998-01-01', '1998-12-31', 20.0, 10.0, 'Test Data')"
        )
        conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh) VALUES ('1998-05-01', 10.0, 2.0)")
        conn.commit()
        body = client.get("/api/costs?from=1998-05-01&to=1998-05-01").get_json()
        assert set(body["days"][0]) == {"date", "power_cost_eur", "gas_cost_eur", "total_cost_eur", "stale"}
        assert body["days"][0]["power_cost_eur"] == pytest.approx(1.8)
