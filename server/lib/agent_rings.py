"""Agent rings: a Clarp agent calls its owner (docs/agent-rings.md).

An agent that needs the user now, by voice, asks the Host to ring the
owner's phone with a reason and the line it will open with. The phone shows
an incoming call (a time-sensitive push with Answer and Decline, plus an
``agent-ring`` event while the app is open). Answering starts an ordinary
group call with that agent on the floor (docs/group-calls.md) and plays the
opening line first, already voiced here in the agent's own voice.

Policy: the owner can turn rings off; an agent rings only as itself (its
turn proof), never on behalf of another; Janitors, archived and muted agents
cannot ring; one ring at a time; an agent waits ``AGENT_INTERVAL_MS`` between
rings and the Host allows ``HOURLY_LIMIT`` per hour. Every request, refused or
not, and every outcome lands in the audit trail.

Records live in ``settings_store`` like group calls, so a ringing call
survives an HTTP restart with no schema change.
"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import time
import uuid
from typing import Any

from . import events, settings_store
from .group_calls import CallError
from .log import log, log_exception

KEY = "agent_ring.records"
AUDIT_KEY = "agent_ring.audit"
ENABLED_KEY = "agent_ring.enabled"
VOIP_KEY = "agent_ring.voip_tokens"
RING_TIMEOUT_MS = 45 * 1000
AGENT_INTERVAL_MS = 60 * 1000
HOUR_MS = 60 * 60 * 1000
HOURLY_LIMIT = 10
KEEP_RECORDS = 50
KEEP_AUDIT = 200
REASON_CHARS = 200
OPENING_CHARS = 600
FINAL = frozenset({"answered", "declined", "missed", "cancelled"})

_lock = threading.RLock()
_context: dict[str, Any] = {"stream": None, "tts": None}


def now_ms() -> int:
    return int(time.time() * 1000)


def bind(ctx) -> None:
    """Remember where events go and how to voice the opening line."""
    _context["stream"] = getattr(ctx, "stream", None)
    _context["tts"] = getattr(ctx, "tts", None)


# ---- persistence ---------------------------------------------------------------

def _load() -> list[dict]:
    raw = settings_store.get(KEY)
    return json.loads(raw) if raw else []


def _save(records: list[dict]) -> None:
    settings_store.set_text(KEY, json.dumps(records[-KEEP_RECORDS:], ensure_ascii=False,
                                            separators=(",", ":")))


def _audit(entry: dict) -> None:
    raw = settings_store.get(AUDIT_KEY)
    trail = json.loads(raw) if raw else []
    trail.append({"at": now_ms(), **entry})
    settings_store.set_text(AUDIT_KEY, json.dumps(trail[-KEEP_AUDIT:], ensure_ascii=False,
                                                  separators=(",", ":")))
    log("agentRingAudit", " ".join(f"{k}={str(v)[:80]!r}" for k, v in entry.items()))


def audit() -> list[dict]:
    """The audit trail, newest first."""
    raw = settings_store.get(AUDIT_KEY)
    return list(reversed(json.loads(raw))) if raw else []


SHOW_REASON_KEY = "agent_ring.show_reason"


def settings() -> dict:
    return {"enabled": settings_store.get_bool(ENABLED_KEY, default=True),
            # The lock screen shows who is calling; why only when opted in.
            "show_reason": settings_store.get_bool(SHOW_REASON_KEY, default=False)}


def update_settings(data: dict) -> dict:
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise CallError(400, "enabled must be true or false")
        settings_store.set_bool(ENABLED_KEY, data["enabled"])
        _audit({"outcome": "settings", "enabled": data["enabled"]})
    if "show_reason" in data:
        if not isinstance(data["show_reason"], bool):
            raise CallError(400, "show_reason must be true or false")
        settings_store.set_bool(SHOW_REASON_KEY, data["show_reason"])
        _audit({"outcome": "settings", "show_reason": data["show_reason"]})
    return settings()


def public(ring: dict) -> dict:
    out = {k: v for k, v in ring.items() if not k.startswith("_")}
    out["server_now"] = now_ms()
    return out


# ---- reads -----------------------------------------------------------------

def _expire(records: list[dict]) -> list[dict]:
    """Mark overdue ringing records missed; returns the ones that changed."""
    now = now_ms()
    missed = []
    for ring in records:
        if ring["state"] == "ringing" and now >= ring["expires_at"]:
            ring["state"], ring["ended_at"] = "missed", now
            missed.append(ring)
    return missed


def _settle(records: list[dict]) -> list[dict]:
    missed = _expire(records)
    if missed:
        _save(records)
        for ring in missed:
            _audit({"outcome": "missed", "ring_id": ring["ring_id"], "session": ring["session"], "by": "timeout"})
            _announce(ring, "missed")
    return missed


def expire_due() -> list[str]:
    """Settle every overdue ring now; returns the ring ids that became missed."""
    with _lock:
        return [r["ring_id"] for r in _settle(_load())]


def get(ring_id: str) -> dict | None:
    with _lock:
        records = _load()
        _settle(records)
        ring = next((r for r in records if r["ring_id"] == ring_id), None)
        return public(ring) if ring else None


def current() -> dict | None:
    """The ring that is ringing now, or None."""
    with _lock:
        records = _load()
        _settle(records)
        ring = next((r for r in reversed(records) if r["state"] == "ringing"), None)
        return public(ring) if ring else None


# ---- ringing -----------------------------------------------------------------

def _caller(caller_agent_id: str, agent: str) -> dict:
    """The agent that calls. A turn-proven agent rings as itself only."""
    from . import agents as agents_db
    from .group_calls import resolve_name
    if caller_agent_id:
        me = agents_db.get_by_agent_id(caller_agent_id)
        if me is None:
            raise CallError(404, "unknown agent")
        if str(agent or "").strip():
            named = resolve_name(agent)
            if named["agent_id"] != me["agent_id"]:
                raise CallError(403, "an agent can only ring as itself")
        return me
    if not str(agent or "").strip():
        raise CallError(400, "agent required")
    named = resolve_name(agent)
    return agents_db.get_by_agent_id(named["agent_id"]) or named


def _admit(agent: dict, records: list[dict]) -> None:
    from . import agents as agents_db
    if not settings()["enabled"]:
        raise CallError(403, "agent calls are turned off")
    if (agent.get("archived_at") or agent.get("deleted_at")
            or not agents_db.interaction_capabilities(agent)["can_voice_target"]):
        raise CallError(403, "this agent cannot call")
    if agent.get("muted"):
        raise CallError(403, "this agent is muted")
    if any(r["state"] == "ringing" for r in records):
        raise CallError(409, "the user is already being called")
    now = now_ms()
    mine = [r for r in records if r["agent_id"] == agent["agent_id"]]
    if mine and now - mine[-1]["created_at"] < AGENT_INTERVAL_MS:
        wait = (AGENT_INTERVAL_MS - (now - mine[-1]["created_at"]) + 999) // 1000
        raise CallError(429, "wait before calling again", retry_after_s=int(wait))
    if sum(1 for r in records if now - r["created_at"] < HOUR_MS) >= HOURLY_LIMIT:
        raise CallError(429, "too many calls this hour")


def _voice(text: str, agent: dict) -> str:
    """The opening line in the agent's own voice, as an /audio URL, or ""."""
    tts = _context["tts"]
    if tts is None or not agent.get("voice_id"):
        return ""
    try:
        path = tts.synthesize(text, str(agent["voice_id"]), session=str(agent["session"]))
    except Exception as exc:  # noqa: BLE001 - the phone still rings without it
        log_exception("agentRingVoiceFail", exc, f"session={agent.get('session')}")
        return ""
    return f"/audio/{pathlib.Path(str(path)).name}"


