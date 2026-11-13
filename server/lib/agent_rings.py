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

A ring may ask to listen first (``opening_mode: "listen_first"``,
docs/agent-rings.md "Listen-first"). A capable phone then answers with an
ACK only and reports how the opening resolved: the user's first utterance
(the greeting ``/send``) or one claim that nobody spoke, which admits ONE
initiative turn for the calling agent. That state lives in its own settings
row per ring, written only inside SQLite transactions, because the runtime's
dispatcher claims the initiative from another process.
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import secrets
import sys
import threading
import time
import uuid
from typing import Any, Iterator

from . import db, events, settings_store
from .group_calls import CallError
from .log import log, log_exception

from .agent_rings_base import (  # noqa: E402,F401  (split_apply: agent-rings)
    KEY, AUDIT_KEY, ENABLED_KEY, VOIP_KEY, DEVICE_KEY, RING_TIMEOUT_MS, AGENT_INTERVAL_MS, HOUR_MS,
    HOURLY_LIMIT, KEEP_RECORDS, KEEP_AUDIT, REASON_CHARS, OPENING_CHARS, LIVE_JOIN_MS, FINAL, _lock,
    _context, _ABSENT, now_ms, bind, _load, _save, _audit, audit,
)
# ---- persistence ---------------------------------------------------------------

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


from .agent_ring_openings import (  # noqa: E402,F401  (split_apply: agent-rings)
    public, SAY, LISTEN_FIRST, OPENING_MODES, OPENING_KEY, CALL_OPEN, SILENCE_MS, OPEN_STATES,
    COMMITTED, SILENCE_CLAIMS, CLAIMS, ABANDON_REASONS, KEEP_REQUESTS, _opening_lock, _server_id,
    _kick_queue, _int, _RECEIPT_ROWS, _generation, _gen_pair, _breach, _identity_breach,
    _terminal_history, _settle_unknown, _identity_gate, _opening_txn, opening_state, _openings,
    _ring_for_token, _public_intent, _public_opening, _open_record, _auth_proof, _fence_breach,
    _enforce_fence, _refuse, _retire, _STARTED_ROWS, _WAITING_ROWS, _DROPPED_ROWS, is_initiative,
    is_foreign_message, _row_admission, _row, _resolve_unknown, _withdraw_row, _supersede,
    _check_live, _header, _intend, _fingerprint, _validate, opening_claim, _apply_claim, greeting,
    greeting_routed, greeting_admitted, admit_initiative, _admit_after_resolve, claim_initiative,
    release_unsent, authorize_parked_launch, resolve_launch_failure, resolve_stale_claims,
    _recover_one, recover_openings, _retire_where, retire_for_call, retire_for_principal,
    _announce_opening, _opening_for_trace, opening_audio, skip_opening_synthesis,
)
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


@contextlib.contextmanager
def _settling() -> Iterator[list[dict]]:
    """``_lock``, and the rings it settled as missed announced once it is released. An announcement
    may push to Android phones through Google, which can take tens of seconds; nothing else waits on
    the ring lock meanwhile (an iPhone's GET /calls/ring, an answer, a new ring)."""
    missed: list[dict] = []
    try:
        with _lock:
            yield missed
    finally:
        for ring in missed:
            _announce(ring, "missed")


def _settle(records: list[dict], missed: list[dict]) -> list[dict]:
    """Expire overdue rings in ``records`` and save them. The rings that became missed are added to
    ``missed`` for the caller to announce after it releases the lock (_settling); returned too."""
    expired = False
    for ring in records:
        # A user call's or an agent ring's Live answer alike: past its one-use join
        # ticket nobody can join it (_join refuses), so it stops counting as live (RTO-04).
        if (ring.get("_live_phase") == "answered_waiting_join"
                and now_ms() >= ring.get("_live_join_expires_at", 0)):
            ring.update(_live_phase="retired", _live_retired_reason="live_join_expired")
            expired = True
    now_missed = _expire(records)
    if expired:
        _save(records)
    if now_missed:
        _save(records)
        for ring in now_missed:
            _audit({"outcome": "missed", "ring_id": ring["ring_id"], "session": ring["session"], "by": "timeout"})
        missed.extend(now_missed)
    return now_missed


def expire_due() -> list[str]:
    """Settle every overdue ring now; returns the ring ids that became missed."""
    with _settling() as missed:
        return [r["ring_id"] for r in _settle(_load(), missed)]


def get(ring_id: str) -> dict | None:
    _recover_one(ring_id)
    with _settling() as missed:
        records = _load()
        _settle(records, missed)
        ring = next((r for r in records if r["ring_id"] == ring_id), None)
        return public(ring) if ring else None


