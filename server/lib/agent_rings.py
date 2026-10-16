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
import threading
import time
import uuid
from typing import Any, Iterator

from . import db, events, settings_store
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
_context: dict[str, Any] = {"stream": None, "tts": None, "ctx": None}
_ABSENT = object()


def now_ms() -> int:
    return int(time.time() * 1000)


def bind(ctx) -> None:
    """Remember where events go and how to voice the opening line."""
    _context["stream"] = getattr(ctx, "stream", None)
    _context["tts"] = getattr(ctx, "tts", None)
    _context["ctx"] = ctx


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
    if ring.get("opening_mode") == LISTEN_FIRST:
        opening = opening_state(ring["ring_id"])
        if opening is not None:
            out["opening"] = _public_opening(opening)
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
    _recover_one(ring_id)
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


def ring(*, caller_agent_id: str = "", agent: str = "", reason: str = "", opening_line: str = "",
         opening_mode: Any = _ABSENT) -> dict:
    """Ring the owner's phone on behalf of an agent. ``opening_mode`` is
    ``say`` (the default, today's ring) or ``listen_first``."""
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
            if opening_mode is not _ABSENT and not (
                    isinstance(opening_mode, str) and opening_mode in OPENING_MODES):
                raise CallError(400, "unknown opening_mode")
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
        if opening_mode == LISTEN_FIRST:
            # The line is still voiced: it is the fallback for a phone that
            # cannot listen first. A say ring keeps today's record exactly.
            record["opening_mode"] = LISTEN_FIRST
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


def answer(principal: str, ring_id: str, opening_mode: Any = _ABSENT) -> dict:
    """Answer: start a group call with the agent and hand back its opening.

    A phone that can listen first sends ``opening_mode: "listen_first"``; on a
    listen_first ring it gets an ACK with the opening's token and nothing is
    spoken or run. Anything else gets today's answer (the line, voiced at
    ring time); a phone that named any opening_mode also learns ``mode: say``.
    """
    from . import group_calls
    capable = opening_mode == LISTEN_FIRST
    with _lock:
        records = _load()
        _settle(records)
        ring = _find(records, ring_id)
        if ring["state"] == "answered":
            if ring["answered_by"] != principal:
                raise CallError(409, "the call was answered elsewhere")
            call = group_calls.get_call(ring["call_id"])
            return _answered(ring, call, changed=False, opening_mode=opening_mode)
        if ring["state"] != "ringing":
            raise CallError(409, "the call is over", ring=public(ring))
        listen = ring.get("opening_mode") == LISTEN_FIRST
        placed = {"ring_id": ring["ring_id"], "reason": ring["reason"], "opening_line": ring["opening_line"]}
        if listen and capable:
            placed["opening_mode"] = LISTEN_FIRST
        started = group_calls.start(
            principal, [ring["session"]], request_id=ring["ring_id"], by=ring["session"], ring=placed)
        call = started["call"]
        ring.update(state="answered", ended_at=now_ms(), answered_by=principal, call_id=call["call_id"])
        _save(records)
        if listen:
            _open(ring, principal, capable=capable)
        _audit({"outcome": "answered", "ring_id": ring["ring_id"], "session": ring["session"], "by": principal,
                "call_id": call["call_id"]})
    _announce(ring, "answered")
    return _answered(ring, call, changed=True, opening_mode=opening_mode)


def _answered(ring: dict, call: dict | None, *, changed: bool, opening_mode: Any = _ABSENT) -> dict:
    opening = opening_state(ring["ring_id"]) if ring.get("opening_mode") == LISTEN_FIRST else None
    if opening is not None and opening["state"] != "fallback" and opening_mode == LISTEN_FIRST:
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


# ---- listen-first openings (docs/agent-rings.md "Listen-first") ---------------------
#
# One opening per listen_first ring, in its own settings row. The phone owns
# timing (readiness, VAD onset, the silence deadline); the Host owns the one
# resolution per ring. Every change is one SQLite transaction (BEGIN
# IMMEDIATE) that reads and writes the row, plus the prompt admission and the
# queued row it touches, so the runtime's dispatcher claim (another process)
# and a greeting can never both win. TTS, events, pushes and the queue kick
# run after commit.

