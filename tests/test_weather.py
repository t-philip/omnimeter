"""Weather config, coarsening, parsing and storage."""

import sqlite3
from datetime import date

import pytest

from src import db, weather


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    return c


class TestCoarsen:
    def test_coarse_rounds_to_one_decimal(self):
        # ~11 km cell. Verified against the real API: 52.3080/4.8560,
        # 52.31/4.86 and 52.3/4.9 all return byte-identical radiation,
        # because the reanalysis grid is ~9 km. Coarsening is free.
        assert weather.coarsen(52.3080, 4.8560, "coarse") == (52.3, 4.9)

    def test_precise_rounds_to_two_decimals(self):
        assert weather.coarsen(52.3080, 4.8560, "precise") == (52.31, 4.86)

    def test_exact_passes_through(self):
        assert weather.coarsen(52.3080, 4.8560, "exact") == (52.3080, 4.8560)

    def test_default_is_coarse(self):
        assert weather.coarsen(52.3080, 4.8560) == weather.coarsen(52.3080, 4.8560, "coarse")

    def test_unknown_precision_rejected(self):
        with pytest.raises(weather.WeatherConfigError):
            weather.coarsen(52.3, 4.9, "street")

    def test_southern_and_western_hemispheres(self):
        assert weather.coarsen(-33.8688, 151.2093, "coarse") == (-33.9, 151.2)


class TestConfiguredLocation:
    def test_reads_and_coarsens(self, monkeypatch):
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "52.3080")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.8560")
        monkeypatch.delenv("OMNIMETER_WEATHER_LOCATION_PRECISION", raising=False)
        # Coarsening happens in code, so exact coordinates in .env are still
        # protected by default.
        assert weather.configured_location() == (52.3, 4.9)

    def test_missing_config_raises(self, monkeypatch):
        monkeypatch.delenv("OMNIMETER_WEATHER_LATITUDE", raising=False)
        monkeypatch.delenv("OMNIMETER_WEATHER_LONGITUDE", raising=False)
        with pytest.raises(weather.WeatherConfigError):
            weather.configured_location()

    def test_out_of_range_rejected(self, monkeypatch):
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "999")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.9")
        with pytest.raises(weather.WeatherConfigError):
            weather.configured_location()

    def test_non_numeric_rejected(self, monkeypatch):
        monkeypatch.setenv("OMNIMETER_WEATHER_LATITUDE", "north")
        monkeypatch.setenv("OMNIMETER_WEATHER_LONGITUDE", "4.9")
        with pytest.raises(weather.WeatherConfigError):
            weather.configured_location()


class TestToggle:
    def test_disabled_by_default(self, conn):
        # OmniMeter's first outbound call must be opt-in, so an
        # already-deployed DB never starts phoning out on upgrade.
        assert weather.weather_enabled(conn) is False

    def test_enabled_when_set(self, conn):
        conn.execute("UPDATE feature_toggles SET weather_enabled = 1 WHERE id = 1")
        conn.commit()
        assert weather.weather_enabled(conn) is True


class TestBuildUrl:
    def test_uses_archive_endpoint_only(self):
        # The forecast endpoint disagrees with the archive by up to 60% on the
        # same date, so they must never be mixed.
        url = weather.build_url(52.3, 4.9, "2026-07-01", "2026-07-31")
        assert url.startswith(weather.ARCHIVE_URL)
        assert "shortwave_radiation_sum" in url
        assert "start_date=2026-07-01" in url and "end_date=2026-07-31" in url


