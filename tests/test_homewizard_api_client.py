import json

import pytest

from src import homewizard_api_client as hwc
from src import meter_device


class _FakeResponse:
    def __init__(self, status, data=b""):
        self.status = status
        self.data = data


class _FakePool:
    def __init__(self, response):
        self._response = response
        self.closed = False
        self.request_kwargs = None

    def request(self, method, path, headers=None):
        self.request_kwargs = {"method": method, "path": path, "headers": headers}
        return self._response

    def close(self):
        self.closed = True


@pytest.fixture
def p1_configured(monkeypatch):
    monkeypatch.setitem(
        hwc.DEVICES,
        "p1",
        {
            "ip": "192.0.2.50",
            "serial": "abc123",
            "product_type": "p1dongle",
            "token_env": "OMNIMETER_P1_TOKEN",
            "protocol": "v2",
            "verify_hostname": "appliance/p1dongle/abc123",
        },
    )


@pytest.fixture
def watermeter_configured(monkeypatch):
    monkeypatch.setitem(
        hwc.DEVICES,
        "watermeter",
        {
            "ip": "192.0.2.52",
            "serial": "abc123",
            "product_type": "watermeter",
            "token_env": "OMNIMETER_WATERMETER_TOKEN",
            "protocol": "v1",
            "verify_hostname": None,
        },
    )


class TestIsConfigured:
    def test_false_when_ip_is_placeholder(self, monkeypatch):
        monkeypatch.setitem(hwc.DEVICES, "p1", {**hwc.DEVICES["p1"], "ip": "REPLACE_ME", "serial": "abc"})
        assert hwc.is_configured("p1") is False

    def test_false_when_serial_is_placeholder(self, monkeypatch):
        monkeypatch.setitem(hwc.DEVICES, "p1", {**hwc.DEVICES["p1"], "ip": "192.0.2.50", "serial": "REPLACE_ME"})
        assert hwc.is_configured("p1") is False

    def test_true_when_both_filled_in(self, monkeypatch):
        monkeypatch.setitem(hwc.DEVICES, "p1", {**hwc.DEVICES["p1"], "ip": "192.0.2.50", "serial": "abc"})
        assert hwc.is_configured("p1") is True


class TestConfiguredDevices:
    def test_returns_only_devices_with_no_placeholder(self, monkeypatch):
        for name in hwc.DEVICES:
            monkeypatch.setitem(hwc.DEVICES, name, {**hwc.DEVICES[name], "ip": "REPLACE_ME", "serial": "REPLACE_ME"})
        monkeypatch.setitem(hwc.DEVICES, "p1", {**hwc.DEVICES["p1"], "ip": "192.0.2.53", "serial": "aaaaaaaaaaaa"})
        assert hwc.configured_devices() == ["p1"]


