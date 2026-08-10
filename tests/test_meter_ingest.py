import sqlite3

import pytest

from src import db
from src import homewizard_api_client as hwc
from src import meter_ingest as api_ingest


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


@pytest.fixture(autouse=True)
def _all_devices_configured(monkeypatch):
    # ingest_all() now checks device.is_configured() per device before
    # fetching (devices are paired one at a time in
    # production, so DEVICES' real ip/serial values change independently of
    # these tests). Pin all three to a known "configured" state here,
    # including the v2 devices' Bearer tokens (HomeWizardDevice.is_configured()
    # now also requires a token for v2
    # devices) so tests below aren't coupled to whatever's actually filled in
    # at any given moment -- device-not-ready behavior gets its own test that
    # deliberately overrides one back to unready.
    for name in hwc.DEVICES:
        monkeypatch.setitem(hwc.DEVICES, name, {**hwc.DEVICES[name], "ip": "192.0.2.99", "serial": "abc123"})
    monkeypatch.setenv("OMNIMETER_P1_TOKEN", "tok-p1")
    monkeypatch.setenv("OMNIMETER_BATTERY_TOKEN", "tok-battery")


class TestFieldMapping:
    def test_map_p1(self):
        measurement = {
            "energy_import_t1_kwh": 100.0,
            "energy_import_t2_kwh": 50.0,
            "energy_import_kwh": 150.0,
            "energy_export_t1_kwh": 1.0,
            "energy_export_t2_kwh": 2.0,
            "energy_export_kwh": 3.0,
            "power_l1_w": 400,
            "power_l2_w": 500,
            "power_l3_w": 600,
        }
        row = api_ingest._map_p1(measurement)
        assert row == {
            "import_t1_kwh": 100.0,
            "import_t2_kwh": 50.0,
            "import_combined_kwh": 150.0,
            "export_t1_kwh": 1.0,
            "export_t2_kwh": 2.0,
            "export_combined_kwh": 3.0,
            "l1_max_w": 400,
            "l2_max_w": 500,
            "l3_max_w": 600,
        }

    def test_map_p1_missing_fields_become_none(self):
        assert api_ingest._map_p1({})["import_combined_kwh"] is None

    def test_map_battery_extracts_cumulative_kwh(self):
        # Confirmed live 2026-07-28: the battery's own /api/measurement does
        # return cumulative energy_import_kwh/energy_export_kwh, same as
        # HA's equivalent sensors -- an earlier version of this mapper
        # assumed otherwise and left both None.
        row = api_ingest._map_battery(
            {
                "state_of_charge_pct": 72.5,
                "power_w": -300,
                "cycles": 12,
                "energy_import_kwh": 936.578,
                "energy_export_kwh": 684.915,
            }
        )
        assert row == {"import_kwh": 936.578, "export_kwh": 684.915, "soc_pct": 72.5}

    def test_map_battery_missing_fields_become_none(self):
        assert api_ingest._map_battery({}) == {"import_kwh": None, "export_kwh": None, "soc_pct": None}

    def test_map_watermeter_converts_m3_to_deciliters(self):
        row = api_ingest._map_watermeter({"total_liter_m3": 12.345})
        assert row["water_usage_dl"] == pytest.approx(123450.0)

    def test_map_watermeter_missing_field_is_none(self):
        assert api_ingest._map_watermeter({})["water_usage_dl"] is None

    def test_map_gas_extracts_from_external_gas_meter_entry(self):
        # Gas isn't a separate device -- DSMR smart meters relay the gas
        # meter reading through the P1 telegram, so it rides on the P1
        # device's own measurement response.
        measurement = {
            "energy_import_kwh": 150.0,
            "external": [{"type": "gas_meter", "value": 7930.423, "unit": "m3", "timestamp": "2026-07-28T01:05:00"}],
        }
        assert api_ingest._map_gas(measurement) == {"total_gas_m3": 7930.423}

    def test_map_gas_returns_none_when_no_gas_meter_entry(self):
        assert api_ingest._map_gas({"external": []}) is None
        assert api_ingest._map_gas({}) is None

    def test_map_gas_ignores_non_gas_external_entries(self):
        measurement = {"external": [{"type": "some_other_device", "value": 1.0}]}
        assert api_ingest._map_gas(measurement) is None