def current(principal: str | None = None) -> dict | None:
    """The ring that is ringing now, or None. With ``principal``, only a ring that client may see (H6)."""
    if principal is not None and not receives_rings(principal):
        return None                     # this phone switched agent rings off for itself
    capable = live_capable(principal) if principal is not None else True
    with _settling() as missed:
        records = _load()
        _settle(records, missed)
        ring = next((r for r in reversed(records) if r["state"] == "ringing" and not r.get("_user_call")
                     and _visible(r, capable)), None)
        return public(ring) if ring else None


# ---- who may see a Live ring (H6) -----------------------------------------------------
#
# A Live ring reaches only a client whose device registration advertises agent_live_v1:
# its SSE event, its GET projection and its VoIP push. An older phone (2818) would report
# any agent-ring event to CallKit as an incoming call, so it must never see a Live ring at
# all, and a Live ring never falls back to the global alert push. Classic rings are unchanged.

def live_capable(principal: str | None) -> bool:
    """The principal's VoIP registration, or its Android push row, says it can run a Live call.

    Two declarations count: an iPhone's VoIP registration (POST /calls/ring/voip-token) and an
    Android phone's FCM push row (POST /devices with supported_conversation_modes,
    _live_device_rows: live, owned by that device, the device still paired with full scope).
    Read fresh at every use, never cached. An absent registration or one without
    agent_live_v1 is not capable. LIMIT (explicit, not a no-ring guarantee): the
    registration is the device's own last word. A phone that registered
    agent_live_v1 and was then downgraded to an app that cannot run Live stays
    "capable" here until it registers again, and keeps receiving Live rings,
    events and VoIP. Only a fresh registration without agent_live_v1, or a
    forgotten token, removes it. The answer itself is still checked separately
    (WIRE-v2 decision 4)."""
    return bool(principal) and (any(row.get("principal") == principal for row in _live_tokens())
                                or any(row["owner"] == principal for row in _live_device_rows()))


def _visible(ring: dict, capable: bool) -> bool:
    from .agent_ring_live import MODE_LIVE
    return capable or ring.get("conversation_mode") != MODE_LIVE


def event_visible(event: dict, principal: str | None) -> bool:
    """Whether one SSE event, or one public ring record (no "type"), may go to this client."""
    from .agent_ring_live import MODE_LIVE
    from .protocol import SSEType
    if event.get("type", SSEType.AGENT_RING) != SSEType.AGENT_RING:
        return True
    if event.get("origin") == "user_call":
        return bool(principal) and event.get("answered_by") == principal
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
    mine = [r for r in records if r["agent_id"] == agent["agent_id"] and not r.get("_user_call")]
    if mine and now - mine[-1]["created_at"] < AGENT_INTERVAL_MS:
        wait = (AGENT_INTERVAL_MS - (now - mine[-1]["created_at"]) + 999) // 1000
        raise CallError(429, "wait before calling again", retry_after_s=int(wait))
    if sum(1 for r in records if not r.get("_user_call") and now - r["created_at"] < HOUR_MS) >= HOURLY_LIMIT:
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
         conversation_mode: Any = None, event_context: Any = None, opening_mode: Any = _ABSENT) -> dict:
    """Ring the owner's phone on behalf of an agent. ``opening_mode`` is
    ``say`` (the default, today's ring) or ``listen_first``.

    ``conversation_mode: agent_live_v1`` (feature agent_ring_live) asks for a
    Live ring instead (_ring_live): its opening is the Live model's, so it takes
    no listen-first opening."""
    from . import agent_ring_live as live
    if conversation_mode == live.MODE_LIVE:
        return _ring_live(caller_agent_id=caller_agent_id, agent=agent, reason=reason,
                          opening_line=opening_line, event_context=event_context, opening_mode=opening_mode)
    reason = " ".join(str(reason or "").split())[:REASON_CHARS]
    opening = " ".join(str(opening_line or "").split())[:OPENING_CHARS]
    # A listen-first ring remembers the host it was placed on; read outside the lock.
    read = _generation() if opening_mode == LISTEN_FIRST else None
    host_gen = _gen_pair(read)
    unprovable = False
    try:
        with _settling() as missed:
            records = _load()
            _settle(records, missed)
            who = {"session": "", "persona": "", "agent_id": caller_agent_id or ""}
            try:
                live.mode_of(conversation_mode)     # an unknown mode is 400; agent_live_v1 never reaches here
                caller = _caller(caller_agent_id, agent)
                who = {"session": str(caller["session"]), "persona": str(caller["persona"]),
                       "agent_id": str(caller["agent_id"])}
                if not reason:
                    raise CallError(400, "reason required")
                if opening_mode is not _ABSENT and not (
                        isinstance(opening_mode, str) and opening_mode in OPENING_MODES):
                    raise CallError(400, "unknown opening_mode")
                if not opening:
                    raise CallError(400, "opening_line required")
                if event_context is not None:
                    raise CallError(400, live.CONTEXT_INVALID)
                if opening_mode == LISTEN_FIRST and host_gen is None:
                    unprovable = True
                    raise CallError(409, "listen-first opening is unavailable: host identity unprovable; "
                                         "ring with opening_mode say")
                _admit(caller, records)
            except CallError as exc:
                _audit({"outcome": "refused", "error": exc.error, **who, "reason": reason})
                raise
            record = _ringing_record(who, reason, opening, caller, opening_mode, host_gen)
            records.append(record)
            _save(records)
            _audit({"outcome": "ringing", "ring_id": record["ring_id"], **who, "reason": reason})
    finally:
        if unprovable:
            log("hostGenerationUnprovable", f"ring {who.get('session') or '-'} "
                                            f"{getattr(read, 'reason', None) or 'unknown'}")
    delivery = _announce(record, "ring")
    _schedule_expiry(record)
    return {"ok": True, "ring": public(record), "delivery": delivery,
            "summary": f"Calling the user. The call rings for {RING_TIMEOUT_MS // 1000} seconds."}


