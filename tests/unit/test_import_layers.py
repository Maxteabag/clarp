"""Import layers in ratchet mode (PT-14, slice S1).

The planned layers, lowest first: persistence, core, features, http. A module
may import its own layer or a lower one. Every import counts, module-level
and function-level alike, because the lazy edges are what hold the 160-module
tangle together (PT-14).

Ratchet mode: the upward edges that exist today are UPWARD_BASELINE ("src ->
dst"). A new upward edge fails; an edge that disappears must be removed from
the baseline, so the list only shrinks until S1 makes the test strict.
Separately, nothing in server/lib outside lib.routes imports lib.routes (the
S3 route layer is reached only from server.py).

The layer map follows docs/architecture/s1-layering.md (approved by the lead):
persistence and core are listed by name or package prefix, http is the entry
points and HTTP/socket adapters, and every other module is a feature. Moving a
module to another layer is an S1 decision; update the map and the baseline in
the same commit.

Old module paths that S1 turns into sys.modules aliases (ALIASES) count as the
module they alias, so importing lib.db_schema is an edge to
lib.persistence.schema. Edges S1 keeps on purpose are NAMED_EXCEPTIONS, each
with its reason; the list is exact (an exception that no longer exists fails).
"""
from __future__ import annotations

import ast
import collections
import pathlib

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
LAYERS = ("persistence", "core", "features", "http")

PERSISTENCE = frozenset({
    "lib", "lib.clock", "lib.db", "lib.db_migrations", "lib.db_schema",
    "lib.lifecycle", "lib.paths", "lib.revisioned_cache", "lib.timing", "lib.xdg",
})
PERSISTENCE_PACKAGES = ("lib.persistence",)
CORE = frozenset({f"lib.{m}" for m in (
    "agent_lifecycle", "agent_store", "agents", "backends", "config", "config_writer",
    "conversation", "deployment", "error_classify", "eventlog", "events", "hook_runtime",
    "http_utils", "identity", "launch_receipts", "log", "message_live", "message_store", "message_turns",
    "message_writes", "origins", "proc_util", "process_registry", "prompt_admissions",
    "protocol", "roster", "roster_seed", "runtime_bridge", "runtime_events", "server_identity",
    "settings_store", "status_labels", "telemetry", "text_util", "trace", "turn_dispatch",
    "turn_identity", "turn_lifecycle", "turn_queue", "turn_slots", "turn_usage", "ws",
)})
CORE_PACKAGES = ("lib.backend", "lib.policies")
HTTP = frozenset({"server", "runtime", "__init__"} | {f"lib.{m}" for m in (
    "bonjour", "controller_actions_http", "janitor_http", "local_transport",
    "podcast_history_http", "relay_connector", "request_security", "static_assets",
    "terminal_ws",
)})
ROUTES = "lib.routes"
# Old paths and their new homes, one module object (S1 commits 2 and 5). Edges
# are named by the new path once it exists. Until the final S1 move the code of
# lib.db_schema stays in the old file and lib.persistence.schema aliases it.
ALIASES = {
    "lib.db_schema": "lib.persistence.schema",
    "lib.db_migrations": "lib.persistence.migrate",
}
# Upward edges kept on purpose (docs/architecture/s1-layering.md, section 2).
NAMED_EXCEPTIONS = {
    "lib.db -> lib.log": "logging is cross-cutting",
    "lib.db_migrations -> lib.tool_explanation_learning": "v99 calls backfill_programs(con), live feature logic",
    "lib.db_migrations -> lib.goal_ledger": "v112 calls goal_ledger.backfill(con) and its ledger helpers",
    "lib.db_migrations -> lib.provider_background_jobs": "v121 calls expire_lost_evidence",
}

