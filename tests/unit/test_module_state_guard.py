"""Rule 2 of docs/architecture/state-and-boundaries.md: module state is
constructed, not global.

Scans server/lib and server/server.py for mutable module state: a
module-level dict/list/set/deque that the module mutates, or a name a
function rebinds through `global`. Each one must appear in ALLOWED with the
reason it is a process-wide singleton rather than something a composition
root should build. New state fails this test; so does an entry whose state is
gone, so the list only shrinks. New caches belong in revisioned_cache.
"""
from __future__ import annotations

import ast
import pathlib

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
_MUTATORS = frozenset({
    "append", "add", "update", "pop", "popitem", "clear", "setdefault", "extend", "insert",
    "remove", "discard", "appendleft", "popleft", "move_to_end", "__setitem__"})
_CONTAINERS = frozenset({
    "dict", "list", "set", "defaultdict", "OrderedDict", "deque", "Counter",
    "WeakValueDictionary", "WeakKeyDictionary", "WeakSet"})

CACHE = "process-local cache of a lookup (provider catalogue, file, config); keyed by a bounded space and reset in tests"
CONFIG = "config/settings cache with its path, stat and check time; reset_cache() clears it under the load lock"
LOCKS = "per-key lock table: a process-wide lock registry, bounded by the keys it guards"
CLIENT = "lazily built process-wide client or registry, replaced in tests through its setter"
PATHS = "database/log location and one-time schema flags, rebound by reset_for_tests"
THROTTLE = "rate limit, circuit breaker or log-once memory; losing it on restart is intended"
JOBS = "tracks in-process background threads, subprocesses or sockets that cannot outlive the process"
ORACLE = "live realtime call and socket registry; sessions die with the process"
IMPORT = "table filled once at import time and read-only afterwards"
TURNS = "Stream A: in-flight/queued turn memory moves into TurnSlots, durable through turns/queued_turns"

