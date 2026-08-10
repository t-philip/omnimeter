from datetime import date, timedelta

import pytest

from src import aggregate
from src.solar_estimate import estimate_daily_production, estimate_self_sufficiency, reconcile_with_export


class TestCleanCumulativeGlitches:
    def test_short_dropout_recovering_same_day_is_excised(self):
        # Mirrors the real Dec 2024 water data: two readings drop to 0 then
        # recover to roughly the pre-drop level within the hour.
        points = [
            ("2026-01-01 10:45", 602650.0),
            ("2026-01-01 11:00", 0.0),
            ("2026-01-01 11:15", 0.0),
            ("2026-01-01 11:30", 602686.0),
            ("2026-01-01 11:45", 0.0),
            ("2026-01-01 12:00", 0.0),
            ("2026-01-01 12:15", 602690.0),
        ]
        cleaned = aggregate.clean_cumulative_glitches(points)
        assert cleaned == [
            ("2026-01-01 10:45", 602650.0),
            ("2026-01-01 11:30", 602686.0),
            ("2026-01-01 12:15", 602690.0),
        ]

    def test_sustained_drop_becomes_new_segment(self):
        # Mirrors the real Nov 2023 water meter reset: value drops and never
        # recovers to the prior level within the lookahead window.
        points = [
            ("2026-01-01 00:00", 500000.0),
            ("2026-01-02 00:00", 0.0),
            ("2026-01-03 00:00", 0.0),
            ("2026-01-04 00:00", 50.0),
            ("2026-01-05 00:00", 120.0),
        ]
        cleaned = aggregate.clean_cumulative_glitches(points, lookahead_hours=48)
        # every point survives -- the "drop" is accepted as a new segment start,
        # and growth resumes normally from the new low baseline
        assert cleaned == points

    def test_clean_series_passes_through_unchanged(self):
        points = [
            ("2026-01-01 00:00", 100.0),
            ("2026-01-01 00:15", 100.5),
            ("2026-01-01 00:30", 101.0),
        ]
        assert aggregate.clean_cumulative_glitches(points) == points

    def test_empty_input(self):
        assert aggregate.clean_cumulative_glitches([]) == []

    def test_glitch_removal_prevents_fake_spike_in_daily_deltas(self):
        rows = [
            {"time": "2026-01-01 10:45", "value": 602650.0},
            {"time": "2026-01-01 11:00", "value": 0.0},
            {"time": "2026-01-01 11:15", "value": 0.0},
            {"time": "2026-01-01 11:30", "value": 602686.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        # a real ~3.6-unit delta, not a ~600000-unit fake spike
        assert usage["2026-01-01"] == pytest.approx(36.0)


class TestFilterPreferredGranularity:
    def test_daily_row_dropped_when_fine_data_exists_same_date(self):
        rows = [
            {"time": "2026-01-01 00:00", "granularity": "daily"},
            {"time": "2026-01-01 00:00", "granularity": "15min"},
            {"time": "2026-01-01 00:15", "granularity": "15min"},
        ]
        result = aggregate.filter_preferred_granularity(rows)
        assert all(r["granularity"] == "15min" for r in result)
        assert len(result) == 2

    def test_daily_row_kept_when_no_fine_data_for_date(self):
        rows = [{"time": "2021-06-01 00:00", "granularity": "daily"}]
        result = aggregate.filter_preferred_granularity(rows)
        assert len(result) == 1

    def test_live_outranks_both_15min_and_daily(self):
        rows = [
            {"time": "2026-01-01 00:00", "granularity": "daily"},
            {"time": "2026-01-01 00:00", "granularity": "15min"},
            {"time": "2026-01-01 00:15", "granularity": "15min"},
            {"time": "2026-01-01 00:30", "granularity": "live"},
        ]
        result = aggregate.filter_preferred_granularity(rows)
        assert len(result) == 1
        assert result[0]["granularity"] == "live"

    def test_15min_kept_when_no_live_data_for_date(self):
        rows = [
            {"time": "2026-01-01 00:00", "granularity": "15min"},
            {"time": "2026-01-02 00:00", "granularity": "live"},
        ]
        result = aggregate.filter_preferred_granularity(rows)
        assert len(result) == 2

    def test_unranked_granularity_kept_when_it_is_the_only_source_for_a_date(self):
        # REAL BUG, 2026-07-24: a date whose only rows are all an unranked
        # granularity (rank 0, e.g. the 'api_live' local-API poller) used to
        # crash with a KeyError -- `rank > best_rank_by_date.get(d, 0)` is
        # never true when both sides are 0, so that date was never seeded
        # into best_rank_by_date, even though it's a real date needing an
        # entry. Every granularity in use before that poller was added was
        # ranked, so this path was never reachable until 'api_live' existed.
        rows = [{"time": "2026-07-24 00:00", "granularity": "api_live"}]
        result = aggregate.filter_preferred_granularity(rows)
        assert len(result) == 1
        assert result[0]["granularity"] == "api_live"

    def test_ranked_granularity_still_outranks_unranked_on_same_date(self):
        rows = [
            {"time": "2026-07-24 00:00", "granularity": "api_live"},
            {"time": "2026-07-24 00:15", "granularity": "15min"},
        ]
        result = aggregate.filter_preferred_granularity(rows)
        assert len(result) == 1
        assert result[0]["granularity"] == "15min"


class TestComputeDailyDeltas:
    def test_simple_two_point_delta(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0},
            {"time": "2026-01-02 00:00", "value": 110.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage == {"2026-01-01": pytest.approx(10.0)}

    def test_fifteen_minute_deltas_sum_within_day(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0},
            {"time": "2026-01-01 00:15", "value": 100.5},
            {"time": "2026-01-01 00:30", "value": 101.2},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage["2026-01-01"] == pytest.approx(1.2)

    def test_negative_delta_skipped_not_subtracted(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0},
            {"time": "2026-01-01 00:15", "value": 50.0},  # meter reset
            {"time": "2026-01-01 00:30", "value": 55.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        # only the 50 -> 55 delta counts; the reset itself contributes nothing
        assert usage["2026-01-01"] == pytest.approx(5.0)

    def test_none_values_ignored(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": None},
            {"time": "2026-01-01 00:15", "value": 100.0},
            {"time": "2026-01-01 00:30", "value": 101.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage["2026-01-01"] == pytest.approx(1.0)


class TestDeltaSpanCap:
    """A delta bridging a gap in the series is not a day's usage — see
    aggregate.MAX_DELTA_SPAN_HOURS."""

    def test_daily_granularity_24h_delta_still_counted(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0},
            {"time": "2026-01-02 00:00", "value": 110.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage == {"2026-01-01": pytest.approx(10.0)}

    def test_dst_fall_back_25h_delta_still_counted(self):
        # 2026-10-25 is the NL fall-back day: local midnight to midnight is 25h.
        rows = [
            {"time": "2026-10-25 00:00", "value": 100.0},
            {"time": "2026-10-26 00:00", "value": 108.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage == {"2026-10-25": pytest.approx(8.0)}

    def test_multi_day_gap_contributes_nothing(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0},
            {"time": "2026-01-05 00:00", "value": 140.0},  # 4-day gap
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage == {}

    def test_readings_either_side_of_a_gap_still_count_their_own_days(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0},
            {"time": "2026-01-01 00:15", "value": 101.0},
            {"time": "2026-01-09 00:00", "value": 500.0},  # gap
            {"time": "2026-01-09 00:15", "value": 502.0},
        ]
        usage = aggregate.compute_daily_deltas(rows, "value")
        assert usage == {"2026-01-01": pytest.approx(1.0), "2026-01-09": pytest.approx(2.0)}


class TestRebuildPowerDailyPhaseLoad:
    """Phase-load (l1/l2/l3_max_w) only ever comes from CSV-sourced rows --
    HA's 'live' ingest has no phase-load entity wired in. Regression sentinel
    for a real production gap (dates from 2026-07-13 onward): once 'live'
    becomes the preferred granularity for a date, filter_preferred_granularity
    used to make the l1/l2/l3 computation see only 'live' rows -- discarding
    real phase data sitting in a 15min row for that same date -- and
    power_daily.l1_max_w etc. went NULL even though the raw data existed."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def test_phase_data_recovered_when_live_wins_the_date(self):
        conn = self._conn()

        # Both sources cover the same date. 'live' has more rows (ranks
        # higher) and no phase data at all; '15min' has real L1/L2/L3 values.
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, granularity) "
            "VALUES ('2026-07-13 08:00', 100.0, 'live')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, granularity) "
            "VALUES ('2026-07-13 08:15', 100.5, 'live')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, l1_max_w, l2_max_w, l3_max_w, granularity) "
            "VALUES ('2026-07-13 12:00', 50.0, 611.0, 507.0, 697.0, '15min')"
        )
        conn.commit()

        aggregate.rebuild_power_daily(conn)
        row = conn.execute("SELECT * FROM power_daily WHERE date = '2026-07-13'").fetchone()

        # import_kwh still correctly comes from 'live' (the preferred source
        # for deltas) -- this fix must not change that.
        assert row["l1_max_w"] == pytest.approx(611.0)
        assert row["l2_max_w"] == pytest.approx(507.0)
        assert row["l3_max_w"] == pytest.approx(697.0)

    def test_live_only_date_has_no_phase_data_not_a_bug(self):
        # A date with genuinely no CSV coverage at all has nothing to recover
        # -- confirms the fix doesn't fabricate data that was never ingested.
        conn = self._conn()
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, granularity) "
            "VALUES ('2026-07-16 08:00', 100.0, 'live')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, granularity) "
            "VALUES ('2026-07-16 08:15', 100.5, 'live')"
        )
        conn.commit()

        aggregate.rebuild_power_daily(conn)
        row = conn.execute("SELECT * FROM power_daily WHERE date = '2026-07-16'").fetchone()
        assert row["l1_max_w"] is None
        assert row["l2_max_w"] is None
        assert row["l3_max_w"] is None


class TestRebuildPowerDailyGranularitySandwich:
    """Regression sentinel for the live/15min/live 'sandwich' — fails against
    pre-MAX_DELTA_SPAN_HOURS code. A single 15min-preferred date inside the
    live era (an HA outage day later covered by a CSV upload) used to punch a
    gap into both the t1/t2 and combined series at once, producing a phantom
    day on an old date and double-counting the outage day onto its neighbour."""

    def test_sandwich_produces_no_phantom_day(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        def add(time, granularity, t1=None, combined=None):
            conn.execute(
                "INSERT INTO power_readings (time, import_t1_kwh, import_combined_kwh, granularity) "
                "VALUES (?, ?, ?, ?)",
                (time, t1, combined, granularity),
            )

        # Old CSV era: T1-only readings.
        add("2026-06-01 00:00", "15min", t1=11000.0)
        add("2026-06-01 00:15", "15min", t1=11000.5)
        # Live era: HA writes the combined meter.
        add("2026-07-01 00:00", "live", combined=21000.0)
        add("2026-07-01 00:15", "live", combined=21001.0)
        # HA outage day, backfilled from a CSV export -> 15min wins this date.
        add("2026-07-02 00:00", "15min", t1=11443.0)
        add("2026-07-02 00:15", "15min", t1=11444.0)
        # Live resumes.
        add("2026-07-03 00:00", "live", combined=21010.0)
        add("2026-07-03 00:15", "live", combined=21011.0)
        conn.commit()

        aggregate.rebuild_power_daily(conn)
        rows = {r["date"]: r["import_kwh"] for r in conn.execute("SELECT * FROM power_daily").fetchall()}

        # The t1 series' 2026-06-01 -> 2026-07-02 bridge (~443 kWh across a
        # month) must not land on 2026-06-01 as a single day's usage.
        assert rows["2026-06-01"] == pytest.approx(0.5)
        # The combined series' 2026-07-01 -> 2026-07-03 bridge must not add
        # 2026-07-02's meter movement onto 2026-07-01.
        assert rows["2026-07-01"] == pytest.approx(1.0)
        # Each day still reports its own within-day usage.
        assert rows["2026-07-02"] == pytest.approx(1.0)
        assert rows["2026-07-03"] == pytest.approx(1.0)


class TestRebuildPowerDailyApiLiveDoubleCount:
    """Regression sentinel. Unlike CSV (t1/t2 only) and the old HA
    'live' sync (combined only), the api_live poller's _map_p1() writes t1,
    t2, AND combined into every row -- fails against the pre-fix code, which
    summed all three and reported ~2x a real day's import (confirmed live,
    2026-08-02: 44.644 kWh actual vs. 89.306 kWh reported)."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def test_api_live_row_with_t1_t2_and_combined_not_double_counted(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, import_t2_kwh, import_combined_kwh, granularity) "
            "VALUES ('2026-08-02 00:00', 11514.338, 10457.292, 21971.630, 'api_live')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, import_t2_kwh, import_combined_kwh, granularity) "
            "VALUES ('2026-08-02 23:59', 11558.982, 10457.292, 22016.274, 'api_live')"
        )
        conn.commit()

        aggregate.rebuild_power_daily(conn)
        row = conn.execute("SELECT * FROM power_daily WHERE date = '2026-08-02'").fetchone()

        # Real usage is the single combined delta (44.644 kWh), not
        # t1-delta + t2-delta + combined-delta (89.288 kWh).
        assert row["import_kwh"] == pytest.approx(44.644)

    def test_csv_only_date_still_uses_t1_plus_t2(self):
        # No combined column at all for this date (the CSV-only case) --
        # must still fall back to t1+t2, not silently report 0.
        conn = self._conn()
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, import_t2_kwh, granularity) "
            "VALUES ('2026-01-01 00:00', 100.0, 50.0, '15min')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_t1_kwh, import_t2_kwh, granularity) "
            "VALUES ('2026-01-01 00:15', 100.5, 50.2, '15min')"
        )
        conn.commit()

        aggregate.rebuild_power_daily(conn)
        row = conn.execute("SELECT * FROM power_daily WHERE date = '2026-01-01'").fetchone()
        assert row["import_kwh"] == pytest.approx(0.7)


class TestComputeDailyExtrema:
    def test_min_max_avg(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 10.0},
            {"time": "2026-01-01 00:15", "value": 20.0},
            {"time": "2026-01-01 00:30", "value": 30.0},
        ]
        result = aggregate.compute_daily_extrema(rows, "value")
        assert result["2026-01-01"] == {"min": 10.0, "max": 30.0, "avg": pytest.approx(20.0)}

    def test_none_values_ignored(self):
        rows = [{"time": "2026-01-01 00:00", "value": None}]
        result = aggregate.compute_daily_extrema(rows, "value")
        assert result == {}


class TestComputeDailyLastValue:
    def test_returns_chronologically_last_reading_per_date(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 67.0},
            {"time": "2026-01-01 12:00", "value": 40.0},
            {"time": "2026-01-01 23:45", "value": 12.0},
            {"time": "2026-01-02 00:00", "value": 11.0},
        ]
        result = aggregate.compute_daily_last_value(rows, "value")
        assert result == {"2026-01-01": 12.0, "2026-01-02": 11.0}

    def test_none_values_ignored(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 50.0},
            {"time": "2026-01-01 12:00", "value": None},
        ]
        result = aggregate.compute_daily_last_value(rows, "value")
        assert result == {"2026-01-01": 50.0}

    def test_empty_input(self):
        assert aggregate.compute_daily_last_value([], "value") == {}


class TestRebuildBatteryDaily:
    def test_populates_eod_soc_alongside_avg(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        for time, soc in [("2026-07-08 22:00", 67.0), ("2026-07-08 23:45", 40.0), ("2026-07-09 01:00", 1.0)]:
            conn.execute(
                "INSERT INTO battery_readings (time, import_kwh, export_kwh, soc_pct, granularity) "
                "VALUES (?, 0, 0, ?, '15min')",
                (time, soc),
            )
        conn.commit()

        aggregate.rebuild_battery_daily(conn)
        rows = {r["date"]: r for r in conn.execute("SELECT * FROM battery_daily").fetchall()}

        assert rows["2026-07-08"]["eod_soc_pct"] == pytest.approx(40.0)
        assert rows["2026-07-08"]["avg_soc_pct"] == pytest.approx((67.0 + 40.0) / 2)
        assert rows["2026-07-09"]["eod_soc_pct"] == pytest.approx(1.0)


class TestPruneLiveReadings:
    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def test_old_live_rows_thinned_to_quarter_hour(self):
        # A code review found HA live rows land every minute and grow
        # without bound, making the full-table rollup rebuild progressively
        # slower. Old rows should thin to the same :00/:15/:30/:45 density as
        # the HomeWizard 15-minute CSV export; recent rows stay untouched.
        from datetime import datetime, timedelta

        old_day = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        conn = self._conn()
        for minute in range(0, 60, 3):  # 20 minute-ish rows for one old day
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, 1.0, 'live')",
                (f"{old_day} 10:{minute:02d}",)
            )
        conn.commit()

        deleted = aggregate.prune_live_readings(conn, keep_days=30)
        assert deleted > 0

        remaining = {
            r["time"][-5:] for r in conn.execute("SELECT time FROM power_readings").fetchall()
        }
        # quarter-hour marks, plus the day's actual last reading (10:57 here)
        # which is always kept regardless of its minute -- see the function
        # docstring for why that anchor matters for day-boundary attribution.
        assert remaining == {"10:00", "10:15", "10:30", "10:45", "10:57"}

    def test_recent_live_rows_untouched(self):
        from datetime import datetime, timedelta

        recent_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = self._conn()
        for minute in range(0, 10):
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, 1.0, 'live')",
                (f"{recent_day} 10:{minute:02d}",)
            )
        conn.commit()

        aggregate.prune_live_readings(conn, keep_days=30)
        count = conn.execute("SELECT COUNT(*) AS c FROM power_readings").fetchone()["c"]
        assert count == 10

    def test_non_live_rows_never_pruned(self):
        # 15min/daily rows are the CSV export's own data -- never thin those,
        # only HA's minute-resolution 'live' rows.
        from datetime import datetime, timedelta

        old_day = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        conn = self._conn()
        for minute in [1, 2, 3, 4]:
            conn.execute(
                "INSERT INTO power_readings (time, import_t1_kwh, granularity) VALUES (?, 1.0, '15min')",
                (f"{old_day} 10:{minute:02d}",)
            )
        conn.commit()

        aggregate.prune_live_readings(conn, keep_days=30)
        count = conn.execute("SELECT COUNT(*) AS c FROM power_readings").fetchone()["c"]
        assert count == 4

    def test_daily_totals_unaffected_by_pruning(self):
        # The whole point: dropping intermediate live points must not change
        # a cumulative meter's daily delta, since compute_daily_deltas only
        # needs the first and last reading of each day.
        from datetime import datetime, timedelta

        old_day = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        conn = self._conn()
        for minute, value in [(0, 100.0), (1, 100.2), (30, 101.0), (59, 101.9)]:
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'live')",
                (f"{old_day} 10:{minute:02d}", value),
            )
        conn.commit()

        rows_before = [dict(r) for r in conn.execute("SELECT * FROM power_readings").fetchall()]
        before = aggregate.compute_daily_deltas(rows_before, "import_combined_kwh")

        aggregate.prune_live_readings(conn, keep_days=30)
        rows_after = [dict(r) for r in conn.execute("SELECT * FROM power_readings").fetchall()]
        after = aggregate.compute_daily_deltas(rows_after, "import_combined_kwh")

        assert after[old_day] == pytest.approx(before[old_day])