class TestIngestAll:
    def test_writes_ok_rows_at_api_live_granularity(self, monkeypatch):
        conn = _conn()

        def fake_fetch(device_name, token):
            return {
                "p1": {
                    "energy_import_kwh": 10.0,
                    "external": [{"type": "gas_meter", "value": 7930.423}],
                },
                "battery": {"state_of_charge_pct": 55.0, "energy_import_kwh": 936.578, "energy_export_kwh": 684.915},
            }[device_name]

        monkeypatch.setattr(hwc, "fetch_measurement", fake_fetch)
        # watermeter is protocol='v1' -- no token, goes through fetch_measurement_v1 instead.
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        results = api_ingest.ingest_all(conn, hwc.all_devices())

        assert results == {"p1": "ok", "battery": "ok", "watermeter": "ok"}
        power = conn.execute("SELECT * FROM power_readings WHERE granularity='api_live'").fetchone()
        assert power["import_combined_kwh"] == pytest.approx(10.0)
        battery = conn.execute("SELECT * FROM battery_readings WHERE granularity='api_live'").fetchone()
        assert battery["soc_pct"] == pytest.approx(55.0)
        assert battery["import_kwh"] == pytest.approx(936.578)
        assert battery["export_kwh"] == pytest.approx(684.915)
        water = conn.execute("SELECT * FROM water_readings WHERE granularity='api_live'").fetchone()
        assert water["water_usage_dl"] == pytest.approx(10_000.0)
        gas = conn.execute("SELECT * FROM gas_readings WHERE granularity='api_live'").fetchone()
        assert gas["total_gas_m3"] == pytest.approx(7930.423)

    def test_no_gas_row_written_when_p1_has_no_external_gas_meter(self, monkeypatch):
        # Not every household has gas -- a P1 response with no gas_meter
        # entry in `external` must not write a phantom gas_readings row.
        conn = _conn()
        monkeypatch.setattr(
            hwc,
            "fetch_measurement",
            lambda device_name, token: {"energy_import_kwh": 10.0} if device_name == "p1" else {"state_of_charge_pct": 1.0},
        )
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        api_ingest.ingest_all(conn, hwc.all_devices())
        assert conn.execute("SELECT COUNT(*) AS n FROM gas_readings").fetchone()["n"] == 0

    def test_missing_token_skips_v2_devices_without_error(self, monkeypatch):
        # watermeter (v1) needs no token at all, so it's excluded from this
        # assertion -- it goes through fetch_measurement_v1 regardless.
        conn = _conn()
        monkeypatch.delenv("OMNIMETER_P1_TOKEN", raising=False)
        monkeypatch.delenv("OMNIMETER_BATTERY_TOKEN", raising=False)
        monkeypatch.setattr(hwc, "fetch_measurement", lambda *a, **kw: pytest.fail("should not be called"))
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        results = api_ingest.ingest_all(conn, hwc.all_devices())
        assert results["p1"] == "skipped: device not configured (unpaired, or no token set)"
        assert results["battery"] == "skipped: device not configured (unpaired, or no token set)"
        assert results["watermeter"] == "ok"

    def test_unconfigured_device_skipped_before_fetch(self, monkeypatch):
        # Devices are paired one at a time -- a device still on REPLACE_ME
        # must be skipped cleanly, not attempted (there's no valid host to
        # even connect to), regardless of whether a token happens to be set.
        conn = _conn()
        monkeypatch.setitem(hwc.DEVICES, "battery", {**hwc.DEVICES["battery"], "ip": "REPLACE_ME"})
        monkeypatch.setattr(hwc, "fetch_measurement", lambda device_name, token: pytest.fail("should not be called")
                             if device_name == "battery" else {"energy_import_kwh": 1.0})
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})

        results = api_ingest.ingest_all(conn, hwc.all_devices())
        assert results["battery"] == "skipped: device not configured (unpaired, or no token set)"
        assert results["p1"] == "ok"

    def test_unrecognized_device_name_is_reported_not_silently_dropped(self, monkeypatch):
        # A device whose name isn't in _DEVICE_TARGETS has nowhere to write --
        # this used to just `continue` with no trace anywhere (the bug that
        # let devices.json.generic.example ship "water" instead of
        # "watermeter" and silently discard every reading). It must now show
        # up in the results dict, not vanish.
        import types

        conn = _conn()
        bogus = types.SimpleNamespace(name="water")
        monkeypatch.setattr(hwc, "fetch_measurement", lambda device_name, token: {"energy_import_kwh": 1.0})
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})

        results = api_ingest.ingest_all(conn, [bogus, *hwc.all_devices()])

        assert "water" in results
        assert results["water"].startswith("config_error:")
        assert "watermeter" in results["water"]  # names the valid keys, not just "it failed"

    def test_one_device_auth_error_does_not_block_others(self, monkeypatch):
        conn = _conn()
        monkeypatch.setattr(
            hwc,
            "fetch_measurement",
            lambda device_name, token: (_ for _ in ()).throw(hwc.HomeWizardAuthError("battery: token revoked"))
            if device_name == "battery"
            else {"energy_import_kwh": 1.0},
        )
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        results = api_ingest.ingest_all(conn, hwc.all_devices())

        assert results["p1"] == "ok"
        assert results["watermeter"] == "ok"
        assert results["battery"].startswith("auth_error")
        assert conn.execute("SELECT COUNT(*) AS n FROM power_readings").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM battery_readings").fetchone()["n"] == 0

    def test_connection_error_isolated_per_device(self, monkeypatch):
        # watermeter (v1) is the device under test here -- unreachable
        # watermeter shouldn't block p1/battery (v2).
        conn = _conn()
        monkeypatch.setattr(
            hwc,
            "fetch_measurement",
            lambda device_name, token: {"energy_import_kwh": 1.0}
            if device_name == "p1"
            else {"state_of_charge_pct": 1.0},
        )

        def fake_fetch_v1(device_name):
            raise hwc.HomeWizardConnectionError("watermeter: unreachable")

        monkeypatch.setattr(hwc, "fetch_measurement_v1", fake_fetch_v1)
        results = api_ingest.ingest_all(conn, hwc.all_devices())

        assert results["watermeter"].startswith("connection_error")
        assert results["p1"] == "ok"
        assert results["battery"] == "ok"


