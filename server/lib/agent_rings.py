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
import secrets
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
DEVICE_KEY = "agent_ring.device_prefs"   # phones that switched agent rings off for themselves
RING_TIMEOUT_MS = 45 * 1000
AGENT_INTERVAL_MS = 60 * 1000
HOUR_MS = 60 * 60 * 1000
HOURLY_LIMIT = 10
KEEP_RECORDS = 50
KEEP_AUDIT = 200
REASON_CHARS = 200
OPENING_CHARS = 600
LIVE_JOIN_MS = 60 * 1000          # a Live answer's one-use join ticket lives this long
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
    for record in records:
        # A Live ring's frozen context is needed only until its Live call can no
        # longer start or run; the digest stays for the event's idempotency.
        if "_live_snapshot" in record and (record["state"] in ("declined", "missed", "cancelled")
                                           or record.get("_live_phase") in ("retired", "classic")):
            del record["_live_snapshot"]
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


def current(principal: str | None = None) -> dict | None:
    """The ring that is ringing now, or None. With ``principal``, only a ring that client may see (H6)."""
    if principal is not None and not receives_rings(principal):
        return None                     # this phone switched agent rings off for itself
    capable = live_capable(principal) if principal is not None else True
    with _lock:
        records = _load()
        _settle(records)
        ring = next((r for r in reversed(records) if r["state"] == "ringing" and _visible(r, capable)), None)
        return public(ring) if ring else None


# ---- who may see a Live ring (H6) -----------------------------------------------------
#
# A Live ring reaches only a client whose device registration advertises agent_live_v1:
# its SSE event, its GET projection and its VoIP push. An older phone (2818) would report
# any agent-ring event to CallKit as an incoming call, so it must never see a Live ring at
# all, and a Live ring never falls back to the global alert push. Classic rings are unchanged.

def live_capable(principal: str | None) -> bool:
    """The principal's VoIP registration says it can run a Live call.

    Read fresh at every use, never cached. An absent registration or one without
    agent_live_v1 is not capable. LIMIT (explicit, not a no-ring guarantee): the
    registration is the device's own last word. A phone that registered
    agent_live_v1 and was then downgraded to an app that cannot run Live stays
    "capable" here until it registers again, and keeps receiving Live rings,
    events and VoIP. Only a fresh registration without agent_live_v1, or a
    forgotten token, removes it. The answer itself is still checked separately
    (WIRE-v2 decision 4)."""
    return bool(principal) and any(row.get("principal") == principal for row in _live_tokens())


def _visible(ring: dict, capable: bool) -> bool:
    from .agent_ring_live import MODE_LIVE
    return capable or ring.get("conversation_mode") != MODE_LIVE


def event_visible(event: dict, principal: str | None) -> bool:
    """Whether one SSE event, or one public ring record (no "type"), may go to this client."""
    from .agent_ring_live import MODE_LIVE
    from .protocol import SSEType
    if event.get("type", SSEType.AGENT_RING) != SSEType.AGENT_RING:
        return True
    if principal and event.get("answered_by") == principal:
        return True                     # the ring this phone answered stays its own: rings off or capability dropped
    if principal is not None and not receives_rings(principal):
        return False                    # this phone switched agent rings off for itself
    if event.get("conversation_mode") != MODE_LIVE:
        return True
    return live_capable(principal)


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


