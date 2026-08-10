"""Entry point for omnimeter-api-ingest.service -- long-running poller
against HomeWizard's local API v2 for the P1 meter, Plug-in Battery, and
Watermeter. Not a timer like omnimeter-ingest: this loops
continuously at a short interval to actually deliver "real-time"
data. Run via:
    python -m src.meter_poller

Expects each v2 device's Bearer token in the environment, under whatever
variable name its devices.json entry's token_env says (the reference
deployment's devices.json uses HOMEWIZARD_*_API_TOKEN names; a self-hosted
install's devices.json.example defaults to OMNIMETER_*_TOKEN -- see
homewizard_api_client.HomeWizardDevice). The reference deployment sources
these from its own secrets backend before this is invoked; a self-hosted
install sources a plain .env instead (see docker-compose.yml's env_file:
entries). A missing token doesn't stop the whole service -- that
device is just skipped every iteration (see meter_ingest.ingest_all's
'skipped' status) until it's paired via the reference deployment's pairing
script or scripts/setup_wizard.py (self-hosted).

Every iteration commits before sleeping, so an abrupt stop (systemctl stop,
kill) never loses more than the in-flight cycle -- no shutdown handling
beyond a plain KeyboardInterrupt catch is needed for correctness.
"""

import logging
import sys
import time

from . import aggregate, db, device_registry
from . import homewizard_api_client as hwc
from . import meter_ingest as api_ingest

POLL_INTERVAL_SECONDS = 20  # comfortably above HomeWizard's 500ms rate-limit floor

# Daily-rollup tables (power_daily etc., what the dashboard's charts actually
# query) only get rebuilt when something calls aggregate.rebuild_all() --
# writing raw rows alone (ingest_all()) doesn't touch them. Without this,
# fresh api_live data would only ever show up in daily-max/rollup charts as
# an incidental side effect of omnimeter-ingest's own 15-min-interval
# rebuild call -- confirmed live 2026-07-24 (Phase load daily-max chart
# stuck days behind real data because of exactly this gap). Rebuilding
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
        else:
            log.info("%s: %s", device_name, status)


def main() -> int:
    conn = db.get_connection()
    db.init_db(conn)

    configured = hwc.configured_devices()
    not_configured = [n for n in hwc.DEVICES if n not in configured]
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
