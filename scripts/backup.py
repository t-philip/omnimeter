"""Nightly backup of omnimeter.db to a configured backup destination.

omnimeter.db holds three things that aren't recoverable from anywhere else
if the host is lost: historical HA-sourced 'live' data (from before
a later change removed that ingest path 2026-07-28 -- HA's own recorder purges it,
so this DB is the only remaining copy) plus the ongoing real-time 'api_live'
history that replaced it, the manually-entered/PDF-parsed rate schedules,
and pv_config. The CSV exports themselves are re-exportable from the
HomeWizard app and are not backed up here.

Uses sqlite3's own online backup API (Connection.backup()) rather than a
raw file copy -- safe against a concurrent writer (the web app / ingest
timers) and correct under WAL mode, where a plain cp could grab a torn
snapshot mid-checkpoint.
"""

import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.db import get_connection, init_db, resolve_db_path

# Generic fallback for a bare/no-Docker run with nothing configured.
# Both real deployment shapes set this explicitly rather than relying on the
# default: the self-hosted Docker image always passes OMNIMETER_BACKUP_DIR=
# /backups (see docker-compose.yml), and a native systemd reference
# deployment sets it in its own backup service unit, outside this repo.
BACKUP_DIR = Path(os.environ.get("OMNIMETER_BACKUP_DIR", "./backups"))
RETENTION_DAYS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("omnimeter-backup")


def create_backup() -> Path:
    src_path = resolve_db_path()
    if not src_path.exists():
        raise FileNotFoundError(f"source DB not found: {src_path}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest_path = BACKUP_DIR / f"omnimeter-{timestamp}.db"

    src_conn = sqlite3.connect(src_path)
    dest_conn = sqlite3.connect(dest_path)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()

    return dest_path


def _parse_ts(name: str) -> datetime | None:
    try:
        return datetime.strptime(name.removeprefix("omnimeter-").removesuffix(".db"), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def prune_old() -> None:
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
    for f in BACKUP_DIR.glob("omnimeter-*.db"):
        ts = _parse_ts(f.name)
        if ts and ts < cutoff:
            f.unlink()
            log.info("Pruned: %s", f.name)


def _nightly_backup_enabled() -> bool:
    """Settings toggle. Goes through get_connection()/init_db() (not a
    bare sqlite3.connect) so feature_toggles + its default row exist even
    against a DB created before this toggle was added -- both calls are
    cheap and idempotent (see app.py's own startup init_db() call)."""
    conn = get_connection()
    try:
        init_db(conn)
        row = conn.execute("SELECT nightly_backup_enabled AS v FROM feature_toggles WHERE id = 1").fetchone()
        return bool(row["v"]) if row else True
    finally:
        conn.close()


def main() -> int:
    if not BACKUP_DIR.parent.is_dir():
        log.error("%s not present -- is the backup bind mount missing?", BACKUP_DIR.parent)
        return 1

    if not _nightly_backup_enabled():
        log.info("Nightly backup disabled in Settings -- skipping")
        return 0

    log.info("=== omnimeter-backup started ===")
    try:
        dest = create_backup()
    except Exception:
        log.exception("Backup failed")
        return 1
    log.info("Backup: %s (%d bytes)", dest.name, dest.stat().st_size)

    prune_old()
    log.info("=== omnimeter-backup complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