def ring(*, caller_agent_id: str = "", agent: str = "", reason: str = "", opening_line: str = "",
         conversation_mode: Any = None, event_context: Any = None) -> dict:
    """Ring the owner's phone on behalf of an agent.

    ``conversation_mode: agent_live_v1`` (feature agent_ring_live) asks for a
    Live ring: its ``event_context`` is validated and frozen with the ring in a
    snapshot, and a ring that cannot be built is refused, never downgraded. A
    producer retry of the same event gets the same ring back."""
    from . import agent_ring_live as live
    reason = " ".join(str(reason or "").split())[:REASON_CHARS]
    opening = " ".join(str(opening_line or "").split())[:OPENING_CHARS]
    snapshot = replay = None
    with _lock:
        records = _load()
        _settle(records)
        who = {"session": "", "persona": "", "agent_id": caller_agent_id or ""}
        try:
            mode = live.mode_of(conversation_mode)
            caller = _caller(caller_agent_id, agent)
            who = {"session": str(caller["session"]), "persona": str(caller["persona"]),
                   "agent_id": str(caller["agent_id"])}
            if not reason:
                raise CallError(400, "reason required")
            if not opening:
                raise CallError(400, "opening_line required")
            if mode == live.MODE_LIVE:
                # Live compares whitespace-normalised texts and never accepts a cut line.
                if len(" ".join(str(opening_line or "").split())) > OPENING_CHARS:
                    raise CallError(400, live.CONTEXT_INVALID)
                context = live.validate_context(event_context, opening)
                event_key = f"{context['source']}:{context['event']['id']}"
                replay = next((r for r in reversed(records) if r["agent_id"] == who["agent_id"]
                               and r.get("_live_event_key") == event_key), None)
                if replay is not None and replay["_live_event_digest"] != live.digest(context):
                    raise CallError(409, live.CONTEXT_CONFLICT)
            elif event_context is not None:
                raise CallError(400, live.CONTEXT_INVALID)
            if replay is None:
                _admit(caller, records)
                if mode == live.MODE_LIVE:
                    if not _eligible_live_tokens():   # capable AND receiving rings
                        raise CallError(409, live.CLIENT_REQUIRED)
                    snapshot = live.build_snapshot(caller, context)
        except CallError as exc:
            _audit({"outcome": "refused", "error": exc.error, **who, "reason": reason})
            raise
        if replay is not None:
            return {"ok": True, "ring": public(replay), "changed": False,
                    "summary": "This event already rang the user; no second call."}
        now = now_ms()
        record = {
            "ring_id": "rng_" + uuid.uuid4().hex, **who, "reason": reason, "opening_line": opening,
            # A Live ring's opening is spoken by the Live model; nothing is pre-voiced for it.
            "opening_audio_url": "" if snapshot else _voice(opening, caller), "state": "ringing",
            "created_at": now, "expires_at": now + RING_TIMEOUT_MS, "ended_at": None, "answered_by": "",
            "call_id": "",
        }
        if snapshot:
            record.update(conversation_mode=live.MODE_LIVE, _live_snapshot=snapshot,
                          _live_context_digest=live.digest(snapshot), _live_event_key=event_key,
                          _live_event_digest=live.digest(context))
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


def answer(principal: str, ring_id: str, conversation_mode: Any = None, *,
           request_id: Any = None, client_nonce: Any = None) -> dict:
    """Answer: start a group call with the agent and hand back its opening.

    A Live ring is answered only by a phone that asks for ``agent_live_v1``; it
    starts no group call and hands back the Live call grant instead. Neither
    kind of ring changes kind on answer (WIRE-v2 decision 5)."""
    from . import agent_ring_live as live
    from . import group_calls
    mode = live.mode_of(conversation_mode, default=None)
    with _lock:
        records = _load()
        _settle(records)
        ring = _find(records, ring_id)
        if ring.get("conversation_mode") == live.MODE_LIVE:
            fresh = _answer_live(records, ring, principal, mode, request_id, client_nonce)
            if not fresh:
                # The owner's repeat gets the call's actual state, never a grant it can no longer use.
                phase = ring.get("_live_phase")
                if phase == "classic":
                    return {**_answered(ring, group_calls.get_call(ring["call_id"]), changed=False),
                            "conversation_mode": live.MODE_CLASSIC}
                if phase not in live.LIVE_PHASES:
                    raise CallError(409, live.CALL_STALE)
                # A live call answers its same grant; live.join_consumed says whether its ticket is already used.
        elif mode == live.MODE_LIVE:
            raise CallError(409, live.MODE_CONFLICT)
        else:
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
    if ring.get("conversation_mode") == live.MODE_LIVE:
        if fresh:
            _announce(ring, "answered")
        return _answered_live(ring, changed=fresh)
    _announce(ring, "answered")
    return _answered(ring, call, changed=True)