UPWARD_BASELINE = frozenset({
    'lib.agent_lifecycle -> lib.contact_assignment',
    'lib.agent_lifecycle -> lib.launch_paths',
    'lib.agent_lifecycle -> lib.mcp_selection',
    'lib.agent_lifecycle -> lib.personas',
    'lib.agent_lifecycle -> lib.session_models',
    'lib.agents -> lib.artifacts',
    'lib.agents -> lib.clip_store',
    'lib.agents -> lib.goal_ledger',
    'lib.agents -> lib.janitor_store',
    'lib.agents -> lib.janitors',
    'lib.agents -> lib.sse_store',
    'lib.agents -> lib.task_plans',
    'lib.agents -> lib.voice_verbosity',
    'lib.backend.agy -> lib.agy_transcript',
    'lib.backend.agy -> lib.live_hub',
    'lib.backend.agy -> lib.message_context',
    'lib.backend.agy -> lib.provider_background_jobs',
    'lib.backend.agy -> lib.provider_capabilities',
    'lib.backend.agy -> lib.task_plans',
    'lib.backend.agy -> lib.tts_queue',
    'lib.backend.agy -> lib.voice_markup',
    'lib.backend.agy -> lib.voice_preamble',
    'lib.backend.agy_hooks -> lib.agy_transcript',
    'lib.backend.agy_hooks -> lib.provider_background_jobs',
    'lib.backend.claude -> lib.fork',
    'lib.backend.claude -> lib.live_claude',
    'lib.backend.claude -> lib.live_pacing',
    'lib.backend.claude -> lib.mcp_selection',
    'lib.backend.claude -> lib.provider_background_jobs',
    'lib.backend.claude -> lib.provider_capabilities',
    'lib.backend.claude -> lib.resume',
    'lib.backend.claude -> lib.send_service',
    'lib.backend.claude -> lib.session_catalog',
    'lib.backend.claude -> lib.voice_preamble',
    'lib.backend.codex -> lib.codex_app_server',
    'lib.backend.codex -> lib.codex_transcript',
    'lib.backend.codex -> lib.provider_background_jobs',
    'lib.backend.codex -> lib.tool_explanation_commands',
    'lib.backend.codex -> lib.tool_explanation_shapes',
    'lib.backend.codex -> lib.tts_queue',
    'lib.backend.codex -> lib.voice_preamble',
    'lib.backend.grok -> lib.grok_transcript',
    'lib.backend.grok -> lib.provider_background_jobs',
    'lib.backend.grok -> lib.tts_queue',
    'lib.backend.grok -> lib.voice_preamble',
    'lib.backend.muse -> lib.muse_transcript',
    'lib.backend.muse -> lib.provider_background_jobs',
    'lib.backend.muse -> lib.tts_queue',
    'lib.backend.muse -> lib.voice_preamble',
    'lib.backend.opencode -> lib.live_hub',
    'lib.backend.opencode -> lib.live_tools',
    'lib.backend.opencode -> lib.opencode_transcript',
    'lib.backend.opencode -> lib.provider_background_jobs',
    'lib.backend.opencode -> lib.tool_explanation_commands',
    'lib.backend.opencode -> lib.tts_queue',
    'lib.backend.opencode -> lib.voice_preamble',
    'lib.backend.stream_json -> lib.live_hub',
    'lib.backend.stream_json -> lib.live_pacing',
    'lib.backend.stream_json -> lib.voice_markup',
    'lib.conversation -> lib.transcript_import_cache',
    'lib.conversation -> lib.transcript_incremental',
    'lib.db_migrations -> lib.audio_bookkeeper',
    'lib.db_migrations -> lib.janitor_autonomy',
    'lib.db_migrations -> lib.judgments',
    'lib.dispatch_adapters -> lib.janitor_http',
    'lib.eventlog -> lib.diagnostics_settings',
    'lib.message_live -> lib.dreaming',
    'lib.message_live -> lib.heartbeat',
    'lib.message_live -> lib.message_context',
    'lib.message_live -> lib.oracle_delegations',
    'lib.message_live -> lib.team_leader',
    'lib.message_live -> lib.team_store',
    'lib.message_live -> lib.voice_markup',
    'lib.message_store -> lib.message_context',
    'lib.message_store -> lib.message_previews',
    'lib.message_writes -> lib.dreaming',
    'lib.message_writes -> lib.heartbeat',
    'lib.message_writes -> lib.message_context',
    'lib.message_writes -> lib.oracle_delegations',
    'lib.message_writes -> lib.team_leader',
    'lib.message_writes -> lib.team_store',
    'lib.message_writes -> lib.voice_markup',
    'lib.policies.agent_spec -> lib.voice',
    'lib.runtime_bridge -> lib.compaction',
    'lib.runtime_bridge -> lib.janitor_autonomy',
    'lib.runtime_bridge -> lib.live_hub',
    'lib.runtime_bridge -> lib.runtime_attestation',
    'lib.runtime_bridge -> lib.runtime_release',
    'lib.runtime_bridge -> lib.tool_explanation_commands',
    'lib.runtime_events -> lib.live_hub',
    'lib.runtime_events -> lib.live_pacing',
    'lib.turn_dispatch -> lib.account_failover',
    'lib.turn_dispatch -> lib.agent_rings',
    'lib.turn_dispatch -> lib.backend_usage',
    'lib.turn_dispatch -> lib.codex_app_server',
    'lib.turn_dispatch -> lib.compaction',
    'lib.turn_dispatch -> lib.dreaming',
    'lib.turn_dispatch -> lib.heartbeat',
    'lib.turn_dispatch -> lib.interrupted_turns',
    'lib.turn_dispatch -> lib.janitor_autonomy',
    'lib.turn_dispatch -> lib.janitor_design_policy',
    'lib.turn_dispatch -> lib.janitors',
    'lib.turn_dispatch -> lib.judgment_sites',
    'lib.turn_dispatch -> lib.launch_paths',
    'lib.turn_dispatch -> lib.live_hub',
    'lib.turn_dispatch -> lib.oracle_delegations',
    'lib.turn_dispatch -> lib.recovery_backoff',
    'lib.turn_dispatch -> lib.send_service',
    'lib.turn_dispatch -> lib.task_goal_recovery',
    'lib.turn_dispatch -> lib.task_plans',
    'lib.turn_dispatch -> lib.team_store',
    'lib.turn_dispatch -> lib.tts_queue',
    'lib.turn_identity -> lib.provider_background_jobs',
    'lib.turn_lifecycle -> lib.live_hub',
    'lib.turn_slots -> lib.compaction',
    'lib.turn_slots -> lib.terminal_ws',
})