def _ringing_record(who: dict, reason: str, opening: str, caller: dict, opening_mode: Any,
                    host_gen: dict | None) -> dict:
    now = now_ms()
    record = {
        "ring_id": "rng_" + uuid.uuid4().hex, **who, "reason": reason, "opening_line": opening,
        "opening_audio_url": _voice(opening, caller), "state": "ringing", "created_at": now,
        "expires_at": now + RING_TIMEOUT_MS, "ended_at": None, "answered_by": "", "call_id": "",
    }
    if opening_mode == LISTEN_FIRST:
        # The line is still voiced: it is the fallback for a phone that
        # cannot listen first. A say ring keeps today's record exactly.
        record["opening_mode"] = LISTEN_FIRST
        # Internal (never on the wire: public() drops "_" keys).
        record["_host_gen"] = host_gen
    return record


def _find(records: list[dict], ring_id: str) -> dict:
    ring = next((r for r in records if r["ring_id"] == str(ring_id or "")), None)
    if ring is None:
        raise CallError(404, "unknown call")
    return ring


def _end(ring_id: str, state: str, *, by: str, allowed=None) -> dict:
    with _settling() as missed:
        records = _load()
        _settle(records, missed)
        ring = _find(records, ring_id)
        if ring.get("_user_call") and ring["_user_call"] != by:
            raise CallError(404, "unknown call")
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
        if ring.get("_user_call"):
            raise CallError(404, "unknown call")
        if ring["state"] in FINAL:
            return {"ok": True, "ring": public(ring), "changed": False}
    return _end(ring_id, "cancelled", by=caller_agent_id or "owner", allowed=own)


def _guarded(step, event: str, detail: str) -> None:
    """Run a cleanup step that must never raise: its failure is logged, or
    failing that written to stderr; if even that fails it is dropped, because the
    exception already propagating takes priority over logging."""
    try:
        step()
    except Exception as exc:  # noqa: BLE001
        try:
            log_exception(event, exc, detail)
        except Exception as log_exc:  # noqa: BLE001
            try:
                print(f"{event}: {exc!r}; logging failed: {log_exc!r}", file=sys.stderr)
            except Exception:  # noqa: BLE001 - nothing left to report through
                pass


def _ring_live(*, caller_agent_id: str = "", agent: str = "", reason: str = "", opening_line: str = "",
               event_context: Any = None, opening_mode: Any = _ABSENT) -> dict:
    """Ring the owner's phone with a Live ring (agent_ring_live), unchanged; see ring().

    ``conversation_mode: agent_live_v1`` (feature agent_ring_live) asks for a
    Live ring: its ``event_context`` is validated and frozen with the ring in a
    snapshot, and a ring that cannot be built is refused, never downgraded. A
    producer retry of the same event gets the same ring back."""
    from . import agent_ring_live as live
    reason = " ".join(str(reason or "").split())[:REASON_CHARS]
    opening = " ".join(str(opening_line or "").split())[:OPENING_CHARS]
    snapshot = replay = None
    with _settling() as missed:
        records = _load()
        _settle(records, missed)
        who = {"session": "", "persona": "", "agent_id": caller_agent_id or ""}
        try:
            mode = live.MODE_LIVE
            caller = _caller(caller_agent_id, agent)
            who = {"session": str(caller["session"]), "persona": str(caller["persona"]),
                   "agent_id": str(caller["agent_id"])}
            if not reason:
                raise CallError(400, "reason required")
            if not opening:
                raise CallError(400, "opening_line required")
            if opening_mode is not _ABSENT and opening_mode != SAY:
                # DECISION D1 (listen-first × Live): a Live ring's opening is spoken by the Live
                # model, so a listen-first opening is refused with the existing mode error.
                raise CallError(400, live.MODE_INVALID)
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
                    if not _eligible_live_principals():   # capable AND receiving rings
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


