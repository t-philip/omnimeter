"""Restore omnimeter.db from a nightly backup.

Companion to scripts/backup.py -- until this script existed, restoring a
backup had never been tested even in principle.

Restore is DB-file-level only. It does NOT stop or start any service:
this script runs as the unprivileged `omnimeter` system user, same as
backup.py, with no permission to control services. Before restoring over the
live DB, stop the write paths yourself -- for a native systemd install:

    systemctl stop omnimeter-web.service omnimeter-ingest.timer omnimeter-api-ingest.service

or for the Docker Compose install:

    docker compose stop web ingest api-ingest

and start them again once this script has printed success.

Usage (from /opt/omnimeter, via the venv):
    .venv/bin/python -m scripts.restore --list
    .venv/bin/python -m scripts.restore --file omnimeter-20260717-033012.db
    .venv/bin/python -m scripts.restore --latest
    .venv/bin/python -m scripts.restore --latest --yes   # skip the confirmation prompt
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from scripts.backup import BACKUP_DIR, _parse_ts
from src.db import resolve_db_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("omnimeter-restore")


def list_backups() -> list[Path]:
    """Available backups, newest first. Reuses backup.py's own timestamp
    parser so a filename backup.py wouldn't recognize (and therefore would
    never prune) is likewise never offered here."""
    if not BACKUP_DIR.is_dir():
        return []
    files = [f for f in BACKUP_DIR.glob("omnimeter-*.db") if _parse_ts(f.name)]
    return sorted(files, key=lambda f: _parse_ts(f.name), reverse=True)  # type: ignore[arg-type,return-value]


def restore_from(backup_path: Path, dest_path: Path) -> None:
    if not backup_path.is_file():
        raise FileNotFoundError(f"backup file not found: {backup_path}")
    # sqlite3's online backup API, same direction-reversed approach as
    # backup.py's create_backup() -- correct even if dest_path has stale
    # -wal/-shm sidecars from the live DB being replaced, since backup()
    # goes through SQLite's own locking rather than a raw file copy.
    src_conn = sqlite3.connect(backup_path)
    dest_conn = sqlite3.connect(dest_path)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list available backups, newest first")
    parser.add_argument("--file", help="backup filename to restore, e.g. omnimeter-20260717-033012.db")
    parser.add_argument("--latest", action="store_true", help="restore the most recent backup")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    backups = list_backups()

    if args.list or not (args.file or args.latest):
        if not backups:
            print(f"No backups found in {BACKUP_DIR}")
            return 0
        print(f"Backups in {BACKUP_DIR} (newest first):")
        for f in backups:
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"  {f.name}  ({size_mb:.1f} MB)")
        return 0

    if args.latest:
        if not backups:
            log.error("No backups found in %s", BACKUP_DIR)
            return 1
        chosen = backups[0]
    else:
        assert args.file is not None
        chosen = BACKUP_DIR / Path(args.file).name  # strip any path component from the arg

    dest_path = resolve_db_path()
    print(f"About to restore {chosen.name} over the live DB at {dest_path}.")
    print("Make sure omnimeter-web.service and both ingest timers are already stopped.")
    if not args.yes:
        confirm = input("Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1

    try:
        restore_from(chosen, dest_path)
    except Exception:
        log.exception("Restore failed")
        return 1

    log.info("Restored %s -> %s", chosen.name, dest_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
