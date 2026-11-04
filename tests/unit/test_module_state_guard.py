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
import sys

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
RESET = "test-reset fence and drain bookkeeping of the process-wide import worker; rebound only by reset_for_tests, admission and the worker"

ALLOWED: dict[tuple[str, str], str] = {
    ("lib/apns.py", "_BG_LAST_BY_SESSION"): THROTTLE,
    ("lib/apns.py", "_BG_SENT_AT"): THROTTLE,
    ("lib/apns.py", "_send_locks"): LOCKS,
    ("lib/apns.py", "_CLIENT"): CLIENT,
    ("lib/apns.py", "_CLIENT_CTOR"): CLIENT,
    ("lib/apns.py", "_jwt_cache"): CACHE,
    ("lib/apns.py", "_RELAY_CLIENT"): CLIENT,
    ("lib/apns.py", "_RELAY_CLIENT_CTOR"): CLIENT,
    ("lib/apns.py", "_RELAY_REGISTERED"): CACHE,
    ("lib/avatar_urls.py", "_versions"): CACHE,
    ("lib/backend/agy.py", "_PRIVATE_HOME_LOGS_PRUNED"): THROTTLE,
    ("lib/backend/base.py", "_REGISTRIES"): CLIENT,
    ("lib/backend/registry.py", "_INSTANCES"): CLIENT,
    ("lib/backend_usage.py", "_AGY_LAST_ATTEMPT_MS"): THROTTLE,
    ("lib/backend_auth.py", "_code_submitted"): JOBS,
    ("lib/backend_auth.py", "_processes"): JOBS,
    ("lib/backend_auth.py", "_tasks"): JOBS,
    ("lib/backend_auth.py", "_validation_cache"): CACHE,
    ("lib/cartesia_preview_locks.py", "_CARTESIA_PREVIEW_LOCKS"): LOCKS,
    ("lib/harness_providers.py", "_methods_refresh"): JOBS,
    ("lib/harness_providers.py", "_sessions"): JOBS,
    ("lib/harness_providers.py", "_tasks"): JOBS,
    ("lib/backends.py", "_RUNTIME_CLIENT"): CLIENT,
    # This process's view of its own wall and monotonic clocks (its start and
    # the last suspend or clock jump it saw); meaningless in any other process.
    ("lib/background_jobs.py", "_clock"): THROTTLE,
    ("lib/task_goal_recovery.py", "_FAILURE_LOGGED"): THROTTLE,
    # Stop's in-process epoch backs up a durable fence write that failed; the
    # durable fence stays the authority, so losing it on restart is intended.
    ("lib/launch_receipts.py", "_STOP_EPOCH"): THROTTLE,
    # F4 delivery admission: health-only, process-local counters (never an
    # admission input), reset on restart and by reset_*_for_tests.
    ("lib/launch_receipts.py", "_RECONCILE_HEALTH"): THROTTLE,
    ("lib/outdated_clients.py", "_OUTDATED_CLIENTS_NOTED"): THROTTLE,
    ("lib/runtime_attestation.py", "_HEALTH"): THROTTLE,
    # The one manager-probe slot and start-rate bucket of this Host process: a
    # rate limit by design; it only withholds work and never proves anything.
    ("lib/runtime_attestation.py", "_GATE"): THROTTLE,
    ("lib/live_hub.py", "_HUB"): CLIENT,
    ("lib/server_identity.py", "_live_probe"): CLIENT,
    ("lib/static_assets.py", "_hashes"): CACHE,
    ("lib/transcript_import_cache.py", "_last_started"): THROTTLE,
    ("lib/transcript_import_cache.py", "_deferred"): THROTTLE,
    ("lib/live_hub.py", "_FANOUT"): CLIENT,
    ("lib/backends.py", "_STATUS_CACHE"): CACHE,
    # Invalidation generation of that window: an F4 O1 never stores a status
    # fetched before a reconfigure / invalidate.
    ("lib/backends.py", "_STATUS_GENERATION"): CACHE,
    ("lib/reconcile.py", "_LEAK_CHECK_ASKED"): THROTTLE,
    ("lib/cartesia_voices.py", "_cache"): CACHE,
    ("lib/codex_app_server.py", "_CLIENTS"): CLIENT,
    # Recycled app-server clients whose process has not been seen to exit;
    # their threads stay held until it has (BACKENDS-07b).
    ("lib/codex_app_server.py", "_RETIRING"): JOBS,
    # Draining app-server generations after a credential change (BACKENDS-06b).
    ("lib/codex_app_server.py", "_DRAINING"): JOBS,
    ("lib/compaction.py", "_active"): JOBS,
    ("lib/compaction.py", "_runtime_client"): CLIENT,
    ("lib/config.py", "PERSONA_PERSONALITIES"): IMPORT,
    # Request threads share which controller-action requests are in flight and
    # when each action last ran; both are meaningless after a restart.
    ("lib/controller_actions.py", "_IN_FLIGHT"): THROTTLE,
    ("lib/controller_actions.py", "_LAST_RUN"): THROTTLE,
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
    ("lib/google_stt.py", "_TOKENS"): CACHE,
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
    ("lib/oracle_gemini_live.py", "_failed_at"): THROTTLE,
    ("lib/oracle_live.py", "_CLOSING"): ORACLE,
    ("lib/oracle_live.py", "_STOP_HOOKS"): ORACLE,
    ("lib/oracle_live_calls.py", "_ATTEMPTS"): ORACLE,
    ("lib/oracle_handoffs.py", "_LIVE"): ORACLE,
    ("lib/oracle_handoffs.py", "_TIMERS"): "phase-deadline timers of live handoffs; recover() rebuilds them from the durable records after a restart",
    ("lib/oracle_handoffs.py", "_context"): "the stream and herald bound once by the server composition root",
    ("lib/group_calls.py", "_context"): "the event stream bound once by the server composition root",
    ("lib/agent_ring_live.py", "_SESSIONS"): JOBS,
    ("lib/agent_ring_live.py", "_RETIRED"): JOBS,          # retirements of the running sessions; bounded
    ("lib/agent_ring_live.py", "_CLOSURES"): JOBS,         # closure state of ended sessions; bounded
    ("lib/agent_rings.py", "_context"): "the event stream and TTS engine bound by the server composition root",
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
    ("lib/tool_explanation_commands.py", "_counts"): THROTTLE,
    ("lib/tool_explanation_commands.py", "_misses"): THROTTLE,
    ("lib/tool_explanation_commands.py", "_peer"): "the runtime-bridge lookup bound once by the server composition root (configure)",
    ("lib/tool_explanation_commands.py", "_recent"): "bounded memory of whole commands behind clipped labels; never persisted by design, a restart only leaves labels clipped",
    ("lib/tool_explanation_commands.py", "_reported"): THROTTLE,
    ("lib/transcript_import_cache.py", "_imported"): JOBS,
    ("lib/transcript_import_cache.py", "_path_locks"): LOCKS,
    ("lib/transcript_import_cache.py", "_pending"): JOBS,
    ("lib/transcript_import_cache.py", "_worker"): JOBS,
    ("lib/transcript_import_cache.py", "_running"): JOBS,
    ("lib/transcript_import_cache.py", "_threads"): JOBS,
    ("lib/transcript_import_cache.py", "_direct_active"): JOBS,
    ("lib/transcript_import_cache.py", "_epoch"): RESET,
    ("lib/transcript_import_cache.py", "_resetting"): RESET,
    ("lib/transcript_import_cache.py", "_reset_owner"): RESET,
    ("lib/transcript_import_cache.py", "_drain"): RESET,
    ("lib/claude_transcript.py", "_INDEXES"): CACHE,
    ("lib/transcription_models.py", "_activation_attempts"): JOBS,
    ("lib/transcription_models.py", "_activation_completed"): JOBS,
    ("lib/transcription_models.py", "_activation_exhausted"): JOBS,
    ("lib/transcription_models.py", "_activation_inflight"): JOBS,
    ("lib/transcription_models.py", "_activation_retry_after"): THROTTLE,
    ("lib/transcription_models.py", "_tasks"): JOBS,
    ("lib/transcription_results.py", "_locks"): LOCKS,
    ("lib/turn_dispatch.py", "_JANITOR_SPAWN_LOCKS"): LOCKS,
    ("lib/turn_dispatch.py", "_REQUEST_LOCKS"): LOCKS,
    ("lib/turn_dispatch.py", "_RUNTIME_CLIENT"): CLIENT,
    ("lib/turn_dispatch.py", "_DELIVERY_STREAM"): (
        "the turn owner's event stream, bound when its TurnDispatchService is built; "
        "used only for best-effort F4 delivery hints from module-level transitions"),
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
}


