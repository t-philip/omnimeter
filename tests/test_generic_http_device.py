"""GenericHttpDevice: live polling for any JSON-over-HTTP meter.

All IPs, hostnames and values here are invented.
"""

import sqlite3

import pytest
import requests

from src import db, device_registry
from src import meter_ingest as api_ingest
from src.generic_http_device import GenericHttpDevice, resolve_path
from src.meter_device import MeterDeviceAuthError, MeterDeviceConnectionError


class _FakeResponse:
    def __init__(self, payload=None, status_code=200, raise_json=False):
        self._payload = payload
        self.status_code = status_code
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def _config(**overrides):
    cfg = {
        "protocol": "generic_json",
        "url": "http://192.0.2.10/api/v1/data",
        "token_env": None,
        "field_map": {
            "import_t1_kwh": "total_power_import_t1_kwh",
            "export_t1_kwh": "total_power_export_t1_kwh",
        },
    }
    cfg.update(overrides)
    return cfg


class TestResolvePath:
    def test_flat_key(self):
        assert resolve_path({"a": 1}, "a") == 1

    def test_nested_key(self):
        assert resolve_path({"data": {"power": {"import": 5}}}, "data.power.import") == 5

    def test_missing_returns_none(self):
        assert resolve_path({"a": 1}, "b") is None
        assert resolve_path({"a": {"b": 1}}, "a.c") is None

    def test_non_dict_midway_returns_none(self):
        assert resolve_path({"a": 5}, "a.b") is None


class TestIsConfigured:
    def test_ready_with_url_and_map(self):
        assert GenericHttpDevice("p1", _config()).is_configured() is True

    def test_placeholder_url_not_ready(self):
        assert GenericHttpDevice("p1", _config(url="http://REPLACE_ME/api")).is_configured() is False

    def test_missing_url_not_ready(self):
        assert GenericHttpDevice("p1", _config(url=None)).is_configured() is False

    def test_empty_field_map_not_ready(self):
        assert GenericHttpDevice("p1", _config(field_map={})).is_configured() is False

    def test_token_required_only_when_declared(self, monkeypatch):
        monkeypatch.delenv("SOME_METER_TOKEN", raising=False)
        dev = GenericHttpDevice("p1", _config(token_env="SOME_METER_TOKEN"))
        assert dev.is_configured() is False
        monkeypatch.setenv("SOME_METER_TOKEN", "abc")
        assert dev.is_configured() is True


class TestFetchMeasurement:
    def test_maps_vendor_keys_to_canonical_names(self, monkeypatch):
        payload = {"total_power_import_t1_kwh": 1111.1, "total_power_export_t1_kwh": 222.2, "unrelated": 9}
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload))
        row = GenericHttpDevice("p1", _config()).fetch_measurement()
        assert row == {"import_t1_kwh": 1111.1, "export_t1_kwh": 222.2}

    def test_unmatched_field_becomes_none(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse({"total_power_import_t1_kwh": 1.0}))
        row = GenericHttpDevice("p1", _config()).fetch_measurement()
        assert row["export_t1_kwh"] is None

    def test_nested_field_map(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse({"d": {"imp": 7.5}}))
        cfg = _config(field_map={"import_combined_kwh": "d.imp"})
        assert GenericHttpDevice("p1", cfg).fetch_measurement() == {"import_combined_kwh": 7.5}

    def test_bearer_token_sent_when_configured(self, monkeypatch):
        seen = {}

        def fake_get(url, headers=None, timeout=None):
            seen["headers"] = headers
            return _FakeResponse({"total_power_import_t1_kwh": 1.0})

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setenv("SOME_METER_TOKEN", "sekrit")
        GenericHttpDevice("p1", _config(token_env="SOME_METER_TOKEN")).fetch_measurement()
        assert seen["headers"]["Authorization"] == "Bearer sekrit"

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_auth_error(self, monkeypatch, status):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(status_code=status))
        with pytest.raises(MeterDeviceAuthError):
            GenericHttpDevice("p1", _config()).fetch_measurement()

    def test_server_error_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(status_code=500))
        with pytest.raises(MeterDeviceConnectionError):
            GenericHttpDevice("p1", _config()).fetch_measurement()

    def test_unreachable_raises_connection_error(self, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.ConnectTimeout("timed out")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(MeterDeviceConnectionError):
            GenericHttpDevice("p1", _config()).fetch_measurement()

    def test_non_json_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(raise_json=True))
        with pytest.raises(MeterDeviceConnectionError, match="not JSON"):
            GenericHttpDevice("p1", _config()).fetch_measurement()

    def test_json_array_rejected(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse([1, 2, 3]))
        with pytest.raises(MeterDeviceConnectionError, match="JSON object"):
            GenericHttpDevice("p1", _config()).fetch_measurement()


