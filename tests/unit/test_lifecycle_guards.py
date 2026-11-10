"""Lifecycle guards (docs/architecture/lifecycles.md), in ratchet mode.

Every status column of state.sqlite either belongs to a declared lifecycle
(a module-level ``Lifecycle(...)`` under server/, whose construction already
refuses a non-final state without a ``Deadline`` or a ``WaitForever`` reason)
or is listed in UNCONVERTED with the lifecycle it will become. A new status
column fails until it is declared; a converted column must leave the list, and
the list may only shrink. The writer side of the same rule lives in
test_table_writers.py.
"""
from __future__ import annotations

import ast
import collections
import functools
import importlib
import pathlib
import re
import sqlite3

from lib import db
from lib.lifecycle import Lifecycle

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
STATUS_COLUMN = re.compile(r"(?:^|_)(?:status|state|phase)$")
# Lifecycle state that does not follow the naming pattern.
EXTRA_STATUS_COLUMNS = {("state_log", "kind")}
NOT_LIFECYCLES = {
    ("agents", "custom_status"): "free text the agent shows as its status line",
    ("background_job_events", "status"): "append-only event log; it records a job status, it has none",
}

_TURN = "turn and CLI process slot (TP-01/10/11)"
_GOAL = "goal (AUT-01/20)"
_LATER = "converts when a slice touches it"
UNCONVERTED: dict[tuple[str, str], str] = {
    ("state_log", "kind"): _TURN,
    ("task_plans", "status"): _GOAL,
    ("task_items", "status"): _GOAL,
    ("agent_goals", "status"): _GOAL,
    ("background_jobs", "status"): "background job (AUT-03)",
    ("tts_queue", "status"): "TTS clip (VP-01/05)",
    ("clips", "status"): "TTS clip (VP-01/05)",
    ("clips", "producer_status"): "TTS clip (VP-01/05)",
    ("dream_rounds", "status"): "dream round (AUT-06)",
    ("dream_runs", "status"): "dream round (AUT-06)",
    ("janitor_runs", "status"): "janitor run (AUT-15)",
    ("agents", "helper_state"): _LATER,
    ("agy_turn_authority", "terminal_status"): _LATER,
    ("artifact_decisions", "status"): _LATER,
    ("artifacts", "status"): _LATER,
    ("decision_deliveries", "status"): _LATER,
    ("decisions", "status"): _LATER,
    ("dream_threads", "status"): _LATER,
    ("dream_threads", "evidence_status"): _LATER,
    ("goal_delegations", "status"): _LATER,
    ("goal_runs", "status"): _LATER,
    ("goal_subgoals", "status"): _LATER,
    ("goals", "status"): _LATER,
    ("janitor_continuity", "status"): _LATER,
    ("messages", "phase"): _LATER,
    ("oracle_admissions", "status"): _LATER,
    ("orchestrator_pending_utterances", "status"): _LATER,
    ("provider_limit_episodes", "status"): _LATER,
    ("tool_explanation_jobs", "status"): _LATER,
    ("tool_explanation_mappings", "status"): _LATER,
    ("user_value_facts", "status"): _LATER,
}
# Lower this when a column converts; never raise it.
UNCONVERTED_CEILING = 31


def status_columns() -> set[tuple[str, str]]:
    return _schema_status_columns() - set(NOT_LIFECYCLES)


@functools.cache
def _schema_status_columns() -> frozenset[tuple[str, str]]:
    con = sqlite3.connect(":memory:")
    db._migrate(con)
    found = set(EXTRA_STATUS_COLUMNS)
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        found |= {(table, row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')
                  if STATUS_COLUMN.search(row[1])}
    con.close()
    return frozenset(found)


def _declaration_sites(source: str) -> list[str]:
    """Module-level names assigned a ``Lifecycle(...)`` call."""
    names = []
    for node in ast.parse(source).body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        func = value.func if isinstance(value, ast.Call) else None
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called == "Lifecycle":
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names += [t.id for t in targets if isinstance(t, ast.Name)]
    return names


@functools.cache
def declared_lifecycles() -> tuple[tuple[str, Lifecycle], ...]:
    found = []
    for path in sorted((SERVER / "lib").rglob("*.py")):
        rel = path.relative_to(SERVER).as_posix()
        if rel == "lib/lifecycle.py":
            continue
        names = _declaration_sites(path.read_text())
        if names:
            module = importlib.import_module(rel[:-3].replace("/", "."))
            found += [(rel, getattr(module, name)) for name in names]
    return tuple(found)


def lifecycle_owners() -> dict[tuple[str, str], str]:
    owners: dict[tuple[str, str], str] = {}
    for _, lc in declared_lifecycles():
        if lc.table:
            owners[(lc.table, lc.state_column)] = lc.owner
    return owners


def test_declarations_are_found():
    source = "from lib import lifecycle as lk\nJOB = lk.Lifecycle(name='x')\nB: Lifecycle = Lifecycle()\n" \
             "def f():\n    LOCAL = Lifecycle()\n"
    assert _declaration_sites(source) == ["JOB", "B"]


def test_every_declared_lifecycle_is_valid_and_named_once():
    declared = declared_lifecycles()
    names = collections.Counter(lc.name for _, lc in declared)
    assert not [n for n, count in names.items() if count > 1], names
    for rel, lc in declared:
        assert isinstance(lc, Lifecycle), rel
        assert lc.problems() == [], (rel, lc.problems())
        assert (SERVER / lc.owner).is_file(), f"{lc.name}: owner {lc.owner} does not exist"


def test_every_status_column_is_declared_or_listed():
    declared = set(lifecycle_owners())
    unlisted = sorted(status_columns() - declared - set(UNCONVERTED))
    assert not unlisted, (
        f"status columns without a lifecycle: {unlisted}. Declare a Lifecycle for them "
        "(docs/architecture/lifecycles.md); do not add them to UNCONVERTED.")


def test_unconverted_list_only_shrinks():
    columns, declared = status_columns(), set(lifecycle_owners())
    stale = sorted(key for key in UNCONVERTED if key not in columns or key in declared)
    assert not stale, f"remove from UNCONVERTED (gone or now declared): {stale}"
    assert len(UNCONVERTED) <= UNCONVERTED_CEILING, "UNCONVERTED grew; declare a Lifecycle instead"
    assert not set(NOT_LIFECYCLES) & set(UNCONVERTED)
    assert set(NOT_LIFECYCLES) <= _schema_status_columns(), "remove stale NOT_LIFECYCLES entries"