def _is_container(value: ast.expr | None) -> bool:
    if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        return name in _CONTAINERS
    return False


def _constructed_containers(tree: ast.Module) -> set[str]:
    containers = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and _is_container(node.value):
            containers |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and _is_container(node.value):
            containers.add(node.target.id)
    return containers


def _tree_state(tree: ast.Module) -> set[str]:
    containers = _constructed_containers(tree)
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


def module_state(path: pathlib.Path) -> set[str]:
    return _tree_state(ast.parse(path.read_text(), filename=str(path)))


def found() -> set[tuple[str, str]]:
    # State an S9 split recipe moved into a part is reported under its old
    # module (the facade's path), so ALLOWED names one place on main and on
    # integration alike. Only declared state maps; anything else in a part
    # keeps its own path, and two physical owners of one name both stay
    # visible (each under its own path), so neither inherits an allowance.
    from tests.patching import logical_split_tree_map, split_origin
    paths = sorted((SERVER / "lib").rglob("*.py")) + [SERVER / "server.py"]
    logical_live = {rel: _tree_state(tree)
                    for rel, tree in logical_split_tree_map(paths, SERVER).items()}
    owners: dict[tuple[str, str], list[str]] = {}
    for p in paths:
        rel = p.relative_to(SERVER).as_posix()
        tree = ast.parse(p.read_text(), filename=str(p))
        state = _tree_state(tree)  # Preserve the physical scan and new-state owners.
        for name in _constructed_containers(tree):
            origin = split_origin(rel, name)
            # A declared constructor and its declared mutator may move into
            # different parts. Only declared moved state gets logical liveness;
            # physical owner counting below still detects separate real owners.
            if origin != rel and name in logical_live.get(origin, set()):
                state.add(name)
        for name in state:
            owners.setdefault((split_origin(rel, name), name), []).append(rel)
    out = set()
    for key, physical in owners.items():
        out |= {key} if len(physical) == 1 else {(rel, key[1]) for rel in physical} | {key}
    return out


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