def ring(*, caller_agent_id: str = "", agent: str = "", reason: str = "", opening_line: str = "") -> dict:
    """Ring the owner's phone on behalf of an agent."""
    reason = " ".join(str(reason or "").split())[:REASON_CHARS]
    opening = " ".join(str(opening_line or "").split())[:OPENING_CHARS]
    with _lock:
        records = _load()
        _settle(records)
        who = {"session": "", "persona": "", "agent_id": caller_agent_id or ""}
        try:
            caller = _caller(caller_agent_id, agent)
            who = {"session": str(caller["session"]), "persona": str(caller["persona"]),
                   "agent_id": str(caller["agent_id"])}
            if not reason:
                raise CallError(400, "reason required")
            if not opening:
                raise CallError(400, "opening_line required")
            _admit(caller, records)
        except CallError as exc:
            _audit({"outcome": "refused", "error": exc.error, **who, "reason": reason})
            raise
        now = now_ms()
        record = {
            "ring_id": "rng_" + uuid.uuid4().hex, **who, "reason": reason, "opening_line": opening,
            "opening_audio_url": _voice(opening, caller), "state": "ringing", "created_at": now,
            "expires_at": now + RING_TIMEOUT_MS, "ended_at": None, "answered_by": "", "call_id": "",
        }
        records.append(record)
        _save(records)
        _audit({"outcome": "ringing", "ring_id": record["ring_id"], **who, "reason": reason})
    delivery = _announce(record, "ring")
    _schedule_expiry(record)
    return {"ok": True, "ring": public(record), "delivery": delivery,
            "summary": f"Calling the user. The call rings for {RING_TIMEOUT_MS // 1000} seconds."}