SAY, LISTEN_FIRST = "say", "listen_first"
OPENING_MODES = (SAY, LISTEN_FIRST)
OPENING_KEY = "agent_ring.opening."
CALL_OPEN = "call-open:"
SILENCE_MS = 1250
OPEN_STATES = frozenset({"pending", "listening", "speech_latched"})
COMMITTED = frozenset({"greeting", "initiative_started"})
SILENCE_CLAIMS = frozenset({"silence", "no_mic", "not_ready"})
CLAIMS = frozenset({"listening", "speech_started", "speech_abandoned", "renew"}) | SILENCE_CLAIMS
ABANDON_REASONS = frozenset({"empty_capture", "capture_aborted"})
KEEP_REQUESTS = 64
_opening_lock = threading.Lock()


def _server_id() -> str:
    from .server_identity import server_id
    return server_id()


def _kick_queue() -> None:
    """Let the dispatcher see a newly admitted initiative (the runtime's own
    recovery pass; a Host with a runtime forwards it)."""
    ctx = _context.get("ctx")
    if ctx is None:
        return

    def run() -> None:
        try:
            from .turn_dispatch import TurnDispatchService
            TurnDispatchService(ctx).recover_queued()
        except Exception as exc:  # noqa: BLE001 - recovery retries on its own
            log_exception("agentRingOpeningKickFail", exc)
    threading.Thread(target=run, daemon=True).start()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@contextlib.contextmanager
def _opening_txn(ring_id: str) -> Iterator[dict]:
    """One transaction on the ring's opening row: ``box["op"]`` is the record
    (None if absent); set ``box["dirty"]`` to write it back. ``box["error"]``
    is raised after COMMIT (a refusal that still records something)."""
    with _opening_lock:
        con = db.conn()
        con.execute("BEGIN IMMEDIATE")
        box: dict[str, Any] = {}
        try:
            row = con.execute("SELECT value FROM settings WHERE key = ?",
                              (OPENING_KEY + ring_id,)).fetchone()
            box["op"] = json.loads(row[0]) if row else None
            yield box
            if box.get("dirty") and box["op"] is not None:
                box["op"]["updated_at"] = now_ms()
                settings_store.set_text(OPENING_KEY + ring_id,
                                        json.dumps(box["op"], ensure_ascii=False, separators=(",", ":")))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if box.get("error") is not None:
        raise box["error"]


def opening_state(ring_id: str) -> dict | None:
    """The stored opening of a listen_first ring (all fields), or None."""
    raw = settings_store.get(OPENING_KEY + str(ring_id or ""))
    return json.loads(raw) if raw else None


def _openings() -> list[dict]:
    return [json.loads(raw) for raw in settings_store.values_with_prefix(OPENING_KEY)]


def _ring_for_token(token: Any) -> str:
    if not isinstance(token, str) or not token.startswith("opn_"):
        raise CallError(404, "unknown opening")
    for op in _openings():
        if op.get("token") == token:
            return op["ring_id"]
    raise CallError(404, "unknown opening")


def _public_intent(op: dict) -> dict | None:
    intent = op.get("intent")
    if not intent:
        return None
    return {"kind": intent["kind"], "admission_id": intent["admission_id"], "gen": intent["gen"],
            "state": intent["state"]}


def _public_opening(op: dict, *, secret: bool = False) -> dict:
    """The wire opening. Token and owner nonce go only to the answering
    phone (answer and claim responses), never into events or reads."""
    out: dict[str, Any] = {"mode": LISTEN_FIRST, "ring_id": op["ring_id"], "call_id": op["call_id"]}
    if secret and op.get("token"):
        out["token"], out["owner_nonce"] = op["token"], op["owner_nonce"]
    out.update({"epoch": op["epoch"], "state": op["state"], "silence_ms": op["silence_ms"],
                "session": op["session"], "persona": op["persona"], "server_id": op["server_id"],
                "intent": _public_intent(op)})
    if op.get("late_greeting"):
        out["greeting"] = "ordinary"
    if op.get("retired"):
        out["retired"] = op["retired"]["reason"]
    return out