ALLOWED: dict[tuple[str, str], str] = {
    ("lib/apns.py", "_BG_LAST_BY_SESSION"): THROTTLE,
    ("lib/apns.py", "_BG_SENT_AT"): THROTTLE,
    ("lib/apns.py", "_send_locks"): LOCKS,
    ("lib/apns.py", "_CLIENT"): CLIENT,
    ("lib/apns.py", "_CLIENT_CTOR"): CLIENT,
    ("lib/apns.py", "_jwt_cache"): CACHE,
    ("lib/avatar_urls.py", "_versions"): CACHE,
    ("lib/backend/base.py", "_REGISTRIES"): CLIENT,
    ("lib/backend/registry.py", "_INSTANCES"): CLIENT,
    ("lib/backend_auth.py", "_code_submitted"): JOBS,
    ("lib/backend_auth.py", "_processes"): JOBS,
    ("lib/backend_auth.py", "_tasks"): JOBS,
    ("lib/backend_auth.py", "_validation_cache"): CACHE,
    ("lib/backends.py", "_RUNTIME_CLIENT"): CLIENT,
    ("lib/backends.py", "_STATUS_CACHE"): CACHE,
    ("lib/cartesia_voices.py", "_cache"): CACHE,
    ("lib/codex_app_server.py", "_CLIENTS"): CLIENT,
    ("lib/compaction.py", "_active"): JOBS,
    ("lib/compaction.py", "_runtime_client"): CLIENT,
    ("lib/config.py", "PERSONA_PERSONALITIES"): IMPORT,
    ("lib/config.py", "_CACHED"): CONFIG,
    ("lib/config.py", "_CACHED_PATH"): CONFIG,
    ("lib/config.py", "_CACHED_STAT"): CONFIG,
    ("lib/config.py", "_LAST_STAT_AT"): CONFIG,
    ("lib/custom_stt_adapters.py", "_ERROR_CACHE"): CACHE,
    ("lib/custom_stt_adapters.py", "_INFERENCE_LOCKS"): LOCKS,
    ("lib/custom_stt_adapters.py", "_MODEL_CACHE"): CACHE,
    ("lib/custom_tts_adapters.py", "_VOICE_CACHE"): CACHE,
    ("lib/custom_tts_adapters.py", "_VOICE_ERROR_CACHE"): CACHE,
    ("lib/db.py", "_TRANSACTION_OWNERS"): "diagnostic map of which thread holds a write transaction, under _TRANSACTION_LOCK",
    ("lib/db.py", "DB_PATH"): PATHS,
    ("lib/db.py", "_LAST_LOCK_REPORT_AT"): THROTTLE,
    ("lib/db.py", "_MIGRATED"): PATHS,
    ("lib/db.py", "_STAMP_CONN"): "the one connection change_stamp() reads PRAGMA data_version from, under _STAMP_LOCK",
    ("lib/db.py", "_WRITE_GENERATION"): "local half of change_stamp(), bumped under its lock",
    ("lib/deepgram_voices.py", "_cache"): CACHE,
    ("lib/diagnostics_settings.py", "_CACHED"): CONFIG,
    ("lib/diagnostics_settings.py", "_CACHED_AT"): CONFIG,
    ("lib/dreaming.py", "_advance_request_callback"): CLIENT,
    ("lib/elevenlabs_voices.py", "_cache"): CACHE,
    ("lib/eventlog.py", "LOG_DIR"): PATHS,
    ("lib/health.py", "_STATE"): JOBS,
    ("lib/heartbeat.py", "_STATE_BY_AGENT"): "per-agent heartbeat timers of the running worker; rebuilt from agents rows on start",
    ("lib/janitor_autonomy.py", "_SCHEMA_READY_FOR"): PATHS,
    ("lib/judgments.py", "_CLIENT"): CLIENT,
    ("lib/judgments.py", "_CLIENT_CTOR"): CLIENT,
    ("lib/judgments.py", "_consecutive_failures"): THROTTLE,
    ("lib/judgments.py", "_open_until"): THROTTLE,
    ("lib/model_avatars.py", "_versions"): CACHE,
    ("lib/oracle_calls.py", "_CALLS"): ORACLE,
    ("lib/oracle_calls_stable.py", "_CALLS"): ORACLE,
    ("lib/oracle_live.py", "_CLOSING"): ORACLE,
    ("lib/oracle_live.py", "_STOP_HOOKS"): ORACLE,
    ("lib/oracle_live_calls.py", "_ATTEMPTS"): ORACLE,
    ("lib/oracle_live_stable.py", "_CLOSING"): ORACLE,
    ("lib/oracle_live_stable.py", "_STOP_HOOKS"): ORACLE,
    ("lib/oracle_realtime.py", "_ACTIVE_PRINCIPALS"): ORACLE,
    ("lib/oracle_realtime.py", "_LEGACY_TOKENS"): ORACLE,
    ("lib/personas.py", "_seeded_db"): PATHS,
    ("lib/provider_capabilities.py", "_cache"): CACHE,
    ("lib/provider_capabilities.py", "_last_error"): CACHE,
    ("lib/provider_capabilities.py", "_last_error_generation"): CACHE,
    ("lib/provider_capabilities.py", "_opencode_refresh_thread"): JOBS,
    ("lib/provider_capabilities.py", "_refresh_generation"): CACHE,
    ("lib/provider_capabilities.py", "_refreshing"): JOBS,
    ("lib/revisioned_cache.py", "_REGISTRY"): "the cache registry itself: names are unique and reset_all() walks it",
    ("lib/server_identity.py", "FEATURE_CONTRACTS"): IMPORT,
    ("lib/server_update.py", "_cache"): CACHE,
    ("lib/telemetry.py", "TELEMETRY_PATH"): PATHS,
    ("lib/telemetry.py", "_SCHEMA_READY"): PATHS,
    ("lib/terminal_ws.py", "_live"): JOBS,
    ("lib/transcript_import_cache.py", "_imported"): JOBS,
    ("lib/transcript_import_cache.py", "_path_locks"): LOCKS,
    ("lib/transcript_import_cache.py", "_pending"): JOBS,
    ("lib/transcript_import_cache.py", "_worker"): JOBS,
    ("lib/transcript_log.py", "_INDEXES"): CACHE,
    ("lib/transcription_models.py", "_activation_attempts"): JOBS,
    ("lib/transcription_models.py", "_activation_completed"): JOBS,
    ("lib/transcription_models.py", "_activation_exhausted"): JOBS,
    ("lib/transcription_models.py", "_activation_inflight"): JOBS,
    ("lib/transcription_models.py", "_activation_retry_after"): THROTTLE,
    ("lib/transcription_models.py", "_tasks"): JOBS,
    ("lib/transcription_results.py", "_locks"): LOCKS,
    ("lib/turn_dispatch.py", "_CLAIMED_AT"): TURNS,
    ("lib/turn_dispatch.py", "_INFLIGHT"): TURNS,
    ("lib/turn_dispatch.py", "_JANITOR_SPAWN_LOCKS"): LOCKS,
    ("lib/turn_dispatch.py", "_QUEUED"): TURNS,
    ("lib/turn_dispatch.py", "_REQUEST_LOCKS"): LOCKS,
    ("lib/turn_dispatch.py", "_RUNTIME_CLIENT"): CLIENT,
    ("lib/viz_learning.py", "_failed_until"): THROTTLE,
    ("lib/viz_learning.py", "_pending"): JOBS,
    ("lib/viz_learning.py", "_active"): JOBS,
    ("lib/viz_learning.py", "_last_result"): JOBS,
    ("lib/viz_learning.py", "_running"): JOBS,
    ("lib/viz_learning.py", "_stage"): JOBS,
    ("lib/viz_library.py", "_LAST_GOOD"): CACHE,
    ("lib/viz_native.py", "_cache"): CACHE,
    ("lib/viz_native.py", "_paths"): CACHE,
    ("lib/viz_owner_avatar.py", "_retry"): THROTTLE,
    ("lib/vocab_compile.py", "_MODEL_BUDGETS"): CACHE,
    ("lib/workspace_vocab.py", "_CACHE"): CACHE,
    ("server.py", "_CARTESIA_PREVIEW_LOCKS"): LOCKS,
    ("server.py", "_OUTDATED_CLIENTS_NOTED"): THROTTLE,
}


