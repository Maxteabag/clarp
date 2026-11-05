"""Rule 5 of docs/architecture/state-and-boundaries.md: stores own their tables.

Counts the modules under server/ whose SQL string literals write each table
(INSERT/REPLACE/UPDATE/DELETE) and checks them against the owner map. A table
may gain a second writer only through ALLOWED, with a reason; an entry that no
longer matches a real writer fails too, so the list only shrinks.
"""
from __future__ import annotations

import ast
import collections
import functools
import pathlib
import re

import pytest

from tests.patching import logical_split_trees

ROOT = pathlib.Path(__file__).resolve().parents[2] / "server"
_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)\b")
_NOT_TABLES = {"set", "the", "a", "of", "to", "on", "it", "its", "or", "and"}

# Tables with a named store. `janitor_` covers every janitor_* table.
OWNERS = {
    "settings": "lib/settings_store.py",
    "agents": "lib/agents.py",
    "artifacts": "lib/artifacts.py",
    "janitor_": "lib/janitor_store.py",
    "turns": "lib/turn_lifecycle.py",
    "state_log": "lib/turn_lifecycle.py",
    "queued_turns": "lib/turn_queue.py",
}

_MIGRATION = "schema creation, seed rows or a one-time migration"
ALLOWED = {
    # Schema seeds and migrations.
    ("settings", "lib/db_migrations.py"): _MIGRATION,
    ("agents", "lib/db_migrations.py"): _MIGRATION,
    ("artifact_decisions", "lib/db_migrations.py"): _MIGRATION,
    ("decision_deliveries", "lib/db_migrations.py"): _MIGRATION,
    ("message_clock", "lib/db_migrations.py"): _MIGRATION,
    ("teams", "lib/db_migrations.py"): _MIGRATION,
    ("janitor_trigger_definitions", "lib/db_schema.py"): _MIGRATION,
    ("janitor_trigger_definitions", "lib/audio_bookkeeper.py"): _MIGRATION + " (its SCHEMA seeds the audio trigger)",
    # Retention sweeps: maintenance.py deletes expired rows from many tables.
    ("background_job_events", "lib/maintenance.py"): "retention sweep",
    ("state_log", "lib/maintenance.py"): "retention sweep",
    ("clips", "lib/maintenance.py"): "retention sweep",
    ("judgment_decisions", "lib/maintenance.py"): "retention sweep",
    ("sse_events", "lib/maintenance.py"): "retention sweep",
    ("tts_queue", "lib/maintenance.py"): "retention sweep",
    ("turn_usage", "lib/maintenance.py"): "retention sweep",
    # Existing multi-module stores not yet split behind one module.
    ("clips", "lib/clip_delivery/hls.py"): "HLS packaging records its segment state; clip_store is the owner",
    ("conversation_heads", "lib/message_live.py"): "message pipeline split across message_writes/message_live/prompt_admissions",
    ("conversation_heads", "lib/prompt_admissions.py"): "message pipeline split across message_writes/message_live/prompt_admissions",
    ("messages", "lib/message_live.py"): "message pipeline split across message_writes/message_live/prompt_admissions",
    ("messages", "lib/prompt_admissions.py"): "message pipeline split across message_writes/message_live/prompt_admissions",
    ("message_clock", "lib/prompt_admissions.py"): "message pipeline split across message_writes/message_live/prompt_admissions",
    ("team_inbox", "lib/message_live.py"): "team delivery from the live message path; team_store is the owner",
    ("team_messages", "lib/message_live.py"): "team delivery from the live message path; team_store is the owner",
    ("cursor_positions", "lib/transcript_watcher.py"): "watcher advances the cursor transcript_cursor owns",
    ("diagnostic_events", "lib/telemetry.py"): "telemetry and eventlog share the diagnostic log",
    ("media_assets", "lib/portrait_generation.py"): "generated portraits register their asset; media_store is the owner",
    ("tool_explanation_cache", "lib/tool_explanation_mappings.py"): "tool explanation cache split across three modules",
    ("tool_explanation_cache", "lib/tool_explanation_queue.py"): "tool explanation cache split across three modules",
}


def _sql_strings(tree: ast.AST):
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


def writers() -> dict[str, set[str]]:
    found: dict[str, set[str]] = collections.defaultdict(set)
    for rel, tree in logical_split_trees(sorted(ROOT.rglob("*.py")), ROOT).items():
        if "/tests/" in rel:
            continue
        for text in _sql_strings(tree):
            for match in _WRITE.finditer(text):
                table = match.group(1)
                if table not in _NOT_TABLES:
                    found[table].add(rel)
    return found


