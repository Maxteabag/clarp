"""Exercise migration rehearsal through its CLI on private SQLite fixtures."""
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest


SCRIPT = (Path(__file__).resolve().parents[2] / "skills/clarp-server-admin"
          / "scripts/rehearse_state_upgrade.py")
CATALOG = "janitor_trigger_definitions"
ADDITIONS = f"INSERT INTO {CATALOG} VALUES ('new-one', 1), ('new-two', 0);"


@pytest.fixture
def rehearse(tmp_path):
    tmp_path.chmod(0o700)
    source = tmp_path / "source.sqlite"
    output = tmp_path / "rehearsal.sqlite"
    server = tmp_path / "candidate"
    (server / "lib").mkdir(parents=True)
    (server / "lib/__init__.py").write_text("")

    def run(migration, *, allowed=(), setup=""):
        with sqlite3.connect(source) as connection:
            connection.executescript(f"""
                PRAGMA user_version = 75;
                CREATE TABLE {CATALOG} (name TEXT, value);
                CREATE TABLE records (id INTEGER PRIMARY KEY, title TEXT);
                INSERT INTO records VALUES (1, 'preserve');
            """)
            connection.executemany(
                f"INSERT INTO {CATALOG} VALUES (?, ?)",
                [("seed", 1), ("seed", 1), ("binary", b"\x00x"), ("empty", None)],
            )
            if setup:
                connection.executescript(setup)
        original = source.read_bytes()
        candidate_sql = migration + "\nPRAGMA user_version = 76;"
        (server / "lib/db.py").write_text(
            "_SCHEMA_VERSION = 76\n"
            "def _migrate(connection):\n"
            f"    connection.executescript({candidate_sql!r})\n"
        )
        command = [sys.executable, str(SCRIPT), "--source", str(source),
                   "--server-root", str(server), "--output", str(output)]
        for table in allowed:
            command.extend(["--allow-added-rows", table])
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=tmp_path, timeout=10)
        assert source.read_bytes() == original
        if output.exists():
            assert stat.S_IMODE(output.stat().st_mode) == 0o600
        report = json.loads(result.stdout) if result.stdout else None
        return result, report, output

    return run


def test_strict_default_accepts_schema_additions(rehearse):
    result, report, _ = rehearse(f"ALTER TABLE {CATALOG} ADD COLUMN note TEXT;")
    assert result.returncode == 0, result.stderr
    assert report["verified_additive"]
    assert report["changed_existing_data"] == []
    assert "allowed_added_rows" not in report


def test_strict_default_rejects_catalog_insertions(rehearse):
    result, report, _ = rehearse(ADDITIONS)
    assert result.returncode == 1, result.stderr
    assert not report["verified_additive"]
    assert report["changed_existing_data"] == [CATALOG]


def test_allowed_catalog_reports_two_new_rows(rehearse):
    result, report, _ = rehearse(
        ADDITIONS + f"ALTER TABLE {CATALOG} ADD COLUMN note TEXT DEFAULT 'new';",
        allowed=[CATALOG],
    )
    assert result.returncode == 0, result.stderr
    assert report["verified_additive"]
    assert (report["before_version"], report["after_version"]) == (75, 76)
    assert report["changed_existing_data"] == []
    assert report["allowed_added_rows"] == {
        CATALOG: {"before": 4, "after": 6, "added": 2, "missing": 0},
    }


def test_allowance_is_repeatable_and_table_specific(rehearse):
    result, report, _ = rehearse(
        ADDITIONS + "INSERT INTO records VALUES (2, 'extra');",
        allowed=[CATALOG, "records", CATALOG],
    )
    assert result.returncode == 0, result.stderr
    assert report["allowed_added_rows"]["records"] == {
        "before": 1, "after": 2, "added": 1, "missing": 0,
    }
    assert len(report["allowed_added_rows"]) == 2


def test_unlisted_table_remains_strict(rehearse):
    result, report, _ = rehearse(
        ADDITIONS + "INSERT INTO records VALUES (2, 'unexpected');",
        allowed=[CATALOG],
    )
    assert result.returncode == 1, result.stderr
    assert not report["verified_additive"]
    assert report["changed_existing_data"] == ["records"]


@pytest.mark.parametrize("mutation", [
    f"UPDATE {CATALOG} SET value = 2 WHERE name = 'seed';",
    f"DELETE FROM {CATALOG} WHERE rowid = 1;",
    f"UPDATE {CATALOG} SET value = 1.0 WHERE rowid = 1;",
    f"UPDATE {CATALOG} SET value = '1' WHERE rowid = 1;",
])
def test_allowance_rejects_changed_or_missing_original_rows(rehearse, mutation):
    result, report, _ = rehearse(mutation + ADDITIONS, allowed=[CATALOG])
    assert result.returncode == 1, result.stderr
    assert not report["verified_additive"]
    assert report["changed_existing_data"] == [CATALOG]
    assert report["allowed_added_rows"][CATALOG]["missing"] > 0
    assert report["allowed_added_rows"][CATALOG]["added"] >= 2


@pytest.mark.parametrize("mutation", [
    f"DROP TABLE {CATALOG};",
    f"ALTER TABLE {CATALOG} DROP COLUMN value;",
])
def test_allowance_rejects_removed_table_or_column(rehearse, mutation):
    result, report, _ = rehearse(mutation, allowed=[CATALOG])
    assert result.returncode == 1, result.stderr
    assert not report["verified_additive"]
    assert report["changed_existing_data"] == [CATALOG]


def test_removed_column_cannot_be_replaced_by_sqlite_string_literal(rehearse):
    result, report, _ = rehearse(
        f"ALTER TABLE {CATALOG} DROP COLUMN value;",
        allowed=[CATALOG],
        setup=f"UPDATE {CATALOG} SET value = 'value';",
    )
    assert result.returncode == 1, result.stderr
    assert not report["verified_additive"]
    assert report["changed_existing_data"] == [CATALOG]
    assert report["allowed_added_rows"][CATALOG]["error"]


def test_allowance_compares_generated_columns(rehearse):
    result, report, _ = rehearse(
        f"ALTER TABLE {CATALOG} DROP COLUMN display;"
        f"ALTER TABLE {CATALOG} ADD COLUMN display TEXT "
        "GENERATED ALWAYS AS (name || ':changed') VIRTUAL;",
        allowed=[CATALOG],
        setup=f"ALTER TABLE {CATALOG} ADD COLUMN display TEXT "
              "GENERATED ALWAYS AS (name || ':original') VIRTUAL;",
    )
    assert result.returncode == 1, result.stderr
    assert not report["verified_additive"]
    assert report["changed_existing_data"] == [CATALOG]


@pytest.mark.parametrize("unknown", ["misspelled_catalog", "sqlite_master", "catalog; DROP TABLE records;"])
def test_unknown_allowance_is_rejected_before_migration(rehearse, unknown):
    result, report, output = rehearse(
        "CREATE TABLE migration_was_run (id INTEGER);", allowed=[unknown],
    )
    assert result.returncode == 2
    assert "unknown table" in result.stderr.lower()
    assert report is None
    if output.exists():
        with sqlite3.connect(output) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 75
            assert not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'migration_was_run'"
            ).fetchall()
