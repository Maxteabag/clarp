"""Custom actions a physical-controller gesture can run.

Agents define named actions (`clarp-admin controller-action`) and map a
controller gesture to one; the phone only ever sends an action id to
`POST /controller-actions/run`. Each action type is a fixed, allow-listed
capability, so a paired device can trigger what an administrator defined but
never chooses a URL, a body or a command itself.

The registry is one JSON document in the settings table, written under a
transaction and stamped with a revision the phone uses to notice changes. The
audit trail lives under its own settings prefix, newest `AUDIT_KEEP` rows.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from . import settings_store
from .controller_events import CONTROLLER_BUTTONS, CONTROLLER_EVENTS
from .db import now_ms

REGISTRY_KEY = "controller_actions.registry"
AUDIT_PREFIX = "controller_actions.audit:"
REQUEST_PREFIX = "controller_actions.request:"
AUDIT_KEEP = 500
REQUEST_TTL_MS = 10 * 60_000
# A second press of the same action this soon after the first (a bouncing
# button, a double-tap) reports the first run instead of running again.
DEBOUNCE_MS = 1_500

ACTION_TYPES = ("http_post", "agent_prompt")
# A button's raw edges drive recording; only finished gestures can be mapped.
MAPPABLE_GESTURES = tuple(sorted(CONTROLLER_EVENTS - {"down", "up"}))
WILDCARD_DEVICE = "*"
# Loopback services on this machine are allowed without configuration; any
# other destination must be listed under [controller_actions] allowed_urls.
DEFAULT_ALLOWED_URLS = ("http://127.0.0.1", "http://localhost", "http://[::1]")

HTTP_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 8_192
MAX_TEXT = 4_000
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DEVICE = re.compile(r"^(\*|[A-Za-z0-9][A-Za-z0-9:._-]{0,127})$")


class ActionError(ValueError):
    """A controller action request is invalid; `status` is the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


HttpPost = Callable[[str, dict, float], "tuple[int, str]"]
SendPrompt = Callable[[str, str], dict]


# ---- validation ---------------------------------------------------------

