"""HTTP routes for controller custom actions (docs/controller-actions.md).

Kept out of server.py: the Handler only asks `handles(path)` and passes
itself to `handle`. Defining actions is for local administrators (agents
through clarp-admin); a full device can map gestures; any paired device can
invoke an action by id.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from urllib.parse import unquote

from . import config as app_config, controller_actions, events
from .log import log

PREFIX = "/controller-actions"
_INVOKE_SUFFIX = "/invoke"


def invoke_id(path: str) -> str | None:
    """The action id in POST /controller-actions/<id>/invoke, else None."""
    if not (path.startswith(PREFIX + "/") and path.endswith(_INVOKE_SUFFIX)):
        return None
    action_id = unquote(path[len(PREFIX) + 1:-len(_INVOKE_SUFFIX)])
    return action_id if action_id and "/" not in action_id else None


def handles(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def limited_device_allowed(path: str, method: str) -> bool:
    """A limited device (the Watch) may read and invoke, nothing else."""
    return method == "GET" or (method == "POST" and invoke_id(path) is not None)


def _actor(handler) -> str:
    caller = handler._turn_caller()
    if caller:
        return f"agent:{caller}"
    principal = str(getattr(handler, "_request_principal", "") or "")
    if principal and principal != "administrator":
        return f"device:{principal}"
    return "administrator"


def _admin(handler) -> bool:
    if getattr(handler, "_request_principal", "") == "administrator":
        return True
    return (not (getattr(handler.ctx, "auth_token", "") or "")
            and bool(getattr(handler, "_request_is_local", False)))


def _send_prompt(handler):
    """Deliver an agent_prompt action through this Host's own /send, the
    same path `clarp-admin prompt` takes."""
    def send(session: str, text: str) -> dict:
        port = int(handler.server.server_address[1])
        payload = {"session": session, "text": text, "force_session": True,
                   "synthesize_audio": False, "hands_free": False, "origin": "automation",
                   "client_msg_id": f"controller-action-{secrets.token_hex(8)}"}
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/send", data=json.dumps(payload).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        token = getattr(handler.ctx, "auth_token", "") or ""
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
            return {"ok": True}
        except urllib.error.HTTPError as exc:
            return {"ok": False,
                    "error": f"HTTP {exc.code}: {exc.read(300).decode('utf-8', 'replace')}"}
    return send


def _change(handler, apply, *, status: int = 200, admin: bool = False):
    if admin and not _admin(handler):
        return handler._json_error(403, "only a local administrator can define controller actions")
    try:
        result = apply()
    except controller_actions.ActionError as exc:
        return handler._json_error(exc.status, str(exc))
    events.broadcast_ephemeral(handler.ctx.stream, events.controller_actions_updated(
        revision=controller_actions.registry()["revision"]))
    return handler._json(status, {"ok": True, **result})


def _invoke(handler, action_id: str, data: dict):
    """The caller names the action and its own request id; nothing in the
    body can change what the action does."""
    if not (getattr(handler, "_request_auth_validated", False) or _admin(handler)):
        return handler._json_error(401, "paired device required")
    source = {key: str(data.get(key) or "")[:128]
              for key in ("device", "button", "gesture", "source_kind") if data.get(key)}
    try:
        result = controller_actions.run(
            action_id, actor=_actor(handler), source=source,
            request_id=str(data.get("request_id") or ""),
            allowed_urls=app_config.load().controller_action_urls,
            send_prompt=_send_prompt(handler))
    except controller_actions.ActionError as exc:
        return handler._json_error(exc.status, str(exc))
    log("controllerActionInvoke", f"{action_id} ok={result['ok']}")
    return handler._json_ok(result)


def handle(handler, method: str):
    path = handler.path.split("?", 1)[0]
    if method == "GET" and path == PREFIX:
        return handler._json_ok(controller_actions.registry())
    if method == "GET" and path == PREFIX + "/audit":
        try:
            limit = int(handler._query().get("limit", ["50"])[0] or 50)
        except ValueError:
            return handler._json_error(400, "limit must be an integer")
        return handler._json_ok({"entries": controller_actions.audit_log(limit=min(limit, 500))})
    if method != "POST":
        return handler._json_error(404, "not found")
    data = handler._read_json()
    if not isinstance(data, dict):
        return handler._json_error(400, "bad json")
    action_id = invoke_id(path)
    if action_id is not None:
        return _invoke(handler, action_id, data)
    allowed = app_config.load().controller_action_urls
    actor = _actor(handler)
    if path == PREFIX:
        return _change(handler, lambda: {"action": controller_actions.create_action(
            data, actor=actor, allowed_urls=allowed)}, status=201, admin=True)
    if path == PREFIX + "/update":
        return _change(handler, lambda: {"action": controller_actions.update_action(
            str(data.get("id") or "").strip(), data, actor=actor, allowed_urls=allowed)},
            admin=True)
    if path == PREFIX + "/delete":
        return _change(handler, lambda: controller_actions.delete_action(
            str(data.get("id") or "").strip(), actor=actor), admin=True)
    if path == PREFIX + "/mappings":
        return _change(handler, lambda: {"mapping": controller_actions.set_mapping(
            device=str(data.get("device") or "*"), button=str(data.get("button") or ""),
            gesture=str(data.get("gesture") or ""), action_id=data.get("action_id"),
            actor=actor)})
    return handler._json_error(404, "not found")
