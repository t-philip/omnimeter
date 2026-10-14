"""Layer 3 entry point -- runs the staleness check for
every metric and notifies for anything still stale. Meant to run hourly,
via its own timer/service, independent of meter_poller (see
src/health_monitor.py's module docstring for why).

    python -m scripts.health_check
"""

import os
import sys

from src import db, health_monitor
from src.credentials import configure as configure_credentials


def main() -> int:
    # Optional hardening for a systemd LoadCredential= deployment; a plain-env
    # deployment is unaffected (extra_dir=None falls through to os.environ).
    configure_credentials(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))

    conn = db.get_connection()
    db.init_db(conn)

    active = health_monitor.check_health(conn)
    for episode in active:
        health_monitor.maybe_notify(conn, episode["id"])
        print(f"{episode['metric']}: stale, {episode['total_volume']:.0f} min since last reading")

    if not active:
        print("all metrics with prior live data are fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
