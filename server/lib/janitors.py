"""Optional maintenance configuration, admissions and atomic effect receipts.

Janitors are ordinary agent identities. This module owns the permission to run
maintenance and to alter its explicitly bounded metadata, never worker liveness.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import agents, backends, db, scheduler, turn_queue
from .protocol import AgentState

LABEL_MAX_AGE_MS = 24 * 60 * 60 * 1000
MAX_PAYLOAD_BYTES = 1024 * 1024
TERMINAL_OUTCOMES = frozenset({"changed", "same_task", "insufficient_context", "skipped", "error", "cancelled", "completed"})
_UNSET = object()


class JanitorError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_janitor"):
        super().__init__(message)
        self.status = status
        self.code = code


def _json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise JanitorError("Maintenance configuration is too large")
    return encoded


def _decode(value, fallback):
    return json.loads(value) if value else fallback


@contextmanager
def _write():
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def templates() -> list[dict]:
    return [{"id": "custom-task", "name": "Custom task",
             "description": "Run your own maintenance instructions using the agent tools on this Host.",
             "recommended_backend": "codex", "recommended_model": "",
             "recommended_effort": "low", "allowed_effects": [], "creatable": True,
             "supported_trigger_ids": ["schedule", "active-interval"],
             "default_trigger_id": "schedule", "options": [
                 {"key": "instructions", "label": "Instructions", "type": "text", "default": "",
                  "max_length": 16000,
                  "description": "Describe the work and its limits. Reference installed skills or local helpers."}]},
            {"id": "task-labels", "name": "Task labels",
             "description": "Keep agents' current task labels clear and useful.",
             "recommended_backend": "codex", "recommended_model": "gpt-5.3-codex-spark",
             "recommended_effort": "low",
             "allowed_effects": ["task_label"], "creatable": True,
             "supported_trigger_ids": ["agent-work-completed", "schedule", "active-interval"],
             "default_trigger_id": "agent-work-completed", "options": []},
            {"id": "message-delegator", "name": "Message delegator",
             "description": "Choose an eligible recipient for an explicitly requested message route.",
             "recommended_backend": "codex", "recommended_model": "gpt-5.3-codex-spark",
             "recommended_effort": "low", "allowed_effects": ["message_route"], "creatable": True,
             "supported_trigger_ids": ["routing-requested"], "default_trigger_id": "routing-requested",
             "supported_providers": ["openai", *(item.id for item in backends.routing_adapters())],
             "options": [
                 {"key": "fallback_only", "label": "Only when name matching cannot decide", "type": "boolean", "default": True,
                  "description": "Ask the delegator when the recipient cannot be selected directly from an agent name."},
                 {"key": "hands_free_only", "label": "Only in hands-free mode", "type": "boolean", "default": True},
                 {"key": "confidence_threshold", "label": "Routing confidence", "type": "number", "default": .78,
                  "min": .5, "max": .99, "step": .01},
                 {"key": "timeout_ms", "label": "Routing timeout (milliseconds)", "type": "integer", "default": 30000,
                  "min": 250, "max": 60000, "step": 250},
                 {"key": "voice_id", "label": "Routing voice", "type": "string",
                  "default": "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"}]},
            {"id": "tool-explainer", "name": "Tool explainer",
             "description": "Explain requested tool activity using bounded read-only input.",
             "recommended_backend": "codex", "recommended_model": "gpt-5.3-codex-spark",
             "recommended_effort": "low", "allowed_effects": ["tool_explanation"], "creatable": True,
             "supported_trigger_ids": ["tool-explanation-requested"], "default_trigger_id": "tool-explanation-requested",
             "supported_providers": ["codex"],
             "options": [{"key": "detail_level", "label": "Tool detail", "type": "choice", "default": 0,
                 "description": "Choose how requested tool activity is explained. Developer keeps the original activity.",
                 "choices": [{"value": value, "label": label} for value, label in enumerate(
                     ["Developer", "Technical", "Balanced", "Plain English", "Grandma"])]}]}]


def template(value: str) -> dict:
    selected = next((v for v in templates() if v["id"] == value), None)
    if selected is None:
        raise JanitorError("Unsupported maintenance job", 400, "unsupported_template")
    return selected


def _validate_option(descriptor: dict, value) -> None:
    kind = descriptor["type"]
    if kind == "boolean":
        valid = isinstance(value, bool)
    elif kind in {"integer", "number"}:
        valid = (not isinstance(value, bool) and isinstance(value, int if kind == "integer" else (int, float))
                 and descriptor.get("min", -math.inf) <= value <= descriptor.get("max", math.inf)
                 and (isinstance(value, int) or math.isfinite(value)))
    elif kind == "text":
        valid = isinstance(value, str) and len(value) <= descriptor.get("max_length", 16000) and "\x00" not in value
    elif kind == "string":
        valid = isinstance(value, str) and len(value) <= 160 and not any(ord(ch) < 32 for ch in value)
    elif kind == "choice":
        valid = any(type(value) is type(choice["value"]) and value == choice["value"] for choice in descriptor["choices"])
    else:
        valid = False
    if not valid:
        raise JanitorError(f"Invalid option: {descriptor['key']}", 400, "invalid_option")


def option_values(template_id: str, stored: dict | None = None) -> dict:
    """Resolve typed defaults without marking a device preference as configured."""
    raw = stored if stored is not None else {}
    if not isinstance(raw, dict):
        raise JanitorError("Stored options must be an object", 400, "invalid_option")
    descriptors = {item["key"]: item for item in template(template_id)["options"]}
    for key, value in raw.items():
        if key in descriptors:
            _validate_option(descriptors[key], value)
    return {**{key: item["default"] for key, item in descriptors.items()}, **raw}


def _option_patch(template_id: str, values, stored: dict | None = None) -> dict:
    if not isinstance(values, dict):
        raise JanitorError("Job options must be an object", 400, "invalid_option")
    raw = dict(stored or {})
    descriptors = {item["key"]: item for item in template(template_id)["options"]}
    for key, value in values.items():
        if key in descriptors:
            _validate_option(descriptors[key], value)
        elif key not in raw or _json(raw[key]) != _json(value):
            raise JanitorError(f"Unsupported option: {key}", 400, "unsupported_option")
    result = {**raw, **values}
    option_values(template_id, result)
    _json(result)
    return result


def _builtin_role(c, agent_id: str) -> str | None:
    row = c.execute("SELECT role FROM janitor_builtins WHERE agent_id=?", (agent_id,)).fetchone()
    return row[0] if row else None


def trigger_definitions() -> list[dict]:
    return [{"trigger_id": r["trigger_id"], "version": r["version"],
             "name": r["name"], "kind": r["kind"],
             "defaults": _decode(r["defaults_json"], {})}
            for r in db.conn().execute("SELECT * FROM janitor_trigger_definitions ORDER BY trigger_id, version")]


def _agent(session: str) -> dict:
    if not isinstance(session, str) or not session or len(session) > 160:
        raise JanitorError("Invalid agent identity")
    a = agents.get_by_session(session) or agents.get_by_agent_id(session)
    if not a:
        raise JanitorError("Agent not found", 404, "agent_not_found")
    return a


def _config(c, agent_id: str):
    row = c.execute("SELECT * FROM janitor_configs WHERE agent_id=?", (agent_id,)).fetchone()
    if not row:
        raise JanitorError("Janitor not found", 404, "janitor_not_found")
    return row


def _revision(row, expected_revision: int):
    if isinstance(expected_revision, bool) or expected_revision != row["revision"]:
        raise JanitorError("This configuration changed. Refresh it and repeat your change.", 409, "revision_conflict")


def _scope(value) -> dict:
    if not isinstance(value, dict) or set(value) - {"agent_ids", "exclude_agent_ids"}:
        raise JanitorError("Scope must contain agent_ids and exclude_agent_ids")
    result = {}
    for key in ("agent_ids", "exclude_agent_ids"):
        entries = value.get(key, [])
        if not isinstance(entries, list) or len(entries) > 1000 or any(not isinstance(v, str) or not v.strip() for v in entries):
            raise JanitorError("Scope agent IDs must be a bounded list")
        result[key] = sorted(set(entries))
        for aid in result[key]:
            target = agents.get_by_agent_id(aid)
            if not target or (key == "agent_ids" and target.get("is_janitor")):
                raise JanitorError("Choose existing task agents for the watched scope", 400, "invalid_scope")
    return result


def _template(value: str) -> str:
    template(value)
    return value


def _attachment_values(values, agent_id: str, template_id: str = "task-labels") -> list[dict]:
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise JanitorError("Choose between one and eight triggers")
    definitions = {(r["trigger_id"], r["version"]): r for r in trigger_definitions()}
    out, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise JanitorError("Invalid trigger configuration")
        key = (value.get("trigger_id"), value.get("trigger_version", 1))
        if not isinstance(key[0], str) or isinstance(key[1], bool) or not isinstance(key[1], int):
            raise JanitorError("Invalid trigger identity/version")
        definition = definitions.get(key)
        if not definition:
            raise JanitorError("Unsupported trigger version", 400, "unsupported_trigger")
        if key[0] not in template(template_id)["supported_trigger_ids"]:
            raise JanitorError("This trigger is not supported by the maintenance job", 400, "incompatible_trigger")
        aid = value.get("attachment_id") or f"attachment-{uuid.uuid4().hex}"
        if not isinstance(aid, str) or len(aid) > 100 or aid in seen:
            raise JanitorError("Invalid or duplicate trigger attachment")
        seen.add(aid)
        old = db.conn().execute("SELECT * FROM janitor_attachments WHERE attachment_id=?", (aid,)).fetchone()
        if old and (old["agent_id"] != agent_id or old["retired_at"] is not None):
            raise JanitorError("Trigger attachment belongs to another configuration", 409, "attachment_conflict")
        supplied = value.get("config", {})
        if not isinstance(supplied, dict):
            raise JanitorError("Trigger settings must be an object")
        config = {**definition["defaults"], **supplied}
        allowed = {"max_targets", "coalesce_seconds", "min_interval_seconds"}
        if definition["kind"] == "demand":
            allowed = set()
        if definition["kind"] == "schedule":
            allowed |= {"cron", "timezone"}
        if key[0] == "active-interval":
            allowed |= {"interval_seconds", "idle_timeout_seconds", "run_on_resume"}
        if set(config) - allowed:
            raise JanitorError("Unsupported trigger setting")
        if key[0] == "active-interval":
            for field in ("interval_seconds", "idle_timeout_seconds"):
                if type(config[field]) is not int or not 60 <= config[field] <= 86400:
                    raise JanitorError(f"{field} must be between 60 and 86400 seconds")
            if type(config["run_on_resume"]) is not bool:
                raise JanitorError("run_on_resume must be boolean")
        for field, low, high in (("max_targets", 1, 3), ("coalesce_seconds", 0, 300), ("min_interval_seconds", 0, 3600)):
            if field in config and (isinstance(config[field], bool) or not isinstance(config[field], int) or not low <= config[field] <= high):
                raise JanitorError(f"Invalid {field.replace('_', ' ')}")
        if definition["kind"] == "schedule":
            try:
                scheduler.parse_cron(config["cron"])
                ZoneInfo(config["timezone"])
            except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError) as exc:
                raise JanitorError("Use a valid five-field schedule and IANA timezone") from exc
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise JanitorError("Trigger enabled must be a boolean")
        out.append({"attachment_id": aid, "trigger_id": key[0], "trigger_version": key[1],
                    "enabled": enabled, "config": config})
    return out


def _save_attachments(c, agent_id: str, values: list[dict], now: int):
    ids = {v["attachment_id"] for v in values}
    for row in c.execute("SELECT * FROM janitor_attachments WHERE agent_id=? AND retired_at IS NULL", (agent_id,)).fetchall():
        if row["attachment_id"] not in ids:
            c.execute("UPDATE janitor_attachments SET enabled=0,retired_at=?,next_run_at=NULL WHERE attachment_id=?", (now, row["attachment_id"]))
    for v in values:
        old = c.execute("SELECT * FROM janitor_attachments WHERE attachment_id=?", (v["attachment_id"],)).fetchone()
        compatible = old and old["trigger_id"] == v["trigger_id"] and old["trigger_version"] == v["trigger_version"] and old["config_json"] == _json(v["config"])
        c.execute("""INSERT INTO janitor_attachments
            (attachment_id,agent_id,trigger_id,trigger_version,enabled,config_json,created_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(attachment_id) DO UPDATE SET
            trigger_id=excluded.trigger_id,trigger_version=excluded.trigger_version,
            enabled=excluded.enabled,config_json=excluded.config_json,next_run_at=NULL""",
            (v["attachment_id"], agent_id, v["trigger_id"], v["trigger_version"], int(v["enabled"]), _json(v["config"]), now))
        if not compatible:
            c.execute("DELETE FROM janitor_progress WHERE attachment_id=?", (v["attachment_id"],))


def _model(agent: dict, model, effort, *, provider=None):
    if model is not None and (not isinstance(model, str) or len(model) > 160 or not backends.is_valid_model(agent["backend"], model)):
        raise JanitorError("Invalid model for this agent")
    efforts = ("minimal", "low", "medium", "high") if provider == "openai" else backends.valid_efforts(agent["backend"])
    if effort is not None and (not isinstance(effort, str) or (effort and effort not in efforts)):
        raise JanitorError("Invalid reasoning effort for this agent")


def _execution(template_id: str, value, backend: str) -> dict:
    if template_id in {"task-labels", "custom-task"}:
        if value not in ({}, None):
            raise JanitorError("This job uses the managed agent executor")
        return {}
    if value is None:
        value = {"executor": "ephemeral", "provider": backend}
    if not isinstance(value, dict) or set(value) - {"executor", "provider"}:
        raise JanitorError("Invalid demand execution configuration")
    provider = value.get("provider", backend)
    if value.get("executor", "ephemeral") != "ephemeral" or not isinstance(provider, str):
        raise JanitorError("Demand jobs require an ephemeral executor")
    if provider != "openai" and not any(v.id == provider for v in backends.routing_adapters()):
        raise JanitorError("Unsupported demand execution provider")
    if backend != ("codex" if provider == "openai" else provider):
        raise JanitorError("Execution provider must match the Janitor backend")
    if template_id == "tool-explainer" and (provider != "codex" or backend != "codex"):
        raise JanitorError("Tool explanation requires the codex provider")
    return {"executor": "ephemeral", "provider": provider}


def create(session: str, template_id: str = "task-labels", scope=None, attachments=None, options=_UNSET, execution=None) -> dict:
    a = _agent(session)
    runtime_busy = bool(backends.active_handles(a["backend"], a["agent_id"]))
    observed_backend = a["backend"]
    with _write() as c:
        a = _agent(a["agent_id"])
        existing = c.execute("SELECT 1 FROM janitor_configs WHERE agent_id=?", (a["agent_id"],)).fetchone()
        if existing and a.get("is_janitor"):
            return get(session, include_runtime=False)
        if a.get("archived_at"):
            raise JanitorError("Restore this agent before configuring maintenance", 409, "agent_archived")
        if a["backend"] != observed_backend or agents.is_busy(a["agent_id"]) or runtime_busy or turn_queue.pending_count(a["agent_id"]):
            raise JanitorError("Wait for this agent's current work and queue to finish before converting it", 409, "agent_busy")
        if existing:
            c.execute("UPDATE agents SET is_janitor=1,heartbeat_enabled=0,dreaming_enabled=0 WHERE agent_id=?", (a["agent_id"],))
            return get(session, include_runtime=False)
        selected_scope = _scope(scope if scope is not None else {})
        values = _attachment_values(attachments if attachments is not None else [{"trigger_id": template(template_id)["default_trigger_id"]}], a["agent_id"], template_id)
        now = db.now_ms()
        execution = _execution(template_id, execution, a["backend"])
        _model(a, a["model"], a["effort"], provider=execution.get("provider"))
        selected_options = {} if options is _UNSET else _option_patch(template_id, options)
        c.execute("INSERT INTO janitor_configs(agent_id,template_id,scope_json,execution_json,options_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (a["agent_id"], _template(template_id), _json(selected_scope), _json(execution), _json(selected_options), now, now))
        c.execute("UPDATE agents SET is_janitor=1,heartbeat_enabled=0,dreaming_enabled=0 WHERE agent_id=?", (a["agent_id"],))
        _save_attachments(c, a["agent_id"], values, now)
    return get(session)


def _public_attachment(row) -> dict:
    return {"attachment_id": row["attachment_id"], "trigger_id": row["trigger_id"],
            "trigger_version": row["trigger_version"], "enabled": bool(row["enabled"]),
            "config": _decode(row["config_json"], {}), "next_run_at": row["next_run_at"]}


def _pending_demand_claim(c, agent_id: str) -> bool:
    return bool(c.execute("""SELECT 1 FROM janitor_demand_claims d JOIN janitor_runs r ON r.run_id=d.run_id
        WHERE r.agent_id=? AND d.finished_at IS NULL
        AND json_extract(r.configuration_json,'$.expires_at')>? LIMIT 1""", (agent_id, db.now_ms())).fetchone())


def get(session: str, *, include_runtime: bool = True) -> dict:
    from .avatar_urls import janitor_avatar_url
    a = _agent(session)
    c = db.conn()
    row = _config(c, a["agent_id"])
    builtin_role = _builtin_role(c, a["agent_id"])
    current = c.execute("SELECT run_id FROM janitor_runs WHERE agent_id=? AND status IN ('queued','running')", (a["agent_id"],)).fetchone()
    return {"agent_id": a["agent_id"], "session": a["session"], "name": a["persona"],
            "is_janitor": bool(a.get("is_janitor")), "enabled": bool(row["enabled"]), "revision": row["revision"],
            "builtin_role": builtin_role, "execution": _decode(row["execution_json"], {}),
            "options": option_values(row["template_id"], _decode(row["options_json"], {})),
            "configured_option_keys": sorted(_decode(row["options_json"], {})),
            "avatar_url": janitor_avatar_url(bool(a.get("is_janitor")), row["template_id"]),
            "capabilities": {"can_change_template": not bool(builtin_role), "can_remove": not bool(builtin_role), "can_release": not bool(builtin_role)},
            "supported_trigger_ids": template(row["template_id"])["supported_trigger_ids"],
            "cancellation_pending": not bool(row["enabled"]) and (agents.is_busy(a["agent_id"]) or (include_runtime and bool(backends.active_handles(a["backend"], a["agent_id"]))) or _pending_demand_claim(c, a["agent_id"])),
            "generation": row["generation"], "template_id": row["template_id"],
            "model": a["model"], "effort": a["effort"], "backend": a["backend"],
            "scope": _decode(row["scope_json"], {}), "health": ("paused" if not row["enabled"] else "running" if current else "needs_attention" if row["last_error"] else "ready"),
            "last_error": row["last_error"], "last_run_at": row["last_run_at"],
            "last_change_at": row["last_change_at"], "current_run_id": current[0] if current else None,
            "attachments": [_public_attachment(r) for r in c.execute("SELECT * FROM janitor_attachments WHERE agent_id=? AND retired_at IS NULL ORDER BY created_at,attachment_id", (a["agent_id"],))]}


def list_janitors(*, include_runtime: bool = True) -> list[dict]:
    return [get(r[0], include_runtime=include_runtime) for r in db.conn().execute("SELECT a.agent_id FROM agents a JOIN janitor_configs j ON j.agent_id=a.agent_id WHERE a.is_janitor=1 AND a.deleted_at IS NULL AND a.archived_at IS NULL ORDER BY a.persona")]


def _fence(c, agent_id: str, now: int):
    traces = [r[0] for r in c.execute("SELECT trace_id FROM janitor_runs WHERE agent_id=? AND status IN ('queued','running')", (agent_id,))]
    c.execute("UPDATE janitor_runs SET status='cancelled',outcome='cancelled',finished_at=? WHERE agent_id=? AND status IN ('queued','running')", (now, agent_id))
    for trace in traces:
        c.execute("UPDATE queued_turns SET status='cancelled',text='' WHERE agent_id=? AND trace_id=? AND status IN ('queued','claimed')", (agent_id, trace))
    if traces:
        turn_queue._bump_revision(agent_id)
    c.execute("UPDATE janitor_attachments SET next_run_at=NULL WHERE agent_id=?", (agent_id,))


def configure(session: str, expected_revision: int, *, template_id=None, scope=None, attachments=None, model=None, effort=None, execution=None, backend=None, options=_UNSET) -> dict:
    a = _agent(session)
    with _write() as c:
        row = _config(c, a["agent_id"])
        _revision(row, expected_revision)
        selected_backend = a["backend"] if backend is None else backend
        if not isinstance(selected_backend, str) or not backends.get(selected_backend):
            raise JanitorError("Unsupported Janitor backend")
        selected_scope = _scope(scope) if scope is not None else _decode(row["scope_json"], {})
        selected_template = _template(template_id) if template_id is not None else row["template_id"]
        if _builtin_role(c, a["agent_id"]) and selected_template != row["template_id"]:
            raise JanitorError("A built-in Janitor cannot change its job", 409, "builtin_janitor")
        selected_execution = _execution(selected_template, execution if execution is not None else
            (_decode(row["execution_json"], {}) if selected_template == row["template_id"] else None), selected_backend)
        _model({**a, "backend": selected_backend}, a["model"] if model is None else model,
               a["effort"] if effort is None else effort, provider=selected_execution.get("provider"))
        selected_options = _decode(row["options_json"], {}) if selected_template == row["template_id"] else {}
        if options is not _UNSET:
            selected_options = _option_patch(selected_template, options, selected_options)
        values = _attachment_values(attachments, a["agent_id"], selected_template) if attachments is not None else None
        if values is None and selected_template != row["template_id"]:
            values = _attachment_values([{"trigger_id": template(selected_template)["default_trigger_id"]}], a["agent_id"], selected_template)
        now = db.now_ms()
        _fence(c, a["agent_id"], now)
        c.execute("UPDATE janitor_configs SET enabled=0,generation=generation+1,revision=revision+1,template_id=?,scope_json=?,execution_json=?,options_json=?,last_error='',updated_at=? WHERE agent_id=?", (selected_template, _json(selected_scope), _json(selected_execution), _json(selected_options), now, a["agent_id"]))
        if selected_scope != _decode(row["scope_json"], {}) or selected_template != row["template_id"]:
            c.execute("DELETE FROM janitor_progress WHERE attachment_id IN (SELECT attachment_id FROM janitor_attachments WHERE agent_id=?)", (a["agent_id"],))
        if values is not None:
            _save_attachments(c, a["agent_id"], values, now)
        agents.update_agent(a["agent_id"], model=model, effort=effort, backend=backend)
    return get(session)


def reset_defaults(session: str, expected_revision: int) -> dict:
    """Reset tuning, never identity/history/scope or enablement. Revision fenced."""
    current = get(session)
    if not current:
        raise JanitorError("Janitor not found", 404, "janitor_not_found")
    definitions = {(d["trigger_id"], d["version"]): d for d in trigger_definitions()}
    attachments = [{"attachment_id": a["attachment_id"], "trigger_id": a["trigger_id"],
                    "trigger_version": a["trigger_version"], "enabled": a["enabled"],
                    "config": definitions[(a["trigger_id"], a["trigger_version"])]["defaults"]}
                   for a in current["attachments"]]
    template = next(t for t in templates() if t["id"] == current["template_id"])
    model = template["recommended_model"] if current["backend"] == template["recommended_backend"] else None
    effort = template["recommended_effort"] if model else None
    return configure(session, expected_revision, attachments=attachments, model=model, effort=effort,
        execution={}, options=(current["options"] if current["template_id"] == "custom-task"
                 else {item["key"]: item["default"] for item in template["options"]}))


def adopt_options(session: str, expected_revision: int, options: dict) -> dict:
    """Adopt one existing device's tool-detail choice once, without enabling work.

    Once any device or explicit configuration owns the setting, its value wins.
    A stale repeat is then a read; an unset stale write still conflicts.
    """
    if not isinstance(options, dict) or set(options) != {"detail_level"}:
        raise JanitorError("Only the legacy detail_level option may be adopted")
    _option_patch("tool-explainer", options)
    with _write() as c:
        a = _agent(session)
        row = _config(c, a["agent_id"])
        if (_builtin_role(c, a["agent_id"]) != "tool-explainer" or not a.get("is_janitor")
                or a.get("archived_at") or row["template_id"] != "tool-explainer"):
            raise JanitorError("Legacy options can only be adopted by the built-in tool explainer", 409, "invalid_adoption")
        stored = _decode(row["options_json"], {})
        if "detail_level" in stored:
            return get(a["agent_id"], include_runtime=False)
        _revision(row, expected_revision)
        now = db.now_ms()
        _fence(c, a["agent_id"], now)
        c.execute("UPDATE janitor_configs SET options_json=?,generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?",
                  (_json(_option_patch("tool-explainer", options, stored)), now, a["agent_id"]))
    return get(session, include_runtime=False)


def _overlap(a: dict, b: dict) -> bool:
    ai, bi = set(a.get("agent_ids", [])), set(b.get("agent_ids", []))
    excluded = set(a.get("exclude_agent_ids", [])) | set(b.get("exclude_agent_ids", []))
    if not ai and not bi:
        return True  # Includes future ordinary agents, not merely today's roster.
    return bool(((ai & bi) if ai and bi else ai or bi) - excluded)


def set_enabled(session: str, expected_revision: int, enabled: bool) -> dict:
    if not isinstance(enabled, bool):
        raise JanitorError("Enabled must be a boolean")
    a = _agent(session)
    with _write() as c:
        row = _config(c, a["agent_id"])
        _revision(row, expected_revision)
        if bool(row["enabled"]) == enabled:
            return get(session)
        if enabled:
            if not a.get("is_janitor"):
                raise JanitorError("This Janitor was released; convert it again before enabling", 409, "janitor_released")
            if a.get("archived_at"):
                raise JanitorError("Archived maintenance cannot be enabled", 409, "agent_archived")
            if row["template_id"] == "custom-task" and not option_values(
                    "custom-task", _decode(row["options_json"], {}))["instructions"].strip():
                raise JanitorError("Add instructions before enabling this custom task")
            for other in c.execute("SELECT j.* FROM janitor_configs j JOIN agents a ON a.agent_id=j.agent_id WHERE j.enabled=1 AND j.agent_id!=? AND a.deleted_at IS NULL AND a.archived_at IS NULL", (a["agent_id"],)):
                if (set(template(row["template_id"])["allowed_effects"]) & set(template(other["template_id"])["allowed_effects"])
                        and _overlap(_decode(row["scope_json"], {}), _decode(other["scope_json"], {}))):
                    raise JanitorError("Another enabled Janitor already maintains this job in this scope", 409, "scope_conflict")
            if not c.execute("SELECT 1 FROM janitor_attachments WHERE agent_id=? AND enabled=1 AND retired_at IS NULL", (a["agent_id"],)).fetchone():
                raise JanitorError("Enable at least one trigger first")
        now = db.now_ms()
        _fence(c, a["agent_id"], now)
        c.execute("UPDATE janitor_configs SET enabled=?,generation=generation+1,revision=revision+1,last_error='',updated_at=? WHERE agent_id=?", (int(enabled), now, a["agent_id"]))
    return get(session)


def remove(session: str, expected_revision: int) -> bool:
    a = _agent(session)
    with _write() as c:
        row = _config(c, a["agent_id"])
        _revision(row, expected_revision)
        if _builtin_role(c, a["agent_id"]):
            raise JanitorError("Pause a built-in Janitor instead of removing it", 409, "builtin_janitor")
        now = db.now_ms()
        _fence(c, a["agent_id"], now)
        c.execute("UPDATE janitor_configs SET enabled=0,generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?", (now, a["agent_id"]))
        agents.set_archived(a["agent_id"], True)
    return True


def _release_idle(c, agent: dict, configuration, runtime_busy: dict) -> None:
    if configuration["enabled"]:
        raise JanitorError("Pause this Janitor before releasing or handing off maintenance", 409, "janitor_enabled")
    observed_backend, active = runtime_busy[agent["agent_id"]]
    if (agent["backend"] != observed_backend or agents.is_busy(agent["agent_id"]) or active
            or turn_queue.pending_count(agent["agent_id"]) or has_active_run(agent["agent_id"])
            or _pending_demand_claim(c, agent["agent_id"])):
        raise JanitorError("Wait for current maintenance and cancellation to finish", 409, "agent_busy")


def _handoff_labels(c, source: dict, source_config, successor_session: str, successor_revision: int, now: int, runtime_busy: dict) -> dict:
    successor = _agent(successor_session)
    successor_config = _config(c, successor["agent_id"])
    _revision(successor_config, successor_revision)
    if source["agent_id"] == successor["agent_id"]:
        raise JanitorError("Choose a different successor Janitor")
    for agent, configuration in ((source, source_config), (successor, successor_config)):
        if not agent.get("is_janitor") or agent.get("archived_at"):
            raise JanitorError("Both maintenance identities must be current, unarchived Janitors", 409, "invalid_successor")
        if configuration["template_id"] != "task-labels":
            raise JanitorError("Only compatible task-label jobs can hand off label ownership", 409, "capability_denied")
        if agent["agent_id"] != source["agent_id"]:
            _release_idle(c, agent, configuration, runtime_busy)
        state = agents.latest_state(agent["agent_id"])
        if not state or state["kind"] not in {AgentState.IDLE, AgentState.DONE, AgentState.SPAWNED, AgentState.STOPPED, AgentState.INTERRUPTED}:
            raise JanitorError("Wait for both Janitors to finish before handing off maintenance", 409, "agent_busy")
    scope = _scope(_decode(source_config["scope_json"], {}))
    if scope != _scope(_decode(successor_config["scope_json"], {})):
        raise JanitorError("The successor must have the same watched scope", 409, "scope_conflict")
    targets = [row["agent_id"] for row in c.execute("""SELECT a.* FROM agents a
        JOIN janitor_label_ownership o ON o.target_agent_id=a.agent_id
        WHERE o.owner_agent_id=? AND a.custom_status=o.label""", (source["agent_id"],))
        if _in_scope(scope, dict(row))]
    # Ownership changes, but the originating run and its effect receipts remain
    # the prior Janitor's provenance until the successor writes a new one.
    c.executemany("UPDATE janitor_label_ownership SET owner_agent_id=?,revision=revision+1,updated_at=? WHERE target_agent_id=?",
                  [(successor["agent_id"], now, target_id) for target_id in targets])
    c.execute("UPDATE janitor_configs SET generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?",
              (now, successor["agent_id"]))
    # Repeat the existing idle state; this is an administrative audit, not an
    # inferred completion, a new model receipt, or an event to notify the user.
    agents.record_state(source["agent_id"], agents.latest_state(source["agent_id"])["kind"], {
        "origin": "janitor", "event": "janitor_ownership_handoff", "source_agent_id": source["agent_id"],
        "successor_agent_id": successor["agent_id"], "transferred_count": len(targets)})
    return {"successor_agent_id": successor["agent_id"], "successor_session": successor["session"], "transferred_count": len(targets)}


def release(session: str, expected_revision: int, *, successor_session: str | None = None,
            successor_revision: int | None = None) -> dict:
    """Return an idle Janitor to chat, optionally handing off matching owned labels.

    Both sides of a handoff must be explicitly paused with current revisions and
    equal scope. Text and original run provenance remain untouched. The successor
    remains paused, with a fresh revision required for a later enable operation.
    """
    if (successor_session is None) != (successor_revision is None):
        raise JanitorError("A maintenance successor needs its session and current revision")
    a = _agent(session)
    handoff = None
    # The split runtime can write liveness while answering status. Never wait
    # for it while holding the same database's writer lock. Both Janitors must
    # already be paused; queue/state/claim/revision checks are repeated below.
    participants = [a]
    if successor_session is not None:
        participants.append(_agent(successor_session))
    runtime_busy = {row["agent_id"]: (row["backend"], bool(backends.active_handles(row["backend"], row["agent_id"])))
                    for row in participants}
    with _write() as c:
        a = _agent(a["agent_id"])
        row = _config(c, a["agent_id"])
        _revision(row, expected_revision)
        if _builtin_role(c, a["agent_id"]):
            raise JanitorError("A built-in Janitor cannot be released", 409, "builtin_janitor")
        _release_idle(c, a, row, runtime_busy)
        if not a.get("is_janitor"):
            if successor_session is not None:
                raise JanitorError("This Janitor was already released", 409, "janitor_released")
            return get(session, include_runtime=False)
        now = db.now_ms()
        if successor_session is not None:
            handoff = _handoff_labels(c, a, row, successor_session, successor_revision, now, runtime_busy)
        _fence(c, a["agent_id"], now)
        c.execute("UPDATE janitor_configs SET generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?", (now, a["agent_id"]))
        c.execute("UPDATE agents SET is_janitor=0 WHERE agent_id=?", (a["agent_id"],))
    result = get(session)
    if handoff is not None:
        result["ownership_handoff"] = handoff
    return result


def attachments(enabled_only: bool = False) -> list[dict]:
    query = """SELECT t.*,j.generation,j.template_id,j.scope_json,j.enabled AS janitor_enabled,
        a.session FROM janitor_attachments t JOIN janitor_configs j ON j.agent_id=t.agent_id
        JOIN agents a ON a.agent_id=t.agent_id WHERE t.retired_at IS NULL
        AND a.is_janitor=1 AND a.deleted_at IS NULL AND a.archived_at IS NULL"""
    if enabled_only:
        query += " AND t.enabled=1 AND j.enabled=1"
    return [{**_public_attachment(r), "agent_id": r["agent_id"], "session": r["session"],
             "generation": r["generation"], "template_id": r["template_id"],
             "scope": _decode(r["scope_json"], {}), "janitor_enabled": bool(r["janitor_enabled"])}
            for r in db.conn().execute(query)]


def _active_attachment(c, attachment_id: str, generation: int):
    row = c.execute("""SELECT t.*,j.generation,j.scope_json,j.template_id,j.enabled AS janitor_enabled,a.session
        FROM janitor_attachments t JOIN janitor_configs j ON j.agent_id=t.agent_id
        JOIN agents a ON a.agent_id=t.agent_id WHERE t.attachment_id=?
        AND a.is_janitor=1 AND a.deleted_at IS NULL AND a.archived_at IS NULL""", (attachment_id,)).fetchone()
    if not row or row["generation"] != generation or not row["janitor_enabled"] or not row["enabled"] or row["retired_at"] is not None:
        raise JanitorError("This maintenance run was paused or superseded", 409, "stale_generation")
    return row


def get_progress(attachment_id: str) -> dict:
    row = db.conn().execute("SELECT * FROM janitor_progress WHERE attachment_id=?", (attachment_id,)).fetchone()
    return _decode(row["state_json"], {}) if row else {}


def _save_progress(c, attachment_id, generation, state, next_run_at=None):
    if not isinstance(state, dict):
        raise JanitorError("Progress must be an object")
    c.execute("""INSERT INTO janitor_progress(attachment_id,generation,state_json,updated_at)
        VALUES (?,?,?,?) ON CONFLICT(attachment_id) DO UPDATE SET generation=excluded.generation,
        state_json=excluded.state_json,updated_at=excluded.updated_at""", (attachment_id, generation, _json(state), db.now_ms()))
    if next_run_at is not None or "next_run_at" in state:
        c.execute("UPDATE janitor_attachments SET next_run_at=? WHERE attachment_id=?", (next_run_at, attachment_id))


def save_progress(attachment_id: str, generation: int, state: dict, next_run_at=None):
    with _write() as c:
        _active_attachment(c, attachment_id, generation)
        _save_progress(c, attachment_id, generation, state, next_run_at)


def _in_scope(scope, target):
    return (not target.get("is_janitor") and target.get("deleted_at") is None and target.get("archived_at") is None
            and target["agent_id"] not in scope.get("exclude_agent_ids", [])
            and (not scope.get("agent_ids") or target["agent_id"] in scope["agent_ids"]))


def create_run(attachment_id: str, generation: int, candidates: list[dict], run_id: str | None = None, progress: dict | None = None) -> dict:
    run_id = run_id or f"janitor-{uuid.uuid4().hex}"
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 160:
        raise JanitorError("Invalid maintenance run identity")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
        raise JanitorError("A run needs between one and three candidates")
    with _write() as c:
        old = c.execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
        if old:
            if old["attachment_id"] != attachment_id or old["generation"] != generation or old["candidates_json"] != _json(candidates):
                raise JanitorError("Run identity already refers to different work", 409, "run_conflict")
            return get_run(run_id)
        attachment = _active_attachment(c, attachment_id, generation)
        if attachment["template_id"] != "task-labels":
            raise JanitorError("Only a task-label job can admit label reviews", 409, "capability_denied")
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise JanitorError("Invalid run candidate")
            target = _agent(candidate.get("session", candidate.get("target_session", "")))
            if not _in_scope(_decode(attachment["scope_json"], {}), target) or target["agent_id"] in seen:
                raise JanitorError("Candidate is outside this maintenance scope", 409, "scope_conflict")
            seen.add(target["agent_id"])
            if candidate.get("agent_id") != target["agent_id"]:
                raise JanitorError("Candidate identity changed", 409, "stale_candidate")
            if not all(k in candidate for k in ("fingerprint", "task_key", "change_key", "source_refs")):
                raise JanitorError("Candidate is missing frozen task evidence")
        if has_active_run(attachment["agent_id"]):
            raise JanitorError("This Janitor already has an active run", 409, "janitor_busy")
        frozen = {"template_id": _config(c, attachment["agent_id"])["template_id"],
                  "scope": _decode(attachment["scope_json"], {}), "trigger_id": attachment["trigger_id"],
                  "trigger_version": attachment["trigger_version"], "config": _decode(attachment["config_json"], {})}
        frozen["options"] = option_values(frozen["template_id"], _decode(_config(c, attachment["agent_id"])["options_json"], {}))
        c.execute("INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,candidates_json,configuration_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (run_id, attachment["agent_id"], attachment["session"], attachment_id, generation, run_id, _json(candidates), _json(frozen), db.now_ms()))
        if progress is not None:
            _save_progress(c, attachment_id, generation, progress)
    return get_run(run_id)


def create_custom_run(attachment_id: str, generation: int, *, run_id: str, progress: dict) -> dict:
    """Freeze user instructions and progress atomically, using the existing run ledger."""
    with _write() as c:
        attachment = _active_attachment(c, attachment_id, generation)
        if attachment["template_id"] != "custom-task":
            raise JanitorError("This attachment does not run custom tasks", 409, "capability_denied")
        old = c.execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
        if old:
            if old["attachment_id"] != attachment_id or old["generation"] != generation:
                raise JanitorError("Run identity already refers to different work", 409, "run_conflict")
            return get_run(run_id)
        if has_active_run(attachment["agent_id"]):
            raise JanitorError("This Janitor already has an active run", 409, "janitor_busy")
        options = option_values("custom-task", _decode(_config(c, attachment["agent_id"])["options_json"], {}))
        if not options["instructions"].strip():
            raise JanitorError("Custom task instructions are empty")
        frozen = {"template_id": "custom-task", "options": options,
                  "trigger_id": attachment["trigger_id"], "trigger_version": attachment["trigger_version"],
                  "config": _decode(attachment["config_json"], {}), "scope": _decode(attachment["scope_json"], {})}
        c.execute("INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,candidates_json,configuration_json,created_at) VALUES (?,?,?,?,?,?,'[]',?,?)",
                  (run_id, attachment["agent_id"], attachment["session"], attachment_id, generation,
                   run_id, _json(frozen), db.now_ms()))
        _save_progress(c, attachment_id, generation, progress, progress.get("next_run_at"))
    return get_run(run_id)


def _public_run(row) -> dict:
    value = dict(row)
    value["queue_id"] = value["client_msg_id"] = value["run_id"]
    value["candidates"] = _decode(value.pop("candidates_json"), [])
    value["configuration"] = _decode(value.pop("configuration_json"), {})
    demand = db.conn().execute("SELECT result_json FROM janitor_demand_results WHERE run_id=?", (row["run_id"],)).fetchone()
    value["demand_result"] = _decode(demand[0], {}) if demand else None
    value["results"] = [{"target_agent_id": r["target_agent_id"], "target_session": r["target_session"],
                         "observed_state_id": r["observed_state_id"], "before": r["before_label"],
                         "after": r["after_label"], "outcome": r["outcome"], "reason": r["reason"],
                         "created_at": r["created_at"]}
                        for r in db.conn().execute("SELECT * FROM janitor_effects WHERE run_id=? ORDER BY created_at,target_agent_id", (row["run_id"],))]
    return value


def get_run(run_id: str) -> dict | None:
    row = db.conn().execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
    return _public_run(row) if row else None


def get_run_for_trace(trace_id: str) -> dict | None:
    row = db.conn().execute("SELECT * FROM janitor_runs WHERE trace_id=?", (trace_id,)).fetchone()
    return _public_run(row) if row else None


def list_runs(session: str, limit: int = 30) -> list[dict]:
    a = _agent(session)
    return [_public_run(r) for r in db.conn().execute("SELECT * FROM janitor_runs WHERE agent_id=? ORDER BY created_at DESC,run_id DESC LIMIT ?", (a["agent_id"], max(1, min(int(limit), 100))))]


def active_runs(agent_id: str = "") -> list[dict]:
    return [_public_run(r) for r in db.conn().execute("SELECT * FROM janitor_runs WHERE status IN ('queued','running')" + (" AND agent_id=?" if agent_id else ""), (agent_id,) if agent_id else ())]


def has_active_run(agent_id: str) -> bool:
    return bool(db.conn().execute("SELECT 1 FROM janitor_runs WHERE agent_id=? AND status IN ('queued','running')", (agent_id,)).fetchone())


def _active_run(c, run_id):
    row = c.execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise JanitorError("Maintenance run not found", 404, "run_not_found")
    if row["status"] not in {"queued", "running"}:
        raise JanitorError("This maintenance run is no longer active", 409, "run_terminal")
    _active_attachment(c, row["attachment_id"], row["generation"])
    configured = _config(c, row["agent_id"])
    if _decode(row["configuration_json"], {}).get("options", {}) != option_values(configured["template_id"], _decode(configured["options_json"], {})):
        raise JanitorError("This maintenance run's options were superseded", 409, "stale_generation")
    return row


def validate_dispatch(session: str, run_id: str, trace_id: str) -> bool:
    try:
        row = _active_run(db.conn(), run_id)
        if (row["session"] != session or row["trace_id"] != trace_id
                or _decode(row["configuration_json"], {}).get("template_id") not in {"task-labels", "custom-task"}):
            return False
        attachment = _active_attachment(db.conn(), row["attachment_id"], row["generation"])
        if row["status"] == "queued" and attachment["trigger_id"] == "active-interval":
            from . import application_activity
            if not application_activity.active(_decode(attachment["config_json"], {})["idle_timeout_seconds"]):
                db.conn().execute("UPDATE janitor_runs SET status='cancelled',outcome='cancelled',finished_at=?,error='Application became inactive before execution' WHERE run_id=? AND status='queued'", (db.now_ms(), run_id))
                return False
        return True
    except JanitorError:
        return False


def run_context(run_id: str) -> dict:
    row = _active_run(db.conn(), run_id)
    return {"run_id": run_id, "generation": row["generation"], "candidates": _decode(row["candidates_json"], []),
            "configuration": _decode(row["configuration_json"], {})}


def mark_started(run_id: str) -> dict:
    with _write() as c:
        _active_run(c, run_id)
        c.execute("UPDATE janitor_runs SET status='running',started_at=COALESCE(started_at,?) WHERE run_id=?", (db.now_ms(), run_id))
    return get_run(run_id)


def finish_run(run_id: str, outcome: str = "", error: str = "") -> dict:
    with _write() as c:
        row = c.execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise JanitorError("Maintenance run not found", 404, "run_not_found")
        if row["status"] not in {"queued", "running"}:
            return get_run(run_id)
        _active_run(c, run_id)
        if _decode(row["configuration_json"], {}).get("executor") == "ephemeral":
            raise JanitorError("Demand jobs require their guarded result receipt", 409, "capability_denied")
        if _decode(row["configuration_json"], {}).get("template_id") == "custom-task":
            if outcome not in {"completed", "error", "cancelled"}:
                raise JanitorError("Custom task completion requires an explicit terminal outcome")
            now = db.now_ms()
            safe_error = str(error)[:500]
            c.execute("UPDATE janitor_runs SET status=?,outcome=?,finished_at=?,error=? WHERE run_id=?",
                      ("failed" if outcome == "error" else outcome, outcome, now, safe_error, run_id))
            c.execute("UPDATE janitor_configs SET last_run_at=?,last_error=?,updated_at=? WHERE agent_id=?",
                      (now, safe_error, now, row["agent_id"]))
            return get_run(run_id)
        receipts = list(c.execute("SELECT outcome FROM janitor_effects WHERE run_id=?", (run_id,)))
        if not outcome:
            outcome = ("error" if len(receipts) != len(_decode(row["candidates_json"], [])) or any(r[0] == "error" for r in receipts)
                       else "changed" if any(r[0] == "changed" for r in receipts)
                       else "same_task" if all(r[0] == "same_task" for r in receipts) else "skipped")
        if outcome not in TERMINAL_OUTCOMES:
            raise JanitorError("Invalid maintenance outcome")
        if outcome not in {"cancelled", "error"} and len(receipts) != len(_decode(row["candidates_json"], [])):
            raise JanitorError("Review receipts are incomplete")
        if outcome == "changed" and not any(r[0] == "changed" for r in receipts):
            raise JanitorError("No effect receipt proves a changed label")
        now = db.now_ms()
        safe_error = str(error)[:500] or ("Maintenance ended without all review receipts" if outcome == "error" else "")
        c.execute("UPDATE janitor_runs SET status=?,outcome=?,finished_at=?,error=? WHERE run_id=?", ("failed" if outcome == "error" else "cancelled" if outcome == "cancelled" else "completed", outcome, now, safe_error, run_id))
        c.execute("UPDATE janitor_configs SET last_run_at=?,last_error=?,updated_at=? WHERE agent_id=?", (now, safe_error, now, row["agent_id"]))
    return get_run(run_id)


def owned_label(agent_id: str, target_session: str) -> str | None:
    row = db.conn().execute("SELECT o.label,a.custom_status FROM janitor_label_ownership o JOIN agents a ON a.agent_id=o.target_agent_id WHERE o.owner_agent_id=? AND a.session=?", (agent_id, target_session)).fetchone()
    return row["label"] if row and row["label"] == row["custom_status"] else None


def _task_signatures(c, agent_ids: list[str]) -> dict[str, str]:
    """Batch deterministic annotation validity rather than N queries per caption."""
    result = {}
    for offset in range(0, len(agent_ids), 500):
        chunk = agent_ids[offset:offset + 500]
        marks = ",".join("?" for _ in chunk)
        query = f"""SELECT a.agent_id,r.runtime_id,r.backend_session_id,
            (SELECT COALESCE(MAX(m.revision),0) FROM messages m WHERE m.agent_id=a.agent_id
              AND m.backend_session_id=COALESCE(r.backend_session_id,'') AND m.role='user'
              AND (m.origin='user' OR m.origin IS NULL)) AS request_revision,
            p.plan_id,p.title,p.status,p.updated_at
            FROM agents a LEFT JOIN runtimes r ON r.runtime_id=(
                SELECT runtime_id FROM runtimes WHERE agent_id=a.agent_id
                ORDER BY started_at DESC,runtime_id DESC LIMIT 1)
            LEFT JOIN task_plans p ON p.plan_id=(
                SELECT plan_id FROM task_plans WHERE agent_id=a.agent_id
                ORDER BY updated_at DESC,plan_id LIMIT 1)
            WHERE a.agent_id IN ({marks})"""
        for row in c.execute(query, chunk):
            result[row["agent_id"]] = hashlib.sha256(_json(list(row)[1:]).encode()).hexdigest()
    return result


def _task_signature(c, agent_id: str) -> str:
    return _task_signatures(c, [agent_id])[agent_id]


def visible_labels() -> dict[str, str]:
    c = db.conn()
    now = db.now_ms()
    rows = c.execute("SELECT o.*,a.custom_status FROM janitor_label_ownership o JOIN agents a ON a.agent_id=o.target_agent_id").fetchall()
    signatures = _task_signatures(c, [r["target_agent_id"] for r in rows])
    return {r["target_agent_id"]: "" for r in rows if r["label"] == r["custom_status"]
            and (r["valid_until"] <= now or r["task_signature"] != signatures.get(r["target_agent_id"]))}


def review(run_id: str, target_session: str, observed_state_id: int, outcome: str, label: str | None = None, reason: str = "") -> dict:
    if outcome not in {"changed", "same_task", "insufficient_context", "error"}:
        raise JanitorError("Invalid review outcome")
    if isinstance(observed_state_id, bool) or not isinstance(observed_state_id, int) or observed_state_id < 1:
        raise JanitorError("A review requires its observed state ID")
    if not isinstance(reason, str) or len(reason) > 500:
        raise JanitorError("Keep the review reason under 500 characters")
    if outcome == "changed" and (not isinstance(label, str) or label != label.strip() or len(label) > 20 or not 2 <= len(label.split()) <= 3 or any(ord(ch) < 32 for ch in label)):
        raise JanitorError("A label must have two or three words and at most 20 characters")
    from .janitor_context import build_context_from_connection
    observed_target = _agent(target_session)
    runtime_busy = bool(backends.active_handles(observed_target["backend"], observed_target["agent_id"]))
    with _write() as c:
        target = _agent(target_session)
        existing = c.execute("SELECT * FROM janitor_effects WHERE run_id=? AND target_agent_id=?", (run_id, target["agent_id"])).fetchone()
        if existing:
            # An accepted identical retry is a receipt read, not another effect.
            requested_after = label if outcome == "changed" else existing["before_label"]
            normalized_outcome = "same_task" if outcome == "changed" and label == existing["before_label"] else outcome
            if observed_state_id != existing["observed_state_id"] or requested_after != existing["after_label"] or reason != existing["reason"] or normalized_outcome != existing["outcome"]:
                raise JanitorError("This target already has a different review receipt", 409, "review_conflict")
            return next(r for r in get_run(run_id)["results"] if r["target_agent_id"] == target["agent_id"])
        run = _active_run(c, run_id)
        if _decode(run["configuration_json"], {}).get("template_id") != "task-labels":
            raise JanitorError("Only a task-label job can write label reviews", 409, "capability_denied")
        candidate = next((v for v in _decode(run["candidates_json"], []) if v.get("agent_id") == target["agent_id"] and v.get("session", v.get("target_session")) == target_session), None)
        if not candidate or candidate.get("state_id", candidate.get("observed_state_id")) != observed_state_id:
            raise JanitorError("Target is not part of this frozen run", 409, "stale_candidate")
        config = _config(c, run["agent_id"])
        if not _in_scope(_decode(config["scope_json"], {}), target):
            raise JanitorError("Target left the maintenance scope", 409, "scope_conflict")
        current = build_context_from_connection(c, target_session)
        if outcome == "changed" and not current.get("has_context"):
            raise JanitorError("There is not enough task context to change the label", 409, "insufficient_context")
        before = target.get("custom_status") or ""
        expected = candidate.get("current_status", candidate.get("expected_label", ""))
        queued = c.execute("SELECT 1 FROM queued_turns WHERE agent_id=? AND status IN ('queued','claimed') LIMIT 1", (target["agent_id"],)).fetchone()
        if queued or runtime_busy or target["backend"] != observed_target["backend"] or current["state_id"] != observed_state_id or current["state"] in AgentState.busy_states() or before != expected or any(current.get(k) != candidate.get(k) for k in ("fingerprint", "task_key", "change_key", "source_refs")):
            raise JanitorError("The target's work changed during this review", 409, "stale_candidate")
        ownership = c.execute("SELECT * FROM janitor_label_ownership WHERE target_agent_id=?", (target["agent_id"],)).fetchone()
        owned = bool(ownership and ownership["owner_agent_id"] == run["agent_id"] and ownership["label"] == before)
        if before and not owned:
            raise JanitorError("This label is maintained by another writer", 409, "label_protected")
        after = label if outcome == "changed" else before
        if after == before and outcome == "changed":
            outcome = "same_task"
        now = db.now_ms()
        if outcome == "changed":
            c.execute("UPDATE agents SET custom_status=? WHERE agent_id=?", (after, target["agent_id"]))
            c.execute("""INSERT INTO janitor_label_ownership(target_agent_id,owner_agent_id,run_id,label,task_signature,valid_until,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(target_agent_id) DO UPDATE SET owner_agent_id=excluded.owner_agent_id,
                run_id=excluded.run_id,label=excluded.label,revision=janitor_label_ownership.revision+1,
                task_signature=excluded.task_signature,valid_until=excluded.valid_until,updated_at=excluded.updated_at""",
                (target["agent_id"], run["agent_id"], run_id, after, _task_signature(c, target["agent_id"]), now + LABEL_MAX_AGE_MS, now))
            c.execute("UPDATE janitor_configs SET last_change_at=? WHERE agent_id=?", (now, run["agent_id"]))
        elif outcome == "same_task" and owned:
            c.execute("UPDATE janitor_label_ownership SET task_signature=?,valid_until=?,updated_at=? WHERE target_agent_id=?", (_task_signature(c, target["agent_id"]), now + LABEL_MAX_AGE_MS, now, target["agent_id"]))
        c.execute("INSERT INTO janitor_effects(run_id,target_agent_id,target_session,observed_state_id,outcome,before_label,after_label,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (run_id, target["agent_id"], target_session, observed_state_id, outcome, before, after, reason, now))
    return next(r for r in get_run(run_id)["results"] if r["target_agent_id"] == target["agent_id"])


def validate_configuration(template_id: str = "task-labels", scope=None, attachments=None, options=_UNSET) -> dict:
    """Validate new-identity input before creating any contact or agent row."""
    result = {"template_id": _template(template_id), "scope": _scope(scope if scope is not None else {}),
            "attachments": _attachment_values(attachments if attachments is not None else [{"trigger_id": template(template_id)["default_trigger_id"]}], "", template_id)}
    if options is not _UNSET:
        result["options"] = _option_patch(template_id, options)
    return result


def export_migration(session: str) -> dict:
    a = _agent(session)
    config = get(session)
    event_attachments = [v for v in config["attachments"] if v["trigger_id"] == "agent-work-completed"]
    progress = get_progress(event_attachments[0]["attachment_id"]) if len(event_attachments) == 1 else {}
    ownership = [{"target_session": r["session"], "label": r["label"],
                  "source_state_id": r["observed_state_id"] or 0, "at": r["updated_at"]}
                 for r in db.conn().execute("""SELECT o.*,a.session,e.observed_state_id
                    FROM janitor_label_ownership o JOIN agents a ON a.agent_id=o.target_agent_id
                    LEFT JOIN janitor_effects e ON e.run_id=o.run_id AND e.target_agent_id=o.target_agent_id
                    WHERE o.owner_agent_id=?""", (a["agent_id"],))]
    receipts = [{"target_session": r["target_session"], "before": r["before_label"],
                 "after": r["after_label"], "outcome": r["outcome"], "reason": r["reason"],
                 "source_state_id": r["observed_state_id"], "at": r["created_at"], "batch_id": r["run_id"]}
                for r in db.conn().execute("""SELECT e.* FROM janitor_effects e JOIN janitor_runs r ON r.run_id=e.run_id
                    WHERE r.agent_id=? ORDER BY e.created_at DESC LIMIT 1000""", (a["agent_id"],))]
    return {"expected_agent_id": a["agent_id"], "expected_revision": config["revision"],
            "generation": config["generation"], "progress": progress,
            "ownership": ownership, "receipts": receipts}


def _timestamp(value) -> int:
    try:
        if isinstance(value, str):
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return int(value if value > 100_000_000_000 else value * 1000)
    except (ValueError, OverflowError):
        pass
    raise JanitorError("Invalid imported receipt time")


def import_pilot(session: str, expected_revision: int, expected_agent_id: str,
                 import_id: str, progress: dict, ownership: list, receipts: list) -> dict:
    """Import a stopped pilot's state/history without writing any target caption."""
    payload = _json({"progress": progress, "ownership": ownership, "receipts": receipts})
    if not isinstance(import_id, str) or len(import_id) != 64 or any(ch not in "0123456789abcdef" for ch in import_id):
        raise JanitorError("Import ID must be the frozen pilot's SHA-256")
    if not isinstance(progress, dict) or not isinstance(ownership, list) or not isinstance(receipts, list) or len(ownership) > 1000 or len(receipts) > 1000:
        raise JanitorError("Invalid or oversized pilot state")
    a = _agent(session)
    if a["agent_id"] != expected_agent_id:
        raise JanitorError("Pilot agent identity does not match", 409, "agent_conflict")
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with _write() as c:
        previous = c.execute("SELECT * FROM janitor_pilot_imports WHERE import_id=?", (import_id,)).fetchone()
        if previous:
            if previous["agent_id"] != a["agent_id"] or previous["payload_sha256"] != digest:
                raise JanitorError("Import identity already refers to different data", 409, "import_conflict")
            return get(session)
        config = _config(c, a["agent_id"])
        _revision(config, expected_revision)
        if config["enabled"] or has_active_run(a["agent_id"]):
            raise JanitorError("Pause maintenance before importing the stopped pilot", 409, "janitor_busy")
        event_attachments = list(c.execute("SELECT * FROM janitor_attachments WHERE agent_id=? AND trigger_id='agent-work-completed' AND retired_at IS NULL", (a["agent_id"],)))
        if len(event_attachments) != 1:
            raise JanitorError("Pilot import requires exactly one event trigger")
        attachment = event_attachments[0]
        scope = _decode(config["scope_json"], {})
        normalized = json.loads(_json(progress))
        if isinstance(normalized.get("cursor", 0), bool) or not isinstance(normalized.get("cursor", 0), int) or normalized.get("cursor", 0) < 0:
            raise JanitorError("Invalid pilot event cursor")
        if not isinstance(normalized.get("seen", []), list) or len(normalized.get("seen", [])) > 256 or any(not isinstance(v, str) for v in normalized.get("seen", [])):
            raise JanitorError("Invalid pilot deduplication history")
        now = db.now_ms()
        pending = {}
        for source in (normalized.get("pending", {}), normalized.get("deferred", {})):
            if not isinstance(source, dict) or len(source) > 1000:
                raise JanitorError("Invalid pilot pending targets")
            for target_session, value in source.items():
                target = _agent(target_session)
                if not isinstance(value, dict) or not _in_scope(scope, target):
                    raise JanitorError("Imported target is outside the watched scope")
                pending[target_session] = {**value, "agent_id": target["agent_id"],
                    "first_at": value.get("first_at", int(float(value.get("first_pending_at", now / 1000)) * 1000)),
                    "eligible_at": value.get("eligible_at", now), "retry_count": value.get("retry_count", 0)}
        reviews = normalized.get("reviews", {})
        if not isinstance(reviews, dict) or len(reviews) > 1000 or any(not isinstance(v, dict) for v in reviews.values()):
            raise JanitorError("Invalid pilot review history")
        normalized.update(pending=pending, generation=config["generation"] + 1)
        normalized.pop("deferred", None)
        for key in ("active_run_id", "delivery", "delivery_accepted", "next_delivery_at"):
            normalized.pop(key, None)
        targets = {}
        for value in [*ownership, *receipts]:
            if not isinstance(value, dict):
                raise JanitorError("Invalid pilot receipt")
            target = _agent(value.get("target_session", ""))
            targets[target["session"]] = target
        for value in ownership:
            target = targets[value["target_session"]]
            if not _in_scope(scope, target):
                raise JanitorError("Imported ownership is outside the watched scope")
            if target["custom_status"] != value.get("label") or not value.get("label"):
                raise JanitorError("A pilot-owned label changed; refresh the import", 409, "label_protected")
            owner = c.execute("SELECT owner_agent_id FROM janitor_label_ownership WHERE target_agent_id=?", (target["agent_id"],)).fetchone()
            if owner and owner[0] != a["agent_id"]:
                raise JanitorError("Another Janitor owns an imported label", 409, "label_protected")
        for value in receipts:
            if value.get("outcome") not in TERMINAL_OUTCOMES or any(not isinstance(value.get(k, ""), str) or len(value.get(k, "")) > limit for k, limit in (("before", 100), ("after", 100), ("reason", 500))):
                raise JanitorError("Invalid imported effect receipt")
            if value.get("source_outcome", "") not in (
                    TERMINAL_OUTCOMES | {"", "busy", "protected", "unavailable"}):
                raise JanitorError("Invalid pilot source outcome")
            _timestamp(value.get("at"))
        generation = config["generation"] + 1
        c.execute("UPDATE janitor_configs SET generation=?,revision=revision+1,updated_at=? WHERE agent_id=?", (generation, now, a["agent_id"]))
        _save_progress(c, attachment["attachment_id"], generation, normalized)
        import_run_id = f"pilot-{import_id[:32]}"
        c.execute("INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,status,outcome,candidates_json,created_at,finished_at) VALUES (?,?,?,?,?,?,'completed','skipped','[]',?,?)", (import_run_id, a["agent_id"], a["session"], attachment["attachment_id"], generation, import_run_id, now, now))
        for index, value in enumerate(receipts):
            target = targets[value["target_session"]]
            receipt_run = f"{import_run_id}-{index}"
            timestamp = _timestamp(value["at"])
            c.execute("INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,status,outcome,candidates_json,created_at,finished_at) VALUES (?,?,?,?,?,?,'completed',?,'[]',?,?)", (receipt_run, a["agent_id"], a["session"], attachment["attachment_id"], generation, receipt_run, value["outcome"], timestamp, timestamp))
            c.execute("UPDATE janitor_runs SET configuration_json=? WHERE run_id=?", (_json({"source": "pilot", "import_id": import_id, "source_batch_id": value.get("batch_id", ""), "source_outcome": value.get("source_outcome", value["outcome"])}), receipt_run))
            c.execute("INSERT INTO janitor_effects(run_id,target_agent_id,target_session,observed_state_id,outcome,before_label,after_label,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (receipt_run, target["agent_id"], target["session"], int(value.get("source_state_id") or 0), value["outcome"], value.get("before", ""), value.get("after", ""), value.get("reason", ""), timestamp))
        for value in ownership:
            target = targets[value["target_session"]]
            current_state_id = c.execute("SELECT MAX(state_id) FROM state_log WHERE agent_id=?", (target["agent_id"],)).fetchone()[0]
            valid_until = now + LABEL_MAX_AGE_MS if current_state_id == value.get("source_state_id") else now
            c.execute("""INSERT INTO janitor_label_ownership(target_agent_id,owner_agent_id,run_id,label,task_signature,valid_until,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(target_agent_id) DO UPDATE SET
                owner_agent_id=excluded.owner_agent_id,run_id=excluded.run_id,label=excluded.label,
                revision=janitor_label_ownership.revision+1,task_signature=excluded.task_signature,
                valid_until=excluded.valid_until,updated_at=excluded.updated_at""", (target["agent_id"], a["agent_id"], import_run_id, value["label"], _task_signature(c, target["agent_id"]), valid_until, now))
        c.execute("INSERT INTO janitor_pilot_imports(import_id,agent_id,imported_at,payload_sha256) VALUES (?,?,?,?)", (import_id, a["agent_id"], now, digest))
    return get(session)


def _creation_result(c, row) -> dict:
    """Only a transactionally linked ID proves this request created an agent."""
    occupant = c.execute("SELECT agent_id,deleted_at FROM agents WHERE session=?", (row["session"],)).fetchone()
    if occupant and (not row["agent_id"] or occupant["agent_id"] != row["agent_id"]):
        raise JanitorError("The reserved session belongs to another creation", 409, "creation_conflict")
    if row["agent_id"] and (not occupant or occupant["deleted_at"] is not None):
        raise JanitorError("This creation's agent was removed", 409, "creation_conflict")
    return {"request_id": row["request_id"], "session": row["session"],
            "agent_id": row["agent_id"], "completed": row["completed_at"] is not None,
            "response": _decode(row["response_json"], None),
            "identity": {("name" if k == "persona" else k): v for k, v in _decode(row["identity_json"], {}).items()}}


def begin_creation(request_id: str, payload: dict) -> dict:
    """Reserve one fresh identity before HTTP invokes the existing lifecycle.

    Retried raw payloads compare independently of changing filesystem/catalog
    state. The normalized identity is frozen once, before any identity mutation.
    """
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160 or any(ord(ch) < 33 for ch in request_id):
        raise JanitorError("A stable creation request_id is required")
    if not isinstance(payload, dict):
        raise JanitorError("Creation payload must be an object")
    values = {key: value for key, value in payload.items() if key != "request_id"}
    allowed = {"name", "backend", "cwd", "model", "effort", "template_id", "scope", "attachments", "options", "execution"}
    if set(values) - allowed:
        raise JanitorError("Unsupported new Janitor configuration field")
    encoded = _json(values)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with _write() as c:
        existing = c.execute("SELECT * FROM janitor_creation_requests WHERE request_id=?", (request_id,)).fetchone()
        if existing:
            if existing["payload_sha256"] != digest:
                raise JanitorError("This creation request refers to different settings", 409, "creation_conflict")
            return _creation_result(c, existing)
        name = values.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise JanitorError("A short Janitor name is required")
        if values.get("backend") is not None and (not isinstance(values["backend"], str) or not backends.is_valid(values["backend"])):
            raise JanitorError("Choose an available agent backend")
        if values.get("cwd") is not None and not isinstance(values["cwd"], str):
            raise JanitorError("Workspace must be a path")
        backend = backends.normalize(values.get("backend") or "codex")
        if any(values.get(key) is not None and not isinstance(values[key], str) for key in ("model", "effort")):
            raise JanitorError("Model and effort must be text")
        model = (values.get("model") or "") if "model" in values else (
            "gpt-5.3-codex-spark" if backend == backends.CODEX else "")
        effort = (values.get("effort") or "") if "effort" in values else (
            "low" if model == "gpt-5.3-codex-spark" else "")
        if not isinstance(model, str) or not isinstance(effort, str):
            raise JanitorError("Model and effort must be text")
        model, effort = model.strip(), effort.strip().lower()
        execution = _execution(values.get("template_id", "task-labels"), values.get("execution"), backend)
        _model({"backend": backend}, model, effort, provider=execution.get("provider"))
        if backend == backends.AGY and effort:
            raise JanitorError("AGY model-specific effort compatibility is unknown")
        validate_configuration(**{k: values[k] for k in ("template_id", "scope", "attachments", "options") if k in values})
        from .agent_lifecycle import _existing_cwd
        identity = {"persona": name.strip(), "backend": backend, "cwd": _existing_cwd(values.get("cwd")),
                    "model": model, "effort": effort}
        slug = "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch in "._-")[:40] or "janitor"
        for _ in range(10):
            session = f"{slug}-{uuid.uuid4().hex[:12]}"
            if not agents.session_exists(session) and not c.execute("SELECT 1 FROM janitor_creation_requests WHERE session=?", (session,)).fetchone():
                break
        else:
            raise JanitorError("Could not reserve a fresh session", 409, "creation_conflict")
        c.execute("INSERT INTO janitor_creation_requests(request_id,payload_json,payload_sha256,identity_json,session,created_at) VALUES (?,?,?,?,?,?)", (request_id, encoded, digest, _json(identity), session, db.now_ms()))
        return _creation_result(c, c.execute("SELECT * FROM janitor_creation_requests WHERE request_id=?", (request_id,)).fetchone())


def create_reserved_agent(request_id: str, *, persona: str, voice_id: str, cwd: str,
                          session: str, backend: str, model: str = "", effort: str = "") -> str:
    """Called only by agents.create_agent for private registered creation.

    The ordinary identity INSERT and request provenance link commit together.
    A crash cannot leave an apparently matching but unowned session to adopt.
    """
    with _write() as c:
        row = c.execute("SELECT * FROM janitor_creation_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise JanitorError("No registered creation request", 409, "creation_conflict")
        identity = {"persona": persona, "backend": backend, "cwd": cwd, "model": model, "effort": effort}
        if session != row["session"] or voice_id or identity != _decode(row["identity_json"], {}):
            raise JanitorError("Identity does not match its registered creation", 409, "creation_conflict")
        result = _creation_result(c, row)
        if result["agent_id"]:
            return result["agent_id"]
        agent_id = agents.create_agent(persona=persona, voice_id="", cwd=cwd, session=session,
                                       backend=backend, model=model, effort=effort)
        c.execute("UPDATE agents SET is_janitor=1 WHERE agent_id=?", (agent_id,))
        c.execute("UPDATE janitor_creation_requests SET agent_id=? WHERE request_id=? AND agent_id IS NULL", (agent_id, request_id))
        return agent_id


def complete_creation(request_id: str, session: str) -> dict:
    """Save the response after paused configuration; retries never create again."""
    with _write() as c:
        row = c.execute("SELECT * FROM janitor_creation_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row or row["session"] != session:
            raise JanitorError("Creation request does not own this session", 409, "creation_conflict")
        result = _creation_result(c, row)
        if result["completed"]:
            return result["response"]
        if not result["agent_id"]:
            raise JanitorError("The registered identity has not been created", 409, "creation_incomplete")
        configured = get(session)
        if configured["agent_id"] != result["agent_id"]:
            raise JanitorError("Creation identity changed", 409, "creation_conflict")
        c.execute("UPDATE janitor_creation_requests SET response_json=?,completed_at=? WHERE request_id=?", (_json(configured), db.now_ms(), request_id))
        return configured