def _is_container(value: ast.expr | None) -> bool:
    if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        return name in _CONTAINERS
    return False


def module_state(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    containers = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and _is_container(node.value):
            containers |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and _is_container(node.value):
            containers.add(node.target.id)
    rebound, mutated = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            rebound.update(node.names)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in _MUTATORS and isinstance(node.func.value, ast.Name)):
            mutated.add(node.func.value.id)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
            mutated |= {t.value.id for t in targets
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)}
    return (containers & mutated) | rebound


def found() -> set[tuple[str, str]]:
    paths = sorted((SERVER / "lib").rglob("*.py")) + [SERVER / "server.py"]
    return {(p.relative_to(SERVER).as_posix(), name) for p in paths for name in module_state(p)}


def test_no_new_module_state():
    new = sorted(found() - set(ALLOWED))
    assert not new, (
        "new mutable module state; build it in a composition root, use a RevisionedCache, "
        f"or add it to ALLOWED with a reason: {new}")


def test_allowlist_has_no_stale_entries():
    stale = sorted(set(ALLOWED) - found())
    assert not stale, f"remove stale ALLOWED entries: {stale}"


def test_the_scanner_sees_both_kinds_of_state(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("CONST = {'a': 1}\n_SEEN = set()\n_N = 0\n"
                      "def f(x):\n    global _N\n    _N += 1\n    _SEEN.add(x)\n")
    assert module_state(sample) == {"_SEEN", "_N"}