def test_split_state_maps_only_declared_names_and_keeps_duplicate_owners(tmp_path, monkeypatch):
    # Review B (S9 cut 1b): a new dict in a part with an old allowed name must
    # not inherit the old module's allowance; nor may a second physical owner.
    from tests import patching
    recipes = tmp_path / "splits"
    recipes.mkdir()
    (recipes / "s9.toml").write_text(
        '[[split]]\nname = "x"\nmodule = "lib/old.py"\n'
        '[[split.part]]\nmodule = "lib/old_base.py"\nnames = ["_cache"]\n')
    monkeypatch.setattr(patching, "_RECIPES", recipes)
    root = tmp_path / "server"
    (root / "lib").mkdir(parents=True)
    mutated = "{name} = {{}}\ndef f():\n    {name}['k'] = 1\n"
    (root / "server.py").write_text("")
    (root / "lib" / "old_base.py").write_text(mutated.format(name="_cache"))
    (root / "lib" / "old_other.py").write_text(mutated.format(name="_cache"))
    monkeypatch.setattr(sys.modules[__name__], "SERVER", root)
    assert ("lib/old.py", "_cache") in found()                     # declared: the old module's
    assert ("lib/old_other.py", "_cache") in found()               # not in any recipe: its own
    (root / "lib" / "old.py").write_text(mutated.format(name="_cache"))
    owners = found()
    assert ("lib/old.py", "_cache") in owners and ("lib/old_base.py", "_cache") in owners


def test_declared_state_stays_live_when_another_split_part_mutates_it(tmp_path, monkeypatch):
    from tests import patching
    recipes = tmp_path / 'splits'
    recipes.mkdir()
    (recipes / 's4.toml').write_text(
        '[[split]]\nname="state"\nmodule="lib/old.py"\n'
        '[[split.part]]\nmodule="lib/old_state.py"\nnames=["_cache"]\n'
        '[[split.part]]\nmodule="lib/old_ops.py"\nnames=["put"]\n')
    monkeypatch.setattr(patching, '_RECIPES', recipes)
    root = tmp_path / 'server'
    (root / 'lib').mkdir(parents=True)
    (root / 'server.py').write_text('')
    (root / 'lib/old.py').write_text('from .old_state import _cache\nfrom .old_ops import put\n')
    (root / 'lib/old_state.py').write_text('_cache = {}\n')
    (root / 'lib/old_ops.py').write_text(
        'from .old_state import _cache\ndef put(key, value):\n    _cache[key] = value\n')
    monkeypatch.setattr(sys.modules[__name__], 'SERVER', root)
    assert found() == {('lib/old.py', '_cache')}
    # A new same-named physical owner must not inherit that logical allowance.
    (root / 'lib/old_ops.py').write_text(
        '_cache = {}\ndef put(key, value):\n    _cache[key] = value\n')
    owners = found()
    assert ('lib/old.py', '_cache') in owners
    assert ('lib/old_ops.py', '_cache') in owners
