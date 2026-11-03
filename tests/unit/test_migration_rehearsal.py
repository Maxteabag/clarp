"""scripts/migration_rehearsal.py: a v61 database in production proportions
upgrades to HEAD within budget and keeps its rows.

The default gate runs a tiny rehearsal so the script cannot rot. The
production-sized one (about 1M state_log rows, a few GB in /var/tmp) is
opt-in: CLARP_MIGRATION_REHEARSAL=1, and run it through the heavy wrapper.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

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
