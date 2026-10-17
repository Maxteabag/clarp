"""Agent-live rings (feature ``agent_ring_live``): a ring held as a live voice call.

A ring may ask for ``conversation_mode: agent_live_v1``. Answered by a phone
that asks for it, the call runs on OpenAI GPT-Live (``gpt-live-1``, voice
``marin``) instead of the classic group call with a pre-voiced opening line.
The wire is WIRE-v2 (decisions, schema) with the event context of CONTEXT-v1.

Everything the voice model is told is built here, on the Host, when the ring
is admitted, and frozen with that ring: an operator-approved profile, pinned by
sha256 to its files and bound to the ringing agent, plus the producer's
``event_context``, which is observation data only. A client never supplies a
path or a prompt, and a later edit to a profile file never reaches a ring that
already exists (a changed pin refuses new rings instead).

This module holds no ring state; ``agent_rings`` stores the snapshot on the
ring record (internal, never on the wire).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import re
import threading
import time
from datetime import datetime
from typing import Any

from .deployment import LAYOUT
from .group_calls import CallError
from .log import log_exception

FEATURE = "agent_ring_live"
MODE_CLASSIC = "classic"
MODE_LIVE = "agent_live_v1"
MODES = (MODE_CLASSIC, MODE_LIVE)
MODEL = "gpt-live-1"
VOICE = "marin"
PROVIDER = "openai"
PROTOCOL = 1

# The fixed refusal strings of WIRE-v2: the HTTP body's "error" where the
# route owns the response, and agent_live.error's "code" on the socket.
MODE_INVALID = "live_mode_invalid"
CONTEXT_INVALID = "live_context_invalid"
CONTEXT_TOO_LARGE = "live_context_too_large"
CONTEXT_UNAVAILABLE = "live_context_unavailable"
CONTEXT_CONFLICT = "live_context_conflict"
CLIENT_REQUIRED = "live_client_required"
MODE_CONFLICT = "live_mode_conflict"
CALL_STALE = "live_call_stale"
OWNER_CONFLICT = "live_owner_conflict"
UNAVAILABLE = "live_unavailable"
FALLBACK_NOT_ALLOWED = "live_fallback_not_allowed"
AUTH = "live_auth"

# Profiles live under the Host's data root; tests point this elsewhere.
ROOT = LAYOUT.data_root / "clarp" / "agent-live"

# CONTEXT-v1: an explicit allowlist with fixed bounds; nothing is truncated.
CONTEXT_BYTES = 32 * 1024
SNAPSHOT_BYTES = 64 * 1024
STORIES = ("bottle", "pump")
SOURCES = ("leak-watch", "rk-demo-on-leak-event")
_TOP_KEYS = {"version", "profile_id", "story", "source", "event", "opening_text"}
_EVENT_LIMITS = {"id": 128, "time": 64, "camera_id": 128, "camera_label": 256, "when_text": 64,
                 "what_text": 8192}
_EVENT_REQUIRED = ("id", "time", "camera_id", "what_text")
_META_LIMITS = {"room": 512, "item": 512, "seen": 8192}
LOG_LINES, LOG_LINE_CHARS, OPENING_TEXT_CHARS = 64, 512, 600
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def mode_of(value: Any, *, default: str | None = MODE_CLASSIC) -> str | None:
    """A request's conversation_mode; absent means ``default``, unknown is 400."""
    if value is None:
        return default
    if value not in MODES:
        raise CallError(400, MODE_INVALID)
    return value


def provider_problem() -> str | None:
    """Why the fixed Live provider cannot be used on this Host, or None.

    Only the public OpenAI wire gives gpt-live-1 with marin; the subscription
    wire is a different model and voice, so it is refused, never substituted.
    """
    from . import config
    cfg = config.load()          # only the OpenAI key; never the global Oracle voice settings
    if not cfg.openai_key():
        return "agent-live calls need an OpenAI API key on this Host"
    return None


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# ---- event_context (CONTEXT-v1) ---------------------------------------------------