class TestBuildDevices:
    def test_dispatches_on_protocol(self):
        cfg = {
            "p1": {"protocol": "generic_json", "url": "http://192.0.2.10/x", "field_map": {"a": "b"}},
            "watermeter": {"protocol": "v1", "ip": "192.0.2.11"},
            "battery": {"protocol": "v2", "ip": "192.0.2.12"},
        }
        devices = {d.name: type(d).__name__ for d in device_registry.build_devices(cfg)}
        assert devices["p1"] == "GenericHttpDevice"
        assert devices["watermeter"] == "HomeWizardDevice"
        assert devices["battery"] == "HomeWizardDevice"

    def test_unknown_protocol_skipped_not_fatal(self):
        cfg = {
            "broken": {"protocol": "carrier-pigeon"},
            "good": {"protocol": "v2", "ip": "192.0.2.12"},
        }
        devices = device_registry.build_devices(cfg)
        assert [d.name for d in devices] == ["good"]


class TestEndToEndIntoDatabase:
    """A generic device must land real rows in the right table without any
    HomeWizard mapper being applied to it."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    def test_generic_p1_writes_power_row(self, monkeypatch):
        conn = self._conn()
        payload = {"total_power_import_t1_kwh": 1234.5, "total_power_export_t1_kwh": 67.8}
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload))
        device = GenericHttpDevice("p1", _config())

        results = api_ingest.ingest_all(conn, [device])

        assert results["p1"] == "ok"
        row = conn.execute("SELECT * FROM power_readings").fetchone()
        assert row["import_t1_kwh"] == 1234.5
        assert row["export_t1_kwh"] == 67.8
        assert row["granularity"] == "api_live"

    def test_wholly_wrong_field_map_reports_error_and_writes_nothing(self, monkeypatch):
        conn = self._conn()
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse({"somethingElse": 1}))
        device = GenericHttpDevice("p1", _config())

        results = api_ingest.ingest_all(conn, [device])

        assert results["p1"].startswith("field_mapping_error")
        assert conn.execute("SELECT COUNT(*) AS n FROM power_readings").fetchone()["n"] == 0


class TestListAddressing:
    """A meter may nest readings in an array -- a HomeWizard P1 reports its
    relayed gas meter as external: [{"type": "gas_meter", "value": ...}].
    Without list support such a value is unreachable by any field_map."""

    def test_numeric_segment_indexes_a_list(self):
        payload = {"external": [{"type": "gas_meter", "value": 7933.9}]}
        assert resolve_path(payload, "external.0.value") == 7933.9

    def test_index_out_of_range_returns_none(self):
        assert resolve_path({"external": []}, "external.0.value") is None

    def test_non_numeric_segment_on_list_returns_none(self):
        assert resolve_path({"external": [{"value": 1}]}, "external.value") is None

    def test_gas_via_array_reaches_the_database(self, monkeypatch):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        payload = {"external": [{"type": "gas_meter", "value": 7933.9}]}
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload))
        device = GenericHttpDevice(
            "gas",
            _config(field_map={"total_gas_m3": "external.0.value"}),
        )

        results = api_ingest.ingest_all(conn, [device])

        assert results["gas"] == "ok"
        assert conn.execute("SELECT total_gas_m3 AS v FROM gas_readings").fetchone()["v"] == 7933.9


class TestGasAsItsOwnDevice:
    """Gas reaches this app two structurally different ways: relayed inside a
    HomeWizard P1 telegram, or as a device in its own right. Only the first
    used to be possible, so a non-HomeWizard user could record power, battery
    and water but not gas."""

    def test_generic_gas_device_writes_gas_readings(self, monkeypatch):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse({"total_gas_m3": 1234.5}))
        device = GenericHttpDevice("gas", _config(field_map={"total_gas_m3": "total_gas_m3"}))

        results = api_ingest.ingest_all(conn, [device])

        assert results["gas"] == "ok"
        row = conn.execute("SELECT * FROM gas_readings").fetchone()
        assert row["total_gas_m3"] == 1234.5
        assert row["granularity"] == "api_live"

    def test_gas_is_a_known_device_target(self):
        assert "gas" in api_ingest._DEVICE_TARGETS


class TestNonDeviceEntriesIgnored:
    """devices.json is hand-edited JSON with no comment syntax, so users add
    "_comment" keys. That must not crash the poller."""

    def test_comment_entry_does_not_crash(self):
        cfg = {
            "_comment": "see README",
            "p1": {"protocol": "v2", "ip": "192.0.2.1"},
        }
        assert [d.name for d in device_registry.build_devices(cfg)] == ["p1"]

    def test_null_entry_does_not_crash(self):
        cfg = {"stale": None, "p1": {"protocol": "v2", "ip": "192.0.2.1"}}
        assert [d.name for d in device_registry.build_devices(cfg)] == ["p1"]

    def test_shipped_generic_example_is_valid_json_and_loads(self):
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "devices.json.generic.example"
        config = json.loads(path.read_text(encoding="utf-8"))
        devices = device_registry.build_devices(config)
        # Every real entry is generic_json and unconfigured (REPLACE_ME url),
        # so the file is safe to copy verbatim: nothing polls until edited.
        assert {d.name for d in devices} == {"p1", "gas", "water", "battery"}
        assert all(not d.is_configured() for d in devices)