def _open(ring: dict, principal: str, *, capable: bool) -> None:
    """The opening record at answer: pending for a capable phone, fallback
    (today's answer, nothing dispatched) for any other."""
    now = now_ms()
    with _opening_txn(ring["ring_id"]) as box:
        if box["op"] is not None:
            return
        box["op"] = {
            "ring_id": ring["ring_id"], "call_id": ring["call_id"], "principal": principal,
            "server_id": _server_id(), "agent_id": ring["agent_id"], "session": ring["session"],
            "persona": ring["persona"], "reason": ring["reason"],
            "token": "opn_" + secrets.token_hex(16) if capable else "",
            "owner_nonce": "own_" + secrets.token_hex(16) if capable else "",
            "epoch": 1, "state": "pending" if capable else "fallback", "silence_ms": SILENCE_MS,
            "capture": None, "latch": None, "intent": None, "greeting": None, "late_greeting": None,
            "retired": None, "requests": {}, "created_at": now,
        }
        box["dirty"] = True
    _audit({"outcome": "opening", "ring_id": ring["ring_id"], "state": "pending" if capable else "fallback",
            "by": principal})


def _refuse(op: dict, status: int, error: str, **extra: Any) -> CallError:
    return CallError(status, error, opening=_public_opening(op), **extra)


def _retire(op: dict, reason: str) -> bool:
    """Retire the opening's output authority (in the caller's transaction).
    A tentative initiative is cancelled (its unclaimed row withdrawn); a
    committed outcome is kept in the record but fenced."""
    if op.get("retired") or op["state"] == "fallback":
        return False
    if op["state"] in OPEN_STATES:
        op["state"] = "retired"
    elif op["state"] == "initiative_queued":
        if _cancel_or_lose(op):
            op["state"] = "retired"
        else:
            op["state"] = "initiative_started"
    op["retired"] = {"reason": reason, "at": now_ms()}
    return True


def _withdraw(op: dict) -> bool:
    """Withdraw the admitted, unclaimed initiative row (same transaction)."""
    cursor = db.conn().execute(
        """UPDATE queued_turns SET status = 'withdrawn', text = ''
            WHERE queue_id = ? AND status = 'queued' AND claimed_at IS NULL""",
        (op["intent"]["admission_id"],))
    return cursor.rowcount == 1


def _cancel_or_lose(op: dict) -> bool:
    """A greeting (or speech onset, or retire) against initiative_queued:
    True when the initiative is cancelled (no row, or its row withdrawn),
    False when the dispatcher already claimed it (it is started)."""
    intent = op["intent"]
    if intent["state"] == "intended":
        intent["state"] = "cancelled"
        return True
    if intent["state"] == "admitted" and _withdraw(op):
        intent["state"] = "cancelled"
        return True
    intent["state"] = "started"
    return False


def _check_live(op: dict, box: dict) -> bool:
    """Retire on a Host instance change; refuse a retired opening."""
    if not op.get("retired") and op["server_id"] != _server_id():
        _retire(op, "host_changed")
        box["dirty"] = True
    if op.get("retired"):
        box["error"] = _refuse(op, 409, "opening retired", reason=op["retired"]["reason"])
        return False
    return True


def _header(op: dict, cause: str) -> str:
    from .group_calls import opening_header
    return opening_header(op["reason"], cause, op["silence_ms"])


def _intend(op: dict, cause: str) -> None:
    previous = op.get("intent") or {}
    op["state"] = "initiative_queued"
    op["intent"] = {"kind": "initiative", "admission_id": CALL_OPEN + op["ring_id"],
                    "gen": int(previous.get("gen") or 0) + 1, "state": "intended", "cause": cause,
                    "trace_id": "callopen-" + secrets.token_hex(6)}


