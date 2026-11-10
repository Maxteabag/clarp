"""Start-version matrix for _migrate (S1 commit 4, docs/architecture/s1-layering.md 3.2).

For a fresh database and for every supported start version up to the head,
this records which _migrate_to_vN step functions _migrate calls, a digest of
the exact SQL it executes (inline steps included), the final user_version and
the error it ends with, if any. The step functions are replaced by recorders,
so the matrix pins _migrate's control flow (the else boundary, the v84+ region
that also runs on a fresh database, `0 < version < 121`, the transaction and
the stamp), not what each step does.

Each start is a head-shaped database stamped with that version, so an inline
step can hit an artificial conflict (v72 creating a table that already exists).
The recording connection logs such an SQLite error into the statement list and
carries on, so every start, 61..71 included, reaches every later version test
and the final stamp. Step semantics are proven elsewhere: by the schema goldens
and the migration rehearsal.

S1 moves _migrate and every step verbatim; the matrix must be byte-identical
before and after. Regenerate only for a schema change, with
UPDATE_SCHEMA_GOLDENS=1, and review the diff.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sqlite3

from lib import db_migrations, db_schema

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "schema" / "migration_matrix.json"


def _steps() -> list[str]:
    return sorted((n for n in dir(db_migrations) if re.fullmatch(r"_migrate_to_v\d+", n)),
                  key=lambda n: int(n.rpartition("v")[2]))


class RecordingConnection:
    """The only connection method _migrate uses is execute(). Each statement is
    recorded; an SQLite error is recorded too and answered with an empty
    cursor, so control flow continues past artificial conflicts."""

    def __init__(self, con: sqlite3.Connection, statements: list[str]):
        self._con, self._statements = con, statements

    def execute(self, sql, parameters=()):
        self._statements.append(" ".join(str(sql).split()))
        try:
            return self._con.execute(sql, parameters)
        except sqlite3.Error as exc:
            self._statements.append(f"! {type(exc).__name__}: {exc}")
            return self._con.execute("SELECT 1 WHERE 0")


def run_from(start: int, monkeypatch) -> dict:
    con = sqlite3.connect(":memory:", isolation_level=None)
    if start:
        db_migrations._migrate(con)
        con.execute(f"PRAGMA user_version = {start}")
    calls: list[str] = []
    statements: list[str] = []
    with monkeypatch.context() as patch:
        for name in _steps():
            patch.setattr(db_migrations, name, lambda _con, _name=name: calls.append(_name))
        try:
            db_migrations._migrate(RecordingConnection(con, statements))
            error = None
        except Exception as exc:  # recorded, not swallowed: the error is part of the matrix
            error = f"{type(exc).__name__}: {exc}"
    final = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    digest = hashlib.sha256("\n".join(statements).encode()).hexdigest()
    return {"calls": calls, "statements": len(statements), "sql_sha256": digest, "final": final, "error": error}


def matrix(monkeypatch) -> dict[str, dict]:
    starts = [0, *range(db_schema._MIN_UPGRADABLE_VERSION, db_schema._SCHEMA_VERSION)]
    return {str(start): run_from(start, monkeypatch) for start in starts}


def test_migrate_control_flow_matches_the_matrix(monkeypatch):
    actual = matrix(monkeypatch)
    if os.environ.get("UPDATE_SCHEMA_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    expected = json.loads(GOLDEN.read_text())
    changed = sorted((k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k)), key=int)
    assert not changed, (f"_migrate behaves differently for start versions {changed}; first: "
                         f"expected {expected.get(changed[0])}, got {actual.get(changed[0])}")


def test_the_matrix_shows_the_known_paths(monkeypatch):
    fresh, from_119 = run_from(0, monkeypatch), run_from(119, monkeypatch)
    assert fresh["error"] is None and fresh["final"] == db_schema._SCHEMA_VERSION
    assert "_migrate_to_v89" in fresh["calls"]          # the v84+ region runs on a fresh database
    assert "_migrate_to_v62" not in fresh["calls"]      # the else branch does not
    assert "_migrate_to_v121" not in fresh["calls"]     # 0 < version < 121
    assert from_119["calls"] == ["_migrate_to_v121"] and from_119["final"] == db_schema._SCHEMA_VERSION


def test_every_supported_start_reaches_the_final_stamp(monkeypatch):
    for start in range(db_schema._MIN_UPGRADABLE_VERSION, db_schema._SCHEMA_VERSION):
        result = run_from(start, monkeypatch)
        assert result["error"] is None and result["final"] == db_schema._SCHEMA_VERSION, (start, result)
        assert "_migrate_to_v121" in result["calls"], start      # every upgrade, early ones included