class TestFetchMeasurement:
    def test_returns_parsed_json_on_success(self, p1_configured, monkeypatch):
        fake = _FakePool(_FakeResponse(200, json.dumps({"power_w": 123}).encode()))
        monkeypatch.setattr(hwc.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)

        result = hwc.fetch_measurement("p1", "tok")
        assert result == {"power_w": 123}
        assert fake.closed
        assert fake.request_kwargs["headers"]["Authorization"] == "Bearer tok"
        assert fake.request_kwargs["headers"]["X-Api-Version"] == "2"

    def test_401_raises_auth_error(self, p1_configured, monkeypatch):
        fake = _FakePool(_FakeResponse(401))
        monkeypatch.setattr(hwc.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        with pytest.raises(hwc.HomeWizardAuthError):
            hwc.fetch_measurement("p1", "tok")

    def test_403_raises_auth_error(self, p1_configured, monkeypatch):
        fake = _FakePool(_FakeResponse(403))
        monkeypatch.setattr(hwc.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        with pytest.raises(hwc.HomeWizardAuthError):
            hwc.fetch_measurement("p1", "tok")

    def test_500_raises_connection_error(self, p1_configured, monkeypatch):
        fake = _FakePool(_FakeResponse(500))
        monkeypatch.setattr(hwc.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        with pytest.raises(hwc.HomeWizardConnectionError):
            hwc.fetch_measurement("p1", "tok")

    def test_transport_failure_raises_connection_error(self, p1_configured, monkeypatch):
        class _RaisingPool(_FakePool):
            def request(self, *a, **kw):
                raise hwc.urllib3.exceptions.SSLError("cert mismatch")

        fake = _RaisingPool(None)
        monkeypatch.setattr(hwc.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        with pytest.raises(hwc.HomeWizardConnectionError):
            hwc.fetch_measurement("p1", "tok")
        assert fake.closed

    def test_uses_hostname_and_ssl_context_for_verification(self, p1_configured, monkeypatch):
        captured = {}

        def _fake_pool(host, port, ssl_context, assert_hostname, timeout):
            captured.update(host=host, port=port, ssl_context=ssl_context, assert_hostname=assert_hostname)
            return _FakePool(_FakeResponse(200, b"{}"))

        monkeypatch.setattr(hwc.urllib3, "HTTPSConnectionPool", _fake_pool)
        hwc.fetch_measurement("p1", "tok")

        assert captured["host"] == "192.0.2.50"
        assert captured["assert_hostname"] == "appliance/p1dongle/abc123"
        # CN-based fallback matching must be explicitly enabled (see
        # module docstring's "REAL GOTCHA") -- HomeWizard's device certs
        # carry their identity in the Subject CN, not a SAN extension, and
        # urllib3 only honors CN-fallback via a caller-supplied SSLContext.
        assert captured["ssl_context"].hostname_checks_common_name is True
        assert captured["ssl_context"].check_hostname is False
        assert captured["ssl_context"].verify_mode == hwc.ssl.CERT_REQUIRED


class TestFetchMeasurementV1:
    def test_returns_parsed_json_on_success(self, watermeter_configured, monkeypatch):
        class _Resp:
            status_code = 200

            def json(self):
                return {"total_liter_m3": 155.784}

        captured = {}

        def _fake_get(url, timeout):
            captured["url"] = url
            return _Resp()

        monkeypatch.setattr(hwc.requests, "get", _fake_get)
        result = hwc.fetch_measurement_v1("watermeter")

        assert result == {"total_liter_m3": 155.784}
        assert captured["url"] == "http://192.0.2.52/api/v1/data"

    def test_http_error_raises_connection_error(self, watermeter_configured, monkeypatch):
        class _Resp:
            status_code = 500

        monkeypatch.setattr(hwc.requests, "get", lambda url, timeout: _Resp())
        with pytest.raises(hwc.HomeWizardConnectionError):
            hwc.fetch_measurement_v1("watermeter")

    def test_transport_failure_raises_connection_error(self, watermeter_configured, monkeypatch):
        def _raise(*a, **kw):
            raise hwc.requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(hwc.requests, "get", _raise)
        with pytest.raises(hwc.HomeWizardConnectionError):
            hwc.fetch_measurement_v1("watermeter")


class TestExceptionsAreGenericMeterDeviceErrors:
    # ingest_all() catches the generic meter_device
    # exceptions, not the HomeWizard-specific ones, so these must actually
    # be subclasses, not just similarly-named.
    def test_auth_error_is_a_meter_device_auth_error(self):
        assert isinstance(hwc.HomeWizardAuthError("x"), meter_device.MeterDeviceAuthError)

    def test_connection_error_is_a_meter_device_connection_error(self):
        assert isinstance(hwc.HomeWizardConnectionError("x"), meter_device.MeterDeviceConnectionError)


class TestHomeWizardDevice:
    def test_satisfies_meter_device_protocol(self, p1_configured):
        assert isinstance(hwc.HomeWizardDevice("p1"), meter_device.MeterDevice)

    def test_not_configured_when_unpaired(self, monkeypatch):
        monkeypatch.setitem(hwc.DEVICES, "p1", {**hwc.DEVICES["p1"], "ip": "REPLACE_ME", "serial": "REPLACE_ME"})
        assert hwc.HomeWizardDevice("p1").is_configured() is False

    def test_v2_device_not_configured_when_paired_but_token_missing(self, p1_configured, monkeypatch):
        monkeypatch.delenv("OMNIMETER_P1_TOKEN", raising=False)
        assert hwc.HomeWizardDevice("p1").is_configured() is False

    def test_v2_device_configured_when_paired_and_token_set(self, p1_configured, monkeypatch):
        monkeypatch.setenv("OMNIMETER_P1_TOKEN", "tok")
        assert hwc.HomeWizardDevice("p1").is_configured() is True

    def test_v1_device_configured_as_soon_as_paired_no_token_needed(self, watermeter_configured, monkeypatch):
        monkeypatch.delenv("OMNIMETER_WATERMETER_TOKEN", raising=False)
        assert hwc.HomeWizardDevice("watermeter").is_configured() is True

    def test_fetch_measurement_dispatches_to_v2_with_env_token(self, p1_configured, monkeypatch):
        monkeypatch.setenv("OMNIMETER_P1_TOKEN", "tok-from-env")
        captured = {}

        def fake_fetch(device_name, token):
            captured.update(device_name=device_name, token=token)
            return {"power_w": 1}

        monkeypatch.setattr(hwc, "fetch_measurement", fake_fetch)
        result = hwc.HomeWizardDevice("p1").fetch_measurement()

        assert result == {"power_w": 1}
        assert captured == {"device_name": "p1", "token": "tok-from-env"}

    def test_fetch_measurement_dispatches_to_v1(self, watermeter_configured, monkeypatch):
        monkeypatch.setattr(hwc, "fetch_measurement_v1", lambda device_name: {"total_liter_m3": 1.0})
        assert hwc.HomeWizardDevice("watermeter").fetch_measurement() == {"total_liter_m3": 1.0}

    def test_fetch_measurement_raises_auth_error_when_token_missing(self, p1_configured, monkeypatch):
        # Defense in depth -- ingest_all() gates on is_configured() first, so
        # this path shouldn't normally be reached, but the device must not
        # misbehave (e.g. send a None Authorization header) if called directly.
        monkeypatch.delenv("OMNIMETER_P1_TOKEN", raising=False)
        with pytest.raises(hwc.HomeWizardAuthError):
            hwc.HomeWizardDevice("p1").fetch_measurement()


class TestAllDevices:
    def test_returns_one_device_per_registry_entry(self):
        devices = hwc.all_devices()
        assert {d.name for d in devices} == set(hwc.DEVICES)
        assert all(isinstance(d, hwc.HomeWizardDevice) for d in devices)