def _fingerprint(body: dict) -> str:
    keys = ("token", "epoch", "claim", "expected_epoch", "owner_nonce", "reason")
    return json.dumps({k: body.get(k) for k in keys}, sort_keys=True)


def _validate(body: dict) -> str:
    claim = body.get("claim")
    if not isinstance(claim, str) or claim not in CLAIMS:
        raise CallError(400, "bad claim")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise CallError(400, "bad claim")
    if claim == "renew":
        if _int(body.get("expected_epoch")) is None or not isinstance(body.get("owner_nonce"), str):
            raise CallError(400, "bad claim")
    elif _int(body.get("epoch")) is None:
        raise CallError(400, "bad claim")
    return claim


def opening_claim(principal: str, body: dict) -> dict:
    """``POST /calls/ring/opening``: the phone reports readiness, speech onset,
    an abandoned utterance, no speech by its deadline, no mic, or renews the
    epoch. Returns ``{ok, opening, changed}``; refusals dispatch nothing."""
    if not isinstance(body, dict):
        raise CallError(400, "bad claim")
    claim = _validate(body)
    ring_id = _ring_for_token(body.get("token"))
    request_id = body["request_id"]
    queued = False
    with _opening_txn(ring_id) as box:
        op = box["op"]
        if op is None or op.get("token") != body.get("token"):
            raise CallError(404, "unknown opening")
        if op["principal"] != principal:
            raise _refuse(op, 403, "not the answering device")
        seen = op["requests"].get(request_id)
        if seen is not None:
            if seen["fingerprint"] != _fingerprint(body):
                raise _refuse(op, 400, "bad claim")
            box["result"] = (seen.get("opening") if claim == "renew" else None, False)
        elif _check_live(op, box):
            queued = _apply_claim(op, claim, body, box)
            if box.get("error") is None:
                op["requests"][request_id] = {"fingerprint": _fingerprint(body), "at": now_ms()}
                if len(op["requests"]) > KEEP_REQUESTS:
                    oldest = sorted(op["requests"], key=lambda k: op["requests"][k]["at"])
                    for key in oldest[:len(op["requests"]) - KEEP_REQUESTS]:
                        op["requests"].pop(key, None)
                if claim == "renew":
                    op["requests"][request_id]["opening"] = _public_opening(op, secret=True)
                box["dirty"] = True
    if queued:
        _admit_after_resolve(ring_id)
    stored, changed = box.get("result", (None, True))
    if stored is not None:
        return {"ok": True, "opening": stored, "changed": changed}
    if changed:
        _audit({"outcome": "opening", "ring_id": ring_id, "claim": claim, "by": principal})
        _announce_opening(ring_id)
    return {"ok": True, "opening": _public_opening(opening_state(ring_id), secret=True), "changed": changed}


