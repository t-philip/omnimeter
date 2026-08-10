"""Timezone resolution is a single point of failure for every timestamp in
the app, so it gets its own tests rather than being covered incidentally.

Background: the same os.environ.get("OMNIMETER_TIMEZONE", "Europe/Amsterdam")
call used to be copy-pasted into four modules. tariff_parser.py was added
without a copy and dated imported rate periods in the host's UTC.
These tests exist to keep that from recurring in a fifth module.
"""

import logging
from zoneinfo import ZoneInfo

import pytest

from src import localtime


class TestResolveTimezoneName:
    def test_uses_the_configured_value(self):
        assert localtime.resolve_timezone_name({"OMNIMETER_TIMEZONE": "Asia/Kolkata"}) == "Asia/Kolkata"

    def test_strips_surrounding_whitespace(self):
        # A trailing space in a .env line is invisible and would otherwise
        # reach ZoneInfo and hard-fail the app on an otherwise valid name.
        assert localtime.resolve_timezone_name({"OMNIMETER_TIMEZONE": "  Europe/Amsterdam \n"}) == "Europe/Amsterdam"

    @pytest.mark.parametrize("env", [{}, {"OMNIMETER_TIMEZONE": ""}, {"OMNIMETER_TIMEZONE": "   "}])
    def test_unset_or_blank_falls_back_to_utc(self, env):
        assert localtime.resolve_timezone_name(env) == "UTC"

    def test_the_fallback_is_warned_about_not_silent(self, caplog):
        # The whole failure mode here is silence -- readings quietly landing
        # on the wrong day. If the fallback ever stops warning, this test is
        # the thing that notices.
        with caplog.at_level(logging.WARNING, logger="src.localtime"):
            localtime.resolve_timezone_name({})
        assert "OMNIMETER_TIMEZONE" in caplog.text
        assert "UTC" in caplog.text

    def test_a_configured_value_warns_about_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.localtime"):
            localtime.resolve_timezone_name({"OMNIMETER_TIMEZONE": "America/New_York"})
        assert caplog.text == ""


class TestResolveTimezone:
    def test_returns_a_zoneinfo_for_a_valid_name(self):
        assert localtime.resolve_timezone("Europe/Amsterdam") == ZoneInfo("Europe/Amsterdam")

    @pytest.mark.parametrize("bad", ["Not/AZone", "Europe/Amsterdum", "CEST", ""])
    def test_invalid_name_fails_closed_with_an_actionable_message(self, bad):
        # Fail closed rather than starting up and writing every timestamp in
        # the wrong offset -- same discipline as this app's refusal to start on
        # an unreachable secrets backend.
        with pytest.raises(RuntimeError) as exc:
            localtime.resolve_timezone(bad)
        assert "OMNIMETER_TIMEZONE" in str(exc.value)
        assert "not a valid IANA timezone" in str(exc.value)

    def test_error_names_a_real_example_the_user_can_copy(self):
        with pytest.raises(RuntimeError, match="Europe/Amsterdam"):
            localtime.resolve_timezone("Nowhere/Atall")


class TestModuleLevelResolution:
    def test_name_and_zone_agree_with_each_other(self):
        assert localtime.LOCAL_TZ == ZoneInfo(localtime.TIMEZONE_NAME)

    def test_every_consumer_shares_this_one_source(self):
        # The actual invariant the earlier bug was about. If a sixth module ever
        # re-reads the env var on its own and drifts, this catches it.
        from src import app, meter_ingest, tariff_parser, weather

        assert app._LOCAL_TZ is localtime.LOCAL_TZ
        assert meter_ingest._LOCAL_TZ is localtime.LOCAL_TZ
        assert tariff_parser._LOCAL_TZ is localtime.LOCAL_TZ
        # weather.py needs the string form for Open-Meteo's API, not the object
        assert weather.localtime.TIMEZONE_NAME is localtime.TIMEZONE_NAME

    def test_no_module_still_hardcodes_a_netherlands_default(self):
        # The Dutch default was an artifact of the author's own installation
        # that would silently mis-bucket
        # every reading for a self-hosted user elsewhere. It must not creep
        # back in as a "harmless" fallback.
        import pathlib

        src_dir = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders = [
            f.name
            for f in src_dir.glob("*.py")
            if 'OMNIMETER_TIMEZONE", "Europe/Amsterdam"' in f.read_text(encoding="utf-8")
        ]
        assert offenders == []