def _admit_user_call(agent: dict, records: list[dict]) -> None:
    """The admission of a USER-started call: the agent must be usable by voice and no ring may be ringing.

    Not the agent-ring rules: the rings switch, mute and the per-agent ring interval govern an agent ringing
    the user, not the user calling an agent (a Normal POST /calls ignores them as well)."""
    from . import agents as agents_db
    if (agent.get("archived_at") or agent.get("deleted_at")
            or not agents_db.interaction_capabilities(agent)["can_voice_target"]):
        raise CallError(403, "this agent cannot be called")
    if any(r["state"] == "ringing" for r in records):
        raise CallError(409, "the user is already being called")


def user_call_live(principal: str, agent: Any, request_id: Any, client_nonce: Any) -> dict:
    """Originate one private, already-answered call, atomically bound to the phone's intent."""
    from . import agent_ring_live as live, agents as agents_db
    live.require_device(principal)
    _live_intent(request_id, client_nonce)  # validate everything before persisting a ring
    if not live._request_id_ok(agent):
        raise CallError(400, live.REQUEST_INVALID)
    caller = agents_db.get_by_agent_id(agent)  # exact snapshot raw id; no persona/session resolution
    if caller is None:
        raise CallError(404, "unknown agent")
    with _settling() as missed:
        records = _load()
        _settle(records, missed)
        replay = next((r for r in reversed(records) if r.get("_user_call") == principal
                       and r.get("_live_request_id") == request_id), None)
        if replay is not None:
            if replay["agent_id"] != agent or replay.get("_live_client_nonce") != client_nonce:
                raise CallError(400, live.REQUEST_INVALID)
            if replay.get("_live_retired_reason") == "live_join_expired":
                raise CallError(409, "live_join_expired")
            if replay.get("_live_phase") not in live.LIVE_PHASES:
                raise CallError(409, live.CALL_STALE)
            return _answered_live(replay, changed=False)
        profile_id = live.user_call_profile_id(caller)
        if profile_id is None:
            raise CallError(409, live.CONTEXT_UNAVAILABLE)
        context = live.user_call_context(profile_id, caller, request_id)
        if any(r.get("answered_by") == principal and r.get("_live_phase") in live.LIVE_PHASES
               for r in records):
            raise CallError(409, live.CALL_ACTIVE)
        _admit_user_call(caller, records)
        if not live_capable(principal):
            raise CallError(409, live.CLIENT_REQUIRED)
        snapshot = live.build_snapshot(caller, context)
        who = {key: str(caller[key]) for key in ("session", "persona", "agent_id")}
        now = now_ms()
        record = {
            "ring_id": "rng_" + uuid.uuid4().hex, **who, "reason": "User-initiated call",
            "opening_line": context["opening_text"], "opening_audio_url": "", "state": "ringing",
            "created_at": now, "expires_at": now + RING_TIMEOUT_MS, "ended_at": None,
            "answered_by": "", "call_id": "", "conversation_mode": live.MODE_LIVE,
            "_live_snapshot": snapshot, "_live_context_digest": live.digest(snapshot),
            "_user_call": principal,
        }
        records.append(record)
        # This internal answer runs under the same lock and persists only the answered record.
        _answer_live(records, record, principal, live.MODE_LIVE, request_id, client_nonce)
        return _answered_live(record, changed=True)


def answer(principal: str, ring_id: str, conversation_mode: Any = None, *,
           request_id: Any = None, client_nonce: Any = None, opening_mode: Any = _ABSENT) -> dict:
    """Answer a ring.

    A Live ring is answered only by a phone that asks for ``agent_live_v1``; it
    starts no group call and hands back the Live call grant instead. Neither
    kind of ring changes kind on answer (WIRE-v2 decision 5), so the ring's kind,
    read before the lock, routes it: a Live ring takes the Live branch, any other
    ring today's answer or its listen-first opening (``opening_mode``)."""
    from . import agent_ring_live as live
    mode = live.mode_of(conversation_mode, default=None)
    kind = next((r.get("conversation_mode") for r in _load() if r["ring_id"] == str(ring_id or "")), _ABSENT)
    if kind == live.MODE_LIVE:
        return _answer_live_ring(principal, ring_id, mode, request_id, client_nonce)
    if kind is not _ABSENT and mode == live.MODE_LIVE:
        raise CallError(409, live.MODE_CONFLICT)
    return _answer_classic(principal, ring_id, opening_mode)


def _answer_live_ring(principal: str, ring_id: str, mode: str | None,
                      request_id: Any, client_nonce: Any) -> dict:
    """The Live branch of answer, unchanged from agent_ring_live."""
    from . import agent_ring_live as live
    from . import group_calls
    with _settling() as missed:
        records = _load()
        _settle(records, missed)
        ring = _find(records, ring_id)
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
    if fresh:
        _announce(ring, "answered")
    return _answered_live(ring, changed=fresh)


