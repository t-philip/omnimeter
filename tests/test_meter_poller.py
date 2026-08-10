import pytest

from src import db
from src import homewizard_api_client as hwc
from src import meter_poller as cli


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
    for name in hwc.DEVICES:
        monkeypatch.setitem(hwc.DEVICES, name, {**hwc.DEVICES[name], "ip": "192.0.2.50", "serial": "abc"})


class _StopLoop(Exception):
    pass


class TestMainTogglesGate:
    def test_skips_ingest_when_disabled(self, env, monkeypatch):
        called = []
        monkeypatch.setattr(cli.api_ingest, "ingest_all", lambda *a, **kw: called.append(1) or {})

        def sleep_then_stop(_):
            raise _StopLoop

        monkeypatch.setattr(cli.time, "sleep", sleep_then_stop)

        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("UPDATE feature_toggles SET homewizard_api_enabled = 0 WHERE id = 1")
        conn.commit()
        conn.close()

        with pytest.raises(_StopLoop):
            cli.main()
        assert called == []

    def test_calls_ingest_when_enabled_by_default(self, env, monkeypatch):
        called = []
        monkeypatch.setattr(cli.api_ingest, "ingest_all", lambda *a, **kw: called.append(1) or {})

        def sleep_then_stop(_):
            raise _StopLoop

        monkeypatch.setattr(cli.time, "sleep", sleep_then_stop)

        with pytest.raises(_StopLoop):
            cli.main()
        assert called == [1]

    def test_starts_fine_with_some_devices_still_placeholder(self, monkeypatch, tmp_path):
        # Devices are paired one at a time -- the service must start and poll
        # whatever's ready rather than refusing to run until every device
        # happens to be configured (see HomeWizardDevice.is_configured()).
        monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setitem(hwc.DEVICES, "p1", {**hwc.DEVICES["p1"], "ip": "192.0.2.53", "serial": "aaaaaaaaaaaa"})
        monkeypatch.setitem(hwc.DEVICES, "battery", {**hwc.DEVICES["battery"], "ip": "REPLACE_ME"})
        monkeypatch.setitem(hwc.DEVICES, "watermeter", {**hwc.DEVICES["watermeter"], "ip": "REPLACE_ME"})

        called = []
        monkeypatch.setattr(cli.api_ingest, "ingest_all", lambda *a, **kw: called.append(1) or {})

        def sleep_then_stop(_):
            raise _StopLoop

        monkeypatch.setattr(cli.time, "sleep", sleep_then_stop)

        with pytest.raises(_StopLoop):
            cli.main()
        assert called == [1]
        assert (tmp_path / "test.db").exists()

    def test_passes_one_device_per_registry_entry_regardless_of_readiness(self, env, monkeypatch):
        # No token env vars are set here, so none of the v2 devices are
        # actually "ready" -- main() must still construct and pass a device
        # per DEVICES entry (readiness is ingest_all's/HomeWizardDevice's
        # concern, not cli.main()'s), and must not crash when none are ready.
        devices_seen = []

        def fake_ingest_all(conn, devices):
            devices_seen.append(devices)
            return {d.name: "skipped: device not configured (unpaired, or no token set)" for d in devices}

        monkeypatch.setattr(cli.api_ingest, "ingest_all", fake_ingest_all)

        def sleep_then_stop(_):
            raise _StopLoop

        monkeypatch.setattr(cli.time, "sleep", sleep_then_stop)

        with pytest.raises(_StopLoop):
            cli.main()
        assert len(devices_seen) == 1
        assert {d.name for d in devices_seen[0]} == set(hwc.DEVICES)


class TestPeriodicRebuild:
    def test_rebuild_not_called_before_nth_poll(self, env, monkeypatch):
        monkeypatch.setattr(cli.api_ingest, "ingest_all", lambda *a, **kw: {})
        rebuild_calls = []
        monkeypatch.setattr(cli.aggregate, "rebuild_all", lambda conn: rebuild_calls.append(1))

        sleep_calls = []

        def sleep_then_stop(_):
            sleep_calls.append(1)
            if len(sleep_calls) >= cli.REBUILD_EVERY_N_POLLS - 1:
                raise _StopLoop

        monkeypatch.setattr(cli.time, "sleep", sleep_then_stop)

        with pytest.raises(_StopLoop):
            cli.main()
        assert rebuild_calls == []

    def test_rebuild_called_on_nth_poll(self, env, monkeypatch):
        # Real gap found live 2026-07-24: raw rows were being written every
        # poll, but nothing ever refreshed power_daily/etc -- the dashboard's
        # daily-max charts only updated as an incidental side effect of the
        # unrelated HA-ingest timer's own rebuild call, up to 15 minutes
        # later. This throttled call is the actual fix.
        monkeypatch.setattr(cli.api_ingest, "ingest_all", lambda *a, **kw: {})
        rebuild_calls = []
        monkeypatch.setattr(cli.aggregate, "rebuild_all", lambda conn: rebuild_calls.append(1))

        sleep_calls = []

        def sleep_then_stop(_):
            sleep_calls.append(1)
            if len(sleep_calls) >= cli.REBUILD_EVERY_N_POLLS:
                raise _StopLoop

        monkeypatch.setattr(cli.time, "sleep", sleep_then_stop)

        with pytest.raises(_StopLoop):
            cli.main()
        assert rebuild_calls == [1]