def _string(value: Any, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CallError(400, CONTEXT_INVALID)
    if len(value) > limit:
        raise CallError(413, CONTEXT_TOO_LARGE)
    if required and not value.strip():
        raise CallError(400, CONTEXT_INVALID)
    return value


def validate_context(context: Any, opening_line: str) -> dict:
    """The producer's event_context, checked against CONTEXT-v1 (raises CallError).

    Unknown keys are refused at every level, oversized values are 413 and never
    cut, and ``opening_text`` must be the ring's own opening line."""
    if not isinstance(context, dict):
        raise CallError(400, CONTEXT_INVALID)
    if len(canonical(context)) > CONTEXT_BYTES:
        raise CallError(413, CONTEXT_TOO_LARGE)
    if set(context) != _TOP_KEYS:
        raise CallError(400, CONTEXT_INVALID)
    if type(context["version"]) is not int or context["version"] != 1:
        raise CallError(400, CONTEXT_INVALID)
    if not isinstance(context["profile_id"], str) or not _PROFILE_ID.fullmatch(context["profile_id"]):
        raise CallError(400, CONTEXT_INVALID)
    if context["story"] not in STORIES or context["source"] not in SOURCES:
        raise CallError(400, CONTEXT_INVALID)
    event = context["event"]
    if not isinstance(event, dict) or not set(event) <= {*_EVENT_LIMITS, "log", "meta"}:
        raise CallError(400, CONTEXT_INVALID)
    if not set(_EVENT_LIMITS) | {"log"} <= set(event):
        raise CallError(400, CONTEXT_INVALID)
    for key, limit in _EVENT_LIMITS.items():
        _string(event[key], limit, required=key in _EVENT_REQUIRED)
    try:
        when = datetime.fromisoformat(event["time"])
    except ValueError:
        raise CallError(400, CONTEXT_INVALID) from None
    if when.tzinfo is None:
        raise CallError(400, CONTEXT_INVALID)
    log = event["log"]
    if not isinstance(log, list):
        raise CallError(400, CONTEXT_INVALID)
    if len(log) > LOG_LINES:
        raise CallError(413, CONTEXT_TOO_LARGE)
    for line in log:
        _string(line, LOG_LINE_CHARS)
    meta = event.get("meta", {})
    if not isinstance(meta, dict) or not set(meta) <= set(_META_LIMITS):
        raise CallError(400, CONTEXT_INVALID)
    for key, value in meta.items():
        _string(value, _META_LIMITS[key])
    opening_text = _string(context["opening_text"], OPENING_TEXT_CHARS, required=True)
    if " ".join(opening_text.split()) != opening_line:
        raise CallError(400, CONTEXT_INVALID)
    return json.loads(canonical(context))


# ---- the trusted profile and the frozen snapshot -----------------------------------

def _profile(profile_id: str, agent: dict) -> tuple[pathlib.Path, dict]:
    """The operator-approved profile, only when it belongs to the ringing agent."""
    folder = ROOT / "profiles" / profile_id
    try:
        data = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CallError(409, CONTEXT_UNAVAILABLE) from None
    if (not isinstance(data, dict) or data.get("profile_id") != profile_id
            or not data.get("agent_session") or data.get("agent_session") != agent.get("session")):
        raise CallError(409, CONTEXT_UNAVAILABLE)
    return folder, data


def _pinned_text(folder: pathlib.Path, pin: Any) -> tuple[str, str]:
    if not isinstance(pin, dict) or not isinstance(pin.get("file"), str) or not isinstance(pin.get("sha256"), str):
        raise CallError(409, CONTEXT_UNAVAILABLE)
    if not _FILE_NAME.fullmatch(pin["file"]) or not _SHA256.fullmatch(pin["sha256"]):
        raise CallError(409, CONTEXT_UNAVAILABLE)
    path = folder / pin["file"]
    try:
        if not path.resolve().is_relative_to(folder.resolve()):    # no symlink out of the profile
            raise CallError(409, CONTEXT_UNAVAILABLE)
        if path.stat().st_size > SNAPSHOT_BYTES:                   # bounded before anything is read
            raise CallError(409, CONTEXT_UNAVAILABLE)
        with path.open("rb") as handle:
            raw = handle.read(SNAPSHOT_BYTES + 1)
    except OSError:
        raise CallError(409, CONTEXT_UNAVAILABLE) from None
    if hashlib.sha256(raw).hexdigest() != pin["sha256"]:
        raise CallError(409, CONTEXT_UNAVAILABLE)
    try:
        return raw.decode("utf-8"), pin["sha256"]
    except UnicodeDecodeError:
        raise CallError(409, CONTEXT_UNAVAILABLE) from None


def build_snapshot(agent: dict, context: dict) -> dict:
    """The frozen context of one Live ring from a validated event_context.

    Refuses (409 live_context_unavailable) a missing profile, a profile of
    another agent, a story the profile does not approve, a file whose pin no
    longer matches, or a snapshot over its bound; it never trims one."""
    problem = provider_problem()
    if problem:
        raise CallError(503, UNAVAILABLE)
    folder, profile = _profile(context["profile_id"], agent)
    stories, files = profile.get("stories"), profile.get("files")
    if not isinstance(stories, dict) or not isinstance(files, dict):
        raise CallError(409, CONTEXT_UNAVAILABLE)
    keys = stories.get(context["story"])
    if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or k not in files for k in keys):
        raise CallError(409, CONTEXT_UNAVAILABLE)
    texts, pins = {}, {}
    for key in keys:
        texts[key], pins[key] = _pinned_text(folder, files[key])
    from .group_calls import _host_id
    snapshot = {
        "schema": 1, "host_id": _host_id(),
        "profile": {"profile_id": context["profile_id"], "version": str(profile.get("version") or ""),
                    "display_name": str(profile.get("display_name") or agent.get("persona") or ""),
                    "source": profile.get("source") if isinstance(profile.get("source"), dict) else {},
                    "files": pins},
        "agent": {"agent_id": str(agent.get("agent_id") or ""), "session": str(agent.get("session") or "")},
        "story": context["story"], "texts": texts, "event_context": context,
        "opening_text": context["opening_text"], "language": "nb-NO",
        "provider": PROVIDER, "model": MODEL, "voice": VOICE,
    }
    if len(canonical(snapshot)) > SNAPSHOT_BYTES:
        raise CallError(409, CONTEXT_UNAVAILABLE)
    return snapshot


