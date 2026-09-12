"""Demand jobs share Janitor ownership; only their constrained executor is special.

No model calls or process management belong here. Callers admit bounded metadata,
claim once before invoking a provider, then commit output only through the current
generation fence. ``connection`` lets a caller commit a cache and its run receipt
in the same SQLite transaction. Prompts and transcripts stay outside this store.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import os
import uuid

from . import agents, backends, db, janitors


ROLES = ("message-delegator", "tool-explainer", "audio-bookkeeper")
SEED_VERSION = 1
DEMAND_RUN_TTL_MS = 180_000  # Two bounded 60-second routing probes plus overhead.
_METADATA_KEYS = frozenset({"input_hash", "request_hash", "candidate_count", "item_count",
    "detail_level", "target_count", "target_agent_id", "policy_revision", "summary", "status", "reason"})


def _role(role: str) -> str:
    if role not in ROLES:
        raise janitors.JanitorError("Unsupported built-in Janitor role")
    return role


def _metadata(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _METADATA_KEYS:
        raise janitors.JanitorError("Demand history accepts bounded metadata only")
    for item in value.values():
        if (not isinstance(item, (str, int, bool, type(None)))
                or isinstance(item, str) and len(item) > 500
                or isinstance(item, int) and abs(item) > 10**12):
            raise janitors.JanitorError("Demand history accepts bounded metadata only")
    return dict(value)


def get_builtin(role: str) -> dict | None:
    """Read the installed identity even when paused; never resurrect a tombstone."""
    row = db.conn().execute("SELECT agent_id FROM janitor_builtins WHERE role=?", (_role(role),)).fetchone()
    if not row or not agents.get_by_agent_id(row[0]):
        return None
    return janitors.get(row[0], include_runtime=False)


def ensure_builtins(cwd: str | None = None, *, initial: dict | None = None) -> dict:
    """Install each absent role atomically; upgrades never overwrite user choices.

    Routing starts paused unless the caller imports an already-enabled legacy
    service. Explanations remain guarded by the existing explicit request opt-in.
    Legacy provider/model migration is an input, avoiding a settings import cycle.
    """
    initial = initial or {}
    if not isinstance(initial, dict) or set(initial) - set(ROLES):
        raise janitors.JanitorError("Invalid built-in seed configuration")
    with janitors._write() as c:
        for role in ROLES:
            if c.execute("SELECT 1 FROM janitor_builtins WHERE role=?", (role,)).fetchone():
                continue
            defaults = janitors.template(role)
            seed = initial.get(role, {})
            if not isinstance(seed, dict) or set(seed) - {"enabled", "backend", "model", "effort", "provider", "options"}:
                raise janitors.JanitorError("Invalid built-in seed configuration")
            enabled = seed.get("enabled", role in {"tool-explainer", "audio-bookkeeper"})
            if not isinstance(enabled, bool):
                raise janitors.JanitorError("Enabled must be a boolean")
            backend = seed.get("backend", defaults["recommended_backend"])
            if not isinstance(backend, str) or not backends.get(backend):
                raise janitors.JanitorError("Unsupported built-in backend")
            model = seed.get("model", defaults["recommended_model"])
            effort = seed.get("effort", defaults["recommended_effort"])
            execution = janitors._execution(role, {"executor": "ephemeral", "provider": seed.get("provider", backend)}, backend)
            janitors._model({"backend": backend}, model, effort, provider=execution["provider"])
            options = janitors._option_patch(role, seed.get("options", {}))
            agent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"clarp:janitor:builtin:{role}"))
            if c.execute("SELECT 1 FROM agents WHERE agent_id=?", (agent_id,)).fetchone():
                raise janitors.JanitorError("Built-in identity is already owned", 409, "builtin_identity_conflict")
            session = f"clarp-{role}"
            if c.execute("SELECT 1 FROM agents WHERE session=?", (session,)).fetchone():
                session += "-" + agent_id[:8]
            if c.execute("SELECT 1 FROM agents WHERE session=?", (session,)).fetchone():
                raise janitors.JanitorError("Built-in session is already owned", 409, "builtin_identity_conflict")
            now = db.now_ms()
            c.execute("""INSERT INTO agents(agent_id,persona,voice_id,cwd,session,backend,model,effort,
                is_janitor,heartbeat_enabled,dreaming_enabled,created_at) VALUES (?,?,?,?,?,?,?,?,1,0,0,?)""",
                (agent_id, defaults["name"], "", cwd or os.getcwd(), session, backend, model or "", effort or "", now))
            agents.record_state(agent_id, "spawned", {"origin": "janitor", "builtin_role": role})
            c.execute("""INSERT INTO janitor_configs(agent_id,template_id,enabled,scope_json,execution_json,options_json,created_at,updated_at)
                VALUES (?,?,?,'{}',?,?,?,?)""", (agent_id, role, int(enabled), janitors._json(execution), janitors._json(options), now, now))
            janitors._save_attachments(c, agent_id, [{"attachment_id": f"builtin-{role}-v1",
                "trigger_id": defaults["default_trigger_id"], "trigger_version": 1, "enabled": True, "config": {}}], now)
            c.execute("INSERT INTO janitor_builtins(role,agent_id,seed_version,created_at) VALUES (?,?,?,?)", (role, agent_id, SEED_VERSION, now))
    return {role: get_builtin(role) for role in ROLES}


def resolve(role: str, *, target_agent_id: str | None = None) -> dict | None:
    """Select an enabled subscriber, including an explicitly configured replacement.

    Unscoped input never bypasses a restricted watched scope. Ambiguity or invalid
    execution settings are unavailable, rather than an implicit fallback engine.
    """
    _role(role)
    target = agents.get_by_agent_id(target_agent_id) if target_agent_id else None
    if target_agent_id and not target:
        return None
    selected = []
    for config in janitors.list_janitors(include_runtime=False):
        if config["template_id"] != role or not config["enabled"]:
            continue
        scope = config["scope"]
        if target is None and (scope.get("agent_ids") or scope.get("exclude_agent_ids")):
            continue
        if target is not None and not janitors._in_scope(scope, target):
            continue
        matching = [v for v in config["attachments"] if v["enabled"] and v["trigger_version"] == 1
                    and v["trigger_id"] == janitors.template(role)["default_trigger_id"]]
        if len(matching) != 1:
            continue
        try:
            janitors._execution(role, config["execution"], config["backend"])
            janitors._model(config, config["model"], config["effort"], provider=config["execution"].get("provider"))
        except janitors.JanitorError:
            continue
        selected.append(config)
    return selected[0] if len(selected) == 1 else None


def _request_run_id(role: str, request_id: str) -> str:
    _role(role)
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
        raise janitors.JanitorError("Invalid demand request identity")
    return "janitor-demand-" + hashlib.sha256(f"{role}:{request_id}".encode()).hexdigest()


def get_request_run(role: str, request_id: str) -> dict | None:
    """Read a prior request's receipt before deciding whether new work is eligible."""
    return janitors.get_run(_request_run_id(role, request_id))