def _find(records: list[dict], ring_id: str) -> dict:
    ring = next((r for r in records if r["ring_id"] == str(ring_id or "")), None)
    if ring is None:
        raise CallError(404, "unknown call")
    return ring


def _end(ring_id: str, state: str, *, by: str, allowed=None) -> dict:
    with _lock:
        records = _load()
        _settle(records)
        ring = _find(records, ring_id)
        if allowed is not None:
            allowed(ring)
        if ring["state"] == state:
            return {"ok": True, "ring": public(ring), "changed": False}
        if ring["state"] != "ringing":
            raise CallError(409, "the call is over", ring=public(ring))
        ring["state"], ring["ended_at"] = state, now_ms()
        _save(records)
        _audit({"outcome": state, "ring_id": ring["ring_id"], "session": ring["session"], "by": by})
    _announce(ring, state)
    return {"ok": True, "ring": public(ring), "changed": True}


def decline(principal: str, ring_id: str) -> dict:
    return _end(ring_id, "declined", by=principal)


def cancel(caller_agent_id: str, ring_id: str) -> dict:
    """The agent hangs up before the user answers."""
    def own(ring):
        if caller_agent_id and ring["agent_id"] != caller_agent_id:
            raise CallError(403, "an agent can only cancel its own call")
    with _lock:
        ring = _find(_load(), ring_id)
        if ring["state"] in FINAL:
            return {"ok": True, "ring": public(ring), "changed": False}
    return _end(ring_id, "cancelled", by=caller_agent_id or "owner", allowed=own)


def answer(principal: str, ring_id: str) -> dict:
    """Answer: start a group call with the agent and hand back its opening."""
    from . import group_calls
    with _lock:
        records = _load()
        _settle(records)
        ring = _find(records, ring_id)
        if ring["state"] == "answered":
            if ring["answered_by"] != principal:
                raise CallError(409, "the call was answered elsewhere")
            call = group_calls.get_call(ring["call_id"])
            return _answered(ring, call, changed=False)
        if ring["state"] != "ringing":
            raise CallError(409, "the call is over", ring=public(ring))
        started = group_calls.start(
            principal, [ring["session"]], request_id=ring["ring_id"], by=ring["session"],
            ring={"ring_id": ring["ring_id"], "reason": ring["reason"], "opening_line": ring["opening_line"]})
        call = started["call"]
        ring.update(state="answered", ended_at=now_ms(), answered_by=principal, call_id=call["call_id"])
        _save(records)
        _audit({"outcome": "answered", "ring_id": ring["ring_id"], "session": ring["session"], "by": principal,
                "call_id": call["call_id"]})
    _announce(ring, "answered")
    return _answered(ring, call, changed=True)