def _answer_classic(principal: str, ring_id: str, opening_mode: Any = _ABSENT) -> dict:
    """Answer: start a group call with the agent and hand back its opening.

    A phone that can listen first sends ``opening_mode: "listen_first"``; on a
    listen_first ring it gets an ACK with the opening's token and nothing is
    spoken or run. Anything else gets today's answer (the line, voiced at
    ring time); a phone that named any opening_mode also learns ``mode: say``.
    """
    from . import group_calls
    capable = opening_mode == LISTEN_FIRST
    # Read outside the ring lock, and only when a listen-first identity can
    # matter: a fresh listen-first answer, or a replay asking for its opening.
    # A ring's opening_mode never changes and a ring never returns to ringing,
    # so this peek cannot miss a case that needs the read.
    peek = next((r for r in _load() if r["ring_id"] == str(ring_id or "")), None)
    needs_identity = bool(peek) and peek.get("opening_mode") == LISTEN_FIRST and (
        peek["state"] == "ringing" or opening_mode == LISTEN_FIRST)
    read = _generation() if needs_identity else None
    gen = _gen_pair(read)
    cause = ""
    breach_missed = None
    late_answered = None
    started_call_id = ""            # the exact call this answer started, once started
    started_by = ""
    completed = False
    try:
        with _settling() as missed:
            records = _load()
            _settle(records, missed)
            ring = _find(records, ring_id)
            listen = ring.get("opening_mode") == LISTEN_FIRST
            if ring["state"] == "answered":
                if ring["answered_by"] != principal:
                    raise CallError(409, "the call was answered elsewhere")
                call = group_calls.get_call(ring["call_id"])
                opening = _secret_opening(ring, opening_mode)
                if opening is not None:
                    # R-a: the replay that would hand back the token needs this host,
                    # AND the opening's own authority: never one retired, settled by a
                    # breach, or bound to another identity (ADD-7). Nothing is written.
                    cause = _breach(ring.get("_host_gen"), gen)
                    if not cause and (opening.get("identity_breach") or opening.get("retired")):
                        cause = "opening_not_authorized"
                    if not cause:
                        cause = _identity_breach(opening, gen)
                    if cause:
                        _audit({"outcome": "refused", "error": "the call is over",
                                "ring_id": ring["ring_id"], "cause": cause, "by": principal})
                        raise CallError(409, "the call is over", ring=public(ring))
                return _answered(ring, call, changed=False, opening_mode=opening_mode)
            if ring["state"] != "ringing":
                raise CallError(409, "the call is over", ring=public(ring))
            if listen:
                cause = _breach(ring.get("_host_gen"), gen)
                if not cause:
                    # Prove it under the write lock BEFORE the call starts: a call
                    # (its event and notices) cannot be taken back. No writes here.
                    with _opening_txn(ring["ring_id"]) as proof:
                        proven = proof["gen"]
                    if proven != gen:
                        cause = _breach(ring.get("_host_gen"), proven) or "host_identity_unprovable"
                if cause:
                    # Placed on another boot or instance, or unprovable: the call is
                    # over. No call starts and no opening record is made (N3).
                    ring.update(state="missed", ended_at=now_ms(), _end_reason=cause)
                    _save(records)
                    _audit({"outcome": "refused", "error": "the call is over",
                            "ring_id": ring["ring_id"], "cause": cause, "by": principal})
                    breach_missed = ring
                    raise CallError(409, "the call is over", ring=public(ring))
            placed = {"ring_id": ring["ring_id"], "reason": ring["reason"], "opening_line": ring["opening_line"]}
            if listen and capable:
                placed["opening_mode"] = LISTEN_FIRST
            started = group_calls.start(
                principal, [ring["session"]], request_id=ring["ring_id"], by=ring["session"], ring=placed)
            call = started["call"]
            # From here until `completed`, any failure ends exactly this call once (ADD-8).
            started_call_id, started_by = call["call_id"], ring["session"]
            ring.update(state="answered", ended_at=now_ms(), answered_by=principal, call_id=call["call_id"])
            if listen:
                # The answer, its opening and the §11.L fence commit together. The
                # generation was proven just before the call started; if it moved in
                # between, the opening is recorded already unknown and nothing is minted.
                late = ""
                with _opening_txn(ring["ring_id"]) as box:
                    _save(records)
                    _open_record(box, ring, principal, capable=capable,
                                 host_gen=box["gen"] if box["gen"] == gen else None)
                    if box["gen"] != gen and box["op"] is not None:
                        # Moved after the call started (proven just before it): the
                        # opening is born without authority; never an ACK.
                        late = _breach(ring.get("_host_gen"), box["gen"]) or "host_identity_unprovable"
                        _settle_unknown(box["op"], late)
                        box["dirty"] = True
                if late:
                    cause = late
                    late_answered = ring
                    _audit({"outcome": "refused", "error": "the call is over", "ring_id": ring["ring_id"],
                            "cause": late, "by": principal, "call_id": call["call_id"]})
                    raise CallError(409, "the call is over", ring=public(ring))
                _audit({"outcome": "opening", "ring_id": ring["ring_id"],
                        "state": "pending" if capable else "fallback", "by": principal})
            else:
                _save(records)
            _audit({"outcome": "answered", "ring_id": ring["ring_id"], "session": ring["session"],
                    "by": principal, "call_id": call["call_id"]})
            # Later failures (the announce and the response below) are not
            # compensated: the answer is committed and its call is up.
            completed = True
    finally:
        # Every step here is guarded: nothing may replace the exception (or the
        # 409) already propagating.
        if cause == "host_identity_unprovable":
            _guarded(lambda: log("hostGenerationUnprovable",
                                 f"{ring_id} {getattr(read, 'reason', None) or 'unknown'}"),
                     "agentRingIdentityLogFail", ring_id)
        if started_call_id and not completed:
            # A failure after the call started (or the identity moved after the
            # proof): ONE best-effort end of exactly that call. No retry, no other
            # call. If it fails, the call may stay live until its own lifecycle
            # ends it; a committed, identity-proven opening then stays usable.
            _guarded(lambda: group_calls.end(principal, call_id=started_call_id, by=started_by),
                     "agentRingAnswerCompensationEndFail", f"call={started_call_id}")
        if breach_missed is not None:
            # The missed SSE event and the replacing push are SUBMITTED; devices on
            # the event stream dismiss the ring. A push alone does not end the
            # original CallKit entry (its terminal VoIP push uses another UUID);
            # that waits for the stream, a poll, foreground delivery or expiry.
            # The agent is not told the user did not answer: they did.
            _guarded(lambda: _announce(breach_missed, "missed", notice=False),
                     "agentRingAnnounceFail", ring_id)
        if late_answered is not None:
            _guarded(lambda: _announce(late_answered, "answered", notice=False),
                     "agentRingAnnounceFail", ring_id)
    _announce(ring, "answered")
    return _answered(ring, call, changed=True, opening_mode=opening_mode)


