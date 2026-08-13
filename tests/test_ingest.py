
import pytest

from src import ingest


class TestDetectCategory:
    def test_battery(self):
        assert ingest.detect_category("Bat-2026-1-01-2026-7-14.csv") == "battery"

    def test_power(self):
        assert ingest.detect_category("P1e-2026-1-01-2026-7-14.csv") == "power"

    def test_gas(self):
        assert ingest.detect_category("P1g-2026-1-01-2026-7-14.csv") == "gas"

    def test_water(self):
        assert ingest.detect_category("Water-2026-1-01-2026-7-14.csv") == "water"

    def test_unrecognized(self):
        assert ingest.detect_category("random-file.csv") is None


class TestParseFloat:
    def test_empty_string_is_none(self):
        assert ingest.parse_float("") is None

    def test_none_is_none(self):
        assert ingest.parse_float(None) is None

    def test_valid_number(self):
        assert ingest.parse_float("123.456") == pytest.approx(123.456)

    def test_invalid_string_is_none(self):
        assert ingest.parse_float("not-a-number") is None


class TestDetectGranularity:
    def test_fifteen_minute(self):
        rows = [{"time": "2026-01-01 00:00"}, {"time": "2026-01-01 00:15"}]
        assert ingest.detect_granularity(rows) == "15min"

    def test_daily(self):
        rows = [{"time": "2026-01-01 00:00"}, {"time": "2026-01-02 00:00"}]
        assert ingest.detect_granularity(rows) == "daily"

    def test_single_row_defaults_to_fifteen_min(self):
        assert ingest.detect_granularity([{"time": "2026-01-01 00:00"}]) == "15min"


class TestIngestFile:
    def _write_csv(self, tmp_path, name, content):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_ingest_power_file(self, tmp_path):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        content = (
            "time,Import T1 kWh,Import T2 kWh,Export T1 kWh,Export T2 kWh,L1 max W,L2 max W,L3 max W\n"
            "2026-01-01 00:00,100.0,200.0,10.0,20.0,50,60,70\n"
            "2026-01-01 00:15,100.5,200.2,10.0,20.0,55,61,72\n"
        )
        path = self._write_csv(tmp_path, "P1e-2026-1-01-2026-7-14.csv", content)

        count = ingest.ingest_file(conn, path)
        assert count == 2

        rows = conn.execute("SELECT * FROM power_readings ORDER BY time").fetchall()
        assert len(rows) == 2
        assert rows[0]["import_t1_kwh"] == pytest.approx(100.0)
        assert rows[0]["granularity"] == "15min"

    def test_reingest_unchanged_file_is_skipped(self, tmp_path):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        content = "time,Total gas used\n2026-01-01 00:00,100.0\n2026-01-01 00:15,100.1\n"
        path = self._write_csv(tmp_path, "P1g-2026-1-01-2026-7-14.csv", content)

        first = ingest.ingest_file(conn, path)
        second = ingest.ingest_file(conn, path)
        assert first == 2
        assert second == 0

    def test_reingest_changed_file_reingests(self, tmp_path):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        path = self._write_csv(
            tmp_path, "Water-2026-1-01-2026-7-14.csv", "time,water usage dl\n2026-01-01 00:00,100\n"
        )
        first = ingest.ingest_file(conn, path)
        assert first == 1

        path.write_text(
            "time,water usage dl\n2026-01-01 00:00,100\n2026-01-01 00:15,101\n", encoding="utf-8"
        )
        second = ingest.ingest_file(conn, path)
        assert second == 2

    def test_unrecognized_file_raises_instead_of_silently_skipping(self, tmp_path):
        # Used to return 0 here with zero indication anywhere that the file
        # was ever seen -- a real gap given the README frames this as
        # "judged against the HomeWizard format and rejected" (rejected
        # implies feedback). Now raises, so scan_and_ingest's caller
        # (ingest_cli.py) reports it and moves it to failed/.
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        path = self._write_csv(tmp_path, "unknown.csv", "a,b\n1,2\n")
        with pytest.raises(ValueError, match="unrecognized filename"):
            ingest.ingest_file(conn, path)

    def test_header_drift_raises_instead_of_silently_ingesting_nothing(self, tmp_path):
        # A renamed/missing column must not "succeed" with 0 usable
        # values -- every r.get(...) in _gas_rows would return None here
        # since the real header is "Total gas used", not "Gas total".
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        path = self._write_csv(
            tmp_path, "P1g-2026-1-01-2026-7-14.csv", "time,Gas total\n2026-01-01 00:00,100.0\n2026-01-01 00:15,100.1\n"
        )
        with pytest.raises(ValueError, match="no usable values"):
            ingest.ingest_file(conn, path)

    def test_genuine_column_still_ingests_when_others_are_blank(self, tmp_path):
        # Guards against the header-drift check being too strict: a
        # single-phase household with no L2/L3 wiring has real import/export
        # values but legitimately blank L2/L3 columns every row -- must not
        # be mistaken for header drift.
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        content = (
            "time,Import T1 kWh,Import T2 kWh,Export T1 kWh,Export T2 kWh,L1 max W,L2 max W,L3 max W\n"
            "2026-01-01 00:00,100.0,200.0,10.0,20.0,50,,\n"
            "2026-01-01 00:15,100.5,200.2,10.0,20.0,55,,\n"
        )
        path = self._write_csv(tmp_path, "P1e-2026-1-01-2026-7-14.csv", content)
        assert ingest.ingest_file(conn, path) == 2