def begin_run(role: str, request_id: str, *, context=None, target_agent_id: str | None = None) -> dict | None:
    run_id = _request_run_id(role, request_id)
    metadata = _metadata(context)
    with janitors._write() as c:
        _recover_expired(c, db.now_ms())
        existing = c.execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
        if existing:
            frozen = janitors._decode(existing["configuration_json"], {})
            if frozen.get("context") != metadata or frozen.get("target_agent_id") != target_agent_id:
                raise janitors.JanitorError("Request identity already refers to different work", 409, "run_conflict")
            return janitors.get_run(run_id)
        config = resolve(role, target_agent_id=target_agent_id)
        if (not config or janitors.has_active_run(config["agent_id"])
                or janitors._pending_demand_claim(c, config["agent_id"])):
            return None
        attachment = next(v for v in config["attachments"] if v["enabled"]
                          and v["trigger_id"] == janitors.template(role)["default_trigger_id"])
        janitors._active_attachment(c, attachment["attachment_id"], config["generation"])
        now = db.now_ms()
        frozen = {"template_id": role, "executor": "ephemeral", "provider": config["execution"]["provider"],
            "backend": config["backend"], "model": config["model"], "effort": config["effort"],
            "scope": config["scope"], "options": config["options"], "trigger_id": attachment["trigger_id"],
            "trigger_version": attachment["trigger_version"], "config": attachment["config"],
            "context": metadata, "target_agent_id": target_agent_id, "expires_at": now + DEMAND_RUN_TTL_MS}
        c.execute("""INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,status,
            candidates_json,configuration_json,created_at,started_at) VALUES (?,?,?,?,?,?,'running','[]',?,?,?)""",
            (run_id, config["agent_id"], config["session"], attachment["attachment_id"], config["generation"], run_id,
             janitors._json(frozen), now, now))
    return janitors.get_run(run_id)


def _recover_expired(c, now: int) -> int:
    expired = [dict(row) for row in c.execute("""SELECT * FROM janitor_runs
        WHERE status IN ('queued','running') AND json_extract(configuration_json,'$.executor')='ephemeral'
        AND COALESCE(json_extract(configuration_json,'$.expires_at'),created_at+?)<=?""", (DEMAND_RUN_TTL_MS, now))]
    for run in expired:
        c.execute("UPDATE janitor_runs SET status='cancelled',outcome='cancelled',finished_at=?,error=? WHERE run_id=?",
                  (now, "Demand run expired before completion", run["run_id"]))
        c.execute("UPDATE janitor_configs SET last_run_at=?,last_error=?,updated_at=? WHERE agent_id=?",
                  (now, "Demand run expired before completion", now, run["agent_id"]))
    return len(expired)