def _answered(ring: dict, call: dict | None, *, changed: bool) -> dict:
    return {"ok": True, "ring": public(ring), "call": call, "changed": changed,
            "opening": {"text": ring["opening_line"], "audio_url": ring["opening_audio_url"],
                        "session": ring["session"], "persona": ring["persona"]}}


# ---- telling everyone ---------------------------------------------------------------

_NOTICES = {
    "answered": "The user answered your call about: {reason}",
    "declined": "The user declined your call about: {reason}",
    "missed": "The user did not answer your call about: {reason}",
    "cancelled": "You cancelled your call about: {reason}",
}


def _announce(ring: dict, kind: str) -> dict:
    """Event, push and the agent's chat notice for one ring change. Never raises."""
    stream = _context["stream"]
    if stream is not None:
        try:
            wire = public(ring)
            events.broadcast(stream, events.agent_ring(
                **{key: wire.get(key) for key in events.FIELDS[events.SSEType.AGENT_RING]}))
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingEmitFail", exc, f"ring={ring['ring_id']}")
    delivery: dict = {}
    if kind == "ring":
        # A live ring rings through CallKit when a phone registered a VoIP
        # token; the alert is then a duplicate. Only the live ring is ever
        # VoIP: iOS requires every VoIP push to start a CallKit call.
        try:
            delivery["voip"] = _push_voip(ring) or {}
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingVoipFail", exc, f"ring={ring['ring_id']}")
    if not (delivery.get("voip") or {}).get("sent"):
        try:
            delivery.update(_push(ring, kind) or {})
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingPushFail", exc, f"ring={ring['ring_id']}")
    if kind in _NOTICES:
        _notice(ring, _NOTICES[kind].format(reason=ring["reason"]))
    return delivery


def _notice(ring: dict, text: str) -> None:
    from . import agents as agents_db
    from . import message_store
    try:
        backend_session_id = agents_db.live_backend_session(ring["agent_id"])
        if not backend_session_id:
            return
        row = message_store.record_call_notice(
            agent_id=ring["agent_id"], backend_session_id=backend_session_id,
            notice_id=f"{ring['ring_id']}:{ring['state']}", text="Call: " + text)
        stream = _context["stream"]
        if row is not None and stream is not None:
            events.broadcast(stream, events.transcript_updated(
                agent_id=ring["agent_id"], session=ring["session"], backend_session_id=backend_session_id))
    except Exception as exc:  # noqa: BLE001
        log_exception("agentRingNoticeFail", exc, f"ring={ring['ring_id']}")


# ---- VoIP (CallKit) -----------------------------------------------------------------

def _hex_token(token: str) -> bool:
    return 32 <= len(token) <= 200 and all(c in "0123456789abcdef" for c in token)


def register_voip_token(principal: str, token: str, *, environment: str = "production") -> dict:
    """The phone's PushKit VoIP token, one per principal. "" forgets it."""
    token = str(token or "").strip().lower()
    environment = "sandbox" if str(environment or "").lower() == "sandbox" else "production"
    if token and not _hex_token(token):
        raise CallError(400, "token must be hex")
    with _lock:
        raw = settings_store.get(VOIP_KEY)
        rows = {r["principal"]: r for r in (json.loads(raw) if raw else [])}
        if token:
            rows[principal] = {"principal": principal, "token": token, "environment": environment,
                               "updated_at": now_ms()}
        else:
            rows.pop(principal, None)
        settings_store.set_text(VOIP_KEY, json.dumps(list(rows.values()), separators=(",", ":")))
    log("agentRingVoipToken", f"principal={principal} registered={int(bool(token))} env={environment}")
    return {"ok": True, "registered": bool(token)}


def voip_tokens() -> list[dict]:
    raw = settings_store.get(VOIP_KEY)
    return json.loads(raw) if raw else []