class TestScanAndIngest:
    def _write_csv(self, tmp_path, name, content):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_malformed_file_does_not_block_other_files(self, tmp_path):
        # A code review found a single file whose 'time' column can't be
        # parsed used to raise out of scan_and_ingest entirely, silently
        # skipping every alphabetically-later file on every 15-min run until
        # someone noticed and removed it by hand.
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        # Two rows needed: detect_granularity only parses the timestamp once
        # it has 2+ rows to compare (a single-row file short-circuits to
        # "15min" without ever calling strptime).
        self._write_csv(tmp_path, "Bat-bad.csv", "time,Import kWh\nnot-a-timestamp,1.0\nalso-bad,2.0\n")
        self._write_csv(
            tmp_path, "Water-good.csv", "time,water usage dl\n2026-01-01 00:00,100\n2026-01-01 00:15,101\n"
        )

        summary, errors = ingest.scan_and_ingest(conn, tmp_path)
        assert summary == {"Water-good.csv": 2}
        assert "Bat-bad.csv" in errors

    def test_failing_file_moved_to_failed_dir_not_retried_forever(self, tmp_path):
        # Mirrors app.py's api_import_csv() upload route, which already does
        # this and says why: "a file that crashes ingest would otherwise be
        # retried (and fail) on every 15-min timer run from now on." That
        # was true here too until this test's fix -- a dropzone file (as
        # opposed to a web upload) just sat in place and re-failed every
        # cycle, forever, with no way to acknowledge it short of deleting it.
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        self._write_csv(tmp_path, "Bat-bad.csv", "time,Import kWh\nnot-a-timestamp,1.0\nalso-bad,2.0\n")

        summary, errors = ingest.scan_and_ingest(conn, tmp_path)
        assert summary == {}
        assert "Bat-bad.csv" in errors
        assert "moved to failed/" in errors["Bat-bad.csv"]
        assert not (tmp_path / "Bat-bad.csv").exists()
        assert (tmp_path / "failed" / "Bat-bad.csv").exists()

        # And it must not be retried on the next scan -- it's gone from the
        # dropzone, not re-discovered from failed/.
        summary2, errors2 = ingest.scan_and_ingest(conn, tmp_path)
        assert summary2 == {}
        assert errors2 == {}

    def test_unrecognized_filename_reported_and_moved_not_silently_skipped(self, tmp_path):
        # The gap this whole round of testing was chasing: a CSV with
        # perfectly valid columns but a filename matching no known pattern
        # used to vanish with zero trace anywhere -- no error, no log line,
        # "no new or changed files" printed as if the dropzone were empty.
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        self._write_csv(tmp_path, "wrong-prefix-test.csv", "time,import_t1_kwh\n2026-01-01 00:00,100\n")

        summary, errors = ingest.scan_and_ingest(conn, tmp_path)
        assert summary == {}
        assert "wrong-prefix-test.csv" in errors
        assert "unrecognized filename" in errors["wrong-prefix-test.csv"]
        assert (tmp_path / "failed" / "wrong-prefix-test.csv").exists()

    def test_no_errors_when_all_files_valid(self, tmp_path):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        self._write_csv(tmp_path, "Water-good.csv", "time,water usage dl\n2026-01-01 00:00,100\n")
        summary, errors = ingest.scan_and_ingest(conn, tmp_path)
        assert summary == {"Water-good.csv": 1}
        assert errors == {}