def snapshot_of(ring_id: str) -> dict | None:
    """The frozen snapshot of one ring (Host-internal; never served to clients)."""
    from . import agent_rings
    with agent_rings._lock:
        ring = next((r for r in agent_rings._load() if r["ring_id"] == ring_id), None)
    return json.loads(canonical(ring["_live_snapshot"])) if ring and ring.get("_live_snapshot") else None


# ---- owner controls: close and fallback (WIRE-v2 SCHEMA) ---------------------------
#
# A Live call's state lives on its ring record (internal ``_live_*`` fields):
# ``_live_phase`` is answered_waiting_join → connecting → ready → active, then
# retired, or classic after the one fallback. Authority is always retired on the
# record first; only then is the running session (if any) told to stop, so a
# late frame or callback can never act for a retired or successor call.

REQUEST_INVALID = "live_request_invalid"
CLOSE_REASONS = ("user_hangup", "audio_not_ready", "transport_lost")
LIVE_PHASES = ("answered_waiting_join", "connecting", "ready", "active")
FALLBACK_STOP_WAIT_S = 3.0      # the Live session must confirm its stop this soon before classic starts
_SESSIONS_LOCK = threading.Lock()
_SESSIONS: dict = {}            # call_id → the running session (registered by the socket)


def device_ok(principal: str) -> bool:
    """A paired device still holds full scope and is not revoked. Any other principal
    (the administrator token included) is never a device: False."""
    if not principal.startswith("device_"):
        return False
    from . import db
    row = db.conn().execute("SELECT scope,revoked_at FROM paired_devices WHERE device_id=?", (principal,)).fetchone()
    return row is not None and row["revoked_at"] is None and row["scope"] == "full"


def _session_for(call_id: str):
    with _SESSIONS_LOCK:
        return _SESSIONS.get(call_id)


def _stop_session(call_id: str, reason: str) -> None:
    session = _session_for(call_id)
    if session is not None:
        session.retire(reason)


def _owned(records: list[dict], principal: str, data: Any) -> dict:
    """The Live ring this owner's request is about (under agent_rings._lock).

    Any mismatch of call, owner, nonce or attempt is the same 409 so that a
    stranger learns nothing about another call."""
    if not principal.startswith("device_") or not device_ok(principal):
        raise CallError(403, AUTH)                    # only a paired full-scope device owns Live
    if not isinstance(data, dict):
        raise CallError(400, REQUEST_INVALID)
    call_id, nonce, attempt = data.get("call_id"), data.get("owner_nonce"), data.get("attempt_id")
    if not all(isinstance(v, str) and v for v in (call_id, nonce, attempt)):
        raise CallError(400, REQUEST_INVALID)
    ring = next((r for r in records if r.get("_live_call_id") == call_id), None)
    if (ring is None or ring.get("answered_by") != principal
            or not hmac.compare_digest(str(ring.get("_live_owner_nonce") or ""), nonce)
            or ring.get("_live_attempt_id") != attempt):
        raise CallError(409, OWNER_CONFLICT)
    return ring


