"""Entry point for omnimeter-api-ingest.service -- long-running poller
against HomeWizard's local API v2 for the P1 meter, Plug-in Battery, and
Watermeter. Not a timer like omnimeter-ingest: this loops
continuously at a short interval to actually deliver "real-time"
data. Run via:
    python -m src.meter_poller

Expects each v2 device's Bearer token in the environment, under whatever
variable name its devices.json entry's token_env says (devices.json.example
defaults to OMNIMETER_*_TOKEN -- see
homewizard_api_client.HomeWizardDevice). Tokens come from the process
environment, e.g. a plain .env (see docker-compose.yml's env_file:
entries). A missing token doesn't stop the whole service -- that
device is just skipped every iteration (see meter_ingest.ingest_all's
'skipped' status) until it's paired via scripts/setup_wizard.py.

Every iteration commits before sleeping, so an abrupt stop (systemctl stop,
kill) never loses more than the in-flight cycle -- no shutdown handling
beyond a plain KeyboardInterrupt catch is needed for correctness.
"""

import logging
import os
import sys
import time

from . import aggregate, db, device_registry, runaway_detector
from . import meter_ingest as api_ingest
from .credentials import configure as configure_credentials

POLL_INTERVAL_SECONDS = 20  # comfortably above HomeWizard's 500ms rate-limit floor

# Daily-rollup tables (power_daily etc., what the dashboard's charts actually
# query) only get rebuilt when something calls aggregate.rebuild_all() --
# writing raw rows alone (ingest_all()) doesn't touch them. Without this,
# fresh api_live data would only ever show up in daily-max/rollup charts as
# an incidental side effect of omnimeter-ingest's own 15-min-interval
# rebuild call, so the daily-max charts could lag behind the raw data.
# Rebuilding
# every 20s poll would be wasteful as the raw tables grow over months, so
# this throttles to roughly once a minute instead.
REBUILD_EVERY_N_POLLS = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("meter_poller")


def _feature_enabled(conn) -> bool:
    row = conn.execute("SELECT homewizard_api_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
    return bool(row["v"]) if row else True


def _log_results(results: dict[str, str]) -> None:
    for device_name, status in results.items():
        if status == "ok":
            log.debug("%s: ok", device_name)
        elif status.startswith("auth_error"):
            log.error("%s: %s", device_name, status)
        elif status.startswith("connection_error"):
            log.warning("%s: %s", device_name, status)
        elif status.startswith("config_error"):
            log.warning("%s: %s", device_name, status)
        else:
            log.info("%s: %s", device_name, status)


def main() -> int:
    # Optional hardening for a systemd LoadCredential= deployment; a plain-env
    # deployment is unaffected (extra_dir=None falls through to os.environ).
    configure_credentials(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))

    conn = db.get_connection()
    db.init_db(conn)

    # Vendor-neutral on purpose, via each device's own is_configured() --
    # hwc.configured_devices() assumes every devices.json entry is
    # HomeWizard-shaped (ip/serial) and crashes with a bare KeyError on a
    # generic_json entry. That assumption stopped holding the moment a
    # generic device could legitimately sit in the same file (setup_wizard.py
    # now writes one when the user isn't on HomeWizard), so this startup
    # summary needs to read the same device list the polling loop below
    # actually uses, not hwc's own HomeWizard-only registry.
    startup_devices = device_registry.build_devices()
    configured = [d.name for d in startup_devices if d.is_configured()]
    not_configured = [d.name for d in startup_devices if not d.is_configured()]
    log.info(
        "omnimeter-api-ingest starting, poll interval=%ss, configured=%s, not_configured=%s",
        POLL_INTERVAL_SECONDS,
        configured or "none",
        not_configured or "none",
    )

    poll_count = 0
    try:
        while True:
            # Skip cleanly rather than exiting, so a
            # deliberate pause via Settings doesn't look like a crash to
            # health_check.py's is-active check. Re-checked every iteration
            # (not just at startup) so disabling it takes effect within one
            # poll cycle, not just on next restart.
            if not _feature_enabled(conn):
                log.info("homewizard_api_enabled is off -- skipping this cycle")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            devices = device_registry.build_devices()
            results = api_ingest.ingest_all(conn, devices)
            _log_results(results)

            # Layer 1 runaway detector. Broad except: a
            # defect in new alerting code must never take down the actual
            # ingest loop this service exists for. Network calls inside
            # here (Telegram) already have their own short timeout and
            # never raise -- see telegram_notifier.send_message -- this is
            # a second line of defence, not the primary one.
            if results.get("watermeter") == "ok":
                # Tier 1 (the current run) and Tier 2 (15-minute background
                # buckets) are independent detectors and both run every
                # cycle -- Tier 2 covers the sub-0.3 L/min band that no
                # Tier 1 threshold can reach, so skipping it when Tier 1 is
                # quiet would leave exactly the slowest leaks unwatched.
                #
                # Each gets its own try block, and each notifies before the
                # next one runs. Evaluating both up front and notifying
                # afterwards means a Tier 2 exception -- a malformed
                # water_readings.time row is enough -- swallows the whole
                # cycle and a burst-pipe alert that Tier 1 has already
                # opened is never delivered. That row persists, so it
                # would be suppressed on every subsequent cycle too.
                for detector in (runaway_detector.check_water,
                                 runaway_detector.check_water_background):
                    try:
                        episode = detector(conn)
                        if episode is not None:
                            runaway_detector.maybe_notify(conn, episode["id"])
                    except Exception:
                        log.exception("runaway detector %s failed this cycle", detector.__name__)

            poll_count += 1
            if poll_count % REBUILD_EVERY_N_POLLS == 0:
                aggregate.rebuild_all(conn)
                log.debug("rollups rebuilt")

            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("stopping (interrupt)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