class TestStripLeadingEmptyRows:
    """Collapse a leading run of all-zero readings to one anchor.

    Row shape is (time, *value_columns, granularity) -- same positional
    convention _all_values_none relies on."""

    def _row(self, t, *vals):
        return (t, *vals, "daily")

    def test_leading_zero_run_collapsed_to_single_anchor(self):
        values = [
            self._row("2023-01-01 00:00", 0.0),
            self._row("2023-01-02 00:00", 0.0),
            self._row("2023-01-03 00:00", 0.0),
            self._row("2023-01-04 00:00", 56.0),
            self._row("2023-01-05 00:00", 133.0),
        ]
        kept, dropped = ingest.strip_leading_empty_rows(values)
        assert dropped == 2
        assert [r[0] for r in kept] == [
            "2023-01-03 00:00",
            "2023-01-04 00:00",
            "2023-01-05 00:00",
        ]

    def test_collapse_is_lossless(self):
        # The anchor is the whole point: consecutive zeros contribute zero
        # deltas, but the 0 -> first-real-reading delta is genuine usage by a
        # new meter and must survive.
        values = [self._row(f"2023-01-{i:02d} 00:00", 0.0) for i in range(1, 6)]
        values += [self._row("2023-01-06 00:00", 56.0), self._row("2023-01-07 00:00", 133.0)]
        kept, _ = ingest.strip_leading_empty_rows(values)

        def total(rows):
            return sum(max(0.0, b[1] - a[1]) for a, b in zip(rows, rows[1:], strict=False))

        assert total(kept) == total(values)

    def test_file_starting_with_real_data_untouched(self):
        values = [
            self._row("2023-01-01 00:00", 56.0),
            self._row("2023-01-02 00:00", 133.0),
        ]
        kept, dropped = ingest.strip_leading_empty_rows(values)
        assert dropped == 0
        assert kept == values

    def test_all_zero_file_keeps_one_anchor(self):
        # An export covering only a period before the meter existed -- the
        # exact shape of a real 2021-2022 water export.
        values = [self._row(f"2023-01-{i:02d} 00:00", 0.0) for i in range(1, 11)]
        kept, dropped = ingest.strip_leading_empty_rows(values)
        assert dropped == 9
        assert len(kept) == 1
        assert kept[0][0] == "2023-01-10 00:00"

    def test_none_values_count_as_empty(self):
        values = [
            self._row("2023-01-01 00:00", None),
            self._row("2023-01-02 00:00", 0.0),
            self._row("2023-01-03 00:00", 12.0),
        ]
        kept, dropped = ingest.strip_leading_empty_rows(values)
        assert dropped == 1
        assert [r[0] for r in kept] == ["2023-01-02 00:00", "2023-01-03 00:00"]

    def test_multi_column_row_needs_every_column_zero(self):
        # power rows carry several value columns; a row is only "empty" if
        # none of them has moved.
        values = [
            self._row("2023-01-01 00:00", 0.0, 0.0, 0.0),
            self._row("2023-01-02 00:00", 0.0, 0.0, 0.0),
            self._row("2023-01-03 00:00", 0.0, 4.0, 0.0),  # one column moved
            self._row("2023-01-04 00:00", 0.0, 8.0, 0.0),
        ]
        kept, dropped = ingest.strip_leading_empty_rows(values)
        assert dropped == 1
        assert [r[0] for r in kept] == [
            "2023-01-02 00:00",
            "2023-01-03 00:00",
            "2023-01-04 00:00",
        ]

    def test_empty_input(self):
        assert ingest.strip_leading_empty_rows([]) == ([], 0)