def _owner(table: str) -> str | None:
    return OWNERS.get(table) or next((o for p, o in OWNERS.items() if p.endswith("_") and table.startswith(p)), None)


def test_every_table_has_one_writer_or_an_allowed_reason():
    problems = []
    for table, modules in sorted(writers().items()):
        extra = {m for m in modules if (table, m) not in ALLOWED}
        owner = _owner(table)
        if owner is not None:
            extra.discard(owner)
            if extra:
                problems.append(f"{table}: owned by {owner}, also written by {sorted(extra)}")
        elif len(extra) > 1:
            problems.append(f"{table}: written by {sorted(extra)} with no owner")
    assert not problems, "\n".join(problems)


def test_owned_tables_are_written_by_their_owner():
    found = writers()
    for table, owner in OWNERS.items():
        tables = [t for t in found if t == table or (table.endswith("_") and t.startswith(table))]
        assert tables, f"no writer found for {table}"
        for name in tables:
            assert owner in found[name] or all((name, m) in ALLOWED for m in found[name]), (name, found[name])


def test_allowlist_entries_are_still_real():
    found = writers()
    stale = sorted(key for key in ALLOWED if key[1] not in found.get(key[0], set()))
    assert not stale, f"remove stale ALLOWED entries: {stale}"


# Status columns: docs/architecture/lifecycles.md, property 1. A status
# column is written only by the owner of its declared lifecycle. Columns not
# converted yet start from today's writers; an entry that no longer matches a
# real writer fails, so the list only shrinks. An UPDATE with a dynamic SET
# (or an INSERT with a dynamic column list) counts as writing every status
# column of its table.
# SQLite accepts an identifier bare or quoted as "x", `x` or [x].
_TABLE = r"[\"`\[]?([a-z_][a-z0-9_]*)[\"`\]]?"
_UPDATE_SET = re.compile(rf"\bUPDATE(?:\s+OR\s+\w+)?\s+{_TABLE}\s+SET\b", re.I)
_CLAUSE_END = re.compile(r"\b(?:WHERE|RETURNING|FROM)\b", re.I)


def _set_clause(text: str, start: int) -> str:
    """The SET list from `start` to the first WHERE/RETURNING/FROM outside parentheses."""
    depth, quote = 0, ""
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            quote = "" if ch == quote else quote
            continue
        if ch in "'\"`[":
            quote = "]" if ch == "[" else ch  # strings and quoted identifiers, e.g. [from]
            continue
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and ch.isalpha() and (i == 0 or not text[i - 1].isalnum()) and _CLAUSE_END.match(text, i):
            return text[start:i]
    return text[start:]


def _update_sets(text: str):
    """(table, SET list) for every UPDATE in text."""
    for match in _UPDATE_SET.finditer(text):
        yield match.group(1), _set_clause(text, match.end())
_INSERT_COLUMNS = re.compile(
    rf"\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+{_TABLE}\s*(?:\(([^)]*)\))?", re.I)


def _names_column(text: str, column: str, *, assigned: bool) -> bool:
    quoted = rf"(?<![\w\"`\[])[\"`\[]?{column}[\"`\]]?(?![\w\"`\]])"
    return re.search(quoted + (r"\s*=" if assigned else ""), text, re.I) is not None
_DYNAMIC = "{?}"

STATUS_WRITERS: dict[tuple[str, str], set[str]] = {
    ("agent_goals", "status"): {"lib/agent_goals.py"},
    ("agents", "helper_state"): {"lib/agents.py"},
    ("agy_turn_authority", "terminal_status"): {"lib/message_live.py"},
    ("artifact_decisions", "status"): {"lib/artifacts.py"},
    ("artifacts", "status"): {"lib/artifacts.py"},
    ("background_jobs", "status"): {"lib/background_jobs.py"},
    ("clips", "producer_status"): {"lib/clip_store.py"},
    ("clips", "status"): {"lib/clip_store.py"},
    ("decision_deliveries", "status"): {"lib/artifacts.py"},
    ("decisions", "status"): {"lib/leader_memory.py"},
    ("dream_rounds", "status"): {"lib/dreaming.py"},
    ("dream_runs", "status"): {"lib/dreaming.py"},
    ("dream_threads", "evidence_status"): {"lib/dreaming.py"},
    ("dream_threads", "status"): {"lib/dreaming.py"},
    ("form_submissions", "status"): {"lib/html_forms.py"},
    ("goal_delegations", "status"): {"lib/goal_ledger.py"},
    ("goal_subgoals", "status"): {"lib/goal_ledger.py"},
    ("janitor_continuity", "status"): {"lib/janitor_store.py"},
    ("janitor_runs", "status"): {"lib/janitor_store.py"},
    ("messages", "phase"): {"lib/message_live.py", "lib/message_writes.py"},
    ("model_fallback_attempts", "status"): {"lib/model_fallbacks.py"},
    ("oracle_admissions", "status"): {"lib/oracle_memory.py"},
    ("oracle_delegations", "status"): {"lib/oracle_delegations.py"},
    ("orchestrator_pending_utterances", "status"): {"lib/orchestrator.py"},
    ("podcast_conversations", "status"): {"lib/podcast_history.py"},
    ("provider_limit_episodes", "status"): {"lib/backend_usage.py"},
    ("queued_turns", "status"): {"lib/turn_queue.py"},
    ("state_log", "kind"): {"lib/turn_lifecycle.py"},
    ("task_items", "status"): {"lib/task_plans.py"},
    ("task_plans", "status"): {"lib/task_plans.py"},
    ("team_inbox", "status"): {"lib/team_store.py"},
    ("tool_explanation_jobs", "status"): {"lib/tool_explanation_queue.py"},
    ("tool_explanation_mappings", "status"): {"lib/tool_explanation_mappings.py"},
    ("tts_queue", "status"): {"lib/tts_queue.py"},
    ("user_value_facts", "status"): {"lib/leader_memory.py"},
}


