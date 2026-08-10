import json

import pytest

from scripts import setup_wizard as wiz


class _FakeResponse:
    def __init__(self, status, data=b""):
        self.status = status
        self.data = data


class _FakePool:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        return next(self._responses)

    def close(self):
        self.closed = True


class TestAsk:
    def test_ask_yes_no_defaults_when_blank(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert wiz._ask_yes_no("continue?", default=True) is True
        assert wiz._ask_yes_no("continue?", default=False) is False

    def test_ask_yes_no_parses_yes_variants(self, monkeypatch):
        for word in ("y", "Y", "yes", "YES"):
            monkeypatch.setattr("builtins.input", lambda prompt, w=word: w)
            assert wiz._ask_yes_no("continue?") is True

    def test_ask_yes_no_parses_no_variants(self, monkeypatch):
        for word in ("n", "no", "anything else"):
            monkeypatch.setattr("builtins.input", lambda prompt, w=word: w)
            assert wiz._ask_yes_no("continue?", default=True) is False

    def test_ask_returns_default_when_blank(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert wiz._ask("timezone", "UTC") == "UTC"

    def test_ask_returns_typed_value(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "Europe/Amsterdam")
        assert wiz._ask("timezone", "UTC") == "Europe/Amsterdam"


class TestEnvFileHelpers:
    def test_read_env_lines_missing_file_returns_empty(self, tmp_path):
        assert wiz._read_env_lines(tmp_path / "nope.env") == []

    def test_read_env_lines_preserves_comments_and_order(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# a comment\nFOO=bar\n\nBAZ=qux\n")
        assert wiz._read_env_lines(path) == ["# a comment", "FOO=bar", "", "BAZ=qux"]

    def test_get_env_value_found(self):
        assert wiz._get_env_value(["FOO=bar", "BAZ=qux"], "BAZ") == "qux"

    def test_get_env_value_missing_returns_none(self):
        assert wiz._get_env_value(["FOO=bar"], "MISSING") is None

    def test_set_env_value_updates_existing_line_in_place(self):
        lines = ["# comment", "FOO=old", "BAZ=qux"]
        result = wiz._set_env_value(lines, "FOO", "new")
        assert result == ["# comment", "FOO=new", "BAZ=qux"]

    def test_set_env_value_appends_when_key_absent(self):
        result = wiz._set_env_value(["FOO=bar"], "NEW_KEY", "value")
        assert result == ["FOO=bar", "NEW_KEY=value"]


class TestPairV2Device:
    def test_returns_token_on_immediate_success(self, monkeypatch):
        fake = _FakePool([_FakeResponse(200, json.dumps({"token": "secret-token"}).encode())])
        monkeypatch.setattr(wiz.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        assert wiz.pair_v2_device("198.51.100.50", "appliance/p1dongle/abc123") == "secret-token"
        assert fake.closed

    def test_retries_on_403_until_button_pressed(self, monkeypatch):
        fake = _FakePool(
            [_FakeResponse(403), _FakeResponse(403), _FakeResponse(200, json.dumps({"token": "tok"}).encode())]
        )
        monkeypatch.setattr(wiz.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        monkeypatch.setattr(wiz.time, "sleep", lambda s: None)
        assert wiz.pair_v2_device("198.51.100.50", "appliance/p1dongle/abc123") == "tok"

    def test_unexpected_status_raises_runtime_error(self, monkeypatch):
        fake = _FakePool([_FakeResponse(500)])
        monkeypatch.setattr(wiz.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        with pytest.raises(RuntimeError):
            wiz.pair_v2_device("198.51.100.50", "appliance/p1dongle/abc123")

    def test_timeout_raises_when_deadline_passes(self, monkeypatch):
        fake = _FakePool([_FakeResponse(403)] * 100)
        monkeypatch.setattr(wiz.urllib3, "HTTPSConnectionPool", lambda *a, **kw: fake)
        monkeypatch.setattr(wiz.time, "sleep", lambda s: None)
        # Force the deadline to have already passed on the first loop check.
        times = iter([0, 1000])
        monkeypatch.setattr(wiz.time, "monotonic", lambda: next(times, 1000))
        with pytest.raises(TimeoutError):
            wiz.pair_v2_device("198.51.100.50", "appliance/p1dongle/abc123")


class TestConfigureDevice:
    def _devices(self):
        return {name: dict(cfg) for name, cfg in wiz.hwc._UNCONFIGURED_STUB.items()}

    def test_declining_skips_unconfigured_device(self, monkeypatch):
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: False)
        devices = self._devices()
        result_env = wiz.configure_device("p1", "P1 meter", "p1dongle", devices, [])
        assert devices["p1"]["ip"] == "REPLACE_ME"
        assert result_env == []

    def test_v1_device_configured_without_pairing(self, monkeypatch):
        # First _ask_yes_no call = "do you have one" -> True; second = "is v2 enabled" -> False (use v1).
        yn_answers = iter([True, False])
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: next(yn_answers))
        inputs = iter(["198.51.100.89", "abc123"])  # ip, then serial
        monkeypatch.setattr(wiz, "_ask", lambda *a, **kw: next(inputs))

        devices = self._devices()
        result_env = wiz.configure_device("watermeter", "Watermeter", "watermeter", devices, [])

        assert devices["watermeter"]["ip"] == "198.51.100.89"
        assert devices["watermeter"]["serial"] == "abc123"
        assert devices["watermeter"]["protocol"] == "v1"
        assert devices["watermeter"]["token_env"] == "OMNIMETER_WATERMETER_TOKEN"
        assert result_env == []  # no token written for v1

    def test_v2_device_paired_and_token_written(self, monkeypatch):
        # Answers in order: "do you have one" -> True, "is v2 enabled" -> True,
        # "does identity look right" -> True.
        yn_answers = iter([True, True, True])
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: next(yn_answers))
        monkeypatch.setattr(wiz, "_ask", lambda *a, **kw: "192.0.2.50")
        monkeypatch.setattr(
            wiz, "fetch_device_certificate_identity", lambda ip: ("appliance/p1dongle/abc123", [])
        )
        monkeypatch.setattr(wiz, "pair_v2_device", lambda ip, identity: "the-real-token")
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        devices = self._devices()
        result_env = wiz.configure_device("p1", "P1 meter", "p1dongle", devices, [])

        assert devices["p1"]["ip"] == "192.0.2.50"
        assert devices["p1"]["serial"] == "abc123"
        assert devices["p1"]["protocol"] == "v2"
        assert devices["p1"]["verify_hostname"] == "appliance/p1dongle/abc123"
        assert wiz._get_env_value(result_env, "OMNIMETER_P1_TOKEN") == "the-real-token"

    def test_v2_device_uses_san_over_cn_when_present(self, monkeypatch):
        yn_answers = iter([True, True, True])
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: next(yn_answers))
        monkeypatch.setattr(wiz, "_ask", lambda *a, **kw: "192.0.2.51")
        monkeypatch.setattr(
            wiz,
            "fetch_device_certificate_identity",
            lambda ip: ("appliance/battery/bbbbbbbbbbbb", ["bbbbbbbbbbbb.battery.device.homewizard.energy"]),
        )
        captured = {}

        def fake_pair(ip, identity):
            captured["identity"] = identity
            return "tok"

        monkeypatch.setattr(wiz, "pair_v2_device", fake_pair)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        devices = self._devices()
        wiz.configure_device("battery", "Plug-in Battery", "battery", devices, [])

        assert captured["identity"] == "bbbbbbbbbbbb.battery.device.homewizard.energy"
        assert devices["battery"]["verify_hostname"] == "bbbbbbbbbbbb.battery.device.homewizard.energy"
        # Serial is still parsed from the CN even though the SAN is used for verification.
        assert devices["battery"]["serial"] == "bbbbbbbbbbbb"

    def test_declining_identity_confirmation_skips_device(self, monkeypatch):
        yn_answers = iter([True, True, False])  # have one -> yes, v2 -> yes, identity ok -> no
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: next(yn_answers))
        monkeypatch.setattr(wiz, "_ask", lambda *a, **kw: "192.0.2.50")
        monkeypatch.setattr(wiz, "fetch_device_certificate_identity", lambda ip: ("appliance/p1dongle/abc123", []))
        called = []
        monkeypatch.setattr(wiz, "pair_v2_device", lambda *a, **kw: called.append(1) or "tok")

        devices = self._devices()
        result_env = wiz.configure_device("p1", "P1 meter", "p1dongle", devices, [])

        assert called == []
        assert devices["p1"]["ip"] == "REPLACE_ME"
        assert result_env == []

    def test_pairing_timeout_leaves_device_unconfigured(self, monkeypatch):
        yn_answers = iter([True, True, True])
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: next(yn_answers))
        monkeypatch.setattr(wiz, "_ask", lambda *a, **kw: "192.0.2.50")
        monkeypatch.setattr(wiz, "fetch_device_certificate_identity", lambda ip: ("appliance/p1dongle/abc123", []))

        def fake_pair(*a, **kw):
            raise TimeoutError("no button press")

        monkeypatch.setattr(wiz, "pair_v2_device", fake_pair)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        devices = self._devices()
        result_env = wiz.configure_device("p1", "P1 meter", "p1dongle", devices, [])

        assert devices["p1"]["ip"] == "REPLACE_ME"
        assert result_env == []

    def test_unreachable_device_skipped_cleanly(self, monkeypatch):
        yn_answers = iter([True, True])
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: next(yn_answers))
        monkeypatch.setattr(wiz, "_ask", lambda *a, **kw: "192.0.2.50")

        def fake_fetch(ip):
            raise OSError("no route to host")

        monkeypatch.setattr(wiz, "fetch_device_certificate_identity", fake_fetch)

        devices = self._devices()
        result_env = wiz.configure_device("p1", "P1 meter", "p1dongle", devices, [])

        assert devices["p1"]["ip"] == "REPLACE_ME"
        assert result_env == []

    def test_already_configured_device_declines_reconfigure(self, monkeypatch):
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: False)
        devices = self._devices()
        devices["p1"] = {
            "ip": "192.0.2.50",
            "serial": "abc123",
            "product_type": "p1dongle",
            "token_env": "OMNIMETER_P1_TOKEN",
            "protocol": "v2",
            "verify_hostname": "appliance/p1dongle/abc123",
        }
        original = dict(devices["p1"])
        result_env = wiz.configure_device("p1", "P1 meter", "p1dongle", devices, [])
        assert devices["p1"] == original
        assert result_env == []