class TestStripTrailingEmptyRows:
    """Drop a trailing run of all-zero readings -- export padding past the
    last real reading. Same row shape as TestStripLeadingEmptyRows."""

    def _row(self, t, *vals):
        return (t, *vals, "daily")

    def test_trailing_zero_run_dropped_outright(self):
        # No anchor is retained, unlike the leading case -- nothing after the
        # run has a delta that could depend on it.
        values = [
            self._row("2023-01-01 00:00", 56.0),
            self._row("2023-01-02 00:00", 133.0),
            self._row("2023-01-03 00:00", 0.0),
            self._row("2023-01-04 00:00", 0.0),
        ]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 2
        assert [r[0] for r in kept] == ["2023-01-01 00:00", "2023-01-02 00:00"]

    def test_drop_is_lossless(self):
        values = [self._row("2023-01-01 00:00", 56.0), self._row("2023-01-02 00:00", 133.0)]
        padded = values + [self._row(f"2023-01-{i:02d} 00:00", 0.0) for i in range(3, 8)]
        kept, _ = ingest.strip_trailing_empty_rows(padded)

        def total(rows):
            return sum(max(0.0, b[1] - a[1]) for a, b in zip(rows, rows[1:], strict=False))

        assert total(kept) == total(values)

    def test_mid_series_zeros_kept(self):
        # Only a *trailing* run is padding. A dip that recovers is a meter
        # dropout, and clean_cumulative_glitches -- not this -- handles it.
        values = [
            self._row("2023-01-01 00:00", 56.0),
            self._row("2023-01-02 00:00", 0.0),
            self._row("2023-01-03 00:00", 133.0),
        ]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 0
        assert kept == values

    def test_file_ending_with_real_data_untouched(self):
        values = [
            self._row("2023-01-01 00:00", 56.0),
            self._row("2023-01-02 00:00", 133.0),
        ]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 0
        assert kept == values

    def test_all_zero_file_left_for_leading_pass(self):
        # Must NOT empty the list -- strip_leading_empty_rows runs after and
        # still has to see the full run to retain its anchor.
        values = [self._row(f"2023-01-{i:02d} 00:00", 0.0) for i in range(1, 6)]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 0
        assert kept == values

    def test_none_values_count_as_empty(self):
        values = [
            self._row("2023-01-01 00:00", 12.0),
            self._row("2023-01-02 00:00", 0.0),
            self._row("2023-01-03 00:00", None),
        ]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 2
        assert [r[0] for r in kept] == ["2023-01-01 00:00"]

    def test_multi_column_row_needs_every_column_zero(self):
        # A battery row's soc_pct can legitimately read 0 (empty battery);
        # the row only counts as padding if the cumulative columns are 0 too.
        values = [
            self._row("2023-01-01 00:00", 4.0, 2.0, 50.0),
            self._row("2023-01-02 00:00", 4.0, 2.0, 0.0),  # discharged, still real
            self._row("2023-01-03 00:00", 0.0, 0.0, 0.0),
        ]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 1
        assert [r[0] for r in kept] == ["2023-01-01 00:00", "2023-01-02 00:00"]

    def test_empty_input(self):
        assert ingest.strip_trailing_empty_rows([]) == ([], 0)

    def test_mid_day_export_padding_shape(self):
        # The shape that motivated this: a mid-day export padding every
        # remaining fifteen-minute slot of the day with 0.
        values = [("2023-01-01 11:00", 40000.0, "15min")]
        values += [(f"2023-01-01 {11 + i // 4:02d}:{15 * (i % 4):02d}", 0.0, "15min") for i in range(1, 52)]
        kept, dropped = ingest.strip_trailing_empty_rows(values)
        assert dropped == 51
        assert kept == [("2023-01-01 11:00", 40000.0, "15min")]