def _sql_texts(tree: ast.AST):
    """Like _sql_strings, but an f-string is one text with {?} for each hole."""
    inner = {v.value for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
             for v in node.values if isinstance(v, ast.Constant)}
    yield from (text for text in _sql_strings(tree) if text not in inner)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield "".join(v.value if isinstance(v, ast.Constant) else _DYNAMIC for v in node.values)


def status_writes(text: str, by_table: dict[str, set[str]]) -> set[tuple[str, str]]:
    """The (table, status column) pairs one SQL text may write."""
    out = set()
    for table, assigned in _update_sets(text):
        out |= {(table, c) for c in by_table.get(table, ())
                if _DYNAMIC in assigned or _names_column(assigned, c, assigned=True)}
    for match in _INSERT_COLUMNS.finditer(text):
        table, names = match.groups()
        out |= {(table, c) for c in by_table.get(table, ())
                if names is None or _DYNAMIC in names or _names_column(names, c, assigned=False)}
    return out


@functools.cache
def status_writers() -> dict[tuple[str, str], set[str]]:
    from test_lifecycle_guards import status_columns
    columns = status_columns()
    by_table: dict[str, set[str]] = collections.defaultdict(set)
    for table, column in columns:
        by_table[table].add(column)
    found: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for rel, tree in logical_split_trees(sorted(ROOT.rglob("*.py")), ROOT).items():
        if "/tests/" in rel:
            continue
        for text in _sql_texts(tree):
            for key in status_writes(text, by_table):
                found[key].add(rel)
    return found


@pytest.mark.parametrize("source,writes", [
    ("'UPDATE jobs SET note = ? WHERE id = ?'", False),
    ("'UPDATE jobs SET status = ?, note = ? WHERE status = ?'", True),
    ("f'UPDATE jobs SET {fields} WHERE id = ?'", True),
    ("'UPDATE OR IGNORE \"jobs\" SET \"status\" = ? WHERE id = ?'", True),
    ("'UPDATE jobs SET [status]=? WHERE id = ?'", True),
    ("'UPDATE jobs SET status_note = ?, `status` = ? WHERE id = ?'", True),
    ("'UPDATE jobs SET old_status = ?, status_at = ? WHERE status = ?'", False),
    ("'UPDATE jobs SET n = (SELECT 1 FROM t WHERE x = 1), status = ? WHERE id = ?'", True),
    ("\"UPDATE jobs SET note = '(', status = ? WHERE id = ?\"", True),
    ("'UPDATE jobs SET [from] = ?, status = ? WHERE id = ?'", True),
    ("'UPDATE jobs SET \"where\" = ?, `returning` = ?, status = ? WHERE id = ?'", True),
    ("\"UPDATE jobs SET note = 'it''s FROM', status = ? WHERE id = ?\"", True),
    ("f'INSERT INTO jobs ({cols}) VALUES ({marks})'", True),
    ("'INSERT INTO \"jobs\" (id, \"status\") VALUES (?, ?)'", True),
    ("'INSERT INTO jobs (id, note) VALUES (?, ?)'", False),
    ("'INSERT OR REPLACE INTO jobs VALUES (?, ?)'", True),
])
def test_status_writer_scan_sees_literal_quoted_and_dynamic_writes(source, writes):
    texts = _sql_texts(ast.parse("x = " + source))
    found = any(status_writes(text, {"jobs": {"status"}}) for text in texts)
    assert found is writes