def _apply_claim(op: dict, claim: str, body: dict, box: dict) -> bool:
    """One claim against a live opening (inside its transaction). Returns
    True when it queued an initiative intent; refusals go into ``box``."""
    state = op["state"]
    if claim == "renew":
        if state not in OPEN_STATES:
            box["error"] = _refuse(op, 409, "already resolved", outcome=state)
        elif body["expected_epoch"] != op["epoch"]:
            box["error"] = _refuse(op, 409, "stale epoch")
        elif body["owner_nonce"] != op["owner_nonce"]:
            box["error"] = _refuse(op, 409, "owner changed")
        else:
            # The latch and its captured-turn authority survive a renewal.
            op["epoch"] += 1
            op["owner_nonce"] = "own_" + secrets.token_hex(16)
        return False
    if body["epoch"] != op["epoch"]:
        box["error"] = _refuse(op, 409, "stale epoch")
        return False
    if claim == "speech_started" and state == "initiative_queued":
        if _cancel_or_lose(op):
            op["state"] = "speech_latched"
            op["latch"] = {"epoch": op["epoch"], "at_ms": _int(body.get("at_ms")),
                           "capture_gen": _int(body.get("capture_gen"))}
        else:
            op["state"] = "initiative_started"
            box["error"] = _refuse(op, 409, "already resolved", outcome=op["state"])
        return False
    if state not in OPEN_STATES:
        box["error"] = _refuse(op, 409, "already resolved", outcome=state)
        return False
    if claim == "listening":
        op["capture"] = {"capture_gen": _int(body.get("capture_gen")), "at_ms": _int(body.get("at_ms"))}
        if state == "pending":
            op["state"] = "listening"
        return False
    if claim == "speech_started":
        if state != "speech_latched":
            op["state"] = "speech_latched"
            op["latch"] = {"epoch": op["epoch"], "at_ms": _int(body.get("at_ms")),
                           "capture_gen": _int(body.get("capture_gen"))}
        else:
            box["result"] = (None, False)
        return False
    if claim == "speech_abandoned":
        if state != "speech_latched" or body.get("reason") not in ABANDON_REASONS:
            box["error"] = _refuse(op, 400, "bad claim")
            return False
        _intend(op, "speech_abandoned")
        return True
    # silence / no_mic / not_ready
    if state == "speech_latched":
        box["error"] = _refuse(op, 409, "speech latched")
        return False
    if claim == "silence":
        at_ms, watermark = _int(body.get("at_ms")), _int(body.get("watermark_ms"))
        if at_ms is None or watermark is None or at_ms < op["silence_ms"] or watermark < op["silence_ms"]:
            box["error"] = _refuse(op, 400, "bad claim")
            return False
    _intend(op, claim)
    return True


def greeting(principal: str, *, token: Any, epoch: Any, client_msg_id: str, trace_id: str,
             session: str, call_id: str) -> dict:
    """The phone's first captured utterance (``/send`` with ``call_opening``).

    Returns ``{route, session, opening, changed}``: ``greeting`` resolves the
    opening with this message, ``ordinary`` when the initiative already
    started (the utterance is then an ordinary call utterance). The message
    itself is admitted by the caller's ordinary dispatch, deduplicated by its
    client_msg_id. Refusals (CallError) dispatch nothing."""
    if _int(epoch) is None:
        raise CallError(400, "bad call_opening")
    ring_id = _ring_for_token(token)
    with _opening_txn(ring_id) as box:
        op = box["op"]
        if op is None or op.get("token") != token:
            raise CallError(404, "unknown opening")
        if op["principal"] != principal:
            raise _refuse(op, 403, "not the answering device")
        mine = (op.get("greeting") or {}).get("client_msg_id") == client_msg_id
        if mine and not op.get("retired") and op["state"] == "greeting":
            box["result"] = ("greeting", False)
        elif _check_live(op, box):
            if session != op["session"] or call_id != op["call_id"]:
                box["error"] = _refuse(op, 409, "wrong call")
            elif epoch != op["epoch"]:
                box["error"] = _refuse(op, 409, "stale epoch")
            elif op["state"] in ("greeting", "fallback"):
                box["error"] = _refuse(op, 409, "already resolved", outcome=op["state"])
            else:
                started = op["state"] == "initiative_started"
                if op["state"] == "initiative_queued":
                    started = not _cancel_or_lose(op)
                if started:
                    op["state"] = "initiative_started"
                    if not op.get("late_greeting"):
                        op["late_greeting"] = {"client_msg_id": client_msg_id, "trace_id": trace_id,
                                               "at": now_ms()}
                    box["result"] = ("ordinary", True)
                else:
                    op["state"] = "greeting"
                    op["greeting"] = {"client_msg_id": client_msg_id, "trace_id": trace_id,
                                      "admission": "pending", "at": now_ms()}
                    box["result"] = ("greeting", True)
                box["dirty"] = True
    route, changed = box["result"]
    op = opening_state(ring_id)
    if changed:
        _audit({"outcome": "opening", "ring_id": ring_id, "claim": "greeting", "route": route, "by": principal})
        _announce_opening(ring_id)
    return {"route": route, "session": op["session"], "opening": _public_opening(op), "changed": changed}