class TestGenericCsvFormat:
    """Vendor-neutral import: the way data from a meter this app has no
    driver for gets in at all. Columns are the *_readings column names, so
    no brand-specific parsing is involved."""

    def _conn(self):
        import sqlite3

        from src import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def _write(self, tmp_path, name, content):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_filename_prefix_selects_generic_format(self):
        assert ingest.detect_category("omnimeter-power-anything.csv") == "power"
        assert ingest.detect_category("omnimeter-battery-2026.csv") == "battery"
        assert ingest.detect_generic_category("P1e-2026.csv") is None

    def test_prefix_is_case_insensitive(self):
        assert ingest.detect_category("OmniMeter-Gas-x.csv") == "gas"

    def test_imports_tariff_split_power(self, tmp_path):
        conn = self._conn()
        p = self._write(
            tmp_path,
            "omnimeter-power-a.csv",
            "time,import_t1_kwh,export_t1_kwh\n2026-01-01 00:00,10.0,5.0\n2026-01-02 00:00,11.0,6.0\n",
        )
        assert ingest.ingest_file(conn, p) == 2
        row = conn.execute("SELECT * FROM power_readings ORDER BY time").fetchone()
        assert row["import_t1_kwh"] == 10.0
        assert row["export_t1_kwh"] == 5.0

    def test_combined_only_meter_is_supported(self, tmp_path):
        # A meter with no dual-tariff split -- the normal case outside a
        # dual-tariff market. The HomeWizard-format INSERT cannot express
        # this at all, which is why generic_insert_sql exists.
        conn = self._conn()
        p = self._write(
            tmp_path,
            "omnimeter-power-b.csv",
            "time,import_combined_kwh\n2026-01-01 00:00,100.0\n2026-01-02 00:00,105.0\n",
        )
        assert ingest.ingest_file(conn, p) == 2
        row = conn.execute("SELECT * FROM power_readings ORDER BY time").fetchone()
        assert row["import_combined_kwh"] == 100.0
        assert row["import_t1_kwh"] is None

    def test_all_four_categories(self, tmp_path):
        cases = {
            "omnimeter-gas-x.csv": ("time,total_gas_m3\n2026-01-01 00:00,100.5\n", "gas_readings"),
            "omnimeter-water-x.csv": ("time,water_usage_dl\n2026-01-01 00:00,1000\n", "water_readings"),
            "omnimeter-battery-x.csv": (
                "time,import_kwh,export_kwh,soc_pct\n2026-01-01 00:00,1.0,2.0,55\n",
                "battery_readings",
            ),
        }
        for name, (content, table) in cases.items():
            conn = self._conn()
            p = self._write(tmp_path, name, content)
            assert ingest.ingest_file(conn, p) == 1
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 1

    def test_unknown_column_is_rejected_not_ignored(self, tmp_path):
        # THE point of the strict rule: a typo must be reported, not
        # silently dropped along with that column's data.
        conn = self._conn()
        p = self._write(
            tmp_path,
            "omnimeter-power-c.csv",
            "time,import_t1_kwh,exprot_t1_kwh\n2026-01-01 00:00,10.0,5.0\n",
        )
        with pytest.raises(ingest.GenericCsvError, match="exprot_t1_kwh"):
            ingest.ingest_file(conn, p)
        assert conn.execute("SELECT COUNT(*) AS n FROM power_readings").fetchone()["n"] == 0

    def test_missing_time_column_rejected(self, tmp_path):
        conn = self._conn()
        p = self._write(tmp_path, "omnimeter-power-d.csv", "timestamp,import_t1_kwh\n2026-01-01 00:00,10.0\n")
        with pytest.raises(ingest.GenericCsvError, match="time"):
            ingest.ingest_file(conn, p)

    def test_time_only_file_rejected(self, tmp_path):
        conn = self._conn()
        p = self._write(tmp_path, "omnimeter-power-e.csv", "time\n2026-01-01 00:00\n")
        with pytest.raises(ingest.GenericCsvError, match="no value columns"):
            ingest.ingest_file(conn, p)

    def test_blank_time_value_rejected(self, tmp_path):
        conn = self._conn()
        p = self._write(tmp_path, "omnimeter-power-f.csv", "time,import_t1_kwh\n,10.0\n")
        with pytest.raises(ingest.GenericCsvError, match="row 2"):
            ingest.ingest_file(conn, p)

    def test_generic_insert_sql_matches_declared_columns(self):
        for category, columns in ingest.GENERIC_COLUMNS.items():
            sql = ingest.generic_insert_sql(category)
            assert sql.count("?") == len(columns) + 2  # time + values + granularity
            for c in columns:
                assert c in sql