class TestMain:
    def test_full_run_writes_devices_json_and_env(self, monkeypatch, tmp_path):
        devices_path = tmp_path / "devices.json"
        env_path = tmp_path / ".env"
        monkeypatch.setattr(wiz.hwc, "DEVICES_CONFIG_PATH", devices_path)
        monkeypatch.setattr(wiz, "ENV_FILE_PATH", env_path)

        # Decline every device -- exercises main()'s write path without any pairing.
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: False)
        monkeypatch.setattr(wiz, "_ask", lambda prompt, default="": default)

        assert wiz.main() == 0
        assert devices_path.exists()
        written = json.loads(devices_path.read_text())
        assert set(written) == {"p1", "battery", "watermeter"}

        env_text = env_path.read_text()
        assert "OMNIMETER_WRITE_API_TOKEN=" in env_text
        assert "OMNIMETER_TIMEZONE=UTC" in env_text
        assert "OMNIMETER_BACKUP_HOST_DIR=./backups" in env_text

    def test_rerun_does_not_regenerate_existing_write_token(self, monkeypatch, tmp_path):
        devices_path = tmp_path / "devices.json"
        env_path = tmp_path / ".env"
        env_path.write_text("OMNIMETER_WRITE_API_TOKEN=already-set\n")
        monkeypatch.setattr(wiz.hwc, "DEVICES_CONFIG_PATH", devices_path)
        monkeypatch.setattr(wiz, "ENV_FILE_PATH", env_path)
        monkeypatch.setattr(wiz, "_ask_yes_no", lambda *a, **kw: False)
        monkeypatch.setattr(wiz, "_ask", lambda prompt, default="": default)

        wiz.main()

        assert "OMNIMETER_WRITE_API_TOKEN=already-set" in env_path.read_text()