def greeting_admitted(ring_id: str, client_msg_id: str, *, ok: bool) -> None:
    """The greeting's ordinary dispatch returned: its admission is confirmed,
    or unknown on an ambiguous result (the phone's retry completes it)."""
    try:
        with _opening_txn(ring_id) as box:
            greeting_record = (box["op"] or {}).get("greeting") or {}
            if greeting_record.get("client_msg_id") == client_msg_id and greeting_record.get("admission") != "confirmed":
                greeting_record["admission"] = "confirmed" if ok else "unknown"
                box["dirty"] = True
    except Exception as exc:  # noqa: BLE001 - bookkeeping only
        log_exception("agentRingGreetingAdmitFail", exc, f"ring={ring_id}")


def admit_initiative(ring_id: str) -> str:
    """The admitter: in one transaction, insert the initiative's prompt
    admission and queued row and mark the intent admitted, but only while the
    intent is still ``intended`` (a greeting, retire or earlier admit fences
    it). Returns ``admitted``, ``adopted`` (an admission row already existed)
    or ``fenced``."""
    from . import prompt_admissions, turn_queue
    with _opening_txn(ring_id) as box:
        op = box["op"]
        intent = (op or {}).get("intent")
        if not intent or intent["state"] != "intended" or op.get("retired"):
            box["result"] = "fenced"
            return "fenced"
        admission_id = intent["admission_id"]
        con = db.conn()
        existing = con.execute(
            "SELECT admission_id FROM prompt_admissions WHERE agent_id = ? AND client_admission_id = ?",
            (op["agent_id"], admission_id)).fetchone()
        intent["state"] = "admitted"
        box["dirty"] = True
        if existing is not None:
            box["result"] = "adopted"
        else:
            header = _header(op, intent["cause"])
            admission = prompt_admissions.create(
                authenticated_at_admission=True, origin="automation", sender_agent_id="",
                channel="call", observed_at=now_ms(), client_admission_id=admission_id,
                trace_id=intent["trace_id"], original_text=header)
            admission_row = prompt_admissions.record(admission, agent_id=op["agent_id"], session=op["session"])
            turn_queue.enqueue(
                queue_id=admission_id, agent_id=op["agent_id"], session=op["session"], text=header,
                trace_id=intent["trace_id"], client_msg_id=admission_id, synthesize_audio=True,
                origin="automation", sender_agent_id="", prompt_admission_id=admission_row)
            # Answering the call is the owner's fresh intent: a Stop's leftover
            # pause must not hold the opening while the phone listens.
            con.execute("UPDATE queued_turns SET allow_paused = 1 WHERE queue_id = ?", (admission_id,))
            box["result"] = "admitted"
    _audit({"outcome": "opening", "ring_id": ring_id, "admission": box["result"]})
    return box["result"]


def _admit_after_resolve(ring_id: str) -> None:
    """Admission runs outside the resolve transaction; a crash in between
    leaves the intent ``intended`` for recovery."""
    if admit_initiative(ring_id) in ("admitted", "adopted"):
        _kick_queue()


def claim_initiative(queue_id: str) -> bool:
    """The dispatcher's claim of a call-open queued row, before it spawns:
    queued -> claimed and the intent -> started in one transaction. False
    when the row is not claimable (never admitted, withdrawn, or already
    claimed): the dispatcher then does not run it."""
    queue_id = str(queue_id or "")
    if not queue_id.startswith(CALL_OPEN):
        return True
    ring_id = queue_id[len(CALL_OPEN):]
    with _opening_txn(ring_id) as box:
        cursor = db.conn().execute(
            """UPDATE queued_turns SET status = 'claimed', claimed_at = ?
                WHERE queue_id = ? AND status = 'queued'""", (now_ms(), queue_id))
        if cursor.rowcount != 1:
            box["result"] = False
        else:
            op = box["op"]
            if op is not None and op.get("intent"):
                op["intent"]["state"] = "started"
                if op["state"] == "initiative_queued":
                    op["state"] = "initiative_started"
                box["dirty"] = True
            box["result"] = True
    if box["result"]:
        _announce_opening(ring_id)
    return box["result"]