class TestV1FieldNameCompatibility:
    """HomeWizard's v1 /api/v1/data and v2 /api/measurement use DIFFERENT key
    names for the same DSMR quantities. The mappers originally read v2 names
    only, so a device on protocol='v1' -- the default, since v2 is an
    experimental opt-in -- silently produced rows of NULLs. All values below
    are invented, not read off any real meter."""

    def test_v1_p1_payload_maps_to_real_values(self):
        v1_payload = {
            "total_power_import_t1_kwh": 1111.111,
            "total_power_import_t2_kwh": 2222.222,
            "total_power_import_kwh": 3333.333,
            "total_power_export_t1_kwh": 4444.444,
            "total_power_export_t2_kwh": 5555.555,
            "total_power_export_kwh": 6666.666,
            "active_power_l1_w": 11,
            "active_power_l2_w": 22,
            "active_power_l3_w": 33,
        }
        row = api_ingest._map_p1(v1_payload)
        assert row["import_t1_kwh"] == 1111.111
        assert row["export_t1_kwh"] == 4444.444
        assert row["import_combined_kwh"] == 3333.333
        assert row["l2_max_w"] == 22
        assert not any(v is None for v in row.values())

    def test_v2_p1_payload_still_maps(self):
        v2_payload = {
            "energy_import_t1_kwh": 10.0,
            "energy_export_t1_kwh": 20.0,
            "power_l1_w": 30,
        }
        row = api_ingest._map_p1(v2_payload)
        assert row["import_t1_kwh"] == 10.0
        assert row["export_t1_kwh"] == 20.0
        assert row["l1_max_w"] == 30

    def test_v2_spelling_wins_when_both_present(self):
        row = api_ingest._map_p1({"energy_import_t1_kwh": 1.0, "total_power_import_t1_kwh": 999.0})
        assert row["import_t1_kwh"] == 1.0


class TestFieldMappingErrorIsLoud:
    """A response that maps to nothing is a key-name mismatch, not an empty
    meter -- it must not be written as a row of NULLs indistinguishable from
    a real reading."""

    def test_unrecognized_payload_is_not_written(self, monkeypatch):
        conn = _conn()
        monkeypatch.setattr(
            hwc, "fetch_measurement", lambda device_name, token: {"someOtherVendorKey": 42, "andAnother": 7}
        )
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        results = api_ingest.ingest_all(conn, hwc.all_devices())

        assert results["p1"].startswith("field_mapping_error")
        assert conn.execute("SELECT COUNT(*) AS n FROM power_readings").fetchone()["n"] == 0

    def test_error_names_the_keys_that_did_arrive(self, monkeypatch):
        conn = _conn()
        monkeypatch.setattr(hwc, "fetch_measurement", lambda device_name, token: {"someOtherVendorKey": 42})
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        results = api_ingest.ingest_all(conn, hwc.all_devices())

        assert "someOtherVendorKey" in results["p1"]
        assert "import_t1_kwh" in results["p1"]