def _answer_live(records: list[dict], ring: dict, principal: str, mode: str | None,
                 request_id: Any, client_nonce: Any) -> bool:
    """The Live branch of answer (under _lock): one grant per ring, first owner
    wins. True when this request answered it, False for the owner's repeat."""
    from . import agent_ring_live as live
    if mode != live.MODE_LIVE:
        raise CallError(409, live.CLIENT_REQUIRED)
    # The grant binds a paired, full-scope device: its principal is the device identity.
    if not principal.startswith("device_") or not live.device_ok(principal):
        raise CallError(403, live.AUTH)
    if ring["state"] == "answered":
        if ring["answered_by"] != principal:
            raise CallError(409, live.OWNER_CONFLICT)
        _live_intent(request_id, client_nonce)
        # The owner's repeat binds its exact original intent: the same request with the same nonce.
        if request_id != ring.get("_live_request_id"):
            raise CallError(409, live.CALL_STALE)   # another request mints no new attempt (fef6043f)
        if client_nonce != ring.get("_live_client_nonce"):
            raise CallError(400, live.REQUEST_INVALID)
        return False
    if ring["state"] != "ringing":
        raise CallError(409, live.CALL_STALE)
    _live_intent(request_id, client_nonce)
    now = now_ms()
    ring.update(state="answered", ended_at=now, answered_by=principal,
                _live_call_id="alv_" + uuid.uuid4().hex, _live_call_epoch=1, _live_phase="answered_waiting_join",
                _live_attempt_id="att_" + uuid.uuid4().hex, _live_owner_nonce=secrets.token_hex(32),
                _live_join_ticket=secrets.token_hex(32), _live_join_expires_at=now + LIVE_JOIN_MS,
                _live_request_id=request_id, _live_client_nonce=client_nonce)
    _save(records)
    _audit({"outcome": "answered", "ring_id": ring["ring_id"], "session": ring["session"], "by": principal,
            "mode": live.MODE_LIVE, "live_call_id": ring["_live_call_id"]})
    return True


def _live_intent(request_id: Any, client_nonce: Any) -> None:
    """A Live answer names its intent exactly: a request id and a client nonce, both required, each 1 to 128
    UTF-8 bytes, kept as sent and never cut."""
    from . import agent_ring_live as live
    if not live._request_id_ok(request_id) or not live._request_id_ok(client_nonce):
        raise CallError(400, live.REQUEST_INVALID)


def _answered_live(ring: dict, *, changed: bool) -> dict:
    """The Live answer of WIRE-v2: no group call, no opening; the call grant."""
    from . import agent_ring_live as live
    from .group_calls import _host_id
    return {"ok": True, "ring": public(ring), "changed": changed, "conversation_mode": live.MODE_LIVE,
            "live": {"protocol": live.PROTOCOL, "host_id": _host_id(), "ring_id": ring["ring_id"],
                     "agent_id": ring["agent_id"], "call_id": ring["_live_call_id"],
                     "call_epoch": ring["_live_call_epoch"], "attempt_id": ring["_live_attempt_id"],
                     "context_digest": ring["_live_context_digest"], "provider": live.PROVIDER,
                     "model": live.MODEL, "voice": live.VOICE, "owner_nonce": ring["_live_owner_nonce"],
                     "join_ticket": ring["_live_join_ticket"], "expires_at": ring["_live_join_expires_at"],
                     "join_consumed": bool(ring.get("_live_join_consumed")), "opening_owner": "live"}}


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
                **{key: wire[key] for key in events.FIELDS[events.SSEType.AGENT_RING] if key in wire}))
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
    from .agent_ring_live import MODE_LIVE
    if ring.get("conversation_mode") == MODE_LIVE:
        # H6: the global alert reaches every phone, also those that cannot run Live. A Live ring is
        # announced by VoIP to capable phones only, for its first ring and for every later state
        # (answered, declined, missed, cancelled): no global alert, ever.
        pass
    elif not (delivery.get("voip") or {}).get("sent"):
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


def register_voip_token(principal: str, token: str, *, environment: str = "production",
                        supported_conversation_modes: Any = None) -> dict:
    """The phone's PushKit VoIP token, one per principal. "" forgets it.

    ``supported_conversation_modes`` (absent: classic only) says which ring
    modes this phone can run; it is a delivery hint, never a credential, and an
    answer is still checked on its own (WIRE-v2 decision 4)."""
    from . import agent_ring_live as live
    token = str(token or "").strip().lower()
    environment = "sandbox" if str(environment or "").lower() == "sandbox" else "production"
    if token and not _hex_token(token):
        raise CallError(400, "token must be hex")
    modes = supported_conversation_modes
    if modes is not None and (not isinstance(modes, list) or len(modes) > len(live.MODES)
                              or any(not isinstance(m, str) or m not in live.MODES for m in modes)):
        raise CallError(400, live.MODE_INVALID)
    with _lock:
        raw = settings_store.get(VOIP_KEY)
        rows = {r["principal"]: r for r in (json.loads(raw) if raw else [])}
        if token:
            rows[principal] = {"principal": principal, "token": token, "environment": environment,
                               "updated_at": now_ms()}
            if modes:
                rows[principal]["supported_conversation_modes"] = list(dict.fromkeys(modes))
        else:
            rows.pop(principal, None)
        settings_store.set_text(VOIP_KEY, json.dumps(list(rows.values()), separators=(",", ":")))
    log("agentRingVoipToken", f"principal={principal} registered={int(bool(token))} env={environment}")
    return {"ok": True, "registered": bool(token)}


