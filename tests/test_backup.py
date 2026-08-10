import pytest

from scripts import backup
from src import db


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    return tmp_path


class TestNightlyBackupToggle:
    def test_skips_when_disabled(self, env):
        # create_backup() must never run when disabled in Settings --
        # main() must still exit 0 so job_monitor.py doesn't flag a
        # deliberately-disabled backup as FAILED.
        conn = db.get_connection()
        db.init_db(conn)
        conn.execute("UPDATE feature_toggles SET nightly_backup_enabled = 0 WHERE id = 1")
        conn.commit()
        conn.close()

        assert backup.main() == 0
        assert not backup.BACKUP_DIR.exists()

    def test_runs_when_enabled_by_default(self, env):
        # No prior init_db() call here on purpose -- _nightly_backup_enabled()
        # must create the schema (including feature_toggles' default row)
        # itself against a DB that doesn't exist yet, same as a fresh
        # install where the timer could in principle fire before the web app
        # ever has.
        assert backup.main() == 0
        backups = list(backup.BACKUP_DIR.glob("omnimeter-*.db"))
        assert len(backups) == 1

    def test_enabled_check_works_against_pre_existing_db_missing_the_table(self, env):
        # Simulates a DB created before this toggle existed: feature_toggles
        # doesn't exist yet, only the older tables do.
        import sqlite3

        conn = sqlite3.connect(str(env / "test.db"))
        conn.execute("CREATE TABLE pv_config (id INTEGER PRIMARY KEY CHECK (id = 1))")
        conn.commit()
        conn.close()

        assert backup._nightly_backup_enabled() is True