def _push_voip(ring: dict) -> dict:
    from . import apns
    return apns.send_agent_ring_voip(ring, voip_tokens())


def _push(ring: dict, kind: str) -> dict:
    from . import apns
    return apns.send_agent_ring(ring, kind)


def _schedule_expiry(ring: dict) -> None:
    delay = max(0.0, (ring["expires_at"] - now_ms()) / 1000) + 0.5
    timer = threading.Timer(delay, _expire_quietly)
    timer.daemon = True
    timer.start()


def _expire_quietly() -> None:
    try:
        expire_due()
    except Exception as exc:  # noqa: BLE001
        log_exception("agentRingExpireFail", exc)


# ---- opening receipts (H1, contract 68, feature opening_receipts) ----------------
#
# The phone acknowledges an answered ring's opening as
# ``ring-opening-<ring_id>``. A receipt is a class of what the phone reported
# (attempted, started, first media-clock progress, terminal), never proof that
# anything was audible. Receipts are stored as voice-timeline rows; the ring
# record, the call and every event stay untouched, and a receipt that arrives
# after the call ended is kept as history only. Only rows with source 'server'
# are receipts: clients can post any voice-event name, so their rows never
# count as, or suppress, a receipt.

OPENING_ID = re.compile(r"ring-opening-(rng_[0-9a-f]{32})")
OPENING_CLASSES = {
    "queued": "attempted",
    "play-start": "started",
    "play-progress": "progress",
    "play-ok": "terminal_ok",
    "play-fail": "terminal_error",
    "held": "held",
}
_receipt_lock = threading.Lock()


def opening_ring_id(opening_id: str) -> str | None:
    """The ring id inside an exact opening id, else None."""
    match = OPENING_ID.fullmatch(opening_id) if isinstance(opening_id, str) else None
    return match.group(1) if match else None


def record_opening_receipt(principal: str, opening_id: str, *, status: str,
                           attempt: Any = 1, error: str | None = None) -> dict:
    """Record one opening receipt for the device that answered the ring.

    Raises CallError: 400 bad status/attempt, 404 unknown opening, 409 ring
    not answered, 403 not your ring. A repeat of the same (ring, attempt,
    class) is accepted without a second row.
    """
    from . import db, group_calls, voice_events
    ring_id = opening_ring_id(opening_id)
    if ring_id is None:
        raise CallError(400, "bad opening_id")
    receipt_class = OPENING_CLASSES.get(status)
    if receipt_class is None:
        raise CallError(400, "bad status")
    if attempt is None:
        attempt = 1
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise CallError(400, "bad opening_attempt")
    with _lock:
        ring = next((r for r in _load() if r["ring_id"] == ring_id), None)
    if ring is None:
        raise CallError(404, "unknown opening")
    if ring["state"] != "answered":
        raise CallError(409, "ring not answered")
    if ring.get("answered_by") != principal:
        raise CallError(403, "not your ring")
    call = group_calls.get_call(ring.get("call_id") or "") if ring.get("call_id") else None
    historical = call is None or call.get("state") != "live"
    event = "opening_" + receipt_class
    detail = {"opening_id": opening_id, "ring_id": ring_id, "call_id": ring.get("call_id") or "",
              "attempt": attempt, "status": status, "class": receipt_class,
              "historical": historical}
    if error:
        detail["error"] = str(error)[:500]
    with _receipt_lock:
        con = db.conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            seen = con.execute(
                "SELECT 1 FROM voice_events WHERE trace_id = ? AND event = ? AND source = 'server'"
                " AND json_extract(detail, '$.attempt') = ? LIMIT 1",
                (ring_id, event, attempt)).fetchone()
            if seen is None:
                voice_events.record(event, source="server", session=ring["session"],
                                    trace_id=ring_id, detail=detail)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return {"ok": True, "updated": seen is None,
            "opening": {"ring_id": ring_id, "call_id": ring.get("call_id") or "", "attempt": attempt,
                        "class": receipt_class, "historical": historical}}