def layer(module: str) -> int:
    if module in PERSISTENCE or any(module == p or module.startswith(p + ".") for p in PERSISTENCE_PACKAGES):
        return 0
    if module in CORE or any(module == p or module.startswith(p + ".") for p in CORE_PACKAGES):
        return 1
    if module in HTTP or module == ROUTES or module.startswith(ROUTES + "."):
        return 3
    return 2


def _module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(SERVER).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" and len(parts) > 1 else parts)


def modules() -> dict[str, pathlib.Path]:
    return {_module_name(p): p for p in sorted(SERVER.rglob("*.py")) if "tests" not in p.parts}


def imports_of(name: str, path: pathlib.Path, known: dict) -> set[str]:
    """Server modules that `name` imports anywhere in its body."""
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    targets = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join([*base, *([node.module] if node.module else [])])
            else:
                target = node.module or ""
            for alias in node.names:
                candidate = f"{target}.{alias.name}" if target else alias.name
                targets.append(candidate if candidate in known else target)
        elif isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
    found = {t for t in targets if t in known}
    return {ALIASES[t] if ALIASES.get(t) in known else t for t in found} - {name}


def edges() -> set[tuple[str, str]]:
    known = modules()
    return {(name, target) for name, path in known.items() for target in imports_of(name, path, known)}


def upward() -> set[str]:
    return {f"{src} -> {dst}" for src, dst in edges() if layer(dst) > layer(src)}


def test_named_exceptions_are_exact():
    now = upward()
    stale = sorted(set(NAMED_EXCEPTIONS) - now)
    assert not stale, f"remove from NAMED_EXCEPTIONS (no longer an upward edge): {stale}"
    assert not set(NAMED_EXCEPTIONS) & UPWARD_BASELINE, "an edge is either a named exception or baseline debt"


def test_no_new_upward_imports():
    new = sorted(upward() - UPWARD_BASELINE - set(NAMED_EXCEPTIONS))
    assert not new, ("imports from a lower layer into a higher one (persistence < core < features < http):\n"
                     + "\n".join(f"{e}  ({LAYERS[layer(e.split(' -> ')[0])]} -> {LAYERS[layer(e.split(' -> ')[1])]})"
                                 for e in new))


def test_upward_baseline_only_shrinks():
    gone = sorted(UPWARD_BASELINE - upward())
    assert not gone, f"remove from UPWARD_BASELINE (the edge is gone or no longer upward): {gone}"


def test_only_the_route_layer_imports_routes():
    offenders = sorted(f"{src} -> {dst}" for src, dst in edges()
                       if (dst == ROUTES or dst.startswith(ROUTES + ".")) and src.startswith("lib.")
                       and not (src == ROUTES or src.startswith(ROUTES + ".")))
    assert not offenders, "server/lib must not import lib.routes (only server.py does):\n" + "\n".join(offenders)


def test_layer_counts_are_sane():
    counts = collections.Counter(LAYERS[layer(m)] for m in modules())
    assert all(counts[name] for name in LAYERS), counts
    stale = sorted((PERSISTENCE | CORE | HTTP) - set(modules()) - {"lib", "__init__"}
                   - set(ALIASES) - set(ALIASES.values()))
    assert not stale, f"layer map names modules that no longer exist: {stale}"


def test_the_resolver_sees_relative_absolute_and_lazy_imports(tmp_path):
    known = {"lib": None, "lib.db": None, "lib.agents": None, "lib.backend": None,
             "lib.backend.registry": None, "lib.routes": None}
    probe = tmp_path / "probe.py"
    probe.write_text("from . import agents\nfrom .backend import registry\n"
                     "def f():\n    from lib.db import conn\n    import lib.routes\n")
    assert imports_of("lib.probe", probe, known) == {"lib.agents", "lib.backend.registry", "lib.db", "lib.routes"}


def test_an_alias_import_counts_as_its_new_module(tmp_path):
    known = {"lib": None, "lib.db_schema": None, "lib.persistence": None, "lib.persistence.schema": None}
    probe = tmp_path / "probe.py"
    probe.write_text("from . import db_schema\ndef f():\n    import lib.db_schema\n")
    assert imports_of("lib.probe", probe, known) == {"lib.persistence.schema"}
    # Before the target exists the old module is a real module and stays itself.
    assert imports_of("lib.probe", probe, {"lib": None, "lib.db_schema": None}) == {"lib.db_schema"}
    assert layer("lib.persistence.schema") == layer("lib.persistence.migrations.v099") == 0
