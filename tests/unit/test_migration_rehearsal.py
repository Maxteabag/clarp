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
                      "CREATE TABLE empty (x TEXT);")
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
    assert data["tables"] == {"empty": [0, 0], "state_log": [30, 11]}

    profile = rehearsal.load_profile(profile_path)
    report = rehearsal.rehearse("v61", scale=1.0, payload=0.05, budget=60.0,
                                profile={**profile, "agents": (3, 50)})
    assert report["ok"], report
    assert report["state_log_rows"] == 30


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