def url_allowed(url: str, allowed_urls=()) -> bool:
    """Whether `url` falls under one of the allowed prefixes on a URL
    boundary: `https://a.com` admits `https://a.com/x`, not `https://a.com.evil`."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    for prefix in (*DEFAULT_ALLOWED_URLS, *allowed_urls):
        prefix = str(prefix).strip()
        if not prefix or not url.startswith(prefix):
            continue
        if prefix.endswith("/") or len(url) == len(prefix) or url[len(prefix)] in "/:?#":
            return True
    return False


def _text(data: dict, key: str, *, maximum: int, required: bool = True) -> str:
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ActionError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ActionError(f"{key} required")
    if len(value) > maximum:
        raise ActionError(f"{key} too long")
    return value


def _validate(data: dict, allowed_urls) -> dict:
    if not isinstance(data, dict):
        raise ActionError("action must be an object")
    action_id = str(data.get("id") or "").strip()
    if not _ID.match(action_id):
        raise ActionError("id must be lowercase letters, digits, - or _ (max 64)")
    kind = str(data.get("type") or "").strip()
    if kind not in ACTION_TYPES:
        raise ActionError(f"type must be one of {', '.join(ACTION_TYPES)}")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ActionError("enabled must be a boolean")
    action = {"id": action_id, "title": _text(data, "title", maximum=80),
              "type": kind, "enabled": enabled}
    if kind == "http_post":
        url = _text(data, "url", maximum=2_000)
        if not url_allowed(url, allowed_urls):
            raise ActionError("url is not allow-listed")
        body = data.get("body", {})
        if not isinstance(body, dict):
            raise ActionError("body must be a JSON object")
        if len(json.dumps(body)) > MAX_BODY_BYTES:
            raise ActionError("body too large")
        action.update(url=url, body=body)
    else:
        action.update(session=_text(data, "session", maximum=128),
                      text=_text(data, "text", maximum=MAX_TEXT))
    return action


def _validate_mapping(device: str, button: str, gesture: str) -> tuple[str, str, str]:
    device = str(device or WILDCARD_DEVICE).strip()
    if not _DEVICE.match(device):
        raise ActionError("device must be * or a controller id")
    if button not in CONTROLLER_BUTTONS:
        raise ActionError(f"button must be one of {', '.join(sorted(CONTROLLER_BUTTONS))}")
    if gesture not in MAPPABLE_GESTURES:
        raise ActionError(f"gesture must be one of {', '.join(MAPPABLE_GESTURES)}")
    return device, button, gesture


# ---- storage --------------------------------------------------------------

def _load() -> dict:
    raw = settings_store.get(REGISTRY_KEY)
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    return {"revision": int(state.get("revision") or 0),
            "actions": dict(state.get("actions") or {}),
            "mappings": list(state.get("mappings") or [])}


def _save(state: dict) -> None:
    state["revision"] = max(int(state["revision"]) + 1, now_ms())
    settings_store.set_text(REGISTRY_KEY, json.dumps(state, sort_keys=True))


def _audit(event: str, *, actor: str, action_id: str = "", **detail: Any) -> None:
    at = now_ms()
    row = {"at": at, "event": event, "actor": actor, "action_id": action_id, **detail}
    # Nanoseconds keep rows written in the same millisecond in order.
    settings_store.set_text(f"{AUDIT_PREFIX}{time.time_ns():020d}:{secrets.token_hex(2)}",
                            json.dumps(row, sort_keys=True))
    if settings_store.count_prefix(AUDIT_PREFIX) > AUDIT_KEEP:
        for key in settings_store.keys_with_prefix(AUDIT_PREFIX)[:-AUDIT_KEEP]:
            settings_store.delete(key)


def registry() -> dict:
    """Everything a client needs to render and sync the controller actions."""
    state = _load()
    return {
        "revision": state["revision"],
        "actions": [state["actions"][key] for key in sorted(state["actions"])],
        "mappings": sorted(state["mappings"], key=lambda m: (
            m["device"], m["button"], m["gesture"])),
        "types": list(ACTION_TYPES),
        "buttons": sorted(CONTROLLER_BUTTONS),
        "gestures": list(MAPPABLE_GESTURES),
    }


def get_action(action_id: str) -> dict | None:
    return _load()["actions"].get(str(action_id or "").strip())


def create_action(data: dict, *, actor: str, allowed_urls=()) -> dict:
    action = _validate(data, allowed_urls)
    with settings_store.transaction():
        state = _load()
        if action["id"] in state["actions"]:
            raise ActionError(f"action {action['id']} exists", 409)
        state["actions"][action["id"]] = action
        _save(state)
        _audit("create", actor=actor, action_id=action["id"], action=action)
    return action


def update_action(action_id: str, changes: dict, *, actor: str, allowed_urls=()) -> dict:
    if not isinstance(changes, dict):
        raise ActionError("changes must be an object")
    with settings_store.transaction():
        state = _load()
        current = state["actions"].get(action_id)
        if current is None:
            raise ActionError(f"no such action {action_id}", 404)
        merged = {**current, **{k: v for k, v in changes.items() if k != "id"}}
        if merged["type"] != current["type"]:
            for stale in ("url", "body", "session", "text"):
                if stale not in changes:
                    merged.pop(stale, None)
        action = _validate(merged, allowed_urls)
        state["actions"][action_id] = action
        _save(state)
        _audit("update", actor=actor, action_id=action_id, action=action)
    return action


def delete_action(action_id: str, *, actor: str) -> dict:
    with settings_store.transaction():
        state = _load()
        if action_id not in state["actions"]:
            raise ActionError(f"no such action {action_id}", 404)
        del state["actions"][action_id]
        kept = [m for m in state["mappings"] if m["action_id"] != action_id]
        removed = len(state["mappings"]) - len(kept)
        state["mappings"] = kept
        _save(state)
        _audit("delete", actor=actor, action_id=action_id, mappings_removed=removed)
    return {"deleted": True, "mappings_removed": removed}


def set_mapping(*, device: str, button: str, gesture: str, action_id: str | None,
                actor: str) -> dict | None:
    """Map one gesture to an action, or clear it when `action_id` is None."""
    device, button, gesture = _validate_mapping(device, button, gesture)
    action_id = str(action_id or "").strip() or None
    with settings_store.transaction():
        state = _load()
        if action_id is not None and action_id not in state["actions"]:
            raise ActionError(f"no such action {action_id}", 404)
        key = (device, button, gesture)
        state["mappings"] = [m for m in state["mappings"]
                             if (m["device"], m["button"], m["gesture"]) != key]
        mapping = None
        if action_id is not None:
            mapping = {"device": device, "button": button, "gesture": gesture,
                       "action_id": action_id}
            state["mappings"].append(mapping)
        _save(state)
        _audit("map" if mapping else "unmap", actor=actor, action_id=action_id or "",
               device=device, button=button, gesture=gesture)
    return mapping


def audit_log(*, limit: int = 50) -> list[dict]:
    """Newest first."""
    keys = settings_store.keys_with_prefix(AUDIT_PREFIX)[::-1][:max(0, int(limit))]
    values = settings_store.get_many(keys)
    return [json.loads(values[key]) for key in keys if key in values]


# ---- execution ------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would let an allow-listed URL send the body anywhere.
    def redirect_request(self, *args, **kwargs):
        return None


def default_http_post(url: str, body: dict, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "clarp-controller-action"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(2_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(2_000).decode("utf-8", "replace")


def _execute(action: dict, *, allowed_urls, http_post: HttpPost | None,
             send_prompt: SendPrompt | None) -> dict:
    if action["type"] == "http_post":
        if not url_allowed(action["url"], allowed_urls):
            return {"ok": False, "error": "url is no longer allow-listed"}
        status, text = (http_post or default_http_post)(action["url"], action["body"],
                                                       HTTP_TIMEOUT_S)
        result = {"ok": 200 <= int(status) < 300, "status": int(status),
                  "response": str(text or "")[:500]}
        if not result["ok"]:
            result["error"] = f"HTTP {status}"
        return result
    if send_prompt is None:
        return {"ok": False, "error": "agent prompts are unavailable"}
    reply = send_prompt(action["session"], action["text"]) or {}
    ok = bool(reply.get("ok", True))
    result = {"ok": ok}
    if not ok:
        result["error"] = str(reply.get("error") or "prompt was not accepted")
    return result


_RUN_LOCK = threading.Lock()
_IN_FLIGHT: dict[str, threading.Event] = {}
_LAST_RUN: dict[str, tuple[float, dict | None]] = {}


def _reset_runtime_state() -> None:
    """Forget in-process debounce state (tests)."""
    with _RUN_LOCK:
        _IN_FLIGHT.clear()
        _LAST_RUN.clear()


def run(action_id: str, *, actor: str, request_id: str, source: dict | None = None,
        allowed_urls=(), http_post: HttpPost | None = None,
        send_prompt: SendPrompt | None = None) -> dict:
    """Execute one action and audit it. `request_id` makes the call
    idempotent: a repeat, even one arriving while the first still runs,
    returns the first result with `duplicate: true`. A different request for
    the same action inside DEBOUNCE_MS returns `debounced: true` without
    running. A failing action is a result, not an exception; only a missing
    request id or an unknown or disabled action raises."""
    request_id = str(request_id or "").strip()[:128]
    if not request_id:
        raise ActionError("request_id required")
    action = get_action(action_id)
    if action is None:
        raise ActionError(f"no such action {action_id}", 404)
    if not action.get("enabled", True):
        raise ActionError(f"action {action_id} is disabled", 409)
    key = REQUEST_PREFIX + request_id
    while True:
        with _RUN_LOCK:
            settings_store.prune_prefix(REQUEST_PREFIX, updated_before=now_ms() - REQUEST_TTL_MS)
            previous = settings_store.get(key)
            if previous:
                return {**json.loads(previous), "duplicate": True}
            waiting = _IN_FLIGHT.get(request_id)
            if waiting is None:
                last = _LAST_RUN.get(action["id"])
                # Only a run in flight or a success holds the window; a
                # failure can be retried at once.
                if (last and (last[1] is None or last[1].get("ok"))
                        and (time.monotonic() - last[0]) * 1000 < DEBOUNCE_MS):
                    result = {**(last[1] or {"action_id": action["id"], "title": action["title"],
                                             "type": action["type"], "ok": True}),
                              "debounced": True, "duration_ms": 0}
                    _audit("debounced", actor=actor, action_id=action["id"], source=source or {})
                    return result
                done = threading.Event()
                _IN_FLIGHT[request_id] = done
                _LAST_RUN[action["id"]] = (time.monotonic(), None)
                break
        waiting.wait(HTTP_TIMEOUT_S * 4)
    started = time.monotonic()
    try:
        try:
            result = _execute(action, allowed_urls=allowed_urls, http_post=http_post,
                              send_prompt=send_prompt)
        except Exception as exc:  # noqa: BLE001 - every failure is reported to the caller
            result = {"ok": False, "error": str(exc)[:300] or type(exc).__name__}
        result = {"action_id": action["id"], "title": action["title"], "type": action["type"],
                  **result, "duration_ms": int((time.monotonic() - started) * 1000)}
        settings_store.set_text(key, json.dumps(result))
        with _RUN_LOCK:
            _LAST_RUN[action["id"]] = (_LAST_RUN.get(action["id"], (started, None))[0], result)
        _audit("run", actor=actor, action_id=action["id"], ok=result["ok"],
               error=result.get("error", ""), status=result.get("status"),
               duration_ms=result["duration_ms"], source=source or {})
        return result
    finally:
        with _RUN_LOCK:
            _IN_FLIGHT.pop(request_id, None)
        done.set()