def _answer_live(records: list[dict], ring: dict, principal: str, mode: str | None,
                 request_id: Any, client_nonce: Any) -> bool:
    """The Live branch of answer (under _lock): one grant per ring, first owner
    wins. True when this request answered it, False for the owner's repeat."""
    from . import agent_ring_live as live
    if ring.get("_user_call") and ring["_user_call"] != principal:
        raise CallError(403, live.AUTH)
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


def _secret_opening(ring: dict, opening_mode: Any) -> dict | None:
    """The stored opening when the answer response would carry its token and
    owner nonce (listen-first ring, listen-first request, not a fallback), else None."""
    if ring.get("opening_mode") != LISTEN_FIRST or opening_mode != LISTEN_FIRST:
        return None
    opening = opening_state(ring["ring_id"])
    return opening if opening is not None and opening["state"] != "fallback" else None


def _answered(ring: dict, call: dict | None, *, changed: bool, opening_mode: Any = _ABSENT) -> dict:
    opening = _secret_opening(ring, opening_mode)
    if opening is not None:
        return {"ok": True, "ring": public(ring), "call": call, "changed": changed,
                "opening": _public_opening(opening, secret=True)}
    legacy = {"text": ring["opening_line"], "audio_url": ring["opening_audio_url"],
              "session": ring["session"], "persona": ring["persona"]}
    if opening_mode is not _ABSENT:
        legacy["mode"] = SAY
    return {"ok": True, "ring": public(ring), "call": call, "changed": changed, "opening": legacy}


# ---- telling everyone ---------------------------------------------------------------

_NOTICES = {
    "answered": "The user answered your call about: {reason}",
    "declined": "The user declined your call about: {reason}",
    "missed": "The user did not answer your call about: {reason}",
    "cancelled": "You cancelled your call about: {reason}",
}


def _announce(ring: dict, kind: str, *, notice: bool = True) -> dict:
    """Event, push and the agent's chat notice for one ring change. Never raises.
    ``notice=False`` skips the chat notice when its wording would be untrue."""
    if ring.get("_user_call"):
        return {}                       # issue #216: a user-started Live call is this phone's own; nothing to announce
    stream = _context["stream"]
    if stream is not None:
        try:
            wire = public(ring)
            events.broadcast(stream, events.agent_ring(
                **{key: wire[key] for key in events.FIELDS[events.SSEType.AGENT_RING] if key in wire}))
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingEmitFail", exc, f"ring={ring['ring_id']}")
    delivery: dict = {}
    from .agent_ring_live import MODE_LIVE
    live = ring.get("conversation_mode") == MODE_LIVE
    fcm_live = None
    if live:
        # H6: the global alert reaches every phone, also those that cannot run Live. A Live ring is
        # announced by VoIP to capable phones only, for its first ring and for every later state
        # (answered, declined, missed, cancelled): no global alert, ever. An Android phone that
        # declared agent_live_v1 on its push row gets every state by FCM instead, as a classic
        # ring's alert reaches it, naming conversation_mode (docs/push-android.md "Live calls").
        # Handed over BEFORE the VoIP push: a slow Apple must not let an answer from an Android
        # phone that was already polling reach Google ahead of the ringing state.
        try:
            fcm_live = _push_live_fcm(ring, kind)
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingLiveFcmFail", exc, f"ring={ring['ring_id']}")
    if kind == "ring":
        # A live ring rings through CallKit when a phone registered a VoIP
        # token; the alert is then a duplicate. Only the live ring is ever
        # VoIP: iOS requires every VoIP push to start a CallKit call.
        try:
            delivery["voip"] = _push_voip(ring) or {}
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingVoipFail", exc, f"ring={ring['ring_id']}")
    if live:
        if fcm_live is not None:
            delivery["fcm_live"] = fcm_live
    elif not (delivery.get("voip") or {}).get("sent"):
        try:
            delivery.update(_push(ring, kind) or {})
        except Exception as exc:  # noqa: BLE001
            log_exception("agentRingPushFail", exc, f"ring={ring['ring_id']}")
    if notice and kind in _NOTICES:
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


