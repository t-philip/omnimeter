import sqlite3

import pytest

from scripts import backup, restore


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "live.db"))
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(restore, "BACKUP_DIR", backup_dir)
    return tmp_path


def _make_backup(tmp_path, name, pv_notes):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE pv_config (id INTEGER PRIMARY KEY, notes TEXT)")
    conn.execute("INSERT INTO pv_config VALUES (1, ?)", (pv_notes,))
    conn.commit()
    conn.close()
    return path


class TestListBackups:
    def test_empty_dir_returns_nothing(self, env):
        assert restore.list_backups() == []

    def test_sorted_newest_first(self, env):
        _make_backup(env, "omnimeter-20260701-030000.db", "old")
        _make_backup(env, "omnimeter-20260715-030000.db", "newer")
        names = [f.name for f in restore.list_backups()]
        assert names == ["omnimeter-20260715-030000.db", "omnimeter-20260701-030000.db"]

    def test_ignores_files_not_matching_the_backup_timestamp_pattern(self, env):
        backup_dir = env / "backups"
        backup_dir.mkdir(parents=True)
        (backup_dir / "omnimeter-not-a-timestamp.db").write_text("junk")
        assert restore.list_backups() == []


class TestRestoreFrom:
    def test_restores_content_over_the_live_db(self, env):
        backup_path = _make_backup(env, "omnimeter-20260715-030000.db", "restored value")
        dest_path = env / "live.db"

        # Live DB currently has different content -- simulates the real
        # scenario of restoring over a DB that's since diverged.
        live_conn = sqlite3.connect(dest_path)
        live_conn.execute("CREATE TABLE pv_config (id INTEGER PRIMARY KEY, notes TEXT)")
        live_conn.execute("INSERT INTO pv_config VALUES (1, 'live value')")
        live_conn.commit()
        live_conn.close()

        restore.restore_from(backup_path, dest_path)

        check_conn = sqlite3.connect(dest_path)
        notes = check_conn.execute("SELECT notes FROM pv_config WHERE id = 1").fetchone()[0]
        check_conn.close()
        assert notes == "restored value"

    def test_missing_backup_file_raises(self, env):
        with pytest.raises(FileNotFoundError):
            restore.restore_from(env / "backups" / "does-not-exist.db", env / "live.db")


class TestMainCli:
    def test_list_flag_reports_no_backups_found(self, env, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["restore.py", "--list"])
        assert restore.main() == 0
        assert "No backups found" in capsys.readouterr().out

    def test_list_flag_shows_available_backups(self, env, monkeypatch, capsys):
        _make_backup(env, "omnimeter-20260715-030000.db", "restored value")
        monkeypatch.setattr("sys.argv", ["restore.py", "--list"])
        assert restore.main() == 0
        assert "omnimeter-20260715-030000.db" in capsys.readouterr().out

    def test_no_args_defaults_to_listing_without_restoring(self, env, monkeypatch, capsys):
        # Neither --file nor --latest given -- must not restore anything.
        _make_backup(env, "omnimeter-20260715-030000.db", "restored value")
        monkeypatch.setattr("sys.argv", ["restore.py"])
        assert restore.main() == 0
        assert not (env / "live.db").exists()

    def test_restore_latest_with_yes_skips_prompt(self, env, monkeypatch, capsys):
        _make_backup(env, "omnimeter-20260715-030000.db", "restored value")
        monkeypatch.setattr("sys.argv", ["restore.py", "--latest", "--yes"])

        assert restore.main() == 0

        conn = sqlite3.connect(env / "live.db")
        notes = conn.execute("SELECT notes FROM pv_config WHERE id = 1").fetchone()[0]
        conn.close()
        assert notes == "restored value"

    def test_restore_without_yes_aborts_on_declined_confirmation(self, env, monkeypatch):
        _make_backup(env, "omnimeter-20260715-030000.db", "restored value")
        monkeypatch.setattr("sys.argv", ["restore.py", "--latest"])
        monkeypatch.setattr("builtins.input", lambda _: "no")

        assert restore.main() == 1
        assert not (env / "live.db").exists()

    def test_file_arg_strips_any_path_component(self, env, monkeypatch):
        # A malicious/careless --file value must not be able to reach outside
        # BACKUP_DIR (e.g. "../../etc/passwd").
        _make_backup(env, "omnimeter-20260715-030000.db", "restored value")
        monkeypatch.setattr("sys.argv", ["restore.py", "--file", "../omnimeter-20260715-030000.db", "--yes"])

        assert restore.main() == 0
        conn = sqlite3.connect(env / "live.db")
        notes = conn.execute("SELECT notes FROM pv_config WHERE id = 1").fetchone()[0]
        conn.close()
        assert notes == "restored value"

    def test_latest_with_no_backups_fails(self, env, monkeypatch):
        monkeypatch.setattr("sys.argv", ["restore.py", "--latest", "--yes"])
        assert restore.main() == 1