def _epoch(data: dict, key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value < 1:
        raise CallError(400, REQUEST_INVALID)
    return value


def retire(ring: dict, reason: str) -> bool:
    """Retire a Live call's authority on its record (caller saves). True if it changed."""
    if ring.get("_live_phase") not in LIVE_PHASES:
        return False
    ring.update(_live_phase="retired", _live_retired_reason=reason)
    return True


def close(principal: str, data: Any) -> dict:
    """The owner hangs up: retire the call's authority first, then stop its session.

    The accepted close is fingerprinted (request_id, call_epoch, reason) and its
    immutable receipt saved; an exact replay returns that saved receipt (also
    after a later fallback), the same request_id with another body is 400
    live_request_invalid, and any other close of an ended call or with another
    epoch is 409 live_call_stale. Stopping the session never depends on the audit
    write; a failed save still stops the session before its error is reported."""
    from . import agent_rings
    changed, failure, stale = False, None, False
    with agent_rings._lock:
        records = agent_rings._load()
        ring = _owned(records, principal, data)
        epoch = _epoch(data, "call_epoch")
        if data.get("reason") not in CLOSE_REASONS or not isinstance(data.get("request_id"), str) \
                or not 1 <= len(data["request_id"].encode("utf-8")) <= 128:
            raise CallError(400, REQUEST_INVALID)
        receipt = ring.get("_live_close_receipt") or {}
        if receipt and receipt.get("request_id") == data["request_id"]:
            if (receipt.get("call_epoch"), receipt.get("reason")) != (epoch, data["reason"]):
                raise CallError(400, REQUEST_INVALID)             # the same close request, another intent
            result = dict(receipt["result"])                       # the saved receipt, never input fields
        elif epoch != ring["_live_call_epoch"] or ring.get("_live_phase") not in LIVE_PHASES:
            stale = True                                           # ended another way, or another epoch
        else:
            result = {"ok": True, "call_id": ring["_live_call_id"], "call_epoch": epoch, "state": "retired"}
            retire(ring, data["reason"])
            changed = True
            ring["_live_close_receipt"] = {"request_id": data["request_id"], "reason": data["reason"],
                                           "call_epoch": epoch, "result": result}
            try:
                agent_rings._save(records)
            except Exception as exc:  # noqa: BLE001 - reported below, after the session is stopped
                failure = exc
            else:
                _audit_quietly({"outcome": "live_closed", "ring_id": ring["ring_id"], "by": principal,
                                "live_call_id": ring["_live_call_id"], "reason": data["reason"]})
    if changed or ring.get("_live_phase") not in LIVE_PHASES:
        # Also on a replayed or stale close of an ended call: a session that outlived it
        # (registered late, or its stop was lost) is stopped now. A no-op when none runs.
        _stop_session(ring["_live_call_id"], data["reason"])
    if failure is not None:
        raise failure
    if stale:
        raise CallError(409, CALL_STALE)
    return result


def fallback(principal: str, data: Any) -> dict:
    """The one explicit switch of a Live-answered ring to the classic call.

    Allowed once, only while the Live opening is not consumed (consumed the moment
    its instruction append is attempted, so never after an uncertain admission)
    and before any user audio went upstream, and only with the phone's word that
    its Live audio has stopped. Ordered (H2-FALLBACK-TRANSITION-DESIGN.md):
    T1 under the ring lock: verify, retire Live, bump the epoch (Live authority is
    gone from here) and save; T2 stop the Live session and confirm it stopped;
    T3 voice the classic opening; T4 start the classic group call (idempotent on
    the ring id); T5 under the lock: mark it classic. No network work happens under
    the ring lock. A failure after T1 answers 503 live_unavailable and leaves the
    record falling_back: Live stays revoked, nothing classic runs twice, and the
    same request_id resumes at T2."""
    from . import agent_rings, group_calls
    from . import agents as agents_db
    with agent_rings._lock:                                            # T1
        records = agent_rings._load()
        ring = _owned(records, principal, data)
        epoch = _epoch(data, "expected_epoch")
        if data.get("audio_stopped") is not True or not isinstance(data.get("request_id"), str) \
                or not data["request_id"]:
            raise CallError(400, REQUEST_INVALID)
        phase = ring.get("_live_phase")
        if phase in ("classic", "falling_back"):
            if data["request_id"] != ring.get("_live_fallback_request_id"):
                raise CallError(409, FALLBACK_NOT_ALLOWED)
        else:
            if epoch != ring["_live_call_epoch"]:
                raise CallError(409, CALL_STALE)
            if ring.get("_live_opening_consumed") or ring.get("_live_audio_forwarded"):
                raise CallError(409, FALLBACK_NOT_ALLOWED)
            if phase not in LIVE_PHASES and phase != "retired":
                raise CallError(409, FALLBACK_NOT_ALLOWED)
            retire(ring, "fallback")
            ring.update(_live_phase="falling_back", _live_call_epoch=epoch + 1,
                        _live_fallback_request_id=data["request_id"])
            agent_rings._save(records)
            _audit_quietly({"outcome": "live_fallback_started", "ring_id": ring["ring_id"], "by": principal,
                            "live_call_id": ring["_live_call_id"]})
        if phase == "classic":
            out = agent_rings._answered(ring, group_calls.get_call(ring["call_id"]), changed=False)
            call_id = ring["_live_call_id"]
        else:
            out = None
        snapshot = dict(ring)
    if out is not None:                                                # an earlier fallback, repeated
        _stop_session(call_id, "fallback")
        return {**out, "conversation_mode": MODE_CLASSIC}
    call_id = snapshot["_live_call_id"]
    _stop_session(call_id, "fallback")                                 # T2
    deadline = time.monotonic() + FALLBACK_STOP_WAIT_S
    while _session_for(call_id) is not None:
        if time.monotonic() >= deadline:
            log_exception("agentLiveFallbackStopTimeout", RuntimeError("Live session did not stop"),
                          f"call={call_id}")
            raise CallError(503, UNAVAILABLE)
        time.sleep(0.02)
    audio_url = snapshot.get("opening_audio_url") or ""
    if not audio_url:                                                  # T3 (never fatal)
        agent = agents_db.get_by_agent_id(snapshot["agent_id"]) or {"session": snapshot["session"]}
        audio_url = agent_rings._voice(snapshot["opening_line"], agent)
    try:                                                               # T4
        started = group_calls.start(
            principal, [snapshot["session"]], request_id=snapshot["ring_id"], by=snapshot["session"],
            ring={"ring_id": snapshot["ring_id"], "reason": snapshot["reason"],
                  "opening_line": snapshot["opening_line"]})
    except Exception as exc:  # noqa: BLE001 - truthful 503; the same request resumes here
        log_exception("agentLiveFallbackStartFail", exc, f"call={call_id}")
        raise CallError(503, UNAVAILABLE) from None
    call = started["call"]
    with agent_rings._lock:                                            # T5
        records = agent_rings._load()
        ring = next(r for r in records if r.get("_live_call_id") == call_id)
        if ring.get("_live_phase") != "falling_back" or \
                ring.get("_live_fallback_request_id") != data["request_id"]:
            raise CallError(409, FALLBACK_NOT_ALLOWED)
        ring.update(_live_phase="classic", call_id=call["call_id"], opening_audio_url=audio_url)
        try:
            agent_rings._save(records)
        except Exception as exc:  # noqa: BLE001 - F7: compensate by the original id, never pretend rollback
            log_exception("agentLiveFallbackFinalSaveFail", exc, f"call={call_id} classic={call['call_id']}")
            save_failed = True
        else:
            save_failed = False
    if save_failed:
        try:
            group_calls.end(principal, call_id=call["call_id"])   # exactly the call this request started
        except Exception as exc:  # noqa: BLE001
            log_exception("agentLiveFallbackCompensateFail", exc, f"classic={call['call_id']}")
        raise CallError(503, UNAVAILABLE)                          # Live stays revoked; same request resumes
    _audit_quietly({"outcome": "live_fallback", "ring_id": ring["ring_id"], "by": principal,
                    "live_call_id": call_id, "call_id": call["call_id"]})
    return {**agent_rings._answered(ring, call, changed=True), "conversation_mode": MODE_CLASSIC}


def _audit_quietly(entry: dict) -> None:
    """The audit trail is diagnostics: it never blocks or undoes a transition."""
    from . import agent_rings
    try:
        agent_rings._audit(entry)
    except Exception as exc:  # noqa: BLE001
        log_exception("agentLiveAuditFail", exc, str(entry.get("outcome")))


def reset_for_tests() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