class TestStore:
    def _rows(self):
        return [
            {
                "date": "2026-07-08",
                "shortwave_radiation_sum": 17.08,
                "sunshine_duration": 33840.0,
                "temperature_2m_max": 21.9,
                "temperature_2m_min": 13.0,
                "temperature_2m_mean": 17.2,
            }
        ]

    def test_round_trip(self, conn):
        assert weather.store(conn, self._rows(), 52.3, 4.9) == 1
        row = conn.execute("SELECT * FROM weather_daily WHERE date='2026-07-08'").fetchone()
        assert row["shortwave_radiation_sum"] == pytest.approx(17.08)
        assert row["sunshine_duration_s"] == pytest.approx(33840.0)
        assert row["latitude"] == pytest.approx(52.3)
        assert row["source"] == weather.SOURCE_ARCHIVE
        assert row["fetched_at"]

    def test_upsert_overwrites_and_restamps(self, conn):
        weather.store(conn, self._rows(), 52.3, 4.9)
        first = conn.execute("SELECT fetched_at FROM weather_daily").fetchone()["fetched_at"]
        revised = self._rows()
        revised[0]["shortwave_radiation_sum"] = 18.5
        weather.store(conn, revised, 52.3, 4.9)
        rows = conn.execute("SELECT * FROM weather_daily").fetchall()
        assert len(rows) == 1  # upsert, not duplicate
        assert rows[0]["shortwave_radiation_sum"] == pytest.approx(18.5)
        assert rows[0]["fetched_at"] >= first

    def test_covered_range(self, conn):
        assert weather.covered_range(conn) == (None, None)
        weather.store(conn, self._rows(), 52.3, 4.9)
        assert weather.covered_range(conn) == ("2026-07-08", "2026-07-08")


class TestMeterDataRange:
    def test_none_when_no_data(self, conn):
        assert weather.meter_data_range(conn) == (None, None)

    def test_spans_every_category(self, conn):
        conn.execute("INSERT INTO power_daily (date, import_kwh) VALUES ('2024-03-01', 4.0)")
        conn.execute("INSERT INTO water_daily (date, usage_l) VALUES ('2023-11-18', 5.6)")
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('2026-01-31', 5.0)")
        conn.commit()
        assert weather.meter_data_range(conn) == ("2023-11-18", "2026-01-31")


class TestRadiationDrivenEstimate:
    """The fix for a production model that could not tell a
    dull day from a bright one."""

    def test_dull_and_bright_days_no_longer_estimate_the_same(self):
        from src.solar_estimate import estimate_daily_production, estimate_daily_production_from_radiation

        # The old model returns the identical figure for every day of a month.
        july_dull, july_bright = date(2026, 7, 8), date(2026, 7, 10)
        assert estimate_daily_production(2.5, july_dull) == estimate_daily_production(2.5, july_bright)

        # Real measured values for those two days, and a real annual reference.
        ref = 4497.0
        dull = estimate_daily_production_from_radiation(2.5, 17.08, ref)
        bright = estimate_daily_production_from_radiation(2.5, 28.70, ref)
        assert bright > dull
        assert bright / dull == pytest.approx(28.70 / 17.08)

    def test_annual_total_is_preserved(self):
        # The point of redistributing rather than recomputing: the calibrated
        # annual figure must come out unchanged.
        from src.solar_estimate import DEFAULT_SPECIFIC_YIELD_KWH_PER_KWP, estimate_daily_production_from_radiation

        ref = 4497.0
        daily_rad = [ref / 365.0] * 365
        total = sum(estimate_daily_production_from_radiation(2.5, r, ref) for r in daily_rad)
        assert total == pytest.approx(2.5 * DEFAULT_SPECIFIC_YIELD_KWH_PER_KWP)

    def test_zero_reference_rejected(self):
        from src.solar_estimate import estimate_daily_production_from_radiation

        with pytest.raises(ValueError):
            estimate_daily_production_from_radiation(2.5, 20.0, 0.0)


class TestReferenceAndTypical:
    def _seed(self, conn, values_by_date):
        weather.store(
            conn,
            [
                {
                    "date": d,
                    "shortwave_radiation_sum": v,
                    "sunshine_duration": None,
                    "temperature_2m_max": None,
                    "temperature_2m_min": None,
                    "temperature_2m_mean": None,
                }
                for d, v in values_by_date.items()
            ],
            52.3,
            4.9,
        )

    def test_reference_annual_is_mean_times_365(self, conn):
        self._seed(conn, {"2026-01-01": 2.0, "2026-07-01": 22.0})
        assert weather.reference_annual_radiation(conn) == pytest.approx(12.0 * 365.0)

    def test_reference_none_when_empty(self, conn):
        assert weather.reference_annual_radiation(conn) is None

    def test_typical_is_seasonal_not_flat(self, conn):
        # Winter and summer must get different references -- a flat average
        # would make 4h of December sun look like a bad day when it is a good
        # one, which is the whole reason the UI shows % of typical.
        from datetime import date as _date
        from datetime import timedelta as _td

        rows = {}
        for i in range(60):
            rows[(_date(2025, 12, 1) + _td(days=i)).isoformat()] = 2.0
        for i in range(60):
            rows[(_date(2026, 6, 1) + _td(days=i)).isoformat()] = 22.0
        self._seed(conn, rows)

        typical = weather.typical_radiation_by_day_of_year(conn)
        assert typical["12-15"] == pytest.approx(2.0)
        assert typical["06-15"] == pytest.approx(22.0)

    def test_radiation_by_date_respects_range(self, conn):
        self._seed(conn, {"2026-07-01": 20.0, "2026-07-05": 25.0, "2026-08-01": 18.0})
        got = weather.radiation_by_date(conn, "2026-07-01", "2026-07-31")
        assert sorted(got) == ["2026-07-01", "2026-07-05"]