class TestExpandOccupancyByDay:
    def test_single_entry_expands_to_every_covered_day(self):
        rows = [{"date_from": "2026-07-10 00:00", "date_to": "2026-07-12 23:59", "occupant_count": 3}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result == {"2026-07-10": 3, "2026-07-11": 3, "2026-07-12": 3}

    def test_single_day_entry(self):
        rows = [{"date_from": "2026-07-10 00:00", "date_to": "2026-07-10 23:59", "occupant_count": 1}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result == {"2026-07-10": 1}

    def test_clamped_to_query_range(self):
        # Entry spans wider than the query window -- only days inside
        # [date_from, date_to] of the query should appear.
        rows = [{"date_from": "2026-06-25 00:00", "date_to": "2026-07-05 23:59", "occupant_count": 2}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result == {"2026-07-01": 2, "2026-07-02": 2, "2026-07-03": 2, "2026-07-04": 2, "2026-07-05": 2}

    def test_days_with_no_entry_are_omitted_not_defaulted(self):
        rows = [{"date_from": "2026-07-10 00:00", "date_to": "2026-07-10 23:59", "occupant_count": 1}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert "2026-07-15" not in result
        assert len(result) == 1

    def test_multiple_non_overlapping_entries(self):
        rows = [
            {"date_from": "2026-07-01 00:00", "date_to": "2026-07-05 23:59", "occupant_count": 1},
            {"date_from": "2026-07-06 00:00", "date_to": "2026-07-10 23:59", "occupant_count": 3},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-05"] == 1
        assert result["2026-07-06"] == 3
        assert len(result) == 10

    def test_mid_day_transition_majority_wins(self):
        # Left at 08:00 (away, 0 people) then back at 18:00 (occupied, 2
        # people) on the same calendar day -- 08:00-18:00 away (10h) vs.
        # 18:00-24:00 occupied (6h), so "away" covers more of the day.
        rows = [
            {"date_from": "2026-07-10 00:00", "date_to": "2026-07-10 08:00", "occupant_count": 2},
            {"date_from": "2026-07-10 08:00", "date_to": "2026-07-10 18:00", "occupant_count": 0},
            {"date_from": "2026-07-10 18:00", "date_to": "2026-07-10 23:59", "occupant_count": 2},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-10"] == 2

    def test_mid_day_transition_minority_state_loses(self):
        # Away for most of the day (08:00-20:00, 12h) vs. occupied for a
        # smaller morning + evening slice (0-8h + 20-24h = 8h) -- away wins.
        rows = [
            {"date_from": "2026-07-10 00:00", "date_to": "2026-07-10 08:00", "occupant_count": 2},
            {"date_from": "2026-07-10 08:00", "date_to": "2026-07-10 20:00", "occupant_count": 0},
            {"date_from": "2026-07-10 20:00", "date_to": "2026-07-10 23:59", "occupant_count": 2},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-10"] == 0

    def test_exact_tie_later_entry_wins(self):
        # Exactly 6h each -- the later (chronologically last) entry's state
        # wins the tie, per expand_occupancy_by_day's documented rule. (Uses
        # a mid-day split rather than a 00:00/23:59 day-boundary split --
        # "23:59" is one minute short of true midnight, which would make a
        # 00:00-12:00 / 12:00-23:59 split 720 vs. 719 minutes, not a tie.)
        rows = [
            {"date_from": "2026-07-10 06:00", "date_to": "2026-07-10 12:00", "occupant_count": 2},
            {"date_from": "2026-07-10 12:00", "date_to": "2026-07-10 18:00", "occupant_count": 0},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-10"] == 0

    def test_partial_day_coverage_with_gap_still_classified_from_logged_minutes(self):
        # Only 06:00-10:00 (4h) logged as occupied; the rest of the day is an
        # unlogged gap. The day is still included (some minutes are known),
        # classified purely from the logged portion.
        rows = [{"date_from": "2026-07-10 06:00", "date_to": "2026-07-10 10:00", "occupant_count": 2}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-10"] == 2

    def test_gap_between_two_entries_is_bridged_with_earlier_count(self):
        # A follow-up request: a gap strictly between two logged
        # entries carries the earlier entry's count forward, rather than
        # being omitted as unknown. Second entry starts exactly at a day
        # boundary (00:00) so the bridge contributes 0 minutes to that day,
        # keeping its own classification unambiguous.
        rows = [
            {"date_from": "2026-07-05 17:30", "date_to": "2026-07-05 20:00", "occupant_count": 2},
            {"date_from": "2026-07-08 00:00", "date_to": "2026-07-08 03:00", "occupant_count": 0},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        # 07-06 and 07-07 are entirely inside the bridged gap -> carried
        # forward as 2 (the earlier entry's count).
        assert result["2026-07-06"] == 2
        assert result["2026-07-07"] == 2
        assert result["2026-07-08"] == 0

    def test_long_gap_is_not_bridged(self):
        # Bridging always claimed to fill "a short unlogged stretch"
        # but never enforced it, so a 3-day trip carried its count across
        # every unlogged day until the next entry -- measured at up to 75
        # days on the real log. With a log made largely of absences that
        # propagated "house empty" across whole months: 272 days resolved as
        # empty while the meters showed real use, some at 900 L/day.
        rows = [
            {"date_from": "2024-06-27 00:00", "date_to": "2024-06-29 23:59", "occupant_count": 0},
            {"date_from": "2024-09-01 00:00", "date_to": "2024-09-01 23:59", "occupant_count": 2},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2024-06-01", "2024-09-30")
        # The two months between are unknown, not "empty".
        assert "2024-07-15" not in result
        assert "2024-08-01" not in result
        assert result["2024-06-28"] == 0
        assert result["2024-09-01"] == 2

    def test_gap_exactly_at_the_cap_is_still_bridged(self):
        cap = aggregate.OCCUPANCY_BRIDGE_MAX_GAP_DAYS
        rows = [
            {"date_from": "2026-07-01 00:00", "date_to": "2026-07-01 12:00", "occupant_count": 2},
            {"date_from": f"2026-07-{1 + cap:02d} 00:00", "date_to": f"2026-07-{1 + cap:02d} 12:00",
             "occupant_count": 0},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        # Every day inside the capped gap is carried forward.
        for offset in range(1, cap):
            assert result[f"2026-07-{1 + offset:02d}"] == 2

    def test_gap_before_first_entry_is_not_bridged(self):
        # No entry precedes the first one -- there's no "last known fact" to
        # carry into the days before it, so they stay omitted.
        rows = [{"date_from": "2026-07-10 00:00", "date_to": "2026-07-10 23:59", "occupant_count": 1}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert "2026-07-05" not in result
        assert "2026-07-09" not in result

    def test_gap_after_last_entry_is_not_bridged(self):
        # Deliberately not carried forward past the last entry -- a
        # forgotten log update must surface as missing data, not silently
        # misreport an open-ended stretch as the old state.
        rows = [{"date_from": "2026-07-10 00:00", "date_to": "2026-07-10 23:59", "occupant_count": 1}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert "2026-07-11" not in result
        assert "2026-07-31" not in result

    def test_bridged_gap_competes_in_majority_of_day(self):
        # A day split between a bridged carry-forward segment and a small
        # real logged segment still resolves by majority-of-day -- proving
        # bridged minutes are genuinely counted, not just filler, since here
        # they're what decides the outcome (22h bridged @2 vs. 2h real @1).
        rows = [
            {"date_from": "2026-07-08 00:00", "date_to": "2026-07-08 06:00", "occupant_count": 2},
            {"date_from": "2026-07-09 22:00", "date_to": "2026-07-10 00:00", "occupant_count": 1},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-09"] == 2

    def test_nested_period_overrides_surrounding_one(self):
        # A reported real case: a longer visit ("Mom visiting NL") with a
        # shorter trip nested inside it ("Lisbon") -- the nested, shorter
        # entry should win for the days it covers; the surrounding entry
        # still governs the rest of its own range.
        rows = [
            {"date_from": "2026-04-08 21:30", "date_to": "2026-07-05 17:30", "occupant_count": 2},
            {"date_from": "2026-06-10 00:00", "date_to": "2026-06-13 23:59", "occupant_count": 1},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-06-01", "2026-06-30")
        assert result["2026-06-09"] == 2
        assert result["2026-06-10"] == 1
        assert result["2026-06-11"] == 1
        assert result["2026-06-12"] == 1
        assert result["2026-06-13"] == 1
        assert result["2026-06-14"] == 2

    def test_deleting_nested_period_reasserts_surrounding_one(self):
        # Non-destructive: removing the nested entry (simulated here by just
        # not including it) leaves the surrounding entry's full range intact.
        rows = [{"date_from": "2026-04-08 21:30", "date_to": "2026-07-05 17:30", "occupant_count": 2}]
        result = aggregate.expand_occupancy_by_day(rows, "2026-06-01", "2026-06-30")
        assert result["2026-06-10"] == 2
        assert result["2026-06-13"] == 2

    def test_partial_overlap_shortest_duration_wins_regardless_of_order(self):
        # Two entries overlap without either containing the other. The
        # shorter-duration entry is "more specific" and wins the overlap,
        # even though it starts later than the entry it's overriding.
        rows = [
            {"date_from": "2026-07-10 00:00", "date_to": "2026-07-20 23:59", "occupant_count": 2},
            {"date_from": "2026-07-15 00:00", "date_to": "2026-07-16 23:59", "occupant_count": 0},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-14"] == 2
        assert result["2026-07-15"] == 0
        assert result["2026-07-16"] == 0
        assert result["2026-07-17"] == 2

    def test_equal_duration_overlap_later_start_wins(self):
        # Same total duration, genuinely overlapping (not nested) -- ambiguous
        # by duration alone, so the later-starting entry wins the overlap.
        rows = [
            {"date_from": "2026-07-10 00:00", "date_to": "2026-07-14 23:59", "occupant_count": 3},
            {"date_from": "2026-07-12 00:00", "date_to": "2026-07-16 23:59", "occupant_count": 1},
        ]
        result = aggregate.expand_occupancy_by_day(rows, "2026-07-01", "2026-07-31")
        assert result["2026-07-11"] == 3
        assert result["2026-07-12"] == 1
        assert result["2026-07-13"] == 1
        assert result["2026-07-14"] == 1
        assert result["2026-07-15"] == 1


class TestSolarEstimate:
    def test_summer_day_produces_more_than_winter_day(self):
        june = estimate_daily_production(5.0, date(2026, 6, 15))
        december = estimate_daily_production(5.0, date(2026, 12, 15))
        assert june > december

    def test_zero_kwp_produces_nothing(self):
        assert estimate_daily_production(0.0, date(2026, 6, 15)) == 0.0

    def test_reconcile_never_below_export(self):
        assert reconcile_with_export(estimated_kwh=5.0, exported_kwh=8.0) == 8.0
        assert reconcile_with_export(estimated_kwh=10.0, exported_kwh=8.0) == 10.0

    def test_self_sufficiency_full_coverage(self):
        # no grid import at all -> 100% self-sufficient
        result = estimate_self_sufficiency(production_kwh=10.0, export_kwh=2.0, import_kwh=0.0)
        assert result == pytest.approx(1.0)

    def test_self_sufficiency_no_solar_all_grid(self):
        result = estimate_self_sufficiency(production_kwh=0.0, export_kwh=0.0, import_kwh=10.0)
        assert result == pytest.approx(0.0)

    def test_self_sufficiency_none_when_no_consumption(self):
        assert estimate_self_sufficiency(production_kwh=0.0, export_kwh=0.0, import_kwh=0.0) is None


# ---------------------------------------------------------------------------
# Data quality diagnostics
# ---------------------------------------------------------------------------


class TestFindOutlierDays:
    def _flat_rows(self, n, value=4.0, start="2026-07-01"):
        d0 = date.fromisoformat(start)
        return [{"date": (d0 + timedelta(days=i)).isoformat(), "value": value} for i in range(n)]

    def test_no_outliers_in_flat_series(self):
        rows = self._flat_rows(20)
        assert aggregate.find_outlier_days(rows, "value") == []

    def test_spike_flagged_high(self):
        # Mirrors the real 2026-07-29 EV-charging day (~8x a ~4 kWh/day
        # baseline) -- a legitimate spike, and this feature's design is to
        # report it, not suppress it.
        rows = self._flat_rows(14)
        rows.append({"date": "2026-07-15", "value": 32.0})
        rows.append({"date": "2026-07-16", "value": 4.0})  # anchor -- spike day must not be the last point
        result = aggregate.find_outlier_days(rows, "value")
        assert len(result) == 1
        assert result[0]["date"] == "2026-07-15"
        assert result[0]["direction"] == "high"
        assert result[0]["ratio"] == pytest.approx(8.0)

    def test_dip_flagged_low(self):
        rows = self._flat_rows(14)
        rows.append({"date": "2026-07-15", "value": 0.5})
        rows.append({"date": "2026-07-16", "value": 4.0})  # anchor -- dip day must not be the last point
        result = aggregate.find_outlier_days(rows, "value")
        assert len(result) == 1
        assert result[0]["direction"] == "low"

    def test_most_recent_day_never_evaluated(self):
        # Found live 2026-08-03: "Battery discharge_kwh: 2026-08-03 down0.0x
        # baseline (0.00 vs 2.17)" -- today's total was still accumulating
        # (a few hours into the day), not genuinely low. Comparing a partial
        # day against a baseline of complete days would flag this every
        # single morning, for every category. Same shape as test_dip_flagged_low
        # but without the anchor day -- the dip is the last point and must
        # NOT be flagged, exactly the live case.
        # `today` is now passed explicitly rather than inferred from the last
        # row: the old "skip the final point" rule was a proxy that also
        # silently skipped a *complete* day whenever ingestion stalled.
        # Passing it also makes the test independent of the wall
        # clock, which the previous version was not.
        rows = self._flat_rows(14)
        rows.append({"date": "2026-07-15", "value": 0.5})
        assert aggregate.find_outlier_days(rows, "value", today="2026-07-15") == []

    def test_complete_final_day_is_evaluated(self):
        # The other half of that fix: once the day is genuinely over, it
        # must be judged like any other. Under the old "never evaluate the
        # last row" rule this dip was invisible forever if no newer row
        # arrived -- exactly what happens when an ingest path stops.
        rows = self._flat_rows(14)
        rows.append({"date": "2026-07-15", "value": 0.5})
        result = aggregate.find_outlier_days(rows, "value", today="2026-07-16")
        assert [r["date"] for r in result] == ["2026-07-15"]
        assert result[0]["direction"] == "low"

    def test_insufficient_history_not_flagged(self):
        # Fewer than OUTLIER_MIN_HISTORY_DAYS prior rows -- too early in a
        # category's history to have a meaningful baseline, so nothing is
        # flagged even for a wildly different value.
        rows = self._flat_rows(5)
        rows.append({"date": "2026-07-06", "value": 100.0})
        rows.append({"date": "2026-07-07", "value": 4.0})  # anchor
        assert aggregate.find_outlier_days(rows, "value") == []

    def test_unused_metric_never_flagged(self):
        # A metric that is genuinely always zero (a household with no gas
        # supply at all) has no meaningful ratio -- guarded by requiring the
        # long-run median to be > 0, which also makes the division safe.
        rows = self._flat_rows(20, value=0.0)
        assert aggregate.find_outlier_days(rows, "value", today="2026-08-01") == []

    def test_small_but_real_baseline_is_still_evaluated(self):
        # The old absolute epsilon (0.5) was unit-blind: it sat
        # *above* a typical summer gas baseline of ~0.3 m3, so gas outlier
        # detection was silently off for most of the year, while for water
        # (~120 L) the same constant never fired at all. A 50x jump on a small
        # but genuinely non-zero baseline is a real event and must be
        # reported -- suppressing it purely because the numbers are small was
        # the bug, not the safeguard.
        rows = self._flat_rows(14, value=0.1)
        rows.append({"date": "2026-07-15", "value": 5.0})
        rows.append({"date": "2026-07-16", "value": 0.1})
        result = aggregate.find_outlier_days(rows, "value", today="2026-07-17")
        assert [r["date"] for r in result] == ["2026-07-15"]
        assert result[0]["direction"] == "high"

    def test_dead_era_does_not_disable_the_metric_forever(self):
        # REAL DATA: 699 consecutive water_daily rows, spanning nearly two
        # years, are exactly 0.0 -- the water meter was installed some time
        # after the P1 power/gas meter and the long-range export
        # padded the pre-installation period with zeros to align the two series.
        # That dead era is 63% of the water history, so a whole-history median
        # was 0.00 and switched water outlier detection off entirely (170
        # notes -> 0). A trailing window steps past it: nothing is flagged
        # inside the dead era, and detection works normally once real data
        # starts.
        rows = self._flat_rows(500, value=0.0, start="2021-02-01")
        rows += self._flat_rows(400, value=180.0, start="2022-06-16")
        rows.append({"date": "2023-07-21", "value": 5.0})  # a real dip in the live era
        rows.append({"date": "2023-07-22", "value": 180.0})
        result = aggregate.find_outlier_days(rows, "value", today="2023-07-23")

        assert [o["date"] for o in result if o["date"] < "2022-06-16"] == []  # dead era stays silent
        assert "2023-07-21" in {o["date"] for o in result}  # real dip still caught
        assert next(o for o in result if o["date"] == "2023-07-21")["direction"] == "low"

    def test_epsilon_scales_with_the_metric_so_units_do_not_matter(self):
        # The same relative event must behave identically whether the series
        # is in m3 (~0.3) or litres (~120). Under the old absolute epsilon
        # these two produced opposite outcomes.
        for scale in (0.3, 120.0):
            rows = self._flat_rows(14, value=scale)
            rows.append({"date": "2026-07-15", "value": scale * 5})
            rows.append({"date": "2026-07-16", "value": scale})
            result = aggregate.find_outlier_days(rows, "value", today="2026-07-17")
            assert [r["date"] for r in result] == ["2026-07-15"], f"scale={scale}"
            assert result[0]["direction"] == "high", f"scale={scale}"

    def test_seasonal_low_day_not_flagged_once_baseline_itself_is_low(self):
        # Follow-up finding, found by using the feature against real
        # data: 713 low-direction flags total (181 of them an exact 0.0),
        # almost all seasonal (gas in summer, solar export in winter) rather
        # than real anomalies -- ratio alone can't tell "genuinely quiet
        # season" from "real drop" apart. 60 days at a high value establish
        # a real long-run typical (median), then 20 days at a much lower
        # value simulate an off-season stretch -- the transition itself is
        # still informative and correctly flagged (the trailing 30-day
        # window takes ~15 quiet days to pull its own median down below the
        # stability floor), but once the trailing baseline has adapted to
        # the new normal, further quiet days -- including a real dip deep
        # into the stretch -- must stop being repeatedly re-flagged. That's
        # the actual fix for real data: not zero low-flags forever, but
        # no longer hundreds of near-duplicate flags for one long season.
        rows = self._flat_rows(60, value=10.0)
        rows += self._flat_rows(20, value=2.0, start="2026-08-30")
        rows.append({"date": "2026-09-20", "value": 0.3})
        rows.append({"date": "2026-09-21", "value": 2.0})  # anchor -- the test day must not be the last point
        result = aggregate.find_outlier_days(rows, "value", today="2026-09-22")
        flagged_dates = {r["date"] for r in result}
        assert "2026-09-20" not in flagged_dates  # deep in the quiet stretch, baseline has adapted
        assert len(result) < 20  # the whole stretch is not repeatedly re-flagged
        assert len(result) > 0  # the transition itself is still real, useful information

    def test_past_findings_do_not_change_when_later_data_arrives(self):
        # The long-run median used by the stability floor was
        # computed over the WHOLE series including days *after* the one being
        # judged, so a historical day could silently flip flagged/unflagged as
        # new data landed -- and acknowledgements are stored per date, so the
        # list could shift under the user. The median is now causal: only days
        # strictly before the one under test contribute.
        base = self._flat_rows(40, value=10.0)
        base.append({"date": "2026-08-10", "value": 1.0})
        before = aggregate.find_outlier_days(base, "value", today="2026-08-11")

        extended = base + self._flat_rows(40, value=0.5, start="2026-08-11")
        after = [o for o in aggregate.find_outlier_days(extended, "value", today="2026-09-21") if o["date"] <= "2026-08-10"]

        assert [(o["date"], o["direction"]) for o in before] == [(o["date"], o["direction"]) for o in after]

    def test_genuine_drop_from_stable_baseline_still_flagged(self):
        # Companion to the seasonal test above -- a real drop while the
        # trailing baseline is still solidly established (not itself
        # depressed by a prior quiet stretch) must still be caught. Same
        # shape as test_dip_flagged_low, just confirming the new floor check
        # doesn't accidentally suppress this case too.
        rows = self._flat_rows(30, value=10.0)
        rows.append({"date": "2026-08-01", "value": 1.0})
        rows.append({"date": "2026-08-02", "value": 10.0})  # anchor -- the drop day must not be the last point
        result = aggregate.find_outlier_days(rows, "value")
        assert len(result) == 1
        assert result[0]["direction"] == "low"


class TestFindNegativeDeltas:
    def test_reset_delta_collected_not_silently_dropped(self):
        # Same shape as TestComputeDailyDeltas.test_negative_delta_skipped_not_subtracted --
        # that test only checks the resulting usage total; this checks the
        # negative delta itself is surfaced, not just discarded.
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0, "granularity": "api_live"},
            {"time": "2026-01-01 00:15", "value": 50.0, "granularity": "api_live"},  # meter reset
            {"time": "2026-01-01 00:30", "value": 55.0, "granularity": "api_live"},
        ]
        negatives = aggregate.find_negative_deltas(rows, "value")
        assert negatives == [
            {
                "time": "2026-01-01 00:15",
                "from_value": 100.0,
                "to_value": 50.0,
                "delta": -50.0,
                "granularity": "api_live",
            }
        ]

    def test_no_negative_deltas_returns_empty(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0, "granularity": "api_live"},
            {"time": "2026-01-01 00:15", "value": 105.0, "granularity": "api_live"},
        ]
        assert aggregate.find_negative_deltas(rows, "value") == []

    def test_pairing_does_not_cross_granularity_boundary(self):
        # A 'live' point immediately followed by a lower-valued
        # '15min' point at a different timestamp must not read as a real
        # negative delta -- the two sources aren't a contiguous series.
        rows = [
            {"time": "2026-01-01 00:00", "value": 500.0, "granularity": "live"},
            {"time": "2026-01-01 00:15", "value": 10.0, "granularity": "15min"},
            {"time": "2026-01-01 00:30", "value": 15.0, "granularity": "15min"},
        ]
        assert aggregate.find_negative_deltas(rows, "value") == []


class TestFindGlitchEpisodes:
    def test_transient_dropout_counted_as_one_episode_each(self):
        # Same 7-point series as TestCleanCumulativeGlitches's own dropout
        # test -- two separate dip-and-recover episodes (11:00-11:15, then
        # 11:45-12:00), each counted once regardless of how many raw points
        # each spans.
        rows = [
            {"time": "2026-01-01 10:45", "value": 602650.0, "granularity": "15min"},
            {"time": "2026-01-01 11:00", "value": 0.0, "granularity": "15min"},
            {"time": "2026-01-01 11:15", "value": 0.0, "granularity": "15min"},
            {"time": "2026-01-01 11:30", "value": 602686.0, "granularity": "15min"},
            {"time": "2026-01-01 11:45", "value": 0.0, "granularity": "15min"},
            {"time": "2026-01-01 12:00", "value": 0.0, "granularity": "15min"},
            {"time": "2026-01-01 12:15", "value": 602690.0, "granularity": "15min"},
        ]
        episodes = aggregate.find_glitch_episodes(rows, "value")
        assert len(episodes) == 2
        assert episodes[0]["start_time"] == "2026-01-01 11:00"
        assert episodes[0]["end_time"] == "2026-01-01 11:15"
        assert episodes[0]["magnitude"] == pytest.approx(602650.0)
        assert episodes[0]["granularity"] == "15min"
        assert episodes[1]["start_time"] == "2026-01-01 11:45"
        assert aggregate.count_glitch_corrections(rows, "value") == len(episodes) == 2

    def test_no_glitches_returns_empty(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0, "granularity": "api_live"},
            {"time": "2026-01-01 00:15", "value": 105.0, "granularity": "api_live"},
        ]
        assert aggregate.find_glitch_episodes(rows, "value") == []
        assert aggregate.count_glitch_corrections(rows, "value") == 0

    def test_sub_threshold_noise_not_counted(self):
        # Follow-up finding: confirmed real against OmniMeter's live battery
        # data -- device-reporting jitter (dips of ~0.001 kWh) must not
        # count as a "glitch correction" worth a human's attention.
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.000, "granularity": "api_live"},
            {"time": "2026-01-01 00:15", "value": 99.999, "granularity": "api_live"},  # -0.001, pure noise
            {"time": "2026-01-01 00:30", "value": 100.100, "granularity": "api_live"},
        ]
        assert aggregate.find_glitch_episodes(rows, "value") == []

    def test_large_drop_still_counted_at_default_threshold(self):
        rows = [
            {"time": "2026-01-01 00:00", "value": 100.0, "granularity": "api_live"},
            {"time": "2026-01-01 00:15", "value": 0.0, "granularity": "api_live"},  # a real dropout, not noise
            {"time": "2026-01-01 00:30", "value": 105.0, "granularity": "api_live"},
        ]
        episodes = aggregate.find_glitch_episodes(rows, "value")
        assert len(episodes) == 1
        assert episodes[0]["magnitude"] == pytest.approx(100.0)

    def test_episode_does_not_cross_granularity_boundary(self):
        # Mirrors find_negative_deltas' equivalent fix -- a dip
        # detected only because two different sources' points were paired
        # together isn't a real glitch.
        rows = [
            {"time": "2026-01-01 00:00", "value": 500.0, "granularity": "live"},
            {"time": "2026-01-01 00:15", "value": 10.0, "granularity": "15min"},
            {"time": "2026-01-01 00:30", "value": 15.0, "granularity": "15min"},
        ]
        assert aggregate.find_glitch_episodes(rows, "value") == []


class TestFindGranularityDisagreements:
    def test_disagreement_beyond_tolerance_flagged(self):
        rows = [
            {"time": "2026-07-01 00:00", "value": 100.0, "granularity": "live"},
            {"time": "2026-07-01 23:45", "value": 110.0, "granularity": "live"},  # live day total = 10.0
            {"time": "2026-07-01 00:00", "value": 200.0, "granularity": "15min"},
            {"time": "2026-07-01 23:45", "value": 203.0, "granularity": "15min"},  # 15min day total = 3.0
        ]
        result = aggregate.find_granularity_disagreements(rows, "value")
        assert len(result) == 1
        assert result[0]["date"] == "2026-07-01"
        assert result[0]["by_granularity"] == {"live": pytest.approx(10.0), "15min": pytest.approx(3.0)}
        assert result[0]["diff_pct"] == pytest.approx(70.0)

    def test_close_agreement_not_flagged(self):
        rows = [
            {"time": "2026-07-01 00:00", "value": 100.0, "granularity": "live"},
            {"time": "2026-07-01 23:45", "value": 110.0, "granularity": "live"},  # 10.0
            {"time": "2026-07-01 00:00", "value": 200.0, "granularity": "15min"},
            {"time": "2026-07-01 23:45", "value": 209.5, "granularity": "15min"},  # 9.5 -> 5% diff
        ]
        assert aggregate.find_granularity_disagreements(rows, "value") == []

    def test_single_granularity_date_not_flagged(self):
        rows = [
            {"time": "2026-07-01 00:00", "value": 100.0, "granularity": "live"},
            {"time": "2026-07-01 23:45", "value": 110.0, "granularity": "live"},
        ]
        assert aggregate.find_granularity_disagreements(rows, "value") == []

    def test_daily_granularity_agreeing_with_fine_data_not_flagged(self):
        # REGRESSION: 'daily' data has exactly one row per date. The old
        # implementation bucketed points by (granularity, date) before
        # pairing them, so a daily bucket held a single point, no pair could
        # form, and its total came out 0.0 -- making every date carrying both
        # a daily row and a finer row read as a 100% disagreement even when
        # the two sources agreed exactly. Latent on the real DB (the daily
        # era ends before the 15min era starts) but reachable by uploading a
        # long-range daily export to backfill a gap, which the gap "Fix"
        # button actively invites.
        rows = [
            {"time": "2023-01-01 00:00", "value": 100.0, "granularity": "daily"},
            {"time": "2023-01-02 00:00", "value": 105.0, "granularity": "daily"},
            {"time": "2023-01-03 00:00", "value": 110.0, "granularity": "daily"},
            # 15min source over the same 2023-01-02, same real 5.0 of usage
            {"time": "2023-01-02 00:00", "value": 200.0, "granularity": "15min"},
            {"time": "2023-01-02 06:00", "value": 201.25, "granularity": "15min"},
            {"time": "2023-01-02 12:00", "value": 202.5, "granularity": "15min"},
            {"time": "2023-01-02 18:00", "value": 203.75, "granularity": "15min"},
            {"time": "2023-01-03 00:00", "value": 205.0, "granularity": "15min"},
        ]
        assert aggregate.find_granularity_disagreements(rows, "value") == []

    def test_partial_coverage_transition_day_not_flagged(self):
        # REGRESSION: on a source-migration day one poller stops and another
        # starts partway through, so each legitimately sees only its own
        # slice. Comparing their totals compared a part-day against a full
        # one and always "disagreed" -- every one of the 6 disagreements this
        # check reported against a real database was this artifact.
        rows = [
            # full-day source
            {"time": "2026-07-24 00:00", "value": 100.0, "granularity": "15min"},
            {"time": "2026-07-24 12:00", "value": 105.0, "granularity": "15min"},
            {"time": "2026-07-25 00:00", "value": 110.0, "granularity": "15min"},
            # new source starts at 14:00 -- only ~42% of the day
            {"time": "2026-07-24 14:00", "value": 500.0, "granularity": "api_live"},
            {"time": "2026-07-25 00:00", "value": 504.0, "granularity": "api_live"},
        ]
        assert aggregate.find_granularity_disagreements(rows, "value") == []

    def test_real_disagreement_on_fully_covered_day_still_flagged(self):
        # The coverage guard must not blunt the check itself: two sources
        # that both cover the whole day but disagree are still reported.
        rows = [
            {"time": "2026-07-01 00:00", "value": 100.0, "granularity": "live"},
            {"time": "2026-07-02 00:00", "value": 110.0, "granularity": "live"},
            {"time": "2026-07-01 00:00", "value": 200.0, "granularity": "15min"},
            {"time": "2026-07-02 00:00", "value": 203.0, "granularity": "15min"},
        ]
        result = aggregate.find_granularity_disagreements(rows, "value")
        assert len(result) == 1
        assert result[0]["date"] == "2026-07-01"
        assert result[0]["diff_pct"] == pytest.approx(70.0)


class TestFindOutOfRangeValues:
    def test_out_of_range_flagged(self):
        rows = [
            {"date": "2026-07-01", "value": -5.0},
            {"date": "2026-07-02", "value": 50.0},
            {"date": "2026-07-03", "value": 105.0},
        ]
        result = aggregate.find_out_of_range_values(rows, "value", 0.0, 100.0)
        assert result == [
            {"date": "2026-07-01", "value": -5.0, "metric": "value"},
            {"date": "2026-07-03", "value": 105.0, "metric": "value"},
        ]

    def test_all_in_range_returns_empty(self):
        rows = [{"date": "2026-07-01", "value": 50.0}]
        assert aggregate.find_out_of_range_values(rows, "value", 0.0, 100.0) == []


class TestGroupOutlierEpisodes:
    """A 16-day absence is ONE event, not 16 notes -- the same move
    already made for glitches."""

    def _o(self, d, ratio, direction="low", occupancy=None):
        return {
            "date": d,
            "value": 1.0,
            "baseline_median": 10.0,
            "ratio": ratio,
            "direction": direction,
            "occupancy": occupancy,
        }

    def test_consecutive_days_collapse_to_one_episode(self):
        outliers = [self._o(f"2026-01-{d:02d}", 0.1) for d in range(11, 27)]
        episodes = aggregate.group_outlier_episodes(outliers)
        assert len(episodes) == 1
        assert episodes[0]["start"] == "2026-01-11"
        assert episodes[0]["end"] == "2026-01-26"
        assert episodes[0]["days"] == 16

    def test_a_break_in_the_run_starts_a_new_episode(self):
        outliers = [self._o("2026-01-01", 0.1), self._o("2026-01-02", 0.1), self._o("2026-01-05", 0.1)]
        episodes = aggregate.group_outlier_episodes(outliers)
        assert [(e["start"], e["end"], e["days"]) for e in episodes] == [
            ("2026-01-01", "2026-01-02", 2),
            ("2026-01-05", "2026-01-05", 1),
        ]

    def test_opposite_directions_never_merge_across_adjacent_days(self):
        # A low day followed by a high day is two different phenomena, even
        # though the dates are consecutive.
        outliers = [self._o("2026-01-01", 0.1, "low"), self._o("2026-01-02", 5.0, "high")]
        episodes = aggregate.group_outlier_episodes(outliers)
        assert len(episodes) == 2
        assert {e["direction"] for e in episodes} == {"low", "high"}

    def test_month_boundary_is_still_consecutive(self):
        # Date arithmetic, not string adjacency -- 01-31 and 02-01 are one run.
        outliers = [self._o("2026-01-31", 0.1), self._o("2026-02-01", 0.1)]
        assert len(aggregate.group_outlier_episodes(outliers)) == 1

    def test_episode_reports_its_most_extreme_day_not_an_average(self):
        # Averaging a run that starts mild and ends severe would understate
        # exactly the episodes worth looking at.
        outliers = [
            self._o("2026-01-01", 0.20),
            self._o("2026-01-02", 0.05),
            self._o("2026-01-03", 0.15),
        ]
        episode = aggregate.group_outlier_episodes(outliers)[0]
        assert episode["peak_date"] == "2026-01-02"
        assert episode["ratio"] == pytest.approx(0.05)

    def test_peak_for_a_high_episode_is_the_largest_ratio(self):
        outliers = [self._o("2026-01-01", 3.5, "high"), self._o("2026-01-02", 9.0, "high")]
        episode = aggregate.group_outlier_episodes(outliers)[0]
        assert episode["peak_date"] == "2026-01-02"
        assert episode["ratio"] == pytest.approx(9.0)

    def test_occupancy_carried_only_when_every_day_agrees(self):
        agreed = [self._o("2026-01-01", 0.1, occupancy=0), self._o("2026-01-02", 0.1, occupancy=0)]
        assert aggregate.group_outlier_episodes(agreed)[0]["occupancy"] == 0

        # A run spanning a change reports None rather than letting one day's
        # headcount speak for the whole episode.
        mixed = [self._o("2026-01-01", 0.1, occupancy=0), self._o("2026-01-02", 0.1, occupancy=2)]
        assert aggregate.group_outlier_episodes(mixed)[0]["occupancy"] is None

        partly_logged = [self._o("2026-01-01", 0.1, occupancy=0), self._o("2026-01-02", 0.1)]
        assert aggregate.group_outlier_episodes(partly_logged)[0]["occupancy"] is None

    def test_empty_input_produces_no_episodes(self):
        assert aggregate.group_outlier_episodes([]) == []

    def test_unsorted_input_is_grouped_correctly(self):
        outliers = [self._o("2026-01-03", 0.1), self._o("2026-01-01", 0.1), self._o("2026-01-02", 0.1)]
        episodes = aggregate.group_outlier_episodes(outliers)
        assert len(episodes) == 1
        assert (episodes[0]["start"], episodes[0]["end"]) == ("2026-01-01", "2026-01-03")


class TestFindAwayDays:
    """The rule: gas and water both zero means nobody was home.
    Power is deliberately not part of it -- it never reaches zero, because the
    fridge, freezer and router draw continuously."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def _day(self, conn, d, gas, water, power=3.0):
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, ?)", (d, gas))
        conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, ?)", (d, water))
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES (?, ?, 0, 0)", (d, power)
        )
        conn.commit()

    def test_both_zero_is_an_away_day(self):
        conn = self._conn()
        self._day(conn, "2026-06-03", 0.0, 0.0)
        assert aggregate.find_away_days(conn) == {"2026-06-03"}

    def test_either_one_non_zero_is_not_away(self):
        conn = self._conn()
        self._day(conn, "2026-06-03", 0.0, 12.0)  # water used
        self._day(conn, "2026-06-04", 0.4, 0.0)  # gas used
        assert aggregate.find_away_days(conn) == set()

    def test_trickle_within_tolerance_is_still_away(self):
        # "a little tolerance is OK, because someone might enter to
        # check the house." Also covers the heating case -- the 23-day March
        # 2025 absence ran at 0.03-0.08 m3 of gas a day with nobody there, and
        # an exact-zero rule could not see it at all.
        conn = self._conn()
        self._day(conn, "2025-03-13", 0.08, 0.0)  # boiler ticking, no water
        self._day(conn, "2025-03-14", 0.05, 0.7)  # someone collected post
        assert aggregate.find_away_days(conn) == {"2025-03-13", "2025-03-14"}

    def test_real_activity_is_never_within_tolerance(self):
        # Days with contractor work in the house ran 42.7 L and 81.3 L. Those
        # must stay visible -- the slack is for a flush, not for a working day.
        conn = self._conn()
        self._day(conn, "2026-01-17", 0.12, 42.7)
        self._day(conn, "2026-01-19", 0.12, 81.3)
        assert aggregate.find_away_days(conn) == set()

    def test_tolerance_boundaries_are_inclusive(self):
        conn = self._conn()
        self._day(conn, "2026-06-01", aggregate.AWAY_MAX_GAS_M3, aggregate.AWAY_MAX_WATER_L)
        self._day(conn, "2026-06-02", aggregate.AWAY_MAX_GAS_M3 + 0.01, 0.0)
        self._day(conn, "2026-06-03", 0.0, aggregate.AWAY_MAX_WATER_L + 0.1)
        assert aggregate.find_away_days(conn) == {"2026-06-01"}

    def test_high_power_does_not_prevent_an_away_day(self):
        # The whole point of this rule -- an empty house still draws its
        # baseline load, and in winter (frost protection) that is several kWh.
        conn = self._conn()
        self._day(conn, "2026-02-10", 0.0, 0.0, power=5.9)
        assert aggregate.find_away_days(conn) == {"2026-02-10"}

    def test_missing_water_data_is_not_an_absence(self):
        # "No water data" is not "no water used" -- water history only begins
        # 2023-11-18 after an earlier data cleanup, and treating the gap as an empty
        # house would invent two years of absences.
        conn = self._conn()
        conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES ('2022-05-01', 0.0)")
        conn.commit()
        assert aggregate.find_away_days(conn) == set()

    def test_event_entirely_inside_an_absence_is_tagged_explained(self):
        conn = self._conn()
        # 20 ordinary days, then 3 with nothing at all, then 3 ordinary.
        start = date(2026, 6, 1)
        for i in range(26):
            d = (start + timedelta(days=i)).isoformat()
            self._day(conn, d, 0.0 if 20 <= i <= 22 else 0.4, 0.0 if 20 <= i <= 22 else 300.0)

        report = aggregate.consumption_notes_report(conn)
        explained = [e for e in report["events"] if e.get("explained_by")]
        assert len(explained) == 1
        assert explained[0]["explained_by"] == "nobody home"
        assert (explained[0]["start"], explained[0]["end"]) == ("2026-06-21", "2026-06-23")

    def test_explained_events_are_tagged_not_removed(self):
        # Tag-don't-filter, as everywhere else in this module: the count stays
        # honest and the user decides what to do about it.
        conn = self._conn()
        start = date(2026, 6, 1)
        for i in range(26):
            d = (start + timedelta(days=i)).isoformat()
            self._day(conn, d, 0.0 if 20 <= i <= 22 else 0.4, 0.0 if 20 <= i <= 22 else 300.0)

        report = aggregate.consumption_notes_report(conn)
        assert any(e.get("explained_by") for e in report["events"])
        # ...and the underlying notes are all still present and untouched.
        assert report["categories"]["gas"]["outlier_days"] != []


class TestSuggestAbsenceEntries:
    """A short trip taken inside a long visit is invisible to the log
    unless someone nests an entry by hand -- which is why 12 days inside a
    three-month visit went unrecorded."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def _days(self, conn, start, n, gas, water):
        d = date.fromisoformat(start)
        for _ in range(n):
            conn.execute("INSERT INTO gas_daily (date, usage_m3) VALUES (?, ?)", (d.isoformat(), gas))
            conn.execute("INSERT INTO water_daily (date, usage_l) VALUES (?, ?)", (d.isoformat(), water))
            d += timedelta(days=1)
        conn.commit()

    def _log(self, conn, a, b, count, notes=None):
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count, notes) VALUES (?, ?, ?, ?)",
            (f"{a} 00:00", f"{b} 23:59", count, notes),
        )
        conn.commit()

    def test_empty_stretch_inside_a_visit_is_suggested(self):
        # The real case: 3 empty days inside a long "2 people home" span.
        self_conn = self._conn()
        self._days(self_conn, "2026-06-01", 10, 0.4, 300.0)
        self._days(self_conn, "2026-06-11", 3, 0.02, 0.0)  # away
        self._days(self_conn, "2026-06-14", 10, 0.4, 300.0)
        self._log(self_conn, "2026-06-01", "2026-06-23", 2, "Mom visiting")

        out = aggregate.suggest_absence_entries(self_conn)
        assert len(out) == 1
        assert (out[0]["start"], out[0]["end"], out[0]["days"]) == ("2026-06-11", "2026-06-13", 3)
        assert out[0]["logged_counts"] == [2]

    def test_stretch_already_logged_as_empty_is_not_suggested(self):
        conn = self._conn()
        self._days(conn, "2026-06-01", 5, 0.4, 300.0)
        self._days(conn, "2026-06-06", 3, 0.0, 0.0)
        self._log(conn, "2026-06-01", "2026-06-05", 2)
        self._log(conn, "2026-06-06", "2026-06-08", 0, "already known")
        assert aggregate.suggest_absence_entries(conn) == []

    def test_unlogged_stretch_is_suggested_and_marked_unlogged(self):
        conn = self._conn()
        self._days(conn, "2026-06-01", 3, 0.0, 0.0)
        out = aggregate.suggest_absence_entries(conn)
        assert len(out) == 1
        assert out[0]["logged_counts"] == [None]

    def test_partly_logged_stretch_is_still_suggested(self):
        # A trip logged a day short is exactly the case worth correcting.
        conn = self._conn()
        self._days(conn, "2026-06-01", 5, 0.4, 300.0)
        self._days(conn, "2026-06-06", 4, 0.0, 0.0)
        self._log(conn, "2026-06-06", "2026-06-07", 0)  # covers only 2 of the 4
        out = aggregate.suggest_absence_entries(conn)
        assert len(out) == 1
        assert (out[0]["start"], out[0]["end"]) == ("2026-06-06", "2026-06-09")

    def test_separate_stretches_are_separate_suggestions(self):
        conn = self._conn()
        self._days(conn, "2026-06-01", 2, 0.0, 0.0)
        self._days(conn, "2026-06-03", 5, 0.4, 300.0)
        self._days(conn, "2026-06-08", 2, 0.0, 0.0)
        out = aggregate.suggest_absence_entries(conn)
        assert [(s["start"], s["end"]) for s in out] == [
            ("2026-06-01", "2026-06-02"),
            ("2026-06-08", "2026-06-09"),
        ]

    def test_nothing_suggested_when_there_is_no_data(self):
        assert aggregate.suggest_absence_entries(self._conn()) == []

    def test_suggesting_never_writes_anything(self):
        conn = self._conn()
        self._days(conn, "2026-06-01", 3, 0.0, 0.0)
        before = conn.execute("SELECT COUNT(*) FROM occupancy_log").fetchone()[0]
        aggregate.suggest_absence_entries(conn)
        assert conn.execute("SELECT COUNT(*) FROM occupancy_log").fetchone()[0] == before


class TestFindEvChargeDays:
    """The recurring 33-52 kWh days are confirmed EV charging.
    Identified by load SHAPE, not daily total -- see EV_CHARGE_MIN_KW."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def _readings(self, conn, day, hourly_kwh):
        """hourly_kwh: {hour: kWh drawn during that hour} -> cumulative rows."""
        cumulative = 1000.0
        for hour in range(25):
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) "
                "VALUES (?, ?, 'api_live')",
                (f"{day} {hour % 24:02d}:00" if hour < 24 else f"{day} 23:59", cumulative),
            )
            cumulative += hourly_kwh.get(hour, 0.1)
        conn.commit()

    def test_sustained_high_hour_is_a_charge_day(self):
        conn = self._conn()
        self._readings(conn, "2026-08-02", {11: 11.7, 12: 11.8, 13: 11.5})
        assert aggregate.find_ev_charge_days(conn) == {"2026-08-02"}

    def test_ordinary_day_is_not_a_charge_day(self):
        # The busiest non-charging hour in the development dataset is 1.30 kW.
        conn = self._conn()
        self._readings(conn, "2026-08-01", {21: 1.3})
        assert aggregate.find_ev_charge_days(conn) == set()

    def test_a_high_daily_total_alone_does_not_count(self):
        # THE distinction that matters. A cold winter day can total 60 kWh
        # from heating without ever exceeding a couple of kW in any one hour.
        # Judging the total would call that a charge; judging the shape does
        # not. That dataset has 65 days over 25 kWh and the winter ones are heating.
        conn = self._conn()
        self._readings(conn, "2026-12-15", {h: 2.5 for h in range(24)})  # 60 kWh, no spike
        assert aggregate.find_ev_charge_days(conn) == set()

    def test_days_without_raw_readings_are_never_charge_days(self):
        # "Cannot tell from a daily total" is not "did not happen" -- raw
        # power only exists from the live/api_live era onward.
        conn = self._conn()
        conn.execute(
            "INSERT INTO power_daily (date, import_kwh, export_kwh, net_kwh) VALUES ('2025-11-27', 65.93, 0, 0)"
        )
        conn.commit()
        assert aggregate.find_ev_charge_days(conn) == set()

    def test_meter_reset_sized_jump_is_not_a_charge(self):
        conn = self._conn()
        self._readings(conn, "2026-08-05", {14: 5000.0})  # implausible jump
        assert aggregate.find_ev_charge_days(conn) == set()

    def test_charge_only_explains_a_power_high_event(self):
        # A charge explains the import spike it caused and nothing else. It
        # must never wave away a water or gas anomaly on the same day.
        assert aggregate.EV_CHARGE_MIN_KW == 8.0


class TestGroupConsumptionEvents:
    """A week away is one event that shows up in three meters, not
    three separate facts."""

    def _n(self, start, end, direction="low", metric="usage_m3", acknowledged=False):
        return {
            "start": start,
            "end": end,
            "direction": direction,
            "metric": metric,
            "fingerprint": f"{start}|{metric}",
            "acknowledged": acknowledged,
        }

    def test_overlapping_episodes_across_categories_become_one_event(self):
        events = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-07", "2026-05-13")],
                "water": [self._n("2026-05-08", "2026-05-13", metric="usage_l")],
            }
        )
        assert len(events) == 1
        assert events[0]["categories"] == ["gas", "water"]
        assert (events[0]["start"], events[0]["end"], events[0]["days"]) == ("2026-05-07", "2026-05-13", 7)
        assert len(events[0]["parts"]) == 2

    def test_opposite_directions_never_merge_however_well_they_line_up(self):
        # Gas down while water is up is two things happening, and saying so is
        # the whole point of listing the categories.
        events = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-07", "2026-05-13", direction="low")],
                "water": [self._n("2026-05-07", "2026-05-13", direction="high", metric="usage_l")],
            }
        )
        assert len(events) == 2
        assert {e["direction"] for e in events} == {"low", "high"}

    def test_non_overlapping_episodes_stay_separate(self):
        events = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-01", "2026-05-03"), self._n("2026-05-10", "2026-05-12")],
            }
        )
        assert len(events) == 2

    def test_adjacency_alone_does_not_merge(self):
        # Ending the day before the next one starts is NOT an overlap. Merging
        # on adjacency would chain unrelated episodes into ever-longer runs
        # through a series of one-day touches.
        events = aggregate.group_consumption_events(
            {"gas": [self._n("2026-05-01", "2026-05-03"), self._n("2026-05-04", "2026-05-06")]}
        )
        assert len(events) == 2

    def test_merging_is_transitive_through_a_chain_of_overlaps(self):
        # A overlaps B, B overlaps C, A and C do not touch -- still one event.
        events = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-01", "2026-05-05")],
                "water": [self._n("2026-05-04", "2026-05-09", metric="usage_l")],
                "power": [self._n("2026-05-08", "2026-05-12", metric="import_kwh")],
            }
        )
        assert len(events) == 1
        assert events[0]["categories"] == ["gas", "power", "water"]
        assert (events[0]["start"], events[0]["end"]) == ("2026-05-01", "2026-05-12")

    def test_event_is_acknowledged_only_when_every_part_is(self):
        # A half-acknowledged event reading as acknowledged would hide the
        # parts still waiting to be looked at.
        partly = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-07", "2026-05-13", acknowledged=True)],
                "water": [self._n("2026-05-07", "2026-05-13", metric="usage_l", acknowledged=False)],
            }
        )
        assert partly[0]["acknowledged"] is False

        fully = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-07", "2026-05-13", acknowledged=True)],
                "water": [self._n("2026-05-07", "2026-05-13", metric="usage_l", acknowledged=True)],
            }
        )
        assert fully[0]["acknowledged"] is True

    def test_parts_keep_their_own_fingerprints(self):
        # Acknowledgement is never re-keyed by grouping -- every fingerprint
        # already in acknowledged_issues has to keep working.
        events = aggregate.group_consumption_events(
            {
                "gas": [self._n("2026-05-07", "2026-05-13")],
                "water": [self._n("2026-05-07", "2026-05-13", metric="usage_l")],
            }
        )
        assert sorted(p["fingerprint"] for p in events[0]["parts"]) == [
            "2026-05-07|usage_l",
            "2026-05-07|usage_m3",
        ]

    def test_empty_input_produces_no_events(self):
        assert aggregate.group_consumption_events({}) == []
        assert aggregate.group_consumption_events({"gas": [], "water": []}) == []


class TestDataQualityReport:
    """Integration tests for the two split reports.

    data_integrity_report = "is the data itself suspect?"
    consumption_notes_report = "was this day's usage unusual?" -- explicitly
    not a health signal, see the split rationale in aggregate.py."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def test_empty_db_returns_well_formed_report_for_every_category(self):
        conn = self._conn()
        report = aggregate.data_integrity_report(conn)
        notes = aggregate.consumption_notes_report(conn)
        for category in ("power", "gas", "water", "battery"):
            entry = report[category]
            assert entry["negative_deltas"] == {"count": 0, "items": []}
            assert entry["glitch_episodes"] == {"count": 0, "items": []}
            assert entry["granularity_disagreements"] == []
            assert entry["implausible_values"] == []

        # Consumption notes cover what the household actually drew.
        # Battery is absent entirely -- charge/discharge are internal
        # circulation, and reporting "battery: nothing unusual" would claim a
        # check this report does not make. Integrity still covers it above.
        assert set(notes["categories"]) == {"power", "gas", "water"}
        for category in ("power", "gas", "water"):
            assert notes["categories"][category]["outlier_days"] == []
        assert notes["events"] == []

    def test_consumption_notes_exclude_export_and_battery(self):
        # Export is a residual, not consumption -- it was 16 of 43 notes on
        # the 90-day view and none of them described household usage. Both it
        # and battery flow track sunshine rather than behaviour.
        assert aggregate._QUALITY_CATEGORIES["power"]["consumption_metrics"] == ["import_kwh"]
        assert aggregate._QUALITY_CATEGORIES["battery"]["consumption_metrics"] == []
        # ...but neither is dropped from integrity checking, which walks
        # outlier_metrics. These two keys must never be conflated.
        assert "export_kwh" in aggregate._QUALITY_CATEGORIES["power"]["outlier_metrics"]
        assert aggregate._QUALITY_CATEGORIES["battery"]["outlier_metrics"] == ["charge_kwh", "discharge_kwh"]

    def test_export_outlier_is_not_a_consumption_note(self):
        conn = self._conn()
        start = date(2026, 7, 1)
        cumulative = 1000.0
        for i in range(17):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_readings (time, export_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative),
            )
            cumulative += 32.0 if i == 14 else 4.0
        conn.commit()
        aggregate.rebuild_all(conn)

        notes = aggregate.consumption_notes_report(conn)
        assert [o for o in notes["categories"]["power"]["outlier_days"] if o["metric"] == "export_kwh"] == []

    def test_integrity_report_never_carries_outlier_days(self):
        # The whole point of the split: an unusual-usage day is not a data
        # integrity finding, and must not reappear in this report.
        conn = self._conn()
        start = date(2026, 7, 1)
        cumulative = 1000.0
        for i in range(17):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative),
            )
            cumulative += 32.0 if i == 14 else 4.0
        conn.commit()
        aggregate.rebuild_all(conn)

        integrity = aggregate.data_integrity_report(conn)
        assert "outlier_days" not in integrity["power"]
        # ...and the spike is still reported, just on the other report.
        notes = aggregate.consumption_notes_report(conn)
        assert [o["start"] for o in notes["categories"]["power"]["outlier_days"] if o["metric"] == "import_kwh"] == ["2026-07-15"]

    def test_findings_carry_fingerprint_and_acknowledged_flag(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-01-01 00:00', 100.0, 'api_live')"
        )
        conn.execute(
            "INSERT INTO gas_readings (time, total_gas_m3, granularity) VALUES ('2026-01-01 00:15', 50.0, 'api_live')"
        )
        conn.commit()

        report = aggregate.data_integrity_report(conn)
        finding = report["gas"]["negative_deltas"]["items"][0]
        assert finding["fingerprint"] == "2026-01-01 00:15|total_gas_m3"
        assert finding["acknowledged"] is False

        acked = {("gas", "negative_delta", "2026-01-01 00:15|total_gas_m3")}
        report2 = aggregate.data_integrity_report(conn, acknowledged=acked)
        finding2 = report2["gas"]["negative_deltas"]["items"][0]
        assert finding2["acknowledged"] is True
        # Tag-don't-filter: still present in the list, not hidden.
        assert report2["gas"]["negative_deltas"]["count"] == 1

    def test_same_timestamp_different_metrics_do_not_collide_on_fingerprint(self):
        # A regression once found: power's import and export columns live on the
        # same raw row, so a negative delta on both at the same timestamp
        # used to be indistinguishable by fingerprint before "metric" was
        # added to every finding, not just outlier_days/implausible_values.
        conn = self._conn()
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, export_combined_kwh, granularity) "
            "VALUES ('2026-01-01 00:00', 100.0, 200.0, 'api_live')"
        )
        conn.execute(
            "INSERT INTO power_readings (time, import_combined_kwh, export_combined_kwh, granularity) "
            "VALUES ('2026-01-01 00:15', 50.0, 150.0, 'api_live')"
        )
        conn.commit()

        report = aggregate.data_integrity_report(conn)
        fingerprints = {f["fingerprint"] for f in report["power"]["negative_deltas"]["items"]}
        assert fingerprints == {"2026-01-01 00:15|import_combined_kwh", "2026-01-01 00:15|export_combined_kwh"}

    def test_legitimate_spike_day_is_reported_not_suppressed(self):
        # End-to-end version of TestFindOutlierDays.test_spike_flagged_high,
        # through the real rebuild pipeline (already correctness-fixed) rather
        # than a synthetic daily-rows list -- confirms a real EV-charging-shaped
        # spike survives all the way to the report, unsuppressed, exactly
        # as required: this must never read as a false alarm
        # by hiding legitimate high-usage days.
        conn = self._conn()
        start = date(2026, 7, 1)
        cumulative = 1000.0
        # range(17), not 16: the spike day must not be the most recent daily
        # row -- find_outlier_days never evaluates that one (a follow-up fix:
        # a partial/incomplete "today" reads as a false low-flag otherwise),
        # so one more trailing day is needed to push the spike day off the end.
        for i in range(17):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative),
            )
            cumulative += 32.0 if i == 14 else 4.0
        conn.commit()

        aggregate.rebuild_all(conn)
        report = aggregate.consumption_notes_report(conn)

        spikes = [o for o in report["categories"]["power"]["outlier_days"] if o["metric"] == "import_kwh"]
        assert len(spikes) == 1
        # A lone spike day is a one-day episode: start == end == peak.
        assert spikes[0]["start"] == "2026-07-15"
        assert spikes[0]["end"] == "2026-07-15"
        assert spikes[0]["days"] == 1
        assert spikes[0]["peak_date"] == "2026-07-15"
        assert spikes[0]["direction"] == "high"
        assert spikes[0]["ratio"] == pytest.approx(8.0)

    def _seed_spike(self, conn):
        start = date(2026, 7, 1)
        cumulative = 1000.0
        for i in range(17):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative),
            )
            cumulative += 32.0 if i == 14 else 4.0
        conn.commit()
        aggregate.rebuild_all(conn)

    def test_consumption_note_carries_logged_occupancy(self):
        conn = self._conn()
        self._seed_spike(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-15 00:00', '2026-07-15 23:59', 3)"
        )
        conn.commit()

        notes = aggregate.consumption_notes_report(conn)
        spike = next(o for o in notes["categories"]["power"]["outlier_days"] if o["start"] == "2026-07-15")
        assert spike["occupancy"] == 3

    def test_consumption_note_occupancy_is_none_when_unlogged(self):
        # ~80% of real notes land on days the occupancy log says nothing
        # about, so None must be a normal, well-handled case -- not an
        # excuse to suppress or to guess a headcount.
        conn = self._conn()
        self._seed_spike(conn)

        notes = aggregate.consumption_notes_report(conn)
        spike = next(o for o in notes["categories"]["power"]["outlier_days"] if o["start"] == "2026-07-15")
        assert spike["occupancy"] is None

    def test_occupancy_annotation_never_suppresses_a_note(self):
        # Annotation only. A logged "nobody home" day explains a low reading
        # but must not remove it from the report -- the user decides what it
        # means, the code does not decide for them.
        conn = self._conn()
        self._seed_spike(conn)
        conn.execute(
            "INSERT INTO occupancy_log (date_from, date_to, occupant_count) "
            "VALUES ('2026-07-15 00:00', '2026-07-15 23:59', 0)"
        )
        conn.commit()

        notes = aggregate.consumption_notes_report(conn)
        dates = [o["start"] for o in notes["categories"]["power"]["outlier_days"] if o["metric"] == "import_kwh"]
        assert "2026-07-15" in dates

    # ---- date-range scoping ------------------------------------------------
    #
    # The endpoint used to ignore the range entirely -- the sole exception in
    # the app, and the actual reason it returned 794 notes while the charts
    # beside it showed 90 days.

    def _seed_low_run(self, conn):
        """20 flat days, then a 5-day near-zero run (2026-06-21..06-25),
        then 3 flat days again. Shaped like a real absence."""
        increments = [4.0] * 20 + [0.2] * 5 + [4.0] * 3
        start = date(2026, 6, 1)
        cumulative = 1000.0
        for i, inc in enumerate([*increments, 0.0]):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative),
            )
            cumulative += inc
        conn.commit()
        aggregate.rebuild_all(conn)

    def _import_notes(self, conn, **kwargs):
        report = aggregate.consumption_notes_report(conn, **kwargs)
        return [o for o in report["categories"]["power"]["outlier_days"] if o["metric"] == "import_kwh"]

    def test_unbounded_report_groups_the_run_into_one_episode(self):
        conn = self._conn()
        self._seed_low_run(conn)
        notes = self._import_notes(conn)
        assert len(notes) == 1
        assert (notes[0]["start"], notes[0]["end"], notes[0]["days"]) == ("2026-06-21", "2026-06-25", 5)

    def test_range_excludes_episodes_entirely_outside_it(self):
        conn = self._conn()
        self._seed_low_run(conn)
        assert self._import_notes(conn, date_from="2026-07-01", date_to="2026-07-31") == []
        assert self._import_notes(conn, date_from="2026-01-01", date_to="2026-06-10") == []

    def test_episode_overlapping_the_range_edge_is_returned_whole(self):
        # The start date IS the fingerprint. If a window edge could truncate a
        # run, an acknowledgement would appear to vanish purely because the
        # user changed the date preset -- so overlap returns the whole
        # episode, with its original start intact.
        conn = self._conn()
        self._seed_low_run(conn)
        for date_from, date_to in [
            ("2026-06-23", "2026-07-31"),  # range starts mid-run
            ("2026-01-01", "2026-06-22"),  # range ends mid-run
            ("2026-06-22", "2026-06-23"),  # range strictly inside the run
        ]:
            notes = self._import_notes(conn, date_from=date_from, date_to=date_to)
            assert len(notes) == 1
            assert notes[0]["start"] == "2026-06-21"
            assert notes[0]["end"] == "2026-06-25"
            assert notes[0]["days"] == 5

    def test_narrow_range_does_not_suppress_detection(self):
        # THE regression guard for this change. find_outlier_days builds a
        # TRAILING baseline (OUTLIER_MIN_HISTORY_DAYS before a day is eligible
        # at all), so the tempting implementation -- trim the daily rows to
        # the window, then detect -- silently changes which days get flagged.
        # Measured against the real database, a 7-day window returned 0 notes
        # instead of 4 that way: shorter than the eligibility floor, so the
        # shortest preset would have shown nothing at all, permanently.
        # Detection must therefore run over full history and only the OUTPUT
        # be scoped. This window is 7 days wide, i.e. narrower than the floor.
        conn = self._conn()
        self._seed_low_run(conn)
        notes = self._import_notes(conn, date_from="2026-06-20", date_to="2026-06-26")
        assert len(notes) == 1
        assert notes[0]["start"] == "2026-06-21"

    def test_range_scoping_does_not_change_an_episodes_own_numbers(self):
        # Scoping decides what is listed, never what a listed episode says.
        conn = self._conn()
        self._seed_low_run(conn)
        unbounded = self._import_notes(conn)[0]
        scoped = self._import_notes(conn, date_from="2026-06-23", date_to="2026-06-24")[0]
        assert scoped == unbounded

    def test_single_day_fingerprint_survives_the_move_to_episodes(self):
        # Grouping changed the fingerprint from "{date}|{metric}" to
        # "{start}|{metric}", which for a one-day episode is the same string.
        # Acknowledgements made before this change therefore still apply --
        # verified rather than assumed, because silently losing them would
        # look identical to the feature simply not working.
        conn = self._conn()
        self._seed_spike(conn)
        assert self._import_notes(conn)[0]["fingerprint"] == "2026-07-15|import_kwh"

        acknowledged = {("power", "outlier_day", "2026-07-15|import_kwh")}
        assert self._import_notes(conn, acknowledged=acknowledged)[0]["acknowledged"] is True

    def test_acknowledging_a_run_uses_its_start_date(self):
        conn = self._conn()
        self._seed_low_run(conn)
        acknowledged = {("power", "outlier_day", "2026-06-21|import_kwh")}
        notes = self._import_notes(conn, acknowledged=acknowledged)
        assert notes[0]["acknowledged"] is True
        # ...and an acknowledgement of a day *inside* the run does not apply,
        # which is the accepted cost of start-keying (a run extending forward
        # keeps its ack; one extending backward loses it).
        stale = {("power", "outlier_day", "2026-06-23|import_kwh")}
        assert self._import_notes(conn, acknowledged=stale)[0]["acknowledged"] is False