def conversation_modes(value: Any) -> list[str]:
    """A declared ``supported_conversation_modes``: absent (None) or a list of known modes, each at
    most once; any other value is refused with 400 live_mode_invalid. The list, duplicates dropped."""
    from . import agent_ring_live as live
    if value is not None and (not isinstance(value, list) or len(value) > len(live.MODES)
                              or any(not isinstance(m, str) or m not in live.MODES for m in value)):
        raise CallError(400, live.MODE_INVALID)
    return list(dict.fromkeys(value or ()))


def register_voip_token(principal: str, token: str, *, environment: str = "production",
                        supported_conversation_modes: Any = None) -> dict:
    """The phone's PushKit VoIP token, one per principal. "" forgets it.

    ``supported_conversation_modes`` (absent: classic only) says which ring
    modes this phone can run; it is a delivery hint, never a credential, and an
    answer is still checked on its own (WIRE-v2 decision 4)."""
    token = str(token or "").strip().lower()
    environment = "sandbox" if str(environment or "").lower() == "sandbox" else "production"
    if token and not _hex_token(token):
        raise CallError(400, "token must be hex")
    modes = conversation_modes(supported_conversation_modes)
    with _lock:
        raw = settings_store.get(VOIP_KEY)
        rows = {r["principal"]: r for r in (json.loads(raw) if raw else [])}
        if token:
            rows[principal] = {"principal": principal, "token": token, "environment": environment,
                               "updated_at": now_ms()}
            if modes:
                rows[principal]["supported_conversation_modes"] = modes
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
    """iPhones that can run a Live call and still receive rings: the VoIP pushes of a Live ring."""
    return _receiving(_live_tokens())


def _live_device_rows() -> list[dict]:
    """Android push rows (POST /devices) that declared agent_live_v1, of a device still paired with
    full scope. A row disabled (DELETE /devices, rotation, revocation), taken over by another pairing
    (its owner changed) or re-registered without the declaration no longer appears here."""
    from . import apns
    from .agent_ring_live import MODE_LIVE, device_ok
    return [row for row in apns.declared_mode_rows()
            if MODE_LIVE in row["supported_conversation_modes"] and str(row["owner"]).startswith("device_")
            and device_ok(row["owner"])]


def _ring_reachable_device_rows() -> list[dict]:
    """The declared Android rows a Live RING can reach now: this Host has a usable FCM key, the row is
    live (Google has not reported it gone) and its phone receives rings. A phone's own Live call
    (POST /calls/live) needs no push and counts without a key (live_capable)."""
    from . import apns, config
    try:
        usable = apns.fcm_configured(config.load())
    except Exception as exc:  # noqa: BLE001 - an unreadable config: no Android ring recipient
        log_exception("agentRingFcmKeyUnknown", exc)
        usable = False
    if not usable:
        return []
    off = {row["principal"] for row in device_prefs()}
    return [row for row in _live_device_rows() if row["owner"] not in off]


def _eligible_live_principals() -> set[str]:
    """Phones a Live ring can ring: an iPhone by its VoIP registration, an Android phone by its
    declared push row while a push can reach it (_ring_reachable_device_rows); both still receiving
    rings. The only ones a Live ring may admit for: with none, 409 live_client_required tells the
    caller to fall back."""
    principals = {row.get("principal") for row in _eligible_live_tokens()}
    principals |= {row["owner"] for row in _ring_reachable_device_rows()}
    return {p for p in principals if p}


