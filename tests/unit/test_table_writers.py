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
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
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
_UPDATE_SET = re.compile(
    r"\bUPDATE\s+([a-z_][a-z0-9_]*)\s+SET\b(.*?)(?=\bWHERE\b|\bRETURNING\b|\Z)", re.S | re.I)
_INSERT_COLUMNS = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+([a-z_][a-z0-9_]*)\s*(?:\(([^)]*)\))?", re.I)
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
        if isinstance(node, ast.JoinedStr) and id(node) not in inner:
            yield "".join(v.value if isinstance(v, ast.Constant) else _DYNAMIC for v in node.values)


@functools.cache
def status_writers() -> dict[tuple[str, str], set[str]]:
    from test_lifecycle_guards import status_columns
    columns = status_columns()
    by_table: dict[str, set[str]] = collections.defaultdict(set)
    for table, column in columns:
        by_table[table].add(column)
    found: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        for text in _sql_texts(ast.parse(path.read_text(), filename=str(path))):
            for match in _UPDATE_SET.finditer(text):
                assigned = match.group(2)
                for column in by_table.get(match.group(1), ()):
                    if _DYNAMIC in assigned or re.search(rf"\b{column}\s*=", assigned):
                        found[(match.group(1), column)].add(rel)
            for match in _INSERT_COLUMNS.finditer(text):
                names = match.group(2)
                for column in by_table.get(match.group(1), ()):
                    if names is None or _DYNAMIC in names or re.search(rf"\b{column}\b", names):
                        found[(match.group(1), column)].add(rel)
    return found


def test_status_writer_scan_sees_literal_and_dynamic_writes():
    tree = ast.parse(
        "a = 'UPDATE jobs SET note = ? WHERE id = ?'\n"
        "b = 'UPDATE jobs SET status = ?, note = ? WHERE status = ?'\n"
        "c = f'UPDATE jobs SET {fields} WHERE id = ?'\n"
        "d = f'INSERT INTO jobs ({cols}) VALUES ({marks})'\n")
    texts = list(_sql_texts(tree))
    sets = [m.group(2) for t in texts for m in _UPDATE_SET.finditer(t)]
    assert [bool(re.search(r"\bstatus\s*=", s)) or _DYNAMIC in s for s in sets] == [False, True, True]
    assert any(_DYNAMIC in (m.group(2) or "") for t in texts for m in _INSERT_COLUMNS.finditer(t))


def test_status_columns_are_written_only_through_their_lifecycle():
    from test_lifecycle_guards import lifecycle_owners
    owners = lifecycle_owners()
    problems = []
    for key, modules in sorted(status_writers().items()):
        extra = modules - STATUS_WRITERS.get(key, set()) - {owners.get(key)}
        if extra:
            problems.append(f"{key[0]}.{key[1]} written by {sorted(extra)}; go through its lifecycle owner")
    assert not problems, "\n".join(problems)


def test_status_writer_allowlist_entries_are_still_real():
    from test_lifecycle_guards import lifecycle_owners
    found, owners = status_writers(), lifecycle_owners()
    stale = sorted((key, module) for key, modules in STATUS_WRITERS.items() for module in modules
                   if module not in found.get(key, set()) or owners.get(key) == module)
    assert not stale, f"remove stale STATUS_WRITERS entries: {stale}"
