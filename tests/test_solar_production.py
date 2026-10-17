"""Measured production lookup and the battery-aware self-sufficiency. Pure logic: no Flask, 1990s dates."""

import sqlite3

import pytest

from src import db, solar_production
from src.solar_estimate import estimate_self_sufficiency


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


class TestMeasuredByDate:
    def test_finished_days_come_from_the_daily_table_in_kwh(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-01', 8500.0, 'x')")
        conn.commit()
        got = solar_production.measured_by_date(conn, "1998-07-01", "1998-07-31", "1998-07-28")
        assert got == {"1998-07-01": {"kwh": 8.5, "partial": False}}

    def test_today_is_the_latest_snapshot_and_flagged_partial(self, conn):
        conn.executemany(
            "INSERT INTO solaredge_overview (time, day, production_wh) VALUES (?,?,?)",
            [("1998-07-28T08:00:00Z", "1998-07-28", 1000.0), ("1998-07-28T10:00:00Z", "1998-07-28", 4000.0)],
        )
        conn.commit()
        got = solar_production.measured_by_date(conn, "1998-07-01", "1998-07-31", "1998-07-28")
        assert got["1998-07-28"] == {"kwh": 4.0, "partial": True}

    def test_a_stored_final_total_beats_a_snapshot_for_the_same_day(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1998-07-28', 9000.0, 'x')")
        conn.execute("INSERT INTO solaredge_overview (time, day, production_wh) VALUES ('1998-07-28T10:00:00Z', '1998-07-28', 4000.0)")
        conn.commit()
        assert solar_production.measured_by_date(conn, "1998-07-28", "1998-07-28", "1998-07-28")["1998-07-28"]["partial"] is False

    def test_out_of_range_rows_are_ignored(self, conn):
        conn.execute("INSERT INTO solaredge_daily VALUES ('1997-01-01', 1.0, 'x')")
        conn.commit()
        assert solar_production.measured_by_date(conn, "1998-07-01", "1998-07-31", "1998-07-28") == {}

    def test_a_plain_install_without_the_solaredge_tables_is_just_empty(self, tmp_path):
        c = sqlite3.connect(tmp_path / "plain.db")
        c.row_factory = sqlite3.Row
        assert solar_production.measured_by_date(c, "1998-07-01", "1998-07-31", "1998-07-28") == {}


def test_production_source_labels():
    m, e = {"basis": "measured"}, {"basis": "seasonal"}
    assert solar_production.production_source([m, m]) == "measured"
    assert solar_production.production_source([m, e]) == "mixed"
    assert solar_production.production_source([e, {"basis": "weather"}]) == "estimated"
    assert solar_production.production_source([]) == "estimated"


class TestSelfSufficiency:
    """self_sufficiency(production, export, import, charge, discharge) -- all kWh for one day."""

    def test_without_a_battery_it_equals_the_old_formula(self):
        # prod 10, export 4, import 6 -> kept 6, load 12; old: 1 - 6/(6+10-4) = 50%
        assert solar_production.self_sufficiency(10, 4, 6, 0, 0) == pytest.approx(0.5)
        assert solar_production.self_sufficiency(10, 4, 6, 0, 0) == pytest.approx(estimate_self_sufficiency(10, 4, 6))

    def test_all_solar_is_fully_self_sufficient_and_all_grid_is_zero(self):
        assert solar_production.self_sufficiency(8, 0, 0, 0, 0) == pytest.approx(1.0)
        assert solar_production.self_sufficiency(0, 0, 8, 0, 0) == pytest.approx(0.0)

    def test_solar_charged_battery_that_discharges_in_the_evening_counts_as_self_supplied(self):
        # kept 10 (4 stored), import 2, discharge 4 -> load 12, solar-origin 6 + 4 = 10
        assert solar_production.self_sufficiency(10, 0, 2, 4, 4) == pytest.approx(10 / 12)

    def test_grid_charged_battery_energy_is_not_counted_as_self_supplied(self):
        # no solar; 6 imported, 4 of it stored then discharged -> load 6, all of it grid energy
        assert solar_production.self_sufficiency(0, 0, 6, 4, 4) == pytest.approx(0.0)

    def test_energy_stored_for_later_is_not_consumption_today(self):
        # prod 10 kept, import 5, 4 stored (solar-first), nothing discharged: load 11, solar-origin 6.
        new = solar_production.self_sufficiency(10, 0, 5, 4, 0)
        old = estimate_self_sufficiency(10, 0, 5)  # the old formula counted the stored 4 as consumed
        assert new == pytest.approx(6 / 11)
        assert old == pytest.approx(10 / 15)
        assert new < old

    def test_charge_beyond_the_solar_surplus_is_grid_charged(self):
        # kept 2, import 6, charge 5 -> 2 from solar, 3 from grid; discharge 5 -> half... load 2+6+5-5 = 8
        # solar-origin load = (2-2) + 5*(2/5) = 2 -> 25%
        assert solar_production.self_sufficiency(2, 0, 6, 5, 5) == pytest.approx(2 / 8)

    def test_draining_earlier_stock_uses_todays_mix(self):
        # no charge today, discharge 3, kept 6, import 2 -> mix 0.75 solar
        assert solar_production.self_sufficiency(6, 0, 2, 0, 3) == pytest.approx(0.75)

    def test_a_day_with_no_load_has_no_value(self):
        assert solar_production.self_sufficiency(0, 0, 0, 0, 0) is None
        assert solar_production.self_sufficiency(5, 9, 0, 0, 0) is None  # export beyond production -> nothing kept

    def test_result_is_always_within_zero_and_one(self):
        for args in [(3, 0, 7, 1, 5), (12, 0, 0, 9, 0), (0, 0, 1, 0, 9), (10, 10, 4, 4, 0)]:
            v = solar_production.self_sufficiency(*args)
            assert v is None or 0.0 <= v <= 1.0


class TestPeriodStats:
    def days(self, *vals, partial_last=False):
        out = [{"date": f"1998-07-{i + 1:02d}", "kwh": v, "partial": False} for i, v in enumerate(vals)]
        if partial_last:
            out[-1]["partial"] = True
        return out

    def test_totals_average_best_and_worst(self):
        s = solar_production.period_stats(self.days(10.0, 4.0, 7.0), 2.5, {})
        assert s["total_kwh"] == 21.0 and s["avg_kwh_per_day"] == 7.0
        assert s["best"] == {"date": "1998-07-01", "kwh": 10.0} and s["worst"] == {"date": "1998-07-02", "kwh": 4.0}
        assert s["specific_yield_kwh_per_kwp"] == 8.4

    def test_a_partial_day_counts_toward_the_total_but_not_the_averages_or_extremes(self):
        s = solar_production.period_stats(self.days(10.0, 6.0, 1.0, partial_last=True), None, {})
        assert s["total_kwh"] == 17.0 and s["days"] == 3 and s["finished_days"] == 2
        assert s["avg_kwh_per_day"] == 8.0 and s["worst"]["kwh"] == 6.0  # the 1.0 so-far day is not "the weakest day"
        assert s["specific_yield_kwh_per_kwp"] is None

    def test_home_use_vs_export_uses_only_days_that_have_grid_data(self):
        days = self.days(10.0, 10.0, 10.0)
        s = solar_production.period_stats(days, None, {"1998-07-01": 4.0, "1998-07-02": 6.0})  # day 3 has no grid row
        assert s["split_days"] == 2 and s["export_pct"] == 50.0 and s["home_use_pct"] == 50.0

    def test_export_cannot_exceed_production(self):
        s = solar_production.period_stats(self.days(5.0), None, {"1998-07-01": 9.0})
        assert s["export_pct"] == 100.0 and s["home_use_pct"] == 0.0

    def test_no_days_is_all_none(self):
        s = solar_production.period_stats([], 2.5, {})
        assert s["total_kwh"] == 0 and s["avg_kwh_per_day"] is None and s["best"] is None and s["export_pct"] is None


class TestMonthlyHistory:
    def test_groups_by_month_and_year_with_day_counts(self, conn):
        conn.executemany(
            "INSERT INTO solaredge_daily VALUES (?, ?, 'x')",
            [("1997-06-01", 5000.0), ("1997-06-02", 7000.0), ("1998-06-01", 9000.0), ("1998-07-01", 1000.0)],
        )
        conn.commit()
        h = solar_production.monthly_history(conn)
        assert h["available"] is True
        assert h["years"]["1997"][5] == 12.0 and h["years"]["1998"][5] == 9.0 and h["years"]["1998"][6] == 1.0
        assert h["years"]["1997"][0] is None  # no data for January
        assert {"year": 1997, "kwh": 12.0, "days": 2} in h["annual"]
        assert {"month": "1997-06", "kwh": 12.0, "days": 2} in h["months"]

    def test_no_history_is_unavailable(self, conn):
        assert solar_production.monthly_history(conn) == {"available": False}

    def test_a_plain_install_without_the_table_is_unavailable(self, tmp_path):
        c = sqlite3.connect(tmp_path / "plain.db")
        c.row_factory = sqlite3.Row
        assert solar_production.monthly_history(c) == {"available": False}


class TestAnnualInsights:
    def annual(self, **kwh_by_year):
        return [{"year": int(y[1:]), "kwh": k, "days": 365} for y, k in kwh_by_year.items()]

    def weather(self, **irr_by_year):
        # irradiation kWh/m2 per year, full coverage
        return {int(y[1:]): {"days": 365, "irradiation_kwh_m2": v} for y, v in irr_by_year.items()}

    def test_yield_vs_average_and_best_and_lowest_flags(self):
        rows = solar_production.annual_insights(self.annual(y1997=2500.0, y1998=2250.0, y1999=2750.0), {}, 2.5, 2000)
        by = {r["year"]: r for r in rows}
        assert by[1997]["yield_kwh_per_kwp"] == 1000.0 and by[1998]["yield_kwh_per_kwp"] == 900.0
        assert by[1999]["best"] is True and by[1998]["lowest"] is True and by[1997]["best"] is False
        assert by[1999]["yield_vs_avg_pct"] == pytest.approx(10.0, abs=0.1)  # 1100 vs mean 1000
        assert by[1998]["yield_vs_avg_pct"] == pytest.approx(-10.0, abs=0.1)

    def test_incomplete_and_current_years_are_not_compared(self):
        annual = [
            {"year": 1996, "kwh": 2000.0, "days": 315},  # installed part-way through
            {"year": 1997, "kwh": 2500.0, "days": 365},
            {"year": 1998, "kwh": 2250.0, "days": 366},
            {"year": 2000, "kwh": 1500.0, "days": 200},  # current year, still running
        ]
        by = {r["year"]: r for r in solar_production.annual_insights(annual, {}, 2.5, 2000)}
        assert by[1996]["complete"] is False and by[1996]["yield_kwh_per_kwp"] is None and by[1996]["best"] is False
        assert by[2000]["complete"] is False and by[2000]["yield_vs_avg_pct"] is None
        assert by[1997]["best"] is True and by[1998]["lowest"] is True

    def test_sun_adjusted_index_discounts_a_year_that_simply_had_more_sun(self):
        # 1997 yields more, but also got much more sun: per unit of sun 1998 did better.
        rows = solar_production.annual_insights(
            self.annual(y1997=2500.0, y1998=2250.0), self.weather(y1997=1250.0, y1998=1000.0), 2.5, 2000
        )
        by = {r["year"]: r for r in rows}
        # per-sun: 1997 = 1000/1250 = 0.8 ; 1998 = 900/1000 = 0.9 ; mean 0.85
        assert by[1997]["sun_adjusted_index"] == pytest.approx(0.8 / 0.85 * 100, abs=1)
        assert by[1998]["sun_adjusted_index"] == pytest.approx(0.9 / 0.85 * 100, abs=1)
        assert by[1997]["best"] is True and by[1998]["sun_adjusted_index"] > by[1997]["sun_adjusted_index"]

    def test_sun_figures_need_weather_covering_nearly_the_whole_year(self):
        weather = {1997: {"days": 200, "irradiation_kwh_m2": 500.0}}
        rows = solar_production.annual_insights(self.annual(y1997=2500.0, y1998=2250.0), weather, 2.5, 2000)
        assert rows[0]["irradiation_kwh_m2"] is None and rows[0]["sun_adjusted_index"] is None

    def test_sun_energy_and_how_it_compares_with_the_average_year(self):
        rows = solar_production.annual_insights(
            self.annual(y1997=2500.0, y1998=2250.0), self.weather(y1997=1100.0, y1998=900.0), 2.5, 2000
        )
        by = {r["year"]: r for r in rows}
        assert by[1997]["irradiation_kwh_m2"] == 1100.0
        assert by[1997]["sun_vs_avg_pct"] == pytest.approx(10.0, abs=0.1)  # 1100 vs mean 1000
        assert by[1998]["sun_vs_avg_pct"] == pytest.approx(-10.0, abs=0.1)

    def test_no_kwp_means_no_yield_or_flags(self):
        rows = solar_production.annual_insights(self.annual(y1997=2500.0, y1998=2250.0), {}, None, 2000)
        assert all(r["yield_kwh_per_kwp"] is None and not r["best"] and not r["complete"] for r in rows)


class TestYearlyWeather:
    def test_sums_days_and_converts_radiation_to_kwh_per_m2(self, conn):
        conn.executemany(
            "INSERT INTO weather_daily (date, shortwave_radiation_sum, sunshine_duration_s, latitude, longitude, source, fetched_at) "
            "VALUES (?, ?, ?, 0, 0, 'archive', 'x')",
            [("1997-06-01", 18.0, 36000.0), ("1997-06-02", 7.2, 7200.0), ("1998-01-01", None, 3600.0)],
        )
        conn.commit()
        w = solar_production.yearly_weather(conn)
        assert w[1997]["days"] == 2
        assert w[1997]["irradiation_kwh_m2"] == pytest.approx(7.0)  # (18 + 7.2) / 3.6
        assert 1998 not in w  # a day without a radiation value is not counted

    def test_missing_table_is_empty(self, tmp_path):
        c = sqlite3.connect(tmp_path / "plain.db")
        c.row_factory = sqlite3.Row
        assert solar_production.yearly_weather(c) == {}


class TestWeatherRangeCoversSolarHistory:
    def test_range_starts_at_the_earlier_of_meter_and_solar_history(self, conn):
        from src import weather

        conn.execute("INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('1998-01-01', 1, 0, 1)")
        conn.execute("INSERT INTO solaredge_daily VALUES ('1996-01-01', 1000.0, 'x')")
        conn.commit()
        first, last = weather.meter_data_range(conn)
        assert first == "1996-01-01" and last == "1998-01-01"

    def test_install_without_the_solar_table_is_unchanged(self, tmp_path):
        from src import weather

        c = sqlite3.connect(tmp_path / "plain.db")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE power_daily (date TEXT)")
        c.execute("CREATE TABLE gas_daily (date TEXT)")
        c.execute("CREATE TABLE water_daily (date TEXT)")
        c.execute("CREATE TABLE battery_daily (date TEXT)")
        c.execute("INSERT INTO power_daily VALUES ('1998-01-01')")
        assert weather.meter_data_range(c) == ("1998-01-01", "1998-01-01")