class TestReconcileDailyTotals:
    """The invariant check: a day's usage on a cumulative meter is closing
    minus opening, so a stored total that disagrees is provably wrong."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def _seed_power(self, conn, days=6, per_day=4.0):
        cumulative = 1000.0
        start = date(2026, 7, 1)
        for i in range(days):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, export_combined_kwh, granularity) "
                "VALUES (?, ?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative, cumulative / 2.0),
            )
            cumulative += per_day
        conn.commit()
        aggregate.rebuild_all(conn)

    def _seed_water(self, conn):
        cumulative = 500000.0
        start = date(2026, 7, 1)
        for i in range(6):
            d = start + timedelta(days=i)
            conn.execute(
                "INSERT INTO water_readings (time, water_usage_dl, granularity) VALUES (?, ?, 'api_live')",
                (f"{d.isoformat()} 00:00", cumulative),
            )
            cumulative += 1234.0
        conn.commit()
        aggregate.rebuild_all(conn)

    def test_clean_data_reconciles_exactly(self):
        conn = self._conn()
        self._seed_power(conn)
        rep = aggregate.reconcile_daily_totals(conn, tolerance_pct=0.0)
        assert rep["power"]["mismatches"] == []
        assert rep["power"]["unverifiable"] == []
        assert rep["power"]["verified"] > 0

    def test_double_counted_combined_field_is_caught(self):
        # A miniature of a real regression: the rebuild summed t1+t2+combined on rows where
        # the api_live poller populates all three, so every affected date
        # stored exactly twice the real usage. Every heuristic in this module
        # passed it; only a human comparing the meter by eye caught it.
        conn = self._conn()
        self._seed_power(conn)
        conn.execute("UPDATE power_daily SET import_kwh = import_kwh * 2 WHERE date = '2026-07-02'")
        conn.commit()

        rep = aggregate.reconcile_daily_totals(conn)
        mismatches = [m for m in rep["power"]["mismatches"] if m["metric"] == "import_kwh"]
        assert len(mismatches) == 1
        assert mismatches[0]["date"] == "2026-07-02"
        # stored = 2x expected -> diff / max(stored, expected) = 50%
        assert mismatches[0]["diff_pct"] == pytest.approx(50.0)
        assert mismatches[0]["stored"] == pytest.approx(2 * mismatches[0]["expected"])

    def test_newest_date_is_never_checked(self):
        # The rollup always lags the live meter by however long since it last
        # ran, so an in-progress day would mismatch on every single run.
        conn = self._conn()
        self._seed_power(conn)
        newest = conn.execute("SELECT MAX(date) AS d FROM power_daily").fetchone()["d"]
        conn.execute("UPDATE power_daily SET import_kwh = import_kwh * 99 WHERE date = ?", (newest,))
        conn.commit()

        rep = aggregate.reconcile_daily_totals(conn)
        assert [m["date"] for m in rep["power"]["mismatches"]] == []

    def test_meter_reset_date_is_unverifiable_not_wrong(self):
        conn = self._conn()
        for t, v in [
            ("2026-07-01 00:00", 100.0),
            ("2026-07-01 06:00", 110.0),
            ("2026-07-01 12:00", 50.0),  # reset, never recovers past 110
            ("2026-07-01 18:00", 55.0),
            ("2026-07-02 00:00", 60.0),
            ("2026-07-03 00:00", 65.0),
        ]:
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (t, v),
            )
        conn.commit()
        aggregate.rebuild_all(conn)

        rep = aggregate.reconcile_daily_totals(conn)
        reset_dates = [u["date"] for u in rep["power"]["unverifiable"] if u["metric"] == "import_kwh"]
        assert "2026-07-01" in reset_dates
        assert [m["date"] for m in rep["power"]["mismatches"]] == []

    def test_gap_over_span_cap_is_unverifiable_not_wrong(self):
        conn = self._conn()
        for t, v in [
            ("2026-07-01 00:00", 100.0),
            ("2026-07-01 12:00", 104.0),  # normal interval -> the date gets a daily row
            ("2026-07-03 00:00", 120.0),  # 36h later -> over MAX_DELTA_SPAN_HOURS
            ("2026-07-04 00:00", 124.0),
        ]:
            conn.execute(
                "INSERT INTO power_readings (time, import_combined_kwh, granularity) VALUES (?, ?, 'api_live')",
                (t, v),
            )
        conn.commit()
        aggregate.rebuild_all(conn)

        rep = aggregate.reconcile_daily_totals(conn)
        gap_dates = [u["date"] for u in rep["power"]["unverifiable"] if u["metric"] == "import_kwh"]
        assert "2026-07-01" in gap_dates
        assert [m["date"] for m in rep["power"]["mismatches"]] == []

    def test_water_unit_conversion_is_verified(self):
        # water stores dL raw and L daily. The check must reproduce the
        # rebuild's exact `/ 10.0`, not an algebraic equivalent -- expressing
        # it as `* 0.1` made 197 real dates disagree in the last float bit.
        conn = self._conn()
        self._seed_water(conn)
        rep = aggregate.reconcile_daily_totals(conn, tolerance_pct=0.0)
        assert rep["water"]["mismatches"] == []
        assert rep["water"]["verified"] > 0

    def test_wrong_unit_divisor_would_be_caught(self):
        # Regression guard for the historical /100-instead-of-/10 water bug.
        conn = self._conn()
        self._seed_water(conn)
        conn.execute("UPDATE water_daily SET usage_l = usage_l / 10.0 WHERE date = '2026-07-02'")
        conn.commit()

        rep = aggregate.reconcile_daily_totals(conn)
        assert [m["date"] for m in rep["water"]["mismatches"]] == ["2026-07-02"]

    def test_findings_carry_fingerprint_and_acknowledged_flag(self):
        conn = self._conn()
        self._seed_power(conn)
        conn.execute("UPDATE power_daily SET import_kwh = import_kwh * 2 WHERE date = '2026-07-02'")
        conn.commit()

        rep = aggregate.reconcile_daily_totals(conn)
        finding = rep["power"]["mismatches"][0]
        assert finding["fingerprint"] == "2026-07-02|import_kwh"
        assert finding["acknowledged"] is False

        acked = {("power", "reconciliation_mismatch", "2026-07-02|import_kwh")}
        rep2 = aggregate.reconcile_daily_totals(conn, acknowledged=acked)
        assert rep2["power"]["mismatches"][0]["acknowledged"] is True
        # Tag-don't-filter: still reported, not hidden.
        assert len(rep2["power"]["mismatches"]) == 1

    def test_empty_db_is_well_formed(self):
        conn = self._conn()
        rep = aggregate.reconcile_daily_totals(conn)
        for category in ("power", "gas", "water", "battery"):
            assert rep[category] == {"verified": 0, "mismatches": [], "unverifiable": []}


class TestFindLeadingEmptyRun:
    """A cumulative counter that has never registered anything means
    the meter was absent, not that nothing was used."""

    def _rows(self, values, start="2023-01-01"):
        d0 = date.fromisoformat(start)
        return [{"date": (d0 + timedelta(days=i)).isoformat(), "value": v} for i, v in enumerate(values)]

    def test_leading_zero_run_flagged(self):
        rows = self._rows([0.0] * 60 + [150.0] * 30)
        run = aggregate.find_leading_empty_run(rows, "value")
        assert run == {"start": "2023-01-01", "end": "2023-03-01", "days": 60}

    def test_mid_series_zero_run_never_flagged(self):
        # THE critical case. A household may be away for an extended period,
        # during which its gas legitimately reads 0.00 for weeks. A zero run
        # with real data on both sides is behaviour, not a
        # fault, and flagging it would recreate exactly the problem an
        # earlier fix addressed.
        rows = self._rows([150.0] * 30 + [0.0] * 70 + [150.0] * 30)
        assert aggregate.find_leading_empty_run(rows, "value") is None

    def test_short_leading_run_not_flagged(self):
        # A meter installed on a quiet day registers nothing for a little
        # while -- not worth reporting.
        rows = self._rows([0.0] * 5 + [150.0] * 30)
        assert aggregate.find_leading_empty_run(rows, "value") is None

    def test_all_zero_series_flagged(self):
        rows = self._rows([0.0] * 90)
        run = aggregate.find_leading_empty_run(rows, "value")
        assert run is not None
        assert run["days"] == 90

    def test_series_starting_with_real_data_not_flagged(self):
        rows = self._rows([150.0] * 60)
        assert aggregate.find_leading_empty_run(rows, "value") is None

    def test_empty_input(self):
        assert aggregate.find_leading_empty_run([], "value") is None

    def test_surfaces_in_the_integrity_report(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        # A water meter that only starts counting after 60 padded days.
        cumulative = 0.0
        d0 = date(2023, 1, 1)
        for i in range(60):
            conn.execute(
                "INSERT INTO water_readings (time, water_usage_dl, granularity) VALUES (?, 0.0, 'daily')",
                (f"{(d0 + timedelta(days=i)).isoformat()} 00:00",),
            )
        for i in range(60, 100):
            cumulative += 1500.0
            conn.execute(
                "INSERT INTO water_readings (time, water_usage_dl, granularity) VALUES (?, ?, 'daily')",
                (f"{(d0 + timedelta(days=i)).isoformat()} 00:00", cumulative),
            )
        conn.commit()
        aggregate.rebuild_all(conn)

        report = aggregate.data_integrity_report(conn)
        runs = report["water"]["empty_runs"]
        assert len(runs) == 1
        assert runs[0]["metric"] == "usage_l"
        assert runs[0]["days"] >= 30
        assert runs[0]["fingerprint"].endswith("|usage_l")
        assert runs[0]["acknowledged"] is False


class TestEnergyFlowMatrix:
    _FORBIDDEN_PAIRS = {("battery_discharge", "battery_charge"), ("grid_in", "grid_out")}

    def _flow(self, flows, frm, to):
        return sum(f["kwh"] for f in flows if f["from"] == frm and f["to"] == to)

    def test_conservation_row_and_column_sums_match_totals(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=14.3, discharge_kwh=26.1, import_kwh=19.7, charge_kwh=23.4, export_kwh=16.4
        )
        flows = result["flows"]
        for key, total in result["sources"].items():
            assert sum(f["kwh"] for f in flows if f["from"] == key) == pytest.approx(total, abs=1e-6)
        for key, total in result["uses"].items():
            assert sum(f["kwh"] for f in flows if f["to"] == key) == pytest.approx(total, abs=1e-6)
        assert sum(result["sources"].values()) == pytest.approx(sum(result["uses"].values()), abs=1e-6)

    def test_solar_covers_load_before_charging_battery(self):
        # Plenty of solar to cover load, charge, and still have some left for export.
        result = aggregate.energy_flow_matrix(
            solar_kwh=20.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=5.0, export_kwh=3.0
        )
        # load = 20 + 0 + 0 - 5 - 3 = 12
        assert self._flow(result["flows"], "solar", "load") == pytest.approx(12.0)
        assert self._flow(result["flows"], "solar", "battery_charge") == pytest.approx(5.0)
        assert self._flow(result["flows"], "solar", "grid_out") == pytest.approx(3.0)

    def test_solar_shortfall_leaves_remaining_load_to_battery_then_grid(self):
        # Not enough solar for load alone -- battery discharge and grid import
        # must cover the rest, in that priority order.
        result = aggregate.energy_flow_matrix(
            solar_kwh=2.0, discharge_kwh=3.0, import_kwh=10.0, charge_kwh=0.0, export_kwh=0.0
        )
        # load = 2 + 3 + 10 - 0 - 0 = 15
        flows = result["flows"]
        assert self._flow(flows, "solar", "load") == pytest.approx(2.0)
        assert self._flow(flows, "battery_discharge", "load") == pytest.approx(3.0)
        assert self._flow(flows, "grid_in", "load") == pytest.approx(10.0)

    def test_battery_discharge_covers_load_before_export(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=0.0, discharge_kwh=10.0, import_kwh=0.0, charge_kwh=0.0, export_kwh=4.0
        )
        # load = 0 + 10 + 0 - 0 - 4 = 6
        flows = result["flows"]
        assert self._flow(flows, "battery_discharge", "load") == pytest.approx(6.0)
        assert self._flow(flows, "battery_discharge", "grid_out") == pytest.approx(4.0)

    def test_grid_import_charges_battery_only_after_load_satisfied(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=0.0, discharge_kwh=0.0, import_kwh=10.0, charge_kwh=6.0, export_kwh=0.0
        )
        # load = 0 + 0 + 10 - 6 - 0 = 4
        flows = result["flows"]
        assert self._flow(flows, "grid_in", "load") == pytest.approx(4.0)
        assert self._flow(flows, "grid_in", "battery_charge") == pytest.approx(6.0)

    def test_forbidden_pairs_avoided_when_conservation_does_not_require_them(self):
        # Every source's leftover capacity has a physically-sensible use
        # available in this case, so pass 1 alone fully conserves and pass 2
        # (the only path that can produce a forbidden pair) never engages.
        result = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=5.0, import_kwh=5.0, charge_kwh=5.0, export_kwh=5.0
        )
        pairs_present = {(f["from"], f["to"]) for f in result["flows"]}
        assert pairs_present.isdisjoint(self._FORBIDDEN_PAIRS)
        assert result["fallback_kwh"] == 0.0

    def test_forbidden_pair_used_only_as_last_resort_to_keep_conservation(self):
        # These are the actual reference-image numbers from a real analysis.
        # Pass 1 alone leaves battery_discharge with 3.7 kWh
        # unplaceable (load and grid_out already exhausted by then) and
        # battery_charge with exactly 3.7 kWh unserved (grid_in exhausted) --
        # conservation is only achievable via the discharge-to-charge
        # fallback pair, so it must appear here, and with exactly that
        # amount. fallback_kwh must report the same 3.7 -- it's the field
        # the UI caveat is built on, so it has to match the actual fallback
        # flow exactly, not just be non-zero.
        result = aggregate.energy_flow_matrix(
            solar_kwh=14.3, discharge_kwh=26.1, import_kwh=19.7, charge_kwh=23.4, export_kwh=16.4
        )
        assert self._flow(result["flows"], "battery_discharge", "battery_charge") == pytest.approx(3.7, abs=1e-6)
        assert result["fallback_kwh"] == pytest.approx(3.7, abs=1e-6)
        for key, total in result["sources"].items():
            assert self._flow(result["flows"], key, "load") + self._flow(result["flows"], key, "battery_charge") + \
                self._flow(result["flows"], key, "grid_out") == pytest.approx(total, abs=1e-6)

    def test_no_pv_configured(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=0.0, discharge_kwh=5.0, import_kwh=10.0, charge_kwh=3.0, export_kwh=0.0
        )
        assert result["sources"]["solar"] == 0.0
        assert all(f["from"] != "solar" for f in result["flows"])

    def test_no_battery(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=0.0, import_kwh=5.0, charge_kwh=0.0, export_kwh=2.0
        )
        assert result["uses"]["battery_charge"] == 0.0
        assert result["sources"]["battery_discharge"] == 0.0
        assert all("battery" not in (f["from"], f["to"]) for f in result["flows"])

    def test_no_export(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=2.0, export_kwh=0.0
        )
        assert all(f["to"] != "grid_out" for f in result["flows"])

    def test_no_import(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=0.0, export_kwh=2.0
        )
        assert all(f["from"] != "grid_in" for f in result["flows"])

    def test_all_zero_input_returns_no_flows_and_no_divide_by_zero(self):
        result = aggregate.energy_flow_matrix(
            solar_kwh=0.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=0.0, export_kwh=0.0
        )
        assert result["flows"] == []
        assert result["unbalanced_kwh"] == 0.0
        assert result["uses"]["load"] == 0.0

    def test_negative_residual_load_clamped_and_reported(self):
        # charge + export (12) exceeds solar + discharge + import (5) by 7 --
        # a real shape when the battery charged from grid before the window
        # and discharged inside it.
        result = aggregate.energy_flow_matrix(
            solar_kwh=1.0, discharge_kwh=1.0, import_kwh=3.0, charge_kwh=8.0, export_kwh=4.0
        )
        assert result["uses"]["load"] == 0.0
        assert result["unbalanced_kwh"] == pytest.approx(7.0)

    def test_float_noise_tolerance(self):
        # Values that don't divide evenly -- conservation must still hold
        # within floating-point tolerance, not exactly.
        result = aggregate.energy_flow_matrix(
            solar_kwh=14.33333, discharge_kwh=26.10001, import_kwh=19.7777, charge_kwh=23.4001, export_kwh=16.41
        )
        flows = result["flows"]
        # abs=1e-2, not 1e-3: sources/uses totals round to 2dp while
        # individual flow amounts round to 4dp, so the two can legitimately
        # differ by up to half a cent of a kWh at this tolerance.
        for key, total in result["sources"].items():
            assert sum(f["kwh"] for f in flows if f["from"] == key) == pytest.approx(total, abs=1e-2)


class TestSumEnergyFlowMatrices:
    def test_sums_sources_uses_and_flows_across_days(self):
        day1 = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=0.0, import_kwh=2.0, charge_kwh=0.0, export_kwh=3.0
        )
        day2 = aggregate.energy_flow_matrix(
            solar_kwh=1.0, discharge_kwh=0.0, import_kwh=8.0, charge_kwh=0.0, export_kwh=0.0
        )
        result = aggregate.sum_energy_flow_matrices([day1, day2])
        assert result["sources"]["solar"] == pytest.approx(11.0)
        assert result["sources"]["grid_in"] == pytest.approx(10.0)
        assert result["uses"]["grid_out"] == pytest.approx(3.0)
        # Both days' solar->load and grid_in->load flows should merge into
        # single combined entries, not stay as separate per-day rows.
        solar_to_load = [f for f in result["flows"] if f["from"] == "solar" and f["to"] == "load"]
        assert len(solar_to_load) == 1

    def test_reproduces_the_live_export_bug_and_confirms_the_fix(self):
        # The actual shape found against real production data: a sunny day (solar
        # comfortably covers load and still exports) followed by a day with
        # no solar surplus (grid import covers load, nothing exported).
        # Summing period totals first and running one allocation would wrongly
        # force the whole export through grid_in -> grid_out (nothing else
        # left over by the time export needs a source); per-day allocation
        # correctly attributes it to solar on the day it actually happened.
        sunny_day = aggregate.energy_flow_matrix(
            solar_kwh=15.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=0.0, export_kwh=5.0
        )
        cloudy_day = aggregate.energy_flow_matrix(
            solar_kwh=0.0, discharge_kwh=0.0, import_kwh=10.0, charge_kwh=0.0, export_kwh=0.0
        )
        result = aggregate.sum_energy_flow_matrices([sunny_day, cloudy_day])
        flows = result["flows"]
        assert any(f["from"] == "solar" and f["to"] == "grid_out" and f["kwh"] == pytest.approx(5.0) for f in flows)
        assert not any(f["from"] == "grid_in" and f["to"] == "grid_out" for f in flows)
        assert result["fallback_kwh"] == 0.0

    def test_fallback_kwh_sums_across_days(self):
        # Mirrors the actual live finding: same-day import+export mixing
        # forces a fallback pair on one day but not the other -- the total
        # must reflect only the day that actually needed it.
        needs_fallback_day = aggregate.energy_flow_matrix(
            solar_kwh=14.3, discharge_kwh=26.1, import_kwh=19.7, charge_kwh=23.4, export_kwh=16.4
        )  # fallback_kwh 3.7, per the single-day test above
        clean_day = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=0.0, export_kwh=0.0
        )
        result = aggregate.sum_energy_flow_matrices([needs_fallback_day, clean_day])
        assert result["fallback_kwh"] == pytest.approx(3.7, abs=1e-6)

    def test_empty_list_returns_all_zero(self):
        result = aggregate.sum_energy_flow_matrices([])
        assert result["flows"] == []
        assert result["sources"] == {}
        assert result["unbalanced_kwh"] == 0.0
        assert result["fallback_kwh"] == 0.0

    def test_unbalanced_kwh_sums_across_days(self):
        day1 = aggregate.energy_flow_matrix(
            solar_kwh=1.0, discharge_kwh=1.0, import_kwh=3.0, charge_kwh=8.0, export_kwh=4.0
        )  # unbalanced 7.0, per the earlier single-day test
        day2 = aggregate.energy_flow_matrix(
            solar_kwh=10.0, discharge_kwh=0.0, import_kwh=0.0, charge_kwh=0.0, export_kwh=0.0
        )  # balanced
        result = aggregate.sum_energy_flow_matrices([day1, day2])
        assert result["unbalanced_kwh"] == pytest.approx(7.0)