class TestHeatingDegreeDays:
    def test_below_base_is_positive(self):
        assert weather.heating_degree_days(10.0, base_c=18.0) == pytest.approx(8.0)

    def test_above_base_floors_at_zero(self):
        # A mild day needs no heating -- must not go negative.
        assert weather.heating_degree_days(22.0, base_c=18.0) == 0.0

    def test_at_base_is_zero(self):
        assert weather.heating_degree_days(18.0, base_c=18.0) == 0.0

    def test_none_temperature_returns_none(self):
        assert weather.heating_degree_days(None) is None

    def test_default_base_is_18c(self):
        assert weather.heating_degree_days(10.0) == weather.heating_degree_days(10.0, base_c=18.0)
        assert weather.DEFAULT_HDD_BASE_C == 18.0


class TestHeatingDegreeDaysByDate:
    def _seed_temps(self, conn, mean_temp_by_date):
        weather.store(
            conn,
            [
                {
                    "date": d,
                    "shortwave_radiation_sum": None,
                    "sunshine_duration": None,
                    "temperature_2m_max": None,
                    "temperature_2m_min": None,
                    "temperature_2m_mean": t,
                }
                for d, t in mean_temp_by_date.items()
            ],
            52.3,
            4.9,
        )

    def test_computes_per_date(self, conn):
        self._seed_temps(conn, {"2026-01-05": 2.0, "2026-01-06": 20.0})
        got = weather.heating_degree_days_by_date(conn, "2026-01-01", "2026-01-31")
        assert got["2026-01-05"] == pytest.approx(16.0)
        assert got["2026-01-06"] == 0.0

    def test_respects_range(self, conn):
        self._seed_temps(conn, {"2026-01-05": 2.0, "2026-02-05": 2.0})
        got = weather.heating_degree_days_by_date(conn, "2026-01-01", "2026-01-31")
        assert list(got) == ["2026-01-05"]

    def test_empty_when_no_temperature_data(self, conn):
        assert weather.heating_degree_days_by_date(conn, "2026-01-01", "2026-01-31") == {}


class TestTypicalHeatingDegreeDays:
    def _seed_temps(self, conn, mean_temp_by_date):
        weather.store(
            conn,
            [
                {
                    "date": d,
                    "shortwave_radiation_sum": None,
                    "sunshine_duration": None,
                    "temperature_2m_max": None,
                    "temperature_2m_min": None,
                    "temperature_2m_mean": t,
                }
                for d, t in mean_temp_by_date.items()
            ],
            52.3,
            4.9,
        )

    def test_typical_is_seasonal_not_flat(self, conn):
        # Same reasoning as TestReferenceAndTypical.test_typical_is_seasonal_not_flat:
        # a cold December must read as normal, a cold June must not.
        from datetime import date as _date
        from datetime import timedelta as _td

        rows = {}
        for i in range(60):
            rows[(_date(2025, 12, 1) + _td(days=i)).isoformat()] = 2.0
        for i in range(60):
            rows[(_date(2026, 6, 1) + _td(days=i)).isoformat()] = 22.0
        self._seed_temps(conn, rows)

        typical = weather.typical_heating_degree_days_by_day_of_year(conn)
        assert typical["12-15"] == pytest.approx(16.0)  # 18 - 2
        assert typical["06-15"] == pytest.approx(0.0)  # 18 - 22, floored at 0

    def test_empty_when_no_temperature_data(self, conn):
        assert weather.typical_heating_degree_days_by_day_of_year(conn) == {}
