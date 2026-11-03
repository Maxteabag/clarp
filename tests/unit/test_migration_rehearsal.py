"""scripts/migration_rehearsal.py: a v61 database in production proportions
upgrades to HEAD within budget and keeps its rows.

The default gate runs a tiny rehearsal so the script cannot rot. The
production-sized one (about 1M state_log rows, a few GB in /var/tmp) is
opt-in: CLARP_MIGRATION_REHEARSAL=1, and run it through the heavy wrapper.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "migration_rehearsal.py"
_SPEC = importlib.util.spec_from_file_location("migration_rehearsal_script", _SCRIPT)
assert _SPEC and _SPEC.loader
rehearsal = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rehearsal)


def test_small_v61_rehearsal_upgrades_and_keeps_every_row():
    report = rehearsal.rehearse("v61", scale=0.002, payload=0.05, budget=60.0)

    assert report["ok"], report
    assert (report["base_version"], report["head_version"]) == (61, rehearsal_head_version())
    assert report["state_log_rows"] == 2_000
    assert report["state_log_preserved"]
    assert report["row_count_changes"] == {}
    assert report["fk_violations_before"] == {}
    assert report["underfilled_tables"] == []
    assert report["dropped_tables"] == ["diagnostic_events"]


def test_profile_holds_only_counts_and_sizes_and_drives_the_fill(tmp_path):
    source = tmp_path / "source.sqlite"
    con = sqlite3.connect(source)
    con.executescript("CREATE TABLE state_log (state_id INTEGER PRIMARY KEY, kind TEXT, detail TEXT);"
                      "CREATE TABLE empty (x TEXT);"
                      "CREATE TABLE keyed (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;"
                      "INSERT INTO keyed VALUES ('a', 'secret'), ('b', 'secret');")
    con.executemany("INSERT INTO state_log (kind, detail) VALUES (?, ?)", [("idle", "secret")] * 30)
    con.commit()
    con.close()
    before = source.read_bytes()

    profile_path = tmp_path / "profile.json"
    assert rehearsal.main(["--write-profile", str(profile_path), "--from-db", str(source)]) == 0
    assert source.read_bytes() == before
    text = profile_path.read_text()
    assert "secret" not in text and "idle" not in text
    data = json.loads(text)
    assert data["kind"] == "clarp-state-profile"
    assert data["tables"] == {"empty": [0, 0], "keyed": [2, 7], "state_log": [30, 11]}
    assert data["row_methods"] == {"empty": "rowid_span", "keyed": "without_rowid_sample_floor",
                                   "state_log": "rowid_span"}

    profile = rehearsal.load_profile(profile_path)
    report = rehearsal.rehearse("v61", scale=1.0, payload=0.05, budget=60.0,
                                profile={**profile, "agents": (3, 50)})
    assert report["ok"], report
    assert report["state_log_rows"] == 30


def test_profile_rows_come_from_the_rowid_span_without_counting(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t VALUES (?, 'xx')", [(i,) for i in range(1, 101)])
    con.execute("DELETE FROM t WHERE id BETWEEN 1 AND 10")
    con.commit()
    con.close()
    statements = []
    real_connect = sqlite3.connect

    def traced(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(sqlite3, "connect", traced)

    data = rehearsal.write_profile(source, sample=5)

    # ids 11..100 span 90 rows; the newest five (96..100) average 2.2 + 2 bytes.
    assert data["tables"]["t"] == [90, 4]
    assert statements and not [s for s in statements if "count(" in s.lower()]
    con = real_connect(source)
    plans = [r[3] for sql in statements if sql.lstrip().upper().startswith("SELECT") and "LIMIT" not in sql.upper()
             and '"t"' in sql for r in con.execute("EXPLAIN QUERY PLAN " + sql)]
    con.close()
    assert plans and not [p for p in plans if p.upper().startswith("SCAN")], plans


def test_without_rowid_floor_is_fetched_not_counted(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite"
    con = sqlite3.connect(source)
    con.executescript("CREATE TABLE keyed (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;"
                      "INSERT INTO keyed VALUES ('a', 'xyz'), ('b', 'xyz'), ('c', 'xyz');")
    con.close()
    statements = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _traced(real_connect(*a, **k), statements))

    data = rehearsal.write_profile(source, sample=2)

    assert data["tables"]["keyed"] == [2, 4]
    assert not [s for s in statements if "count(" in s.lower()]


def _traced(connection, statements):
    connection.set_trace_callback(statements.append)
    return connection


def test_profile_output_never_replaces_a_database_or_other_file(tmp_path):
    source = tmp_path / "source.sqlite"
    con = sqlite3.connect(source)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'x');")
    con.close()
    before = source.read_bytes()
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        rehearsal.main(["--write-profile", str(source), "--from-db", str(source)])
    assert source.read_bytes() == before
    other = tmp_path / "notes.json"
    other.write_text('{"kind": "something-else"}')
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        rehearsal.main(["--write-profile", str(other), "--from-db", str(source)])
    profile = tmp_path / "profile.json"
    assert rehearsal.main(["--write-profile", str(profile), "--from-db", str(source)]) == 0
    assert rehearsal.main(["--write-profile", str(profile), "--from-db", str(source)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.json", "profile.json", "source.sqlite"]


def _tiny_db(path):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'x');")
    con.close()
    return path.read_bytes()


@pytest.mark.parametrize("name", ["profile.json.partial", ".profile.json.partial"])
def test_staging_never_collides_with_the_source(tmp_path, name):
    source = tmp_path / name
    before = _tiny_db(source)
    target = tmp_path / "profile.json"
    assert rehearsal.main(["--write-profile", str(target), "--from-db", str(source)]) == 0
    assert source.read_bytes() == before
    assert json.loads(target.read_text())["kind"] == "clarp-state-profile"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([name, "profile.json"])


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_the_source_and_its_companions_are_refused_before_any_read(tmp_path, suffix, monkeypatch):
    source = tmp_path / "state.sqlite"
    before = _tiny_db(source)
    monkeypatch.setattr(rehearsal, "write_profile", lambda *a, **k: pytest.fail("profiled before refusing"))
    with pytest.raises(SystemExit, match="it is the source database"):
        rehearsal.main(["--write-profile", str(tmp_path / ("state.sqlite" + suffix)), "--from-db", str(source)])
    assert source.read_bytes() == before


def test_a_hard_link_to_the_source_is_refused(tmp_path):
    source = tmp_path / "state.sqlite"
    before = _tiny_db(source)
    alias = tmp_path / "alias.json"
    os.link(source, alias)
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        rehearsal.main(["--write-profile", str(alias), "--from-db", str(source)])
    assert source.read_bytes() == before


def test_a_large_target_is_refused_from_its_size_without_reading_it(tmp_path, monkeypatch):
    target = tmp_path / "big.json"
    target.write_text('{"kind": "clarp-state-profile"}')
    real_stat = pathlib.Path.stat

    def large(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == target:
            fields = list(result)
            fields[6] = 4 * 1024**3
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(pathlib.Path, "stat", large)
    monkeypatch.setattr(pathlib.Path, "open", lambda *a, **k: pytest.fail("read a large target"))
    monkeypatch.setattr(pathlib.Path, "read_text", lambda *a, **k: pytest.fail("read a large target"))
    with pytest.raises(SystemExit, match="not a clarp-state-profile"):
        rehearsal.save_profile(target, {"kind": "clarp-state-profile", "tables": {}})


def test_a_symlink_target_is_refused(tmp_path):
    source = tmp_path / "state.sqlite"
    before = _tiny_db(source)
    link = tmp_path / "profile.json"
    link.symlink_to(source)
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        rehearsal.main(["--write-profile", str(link), "--from-db", str(tmp_path / "other.sqlite")])
    assert source.read_bytes() == before


def rehearsal_head_version() -> int:
    from lib import db
    return db._SCHEMA_VERSION


@pytest.mark.timeout(3600)
@pytest.mark.skipif(os.environ.get("CLARP_MIGRATION_REHEARSAL") != "1",
                    reason="production-sized rehearsal is opt-in: CLARP_MIGRATION_REHEARSAL=1")
@pytest.mark.parametrize("base", ["v61", None])
def test_production_sized_rehearsal_within_budget(base, capsys):
    args = ["--base", base] if base else []
    assert rehearsal.main(args) == 0, capsys.readouterr().out
