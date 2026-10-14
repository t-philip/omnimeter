"""Layer 2 entry point -- retrains each metric's
context-aware expected-value model and checks yesterday's now-settled day
against it, sending one best-effort Telegram digest for anything flagged.
Meant to run nightly, via its own timer/service (omnimeter-anomaly.timer),
independent of meter_poller (Layer 1) and health_check (Layer 3) -- see
src/anomaly_model.py's module docstring for why.

    python -m scripts.anomaly
"""

import os
import sys

from src import anomaly_model, db
from src.credentials import configure as configure_credentials


def main() -> int:
    # Optional hardening for a systemd LoadCredential= deployment; a plain-env
    # deployment is unaffected (extra_dir=None falls through to os.environ).
    configure_credentials(extra_dir=os.environ.get("OMNIMETER_CREDENTIALS_DIR"))

    conn = db.get_connection()
    db.init_db(conn)

    flagged = anomaly_model.run_daily_check(conn)
    for ep in flagged:
        print(
            f"{ep['metric']}: {ep['total_volume']:.1f} {ep['unit']} "
            f"vs ~{ep['peak_rate']:.1f} {ep['unit']} expected (flagged)"
        )
    if not flagged:
        print("no metric exceeded its expected-usage threshold yesterday")
    return 0


if __name__ == "__main__":
    sys.exit(main())