def recover_expired_runs() -> int:
    """Retire expired ephemeral claims; never interrupt another live request owner."""
    with janitors._write() as c:
        return _recover_expired(c, db.now_ms())


def is_current(run_id: str, *, connection=None) -> bool:
    c = connection if connection is not None else db.conn()
    try:
        run = janitors._active_run(c, run_id)
        frozen = janitors._decode(run["configuration_json"], {})
        if frozen.get("executor") != "ephemeral" or frozen.get("template_id") not in ROLES:
            return False
        if db.now_ms() >= frozen.get("expires_at", run["created_at"] + DEMAND_RUN_TTL_MS):
            return False
        config = c.execute("""SELECT a.backend,a.model,a.effort,j.template_id,j.scope_json,j.execution_json,j.options_json
            FROM agents a JOIN janitor_configs j ON j.agent_id=a.agent_id WHERE a.agent_id=?""", (run["agent_id"],)).fetchone()
        execution = janitors._decode(config["execution_json"], {})
        if any(config[key] != frozen[key] for key in ("backend", "model", "effort", "template_id")):
            return False
        if execution != {"executor": "ephemeral", "provider": frozen["provider"]} or janitors._decode(config["scope_json"], {}) != frozen["scope"]:
            return False
        if janitors.option_values(config["template_id"], janitors._decode(config["options_json"], {})) != frozen.get("options", {}):
            return False
        attachment = janitors._active_attachment(c, run["attachment_id"], run["generation"])
        if (attachment["trigger_id"] != frozen["trigger_id"] or attachment["trigger_version"] != frozen["trigger_version"]
                or janitors._decode(attachment["config_json"], {}) != frozen["config"]):
            return False
        target_id = frozen.get("target_agent_id")
        if target_id:
            target = c.execute("SELECT * FROM agents WHERE agent_id=?", (target_id,)).fetchone()
            if not target or not janitors._in_scope(frozen["scope"], dict(target)):
                return False
        return True
    except (janitors.JanitorError, KeyError, TypeError):
        return False


def claim_run(run_id: str) -> bool:
    """Acquire a durable once-only provider invocation claim for this request."""
    with janitors._write() as c:
        if not is_current(run_id, connection=c):
            return False
        return bool(c.execute("INSERT OR IGNORE INTO janitor_demand_claims(run_id,claimed_at) VALUES (?,?)", (run_id, db.now_ms())).rowcount)


def complete_run(run_id: str, outcome: str = "completed", *, result=None, error: str = "", connection=None) -> bool:
    """Accept one result only while its authority is current; stale output is discarded."""
    if outcome not in {"completed", "failed", "cancelled"}:
        raise janitors.JanitorError("Invalid demand outcome")
    result = _metadata(result)
    if not isinstance(error, str) or len(error) > 500:
        raise janitors.JanitorError("Demand errors must be bounded metadata")
    with (janitors._write() if connection is None else nullcontext(connection)) as c:
        run = c.execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
        if not run or janitors._decode(run["configuration_json"], {}).get("executor") != "ephemeral":
            return False
        c.execute("UPDATE janitor_demand_claims SET finished_at=COALESCE(finished_at,?) WHERE run_id=?", (db.now_ms(), run_id))
        prior = c.execute("SELECT result_json FROM janitor_demand_results WHERE run_id=?", (run_id,)).fetchone()
        if prior:
            if prior[0] != janitors._json(result) or run["outcome"] != outcome or run["error"] != error:
                raise janitors.JanitorError("Demand result already refers to different work", 409, "result_conflict")
            return True
        now = db.now_ms()
        if not is_current(run_id, connection=c):
            c.execute("UPDATE janitor_runs SET status='cancelled',outcome='cancelled',finished_at=? WHERE run_id=? AND status IN ('queued','running')", (now, run_id))
            return False
        c.execute("INSERT INTO janitor_demand_results(run_id,result_json,created_at) VALUES (?,?,?)", (run_id, janitors._json(result), now))
        c.execute("UPDATE janitor_runs SET status=?,outcome=?,finished_at=?,error=? WHERE run_id=?", (outcome, outcome, now, error, run_id))
        c.execute("UPDATE janitor_configs SET last_run_at=?,last_error=?,updated_at=? WHERE agent_id=?", (now, error, now, run["agent_id"]))
        return True