def status_writer_problems(found, owners, allowlist) -> list[str]:
    """Writers outside the owner. A declared column has exactly one writer:
    legacy exemptions end with the conversion."""
    problems = []
    for key, modules in sorted(found.items()):
        allowed = {owners[key]} if key in owners else allowlist.get(key, set())
        if modules - allowed:
            problems.append(f"{key[0]}.{key[1]} written by {sorted(modules - allowed)}; go through its lifecycle owner")
    return problems


def stale_status_writers(found, owners, allowlist) -> list[tuple[tuple[str, str], str]]:
    """Allowlist entries that no longer write, or whose column is now declared."""
    return sorted((key, module) for key, modules in allowlist.items() for module in modules
                  if module not in found.get(key, set()) or key in owners)


def test_a_declared_owner_ends_the_legacy_exemptions():
    key = ("clips", "status")
    found = {key: {"lib/clip_store.py", "lib/new_clip_owner.py"}}
    allowlist = {key: {"lib/clip_store.py"}}
    assert status_writer_problems(found, {}, allowlist) == [
        "clips.status written by ['lib/new_clip_owner.py']; go through its lifecycle owner"]
    owners = {key: "lib/new_clip_owner.py"}
    assert status_writer_problems(found, owners, allowlist) == [
        "clips.status written by ['lib/clip_store.py']; go through its lifecycle owner"]
    assert stale_status_writers(found, owners, allowlist) == [(key, "lib/clip_store.py")]
    assert stale_status_writers({key: set()}, {}, allowlist) == [(key, "lib/clip_store.py")]


def test_status_columns_are_written_only_through_their_lifecycle():
    from test_lifecycle_guards import lifecycle_owners
    problems = status_writer_problems(status_writers(), lifecycle_owners(), STATUS_WRITERS)
    assert not problems, "\n".join(problems)


def test_status_writer_allowlist_entries_are_still_real():
    from test_lifecycle_guards import lifecycle_owners
    stale = stale_status_writers(status_writers(), lifecycle_owners(), STATUS_WRITERS)
    assert not stale, f"remove stale STATUS_WRITERS entries: {stale}"


@pytest.mark.parametrize("split", [False, True])
def test_writer_guards_survive_a_split_and_still_reject_violations(tmp_path, monkeypatch, split):
    from tests import patching
    import test_lifecycle_guards as lifecycle_guards
    root = tmp_path / "server"
    (root / "lib").mkdir(parents=True)
    recipes = tmp_path / "splits"
    recipes.mkdir()
    (recipes / "store.toml").write_text(
        '[[split]]\nname="store"\nmodule="lib/store.py"\n'
        '[[split.part]]\nmodule="lib/store_part.py"\nnames=["write"]\n')
    monkeypatch.setattr(patching, "_RECIPES", recipes)
    monkeypatch.setitem(globals(), "ROOT", root)
    monkeypatch.setitem(globals(), "OWNERS", {"things": "lib/store.py", "forbidden": "lib/other.py"})
    monkeypatch.setitem(globals(), "ALLOWED", {})
    monkeypatch.setitem(globals(), "STATUS_WRITERS", {("things", "status"): {"lib/store.py"}})
    monkeypatch.setattr(lifecycle_guards, "status_columns", lambda: [("things", "status"), ("forbidden", "status")])
    monkeypatch.setattr(lifecycle_guards, "lifecycle_owners", lambda: {})
    owner = root / "lib/store.py"
    source = root / "lib/store_part.py" if split else owner
    if split:
        owner.write_text("from .store_part import write\n")
    good = "def write(conn):\n    conn.execute(\"UPDATE things SET status='done'\")\n"
    source.write_text(good)
    status_writers.cache_clear()
    try:
        test_every_table_has_one_writer_or_an_allowed_reason()
        test_status_columns_are_written_only_through_their_lifecycle()
        test_status_writer_allowlist_entries_are_still_real()
        source.write_text(good + "    conn.execute(\"UPDATE forbidden SET status='done'\")\n")
        status_writers.cache_clear()
        with pytest.raises(AssertionError, match="forbidden"):
            test_every_table_has_one_writer_or_an_allowed_reason()
        with pytest.raises(AssertionError, match="forbidden.status"):
            test_status_columns_are_written_only_through_their_lifecycle()
        if split:
            source.write_text(good + "def unlisted(conn):\n    conn.execute(\"UPDATE things SET status='done'\")\n")
            status_writers.cache_clear()
            with pytest.raises(AssertionError, match="lib/store_part.py"):
                test_every_table_has_one_writer_or_an_allowed_reason()
            with pytest.raises(AssertionError, match="lib/store_part.py"):
                test_status_columns_are_written_only_through_their_lifecycle()
    finally:
        status_writers.cache_clear()
