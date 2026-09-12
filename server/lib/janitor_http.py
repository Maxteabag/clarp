"""Authenticated Janitor management routes; no independent agent executor."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlsplit

from . import agents, db, janitors
from .agent_lifecycle import AgentLifecycleError, AgentLifecycleService
from .protocol import SSEType


def handles(path: str) -> bool:
    return (path in {"/janitors", "/janitor-triggers"}
            or path.startswith("/janitor-triggers/")
            or path.startswith("/janitors/")
            or path.startswith("/janitor-runs/"))


def _send(handler, status: int, value: dict) -> None:
    # An empty stored scope means all eligible agents. Emit the complete wire
    # shape expected by installed clients, without changing stored scopes or
    # the frozen configuration used by an in-flight maintenance run.
    def wire_janitor(row):
        if not isinstance(row, dict) or not isinstance(row.get("scope"), dict):
            return row
        return {**row, "scope": {"agent_ids": [], "exclude_agent_ids": [], **row["scope"]}}

    value = dict(value)
    if "janitor" in value:
        value["janitor"] = wire_janitor(value["janitor"])
    if isinstance(value.get("janitors"), list):
        value["janitors"] = [wire_janitor(row) for row in value["janitors"]]
    handler._send(status, json.dumps(value).encode(), "application/json")


def _changed(handler) -> None:
    from . import janitor_attention
    janitor_attention.reconcile()
    stream = getattr(handler.ctx, "stream", None)
    if stream is not None:
        stream.broadcast({"type": SSEType.AGENT_ROSTER, "kind": "janitor-changed"})


def _required(row, label="Janitor"):
    if row is None:
        raise janitors.JanitorError(f"{label} not found", status=404, code="not_found")
    return row


def _old_runs(session: str) -> list[dict]:
    row = agents.get_by_session(session)
    return janitors.active_runs(row["agent_id"]) if row else []


def _cancel_fenced_runs(handler, runs: list[dict]) -> bool:
    """Effects are already fenced transactionally, then stop exact runtime work."""
    from .turn_dispatch import cancel_janitor_run
    pending = False
    for run in runs:
        try:
            result = cancel_janitor_run(handler.ctx, run["run_id"])
            confirmed = result is True or (
                isinstance(result, dict) and result.get("cancelled") is True)
            pending = not confirmed or pending
        except Exception:
            # A disconnected runtime cannot undo the already committed pause.
            # Keep the control response honest about cancellation confirmation.
            pending = True
    return pending


def _revision(value) -> int:
    if isinstance(value, bool):
        raise ValueError("expected_revision must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("expected_revision is required") from None
    if number < 1 or (isinstance(value, float) and value != number):
        raise ValueError("expected_revision must be a positive integer")
    return number


def runtime_available(ctx) -> bool:
    runtime = getattr(ctx, "runtime_client", None)
    if runtime is None:
        return True
    try:
        return runtime.status().get("capabilities", {}).get("janitor_runs") is True
    except Exception:
        return False


def _require_runtime(ctx) -> None:
    if not runtime_available(ctx):
        raise janitors.JanitorError(
            "The Host agent runtime must finish updating before Janitors can run.",
            status=503, code="janitor_runtime_unavailable")


def handle(handler, method: str) -> None:
    path = urlsplit(handler.path).path
    query = parse_qs(urlsplit(handler.path).query)
    parts = [unquote(part) for part in path.strip("/").split("/")]
    try:
        if any("/" in part or not part for part in parts):
            raise ValueError("Invalid Janitor route")
        from . import janitor_design_policy as design
        if method == "GET" and path == "/janitors/design-policy":
            return _send(handler, 200, design.configuration())
        if method == "GET" and len(parts) == 3 and parts[0] == "janitors" and parts[2] == "effective-model-chain":
            return _send(handler, 200, design.effective_chain(parts[1]))
        if method == "GET" and len(parts) == 3 and parts[0] == "janitor-runs" and parts[2] == "receipt":
            return _send(handler, 200, design.inspect_receipt(parts[1], query.get("target", [""])[0]))
        if method == "POST" and path == "/janitors/design-policy":
            body = handler._read_json() or {}
            return _send(handler, 200, design.configure(body.get("configuration"), body.get("expected_revision")))
        if method == "POST" and path == "/janitors/quota-observation":
            return _send(handler, 200, design.observe_quota(handler._read_json()))
        if method == "GET":
            if path == "/janitors":
                from .orchestrator import provider_options
                return _send(handler, 200, {
                    "janitors": janitors.list_janitors(),
                    "templates": janitors.templates(),
                    "triggers": janitors.trigger_definitions(),
                    "runtime_available": runtime_available(handler.ctx),
                    "providers": provider_options(),
                })
            if path == "/janitor-triggers":
                return _send(handler, 200, {"triggers": janitors.trigger_definitions()})
            if path == "/janitor-triggers/preview":
                from .janitor_schedule import preview_next_runs
                expression = query.get("cron", [""])[0]
                zone = query.get("timezone", [""])[0]
                count = max(1, min(10, int(query.get("count", ["3"])[0])))
                return _send(handler, 200, {
                    "next_runs": preview_next_runs(expression, zone, db.now_ms(), count),
                    "timezone": zone,
                })
            if len(parts) == 2 and parts[0] == "janitors":
                return _send(handler, 200, {"janitor": _required(janitors.get(parts[1]))})
            if len(parts) == 3 and parts[0] == "janitors":
                _required(janitors.get(parts[1]))
                if parts[2] == "runs":
                    limit = max(1, min(100, int(query.get("limit", ["30"])[0])))
                    return _send(handler, 200, {"runs": janitors.list_runs(parts[1], limit=limit)})
                if parts[2] == "migration-state":
                    return _send(handler, 200, janitors.export_migration(parts[1]))
            if len(parts) == 3 and parts[0] == "janitor-runs" and parts[2] == "context":
                return _send(handler, 200, _required(janitors.run_context(parts[1]), "Run"))

        if method == "POST":
            data = handler._read_json()
            if data is None:
                raise ValueError("Expected a JSON object")
            if path == "/janitors":
                if data.get("template_id", "task-labels") == "task-labels":
                    _require_runtime(handler.ctx)
                fields = {key: data[key] for key in ("template_id", "scope", "attachments", "options", "execution") if key in data}
                session = str(data.get("session") or "").strip()
                new_identity = not session
                if new_identity:
                    payload = {key: value for key, value in data.items() if key != "request_id"}
                    intent = janitors.begin_creation(data.get("request_id"), payload)
                    if intent["completed"]:
                        return _send(handler, 200, {"janitor": intent["response"]})
                    session = intent["session"]
                    if not intent.get("agent_id"):
                        identity = dict(intent["identity"])
                        identity.update(session=session, synthesize_audio=False,
                                        creation_request_id=intent["request_id"])
                        try:
                            AgentLifecycleService(handler.ctx).create_janitor(identity)
                        except AgentLifecycleError as exc:
                            # A concurrent identical request may have completed
                            # identity creation while this request waited. Only
                            # durable provenance, never the name, permits reuse.
                            if exc.status != 409:
                                raise
                            current = janitors.begin_creation(intent["request_id"], payload)
                            if not current.get("agent_id"):
                                raise
                result = janitors.create(session, **fields)
                if new_identity:
                    result = janitors.complete_creation(intent["request_id"], session)
                _changed(handler)
                return _send(handler, 200, {"janitor": result})
            if len(parts) == 3 and parts[0] == "janitors":
                session, action = parts[1:]
                _required(janitors.get(session))
                revision = _revision(data.get("expected_revision"))
                old_runs = _old_runs(session)
                if action == "configure":
                    fields = {key: data[key] for key in
                              ("template_id", "scope", "attachments", "model", "effort", "execution", "backend", "options") if key in data}
                    result = janitors.configure(session, revision, **fields)
                elif action == "reset-defaults":
                    result = janitors.reset_defaults(session, revision)
                elif action == "enabled":
                    if not isinstance(data.get("enabled"), bool):
                        raise ValueError("enabled must be true or false")
                    if data["enabled"] and janitors.get(session)["template_id"] == "task-labels":
                        _require_runtime(handler.ctx)
                    result = janitors.set_enabled(session, revision, data["enabled"])
                elif action == "import-pilot":
                    result = janitors.import_pilot(session, expected_revision=revision,
                        **{key: value for key, value in data.items() if key != "expected_revision"})
                    _changed(handler)
                    return _send(handler, 200, result)
                elif action == "release":
                    fields = {key: data[key] for key in ("successor_session", "successor_revision") if key in data}
                    result = janitors.release(session, revision, **fields)
                    _changed(handler)
                    return _send(handler, 200, {"janitor": result, "cancellation_pending": False})
                elif action == "adopt-options":
                    result = janitors.adopt_options(session, revision, data.get("options"))
                    _changed(handler)
                    return _send(handler, 200, {"janitor": result})
                else:
                    return _send(handler, 404, {"error": "Janitor action not found"})
                pending = _cancel_fenced_runs(handler, old_runs) if not result["enabled"] else False
                result["cancellation_pending"] = pending or result.get("cancellation_pending", False)
                _changed(handler)
                return _send(handler, 200, {"janitor": result, "cancellation_pending": pending})
            if len(parts) == 3 and parts[0] == "janitor-runs" and parts[2] == "review":
                result = janitors.review(parts[1],
                    target_session=str(data.get("target_session") or ""),
                    observed_state_id=data.get("observed_state_id"),
                    outcome=str(data.get("outcome") or ""),
                    label=data.get("label"), reason=str(data.get("reason") or ""))
                _changed(handler)
                return _send(handler, 200, {"review": result})

        if method == "DELETE" and len(parts) == 2 and parts[0] == "janitors":
            session = parts[1]
            revision = _revision(query.get("expected_revision", [None])[0])
            old_runs = _old_runs(session)
            janitors.remove(session, revision)
            pending = _cancel_fenced_runs(handler, old_runs)
            _changed(handler)
            return _send(handler, 200, {"ok": True, "cancellation_pending": pending})
        return _send(handler, 404, {"error": "Janitor route not found"})
    except AgentLifecycleError as exc:
        return _send(handler, exc.status, exc.response())
    except janitors.JanitorError as exc:
        return _send(handler, exc.status, {"error": str(exc), "code": exc.code})
    except (TypeError, ValueError) as exc:
        return _send(handler, 400, {"error": str(exc)})