class _LiveRingSender:
    """The Android phones' Live ring states, sent on one daemon thread, oldest first: a ring's states
    reach Google in the order they happened (ringing, then answered, declined, missed or cancelled),
    and Google, which may take tens of seconds, never holds the request that rang, answered or ended
    the call.

    ``_rings`` remembers, per ring whose ringing state this process handled, the push rows the ringing
    was TRIED on (a phone may ring though Google's answer never came back), until the ring's last state
    is handled or the ring is long expired (``FORGET_AFTER_MS``).

    - A ringing state is dropped when a later state of its ring is queued behind it, or when the ring
      has expired. The ring is then known, with nobody told.
    - An end of a known ring goes only to the rows its ringing was tried on: nothing to stop elsewhere.
    - An end of a ring this process does not know (it started before a Host restart, or its entry was
      forgotten) goes to every row: when in doubt the end is sent.
    - An end with a later state of its ring queued behind it is dropped; the later one is the end."""

    FORGET_AFTER_MS = 10 * 60 * 1000

    def __init__(self, name: str):
        self._name = name
        self._cond = threading.Condition()
        self._jobs: list[tuple[dict, str, list[dict]]] = []
        self._rings: dict[str, dict[str, Any]] = {}       # ring_id → {"tried": set of tokens, "expires_at": ms}
        self._busy = False
        self._thread: threading.Thread | None = None

    def submit(self, ring: dict, kind: str, rows: list[dict]) -> None:
        with self._cond:
            self._jobs.append((ring, kind, rows))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._jobs:
                    self._cond.wait()
                ring, kind, rows = self._jobs.pop(0)
                later = any(queued[0]["ring_id"] == ring["ring_id"] for queued in self._jobs)
                self._busy = True
            try:
                self._send(ring, kind, rows, later=later)
            except Exception as exc:  # noqa: BLE001 - one failed push never stops the next
                log_exception("agentRingSenderFail", exc, self._name)
            finally:
                db.close_local()        # a job's own connection: rings are rare, an idle thread holds none
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def _forget_expired_locked(self) -> None:
        cutoff = now_ms() - self.FORGET_AFTER_MS
        for ring_id in [r for r, entry in self._rings.items() if entry["expires_at"] < cutoff]:
            del self._rings[ring_id]

    def _send(self, ring: dict, kind: str, rows: list[dict], *, later: bool) -> None:
        from . import apns
        ring_id = ring["ring_id"]
        expires_at = int(ring.get("expires_at") or 0)
        with self._cond:
            self._forget_expired_locked()
            entry = self._rings.get(ring_id)
        if kind == "ring":
            with self._cond:
                entry = self._rings.setdefault(ring_id, {"tried": set(), "expires_at": expires_at})
            if later or now_ms() >= expires_at:
                log("agentRingLiveFcmDropped",
                    f"ring={ring_id} kind=ring reason={'superseded' if later else 'expired'}")
                return
            for row in rows:
                with self._cond:
                    entry["tried"].add(row["token"])        # before the send: the phone may ring either way
                apns.send_agent_ring(ring, kind, tokens=[row])
            return
        if later:
            log("agentRingLiveFcmDropped", f"ring={ring_id} kind={kind} reason=superseded")
            return
        targets = rows if entry is None else [row for row in rows if row["token"] in entry["tried"]]
        if len(targets) < len(rows):
            log("agentRingLiveFcmDropped", f"ring={ring_id} kind={kind} reason=not_rung rows={len(rows) - len(targets)}")
        for row in targets:
            apns.send_agent_ring(ring, kind, tokens=[row])
        with self._cond:
            self._rings.pop(ring_id, None)

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until every job handed over so far has run (tests)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._jobs or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True


_LIVE_FCM_SENDER = _LiveRingSender("ring-live-fcm")


def _push_live_fcm(ring: dict, kind: str) -> dict | None:
    """One Live ring state by FCM to the Android phones a Live ring can reach, sent off the calling
    thread (_LiveRingSender). Answers at once with how many rows the send is pending for; None
    (nothing reported) when there is none, so a Host without such a phone answers exactly as before."""
    rows = _ring_reachable_device_rows()
    if not rows:
        return None
    _LIVE_FCM_SENDER.submit(dict(ring), kind, rows)
    return {"pending": len(rows)}


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


# ---- listen-first openings (docs/agent-rings.md "Listen-first") ---------------------
#
# One opening per listen_first ring, in its own settings row. The phone owns
# timing (readiness, VAD onset, the silence deadline); the Host owns the one
# resolution per ring. Every change is one SQLite transaction (BEGIN
# IMMEDIATE) that reads and writes the row, plus the prompt admission and the
# queued row it touches, so the runtime's dispatcher claim (another process)
# and a greeting can never both win. TTS, events, pushes and the queue kick
# run after commit.

# ---- host identity (C2-S2b; WIRING-DESIGN §2.2, ADD-1, ADD-2) ---------------------------
# A listen-first ring and its opening remember the host generation (kernel boot
# id + instance token) they were made under. A site that finds another one, or
# cannot prove the current one, never grants authority. Equal generations only
# mean "not shown different": a restore applied by an older Host does not rotate
# the token (docs/agent-rings.md "Host identity").

# ---- opening audio (§11.B) ---------------------------------------------------------------

