"""Oracle handoffs: put the user through to an agent's hands-free session and back.

The binding wire contract is docs/oracle-handoff.md (revision 4). This module
holds the Host side of it: the per-principal records and their state machine,
the idempotent acknowledgements, the phase deadlines, and the registry of live
Oracle v2 calls a handoff can be offered to.

Nothing here reads what the user or a model said. A handoff starts only when
``connect`` is called: by ``POST /oracle/connect`` or by
``clarp-admin oracle connect``, which Clarp agents run with their own judgement.

The phone owns audio and the Host owns the record: every state change here is
either an acknowledged phase from the phone or a phase deadline, and the only
effects on a live Oracle call are the ones the contract names (hold, resume,
cues, one neutral data item).
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any

from . import events, settings_store
from .log import log, log_exception

TTL_MS = {"offered": 10_000, "preparing": 10_000, "activating": 30_000}
TERMINAL = frozenset({"failed", "broken", "cancelled", "ended"})
PHASES = frozenset({"preparing", "activating", "ready", "rolled_back", "rollback_failed", "failed", "abandon"})
ABANDON_REASONS = frozenset({"local_stop", "client_restarted", "local_intent_changed"})
ID_PATTERN = re.compile(r"hof_[0-9a-f]{32}")
KEY = "oracle.handoff."
KEEP_RECORDS = 20
# connect() waits for the handoff to settle: every phase deadline, plus slack.
CONNECT_WAIT_SECONDS = sum(TTL_MS.values()) / 1000 + 5
ADMINISTRATOR = "administrator"

_lock = threading.RLock()
_settled = threading.Condition(_lock)
# principal -> the live Oracle v2 Conversation of that device
_LIVE: dict[str, Any] = {}
_TIMERS: dict[str, threading.Timer] = {}
_BOOT = uuid.uuid4().hex
_context = {"stream": None, "herald": None}


class HandoffError(Exception):
    """A request the caller must hear about: HTTP status plus a short message."""

    def __init__(self, status: int, error: str, record: dict | None = None):
        super().__init__(error)
        self.status, self.error, self.record = status, error, record

    def body(self) -> dict:
        body: dict[str, Any] = {"ok": False, "error": self.error}
        if self.record is not None:
            body["handoff"] = self.record
        return body


def now_ms() -> int:
    return int(time.time() * 1000)


def bind(ctx) -> None:
    """Remember where events go; called once the server context exists."""
    _context["stream"] = getattr(ctx, "stream", None)
    _context["herald"] = getattr(ctx, "herald", None)


def host_id() -> str:
    from .server_identity import server_id
    return server_id()


# ---- persistence ---------------------------------------------------------

def _load(principal: str) -> dict:
    raw = settings_store.get(KEY + principal)
    if not raw:
        return {"generation": 0, "records": []}
    data = json.loads(raw)
    changed = False
    for record in data["records"]:
        if record["state"] in TTL_MS and record.get("_boot") != _BOOT:
            # Mid-transfer when the Host restarted: the phase deadline runs again.
            record["_boot"] = _BOOT
            record["_deadline"] = now_ms() + TTL_MS[record["state"]]
            _schedule(principal, record)
            changed = True
    if changed:
        _save(principal, data)
    return data


def _save(principal: str, data: dict) -> None:
    data["records"] = data["records"][-KEEP_RECORDS:]
    settings_store.set_text(KEY + principal, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def recover() -> None:
    """After a restart, restart the phase deadlines of mid-transfer records."""
    with _lock:
        for principal in _principals():
            _load(principal)


def _find(data: dict, handoff_id: str) -> dict | None:
    return next((r for r in data["records"] if r["handoff_id"] == handoff_id), None)


def _active(data: dict) -> dict | None:
    return next((r for r in reversed(data["records"])
                 if r["direction"] == "oracle_to_agent" and r["state"] == "active"), None)


# ---- projection ----------------------------------------------------------

def _oracle_call(record: dict) -> str:
    """The Oracle session this handoff concerns: running, closed or unknown."""
    call = _LIVE.get(record["principal"])
    if record["direction"] == "oracle_to_agent":
        if call is None or call.voice_session_id != record["oracle"]["voice_session_id"]:
            return "closed"
        return "running" if record.get("_oracle_audio", "live") == "live" and not call.held_for_handoff else "unknown"
    if call is None or getattr(call, "handoff_id", "") != record["handoff_id"]:
        return "closed"
    return "running"


def public(record: dict) -> dict:
    """The record as the wire carries it (docs/oracle-handoff.md)."""
    now = now_ms()
    out = {key: value for key, value in record.items() if not key.startswith("_")}
    if record["state"] not in TERMINAL:
        out["oracle_call"] = _oracle_call(record)
    deadline = record.get("_deadline")
    out["server_now"] = now
    out["ttl_ms"] = max(0, deadline - now) if deadline and record["state"] in TTL_MS else None
    out["expires_at"] = deadline if record["state"] in TTL_MS else None
    return out


def snapshot(principal: str) -> dict:
    with _lock:
        data = _load(principal)
        newest = data["records"][-1] if data["records"] else None
        active = _active(data)
        return {"principal": principal, "host_id": host_id(), "server_now": now_ms(),
                "handoff": public(newest) if newest else None,
                "active": public(active) if active else None}


# ---- events and effects --------------------------------------------------

def _emit(record: dict) -> None:
    """SSE (authoritative), plus the Oracle socket mirror for oracle_to_agent."""
    wire = public(record)
    stream = _context["stream"]
    if stream is not None:
        events.broadcast(stream, events.oracle_handoff(**{
            key: wire[key] for key in events.FIELDS[events.SSEType.ORACLE_HANDOFF] if key in wire}))
    else:
        log("oracleHandoffNoStream", f"handoff={record['handoff_id']} state={record['state']}")
    if record["direction"] == "oracle_to_agent":
        call = _LIVE.get(record["principal"])
        if call is not None and call.voice_session_id == record["oracle"]["voice_session_id"]:
            call.handoff_mirror(wire)


def _schedule(principal: str, record: dict) -> None:
    ident, revision = record["handoff_id"], record["revision"]
    old = _TIMERS.pop(ident, None)
    if old is not None:
        old.cancel()
    if record["state"] not in TTL_MS:
        return
    delay = max(0.0, (record["_deadline"] - now_ms()) / 1000)
    timer = threading.Timer(delay, _expire, (principal, ident, revision))
    timer.daemon = True
    _TIMERS[ident] = timer
    timer.start()


def _set(principal: str, data: dict, record: dict, state: str, reason: str = "") -> None:
    """One state change: revision, deadline, persistence, event, waiters."""
    record["state"], record["reason"] = state, reason
    record["revision"] += 1
    if state in TTL_MS:
        record["_deadline"] = now_ms() + TTL_MS[state]
        record["_boot"] = _BOOT
    else:
        record.pop("_deadline", None)
    if state in TERMINAL:
        record["oracle_call"] = _oracle_call(record)
    _save(principal, data)
    _schedule(principal, record)
    log("oracleHandoff", f"handoff={record['handoff_id']} direction={record['direction']} "
        f"state={state} reason={reason or '-'} revision={record['revision']}")
    _emit(record)
    _settled.notify_all()


def _call_for(record: dict):
    call = _LIVE.get(record["principal"])
    if record["direction"] == "oracle_to_agent":
        return call if call is not None and call.voice_session_id == record["oracle"]["voice_session_id"] else None
    return call if call is not None and getattr(call, "handoff_id", "") == record["handoff_id"] else None


def _resume(record: dict) -> None:
    """oracle_to_agent: the Host feeds the original call again (before the
    failure is recorded, so its oracle_call is derived from the resumed call)."""
    call = _call_for(record) if record["direction"] == "oracle_to_agent" else None
    if call is not None:
        call.handoff_resume()


def _after_failure(record: dict, *, note: bool) -> None:
    """oracle_to_agent only: the original call, if still open, hears the cue
    and, for a rollback, one neutral data item."""
    call = _call_for(record) if record["direction"] == "oracle_to_agent" else None
    if call is not None:
        call.handoff_failed(record["agent"]["persona"], record["reason"], note=note)


def _expire(principal: str, handoff_id: str, revision: int) -> None:
    try:
        with _lock:
            data = _load(principal)
            record = _find(data, handoff_id)
            if record is None or record["revision"] != revision or record["state"] not in TTL_MS:
                return
            state = record["state"]
            if state == "offered":
                _set(principal, data, record, "failed", "no_ack")
                _after_failure(record, note=False)
            elif state == "preparing":
                if record["direction"] == "oracle_to_agent":
                    record["_oracle_audio"] = "paused"   # unknown until rolled_back
                _resume(record)
                _set(principal, data, record, "failed", "prepare_timeout")
                _after_failure(record, note=False)
            else:
                _set(principal, data, record, "broken", "activation_timeout")
    except Exception as exc:
        log_exception("oracleHandoffExpire", exc, f"handoff={handoff_id}")


# ---- live calls ----------------------------------------------------------

def register(call) -> None:
    """A new Oracle v2 call for ``call.principal`` opened (docs: new-call rules)."""
    principal = call.principal
    with _lock:
        _LIVE[principal] = call
        data = _load(principal)
        for record in list(data["records"]):
            if record["state"] in TERMINAL:
                continue
            if record["direction"] == "oracle_to_agent":
                if record["state"] in ("offered", "preparing") and \
                        record["oracle"]["voice_session_id"] != call.voice_session_id:
                    _set(principal, data, record, "cancelled", "superseded")
                elif record["state"] == "active" and not call.handoff_id:
                    _set(principal, data, record, "ended", "client_returned")
            elif call.handoff_id != record["handoff_id"] and record["state"] in ("offered", "preparing"):
                _set(principal, data, record, "cancelled", "superseded")
                parent = _find(data, record["parent_handoff_id"])
                if parent is not None and parent["state"] == "active" and not call.handoff_id:
                    _set(principal, data, parent, "ended", "client_returned")


def unregister(call) -> None:
    with _lock:
        if _LIVE.get(call.principal) is call:
            del _LIVE[call.principal]


# ---- resolving who and which call ----------------------------------------

def _principal_for(caller: str, requested: str | None, *, returning: bool) -> str:
    if caller != ADMINISTRATOR:
        return caller
    if requested:
        return str(requested)
    with _lock:
        if returning:
            found = [key for key in _principals() if _active(_load(key))]
        else:
            found = list(_LIVE)
    if len(found) > 1:
        raise HandoffError(409, "several live calls")
    if not found:
        raise HandoffError(404, "no active handoff" if returning else "no live Oracle call")
    return found[0]


def _principals() -> list[str]:
    """Every principal that has handoff records."""
    return [key[len(KEY):] for key in settings_store.keys_with_prefix(KEY)]


def _new(data: dict, principal: str, direction: str, agent: dict, oracle: dict, parent: str = "") -> dict:
    data["generation"] += 1
    now = now_ms()
    record = {"handoff_id": "hof_" + uuid.uuid4().hex, "parent_handoff_id": parent, "host_id": host_id(),
              "principal": principal, "generation": data["generation"], "revision": 1,
              "direction": direction, "mode": "hands_free", "state": "offered", "reason": "",
              "oracle_call": "", "agent": agent, "oracle": oracle, "issued_at": now,
              "_deadline": now + TTL_MS["offered"], "_boot": _BOOT, "_acks": []}
    data["records"].append(record)
    _save(principal, data)
    _schedule(principal, record)
    log("oracleHandoff", f"handoff={record['handoff_id']} direction={direction} state=offered "
        f"agent={agent['session']} generation={record['generation']}")
    return record


def connect(caller: str, target: str, *, principal: str | None = None,
            wait: float | None = CONNECT_WAIT_SECONDS) -> dict:
    """Put the caller's call through to ``target`` (an agent) or back to "oracle".

    Returns the settled record's body, or raises HandoffError. ``wait`` bounds
    how long to wait for the phone (None: return right after the offer).
    """
    from . import oracle_contact
    wanted = str(target or "").strip()
    if not wanted:
        raise HandoffError(400, "agent required")
    returning = wanted.casefold() == "oracle"
    who = _principal_for(caller, principal, returning=returning)
    with _lock:
        data = _load(who)
        if any(r["state"] == "activating" for r in data["records"]):
            raise HandoffError(409, "handoff in progress")
        if returning:
            parent = _active(data)
            if parent is None:
                raise HandoffError(404, "no active handoff")
            if any(r["state"] in TTL_MS and r["parent_handoff_id"] == parent["handoff_id"] for r in data["records"]):
                raise HandoffError(409, "handoff in progress")
            record = _new(data, who, "agent_to_oracle", dict(parent["agent"]), dict(parent["oracle"]),
                          parent=parent["handoff_id"])
            _emit(record)
        else:
            try:
                agent = oracle_contact.resolve_visible(wanted)
            except ValueError as exc:
                raise HandoffError(404 if str(exc) == "unknown agent" else 409, str(exc)) from exc
            call = _LIVE.get(who)
            if call is None:
                raise HandoffError(404, "no live Oracle call")
            if not call.handoff_capable:
                raise HandoffError(409, "handoff_unsupported")
            for older in data["records"]:
                if older["state"] in ("offered", "preparing"):
                    _set(who, data, older, "cancelled", "superseded")
            record = _new(data, who, "oracle_to_agent",
                          {"session": str(agent["session"]), "agent_id": str(agent["agent_id"]),
                           "persona": str(agent["persona"])},
                          {"voice_session_id": call.voice_session_id,
                           "provider_session": call.provider_session, "thread_id": call.thread_id})
            call.handoff_offer(record["agent"]["persona"])
            _emit(record)
        ident = record["handoff_id"]
        deadline = time.monotonic() + (wait or 0)
        while wait:
            current = _find(_load(who), ident)
            if current is None or current["state"] in TERMINAL or (
                    current["state"] == "active" and not returning):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _settled.wait(remaining)
        current = _find(_load(who), ident) or record
        body = public(current)
    return _outcome(body, wait=bool(wait))


def _outcome(record: dict, *, wait: bool) -> dict:
    state, reason = record["state"], record["reason"]
    if state == "active" or (state == "ended" and reason == "returned") or (not wait and state == "offered"):
        return {"ok": True, "handoff_id": record["handoff_id"], "state": state, "handoff": record}
    if state == "broken":
        raise HandoffError(409, "the handoff broke and the call could not be resumed", record)
    if state in TERMINAL:
        raise HandoffError(409, f"the handoff did not happen ({state}: {reason})", record)
    raise HandoffError(504, f"the handoff is still {state}", record)


# ---- acknowledgements ----------------------------------------------------

def ack(caller: str, body: dict) -> dict:
    """Apply one phase acknowledgement; idempotent per (id, generation, phase)."""
    ident = str(body.get("handoff_id") or "")
    phase = str(body.get("phase") or "")
    generation = body.get("generation")
    reason = str(body.get("reason") or "")
    if not ID_PATTERN.fullmatch(ident) or phase not in PHASES or not isinstance(generation, int) \
            or isinstance(generation, bool):
        raise HandoffError(400, "handoff_id, generation and a known phase are required")
    with _lock:
        principal, data, record = _locate(caller, ident)
        if [generation, phase] in record["_acks"]:
            return {"handoff": public(record)}
        if generation != record["generation"] or record["generation"] < data["generation"]:
            raise HandoffError(409, "stale_generation", public(record))
        if not _apply(principal, data, record, phase, reason):
            raise HandoffError(409, "illegal_transition", public(record))
        record["_acks"].append([generation, phase])
        _save(principal, data)
        return {"handoff": public(record)}


def _locate(caller: str, ident: str):
    principals = [caller] if caller != ADMINISTRATOR else _principals()
    for principal in principals:
        data = _load(principal)
        record = _find(data, ident)
        if record is not None:
            return principal, data, record
    raise HandoffError(404, "unknown handoff")


def _apply(principal: str, data: dict, record: dict, phase: str, reason: str) -> bool:
    """The legal-transition table; False for anything else."""
    state, why, outward = record["state"], record["reason"], record["direction"] == "oracle_to_agent"
    parent = _find(data, record["parent_handoff_id"]) if not outward else None
    call = _call_for(record)

    def end_parent(parent_reason):
        if parent is not None and parent["state"] == "active":
            _set(principal, data, parent, "ended", parent_reason)

    if phase == "preparing" and state == "offered":
        if outward:
            record["_oracle_audio"] = "paused"
            if call is not None:
                call.handoff_hold()
        _set(principal, data, record, "preparing")
    elif phase == "failed" and state == "offered":
        intent_changed = not outward and reason == "local_intent_changed"
        _set(principal, data, record, "failed", "local_intent_changed" if intent_changed else "client_failed")
        if intent_changed:
            end_parent("local_intent_changed")
        _after_failure(record, note=False)
    elif phase == "activating" and state == "preparing":
        if outward:
            record["_oracle_audio"] = "released"
        _set(principal, data, record, "activating")
    elif phase == "rolled_back" and state in ("preparing", "activating"):
        if outward:
            record["_oracle_audio"] = "live"
            _resume(record)
        _set(principal, data, record, "failed", "rolled_back")
        _after_failure(record, note=True)
    elif phase == "ready" and (state == "activating" or (state == "broken" and why == "activation_timeout")):
        if outward:
            _set(principal, data, record, "active")
            _focus(record["agent"])
        else:
            _set(principal, data, record, "ended", "returned")
            end_parent("returned")
            if call is not None:
                call.handoff_returned(record["agent"]["persona"])
    elif phase == "rollback_failed" and state == "activating":
        _set(principal, data, record, "broken", "rollback_failed")
        end_parent("return_failed")
        _after_failure(record, note=False)
    elif phase == "rolled_back" and state == "failed" and why == "prepare_timeout":
        if outward:
            record["_oracle_audio"] = "live"
            _resume(record)
        record["oracle_call"] = _oracle_call(record)
        record["revision"] += 1
        _save(principal, data)
        _emit(record)
    elif phase == "rolled_back" and state == "broken" and why == "activation_timeout":
        if outward:
            record["_oracle_audio"] = "live"
            _resume(record)
        _set(principal, data, record, "failed", "rolled_back")
        _after_failure(record, note=True)
    elif phase == "abandon" and state in TTL_MS and reason in ABANDON_REASONS:
        _set(principal, data, record, "cancelled", reason)
        end_parent(reason)
    elif phase == "abandon" and state == "active" and reason in ABANDON_REASONS:
        _set(principal, data, record, "ended", reason)
    else:
        return False
    return True


def _focus(agent: dict) -> None:
    """Server focus follows the handoff, as POST /select does."""
    from . import agents as agents_db
    herald = _context["herald"]
    try:
        with agents_db.focus_guard():
            agents_db.set_focus(agent["agent_id"])
            if herald is not None:
                herald.set_focus(agent["session"])
    except Exception as exc:
        log_exception("oracleHandoffFocus", exc, f"session={agent['session']}")
        return
    stream = _context["stream"]
    if stream is not None:
        events.broadcast(stream, events.agent_focus(session=agent["session"], agent_id=agent["agent_id"]))