def voip_tokens() -> list[dict]:
    raw = settings_store.get(VOIP_KEY)
    return json.loads(raw) if raw else []


def _live_tokens() -> list[dict]:
    """The VoIP registrations of phones that said they can run a Live call."""
    from .agent_ring_live import MODE_LIVE
    return [row for row in voip_tokens() if MODE_LIVE in (row.get("supported_conversation_modes") or ())]


def _push_voip(ring: dict) -> dict:
    from . import apns
    tokens = _eligible_live_tokens() if ring.get("conversation_mode") else _receiving(voip_tokens())
    return apns.send_agent_ring_voip(ring, tokens)


def _receiving(tokens: list[dict]) -> list[dict]:
    """VoIP registrations less the phones that switched agent rings off."""
    off = {row["principal"] for row in device_prefs()}
    return [row for row in tokens if row.get("principal") not in off] if off else tokens


def _eligible_live_tokens() -> list[dict]:
    """Phones that can run a Live call and still receive rings: the only ones a Live ring may admit for."""
    return _receiving(_live_tokens())


def _push(ring: dict, kind: str) -> dict:
    from . import apns
    off = {row["principal"] for row in device_prefs()}
    if off:
        return apns.send_agent_ring(ring, kind, exclude_owners=off)
    return apns.send_agent_ring(ring, kind)


# ---- a phone that switched agent rings off for itself --------------------------------
#
# Peter, 18 Oct: the phone that films a demo must not ring while another phone does. A phone
# switches rings off for itself only (its own authenticated, paired, full-scope device id); the
# Host then leaves it out of every agent ring: VoIP, ring alert push, agent-ring SSE event and
# the GET /calls/ring poll. Default is on, and with no phone switched off delivery is unchanged.
#
# Ring alerts are excluded by alert-token OWNER: POST /devices binds a token to the authenticated
# device that registers it (apns.register_token). A legacy unbound token cannot be attributed, so
# while any live unbound token remains the answer is "partial" (alerts_suppressed false), never "off".
# Provenance is token possession by an authenticated device, not proof of the physical phone.

_DEVICE_FIELDS = {"receive_rings"}


def device_prefs() -> list[dict]:
    """The phones that switched agent rings off, read fresh at every use."""
    raw = settings_store.get(DEVICE_KEY)
    return json.loads(raw) if raw else []


def receives_rings(principal: str | None) -> bool:
    return not principal or all(row.get("principal") != principal for row in device_prefs())


def device_state(principal: str) -> dict:
    """The truthful state of this phone's rings. ``off`` only when no live alert token could still carry
    a ring alert to it: none of its own is eligible (they are excluded by owner) and no live UNBOUND token
    remains (one might be this phone's). Otherwise rings-off is ``partial`` and must be shown as pending."""
    from . import apns
    off = not receives_rings(principal)
    unbound = sum(1 for row in apns.owned_active_tokens() if not row.get("owner"))
    suppressed = off and unbound == 0
    state = "on" if not off else ("off" if suppressed else "partial")
    answer = {"ok": True, "receive_rings": not off, "alerts_suppressed": suppressed, "state": state}
    if off and not suppressed:
        answer.update(reason="unbound_alert_tokens", unbound_alert_tokens=unbound)
    return answer


def _ring_device(principal: str) -> None:
    from . import agent_ring_live as live
    if not isinstance(principal, str) or not principal.startswith("device_") or not live.device_ok(principal):
        raise CallError(403, "ring_device_required")


def set_receive_rings(principal: str, data: Any) -> dict:
    """POST /calls/ring/device {"receive_rings": bool}: this phone switches agent rings on or off for itself."""
    _ring_device(principal)
    if not isinstance(data, dict) or set(data) - _DEVICE_FIELDS or not isinstance(data.get("receive_rings"), bool):
        raise CallError(400, "ring_device_invalid")
    with _lock:
        rows = {r["principal"]: r for r in device_prefs()}
        if data["receive_rings"]:
            rows.pop(principal, None)
        else:
            rows[principal] = {"principal": principal, "updated_at": now_ms()}
        settings_store.set_text(DEVICE_KEY, json.dumps(list(rows.values()), separators=(",", ":")))
    log("agentRingDevice", f"principal={principal} receive_rings={int(data['receive_rings'])}")
    return device_state(principal)


def get_device_state(principal: str) -> dict:
    """GET /calls/ring/device: whether this phone receives agent rings."""
    _ring_device(principal)
    return device_state(principal)


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
