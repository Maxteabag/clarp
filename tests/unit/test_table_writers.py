"""Rule 5 of docs/architecture/state-and-boundaries.md: stores own their tables.

Counts the modules under server/ whose SQL string literals write each table
(INSERT/REPLACE/UPDATE/DELETE) and checks them against the owner map. A table
may gain a second writer only through ALLOWED, with a reason; an entry that no
longer matches a real writer fails too, so the list only shrinks.
"""
from __future__ import annotations

import ast
import collections
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
}

_MIGRATION = "schema creation, seed rows or a one-time migration"
ALLOWED = {
    # Stream C leftovers that live in files owned by other streams.
    ("settings", "lib/backend_usage.py"): "TODO(integration, Stream B): write through settings_store",
    ("agents", "lib/janitor_store.py"): "TODO(integration: move to agents.py): janitor conversion, release, label and built-in identity writes share the janitor transaction",
    ("agents", "lib/agent_portraits.py"): "TODO(integration: move to agents.py): _set_agent_avatar_path inside the portrait transaction",
    ("janitor_label_ownership", "lib/agents.py"): "TODO(integration): set_custom_status should call janitor_store.forget_label_ownership",
    ("queued_turns", "lib/janitor_store.py"): "TODO(integration: move to turn_queue.py): the janitor fence cancels its own queued turns in one transaction",
    ("turns", "lib/turn_dispatch.py"): "Stream A: turn_lifecycle becomes the only writer of turns",
    ("state_log", "lib/maintenance.py"): "retention pruning; Stream A decides whether turn_lifecycle owns it",
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