def _recover_one(ring_id: str) -> int:
    op = opening_state(ring_id)
    if not op or (op.get("intent") or {}).get("state") != "intended" or op.get("retired"):
        return 0
    try:
        result = admit_initiative(ring_id)
    except Exception as exc:  # noqa: BLE001 - the next read or boot retries
        log_exception("agentRingOpeningRecoverFail", exc, f"ring={ring_id}")
        return 0
    if result in ("admitted", "adopted"):
        _kick_queue()
        return 1
    return 0


def recover_openings() -> int:
    """At Host start (and lazily per ring on read): admit every initiative
    whose intent is still ``intended``. Never acts on anything else."""
    return sum(_recover_one(op["ring_id"]) for op in _openings()
               if (op.get("intent") or {}).get("state") == "intended")


def _retire_where(match, reason: str) -> int:
    count = 0
    for op in _openings():
        if not match(op) or op.get("retired"):
            continue
        with _opening_txn(op["ring_id"]) as box:
            if box["op"] is not None and _retire(box["op"], reason):
                box["dirty"] = True
                count += 1
        _audit({"outcome": "opening", "ring_id": op["ring_id"], "retired": reason})
        _announce_opening(op["ring_id"])
    return count


def retire_for_call(call_id: str, reason: str) -> int:
    """The call ended, was replaced or superseded: its opening retires."""
    return _retire_where(lambda op: op.get("call_id") == call_id, reason)


def retire_for_principal(principal: str, reason: str) -> int:
    """The answering device was unpaired or its auth revoked."""
    return _retire_where(lambda op: op.get("principal") == principal, reason)


def _announce_opening(ring_id: str) -> None:
    """The ``agent-ring`` event with the opening's new state. Never raises."""
    stream = _context["stream"]
    if stream is None:
        return
    try:
        ring = next((r for r in _load() if r["ring_id"] == ring_id), None)
        if ring is None:
            return
        wire = public(ring)
        events.broadcast(stream, events.agent_ring(
            **{key: wire.get(key) for key in events.FIELDS[events.SSEType.AGENT_RING]}))
    except Exception as exc:  # noqa: BLE001
        log_exception("agentRingEmitFail", exc, f"ring={ring_id}")


# ---- opening audio (§11.B) ---------------------------------------------------------------

def _opening_for_trace(trace_id: str) -> tuple[dict, str] | None:
    if not trace_id:
        return None
    for op in _openings():
        intent = op.get("intent") or {}
        if intent.get("trace_id") == trace_id:
            return op, "initiative"
        if (op.get("greeting") or {}).get("trace_id") == trace_id:
            return op, "greeting_reply"
    return None


def opening_audio(trace_id: str) -> dict | None:
    """``{call_id, opening}`` for a clip of an opening turn, else None
    (ordinary follow-ups are never tagged)."""
    found = _opening_for_trace(str(trace_id or ""))
    if found is None:
        return None
    op, kind = found
    admission_id = op["intent"]["admission_id"] if kind == "initiative" else op["greeting"]["client_msg_id"]
    return {"call_id": op["call_id"], "opening": {"ring_id": op["ring_id"], "token": op["token"],
                                                  "epoch": op["epoch"], "kind": kind,
                                                  "admission_id": admission_id}}


def skip_opening_synthesis(trace_id: str) -> bool:
    """Best effort: do not voice an opening clip whose output authority is
    gone (retired), or an initiative that was cancelled or that a greeting
    overtook after it started. The phone's fence stays authoritative."""
    found = _opening_for_trace(str(trace_id or ""))
    if found is None:
        return False
    op, kind = found
    if op.get("retired"):
        return True
    return kind == "initiative" and (op["intent"]["state"] == "cancelled" or bool(op.get("late_greeting")))
