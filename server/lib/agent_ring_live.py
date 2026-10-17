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
from datetime import datetime
from typing import Any

from .deployment import LAYOUT
from .group_calls import CallError

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
JOIN_CONSUMED = "live_join_consumed"
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
        raw = path.read_bytes()
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
_SESSIONS_LOCK = threading.Lock()
_SESSIONS: dict = {}            # call_id → the running session (registered by the socket)


def device_ok(principal: str) -> bool:
    """A paired device still holds full scope and is not revoked (as Oracle voice)."""
    if not principal.startswith("device_"):
        return True
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
    """Retire a Live call's authority on its record (caller saves). True if it changed.

    The retirement is also marked in memory at once (callers hold agent_rings._lock), so
    the running session drops provider output from this moment, before any save or stop."""
    if ring.get("_live_phase") not in LIVE_PHASES:
        return False
    ring.update(_live_phase="retired", _live_retired_reason=reason)
    _mark_retired(ring.get("_live_call_id"))
    return True


_RETIRED_LOCK = threading.Lock()
_RETIRED: dict = {}             # call_id → True, the retirements of this process (bounded)
_RETIRED_MAX = 4096


def _mark_retired(call_id: Any) -> None:
    if not isinstance(call_id, str) or not call_id:
        return
    with _RETIRED_LOCK:
        _RETIRED[call_id] = True
        while len(_RETIRED) > _RETIRED_MAX:
            _RETIRED.pop(next(iter(_RETIRED)))


def retired(call_id: str) -> bool:
    """This process retired the call's authority (cheap; checked per provider frame)."""
    with _RETIRED_LOCK:
        return call_id in _RETIRED


def _log_exception(name: str, exc: BaseException, detail: str = "") -> None:
    from .log import log_exception
    log_exception(name, exc, detail)


def _quietly(name: str, fn, *args) -> None:
    """Best-effort work after a stop (audit, logging): its own failure never escapes."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - nothing to report it with if logging itself fails
        try:
            from .log import log_exception
            import sys
            log_exception(name, sys.exc_info()[1])
        except Exception:  # noqa: BLE001
            pass


def _retire_saved_again(call_id: str, reason: str) -> bool:
    """After a failed save and the stop: record the retirement once more. True when it held."""
    from . import agent_rings
    try:
        with agent_rings._lock:
            records = agent_rings._load()
            stored = next((r for r in records if r.get("_live_call_id") == call_id), None)
            if stored is not None and retire(stored, reason):
                agent_rings._save(records)
        return True
    except Exception:  # noqa: BLE001
        return False


def close(principal: str, data: Any) -> dict:
    """The owner hangs up: retire the call's authority first, then stop its session.

    The stop runs in a finally once the call is retired (now or already), so neither
    a failed save nor logging can skip it, and a retried close still ends a session
    an earlier close failed to stop. The audit runs after the stop, best effort.
    The answer carries the ring's own epoch and actual state (classic after a fallback)."""
    from . import agent_rings
    call_id, stop, save_error = None, False, None
    try:
        with agent_rings._lock:
            records = agent_rings._load()
            ring = _owned(records, principal, data)
            epoch = _epoch(data, "call_epoch")
            if data.get("reason") not in CLOSE_REASONS or not isinstance(data.get("request_id"), str):
                raise CallError(400, REQUEST_INVALID)
            if epoch != ring["_live_call_epoch"] and ring.get("_live_phase") in LIVE_PHASES:
                raise CallError(409, CALL_STALE)
            call_id = ring["_live_call_id"]
            changed = retire(ring, data["reason"])
            stop = ring.get("_live_phase") not in LIVE_PHASES
            if changed:
                try:
                    agent_rings._save(records)
                except Exception as exc:  # noqa: BLE001 - kept; the stop below runs first
                    save_error = exc
            phase = ring.get("_live_phase")
            result = {"ok": True, "call_id": call_id, "call_epoch": ring["_live_call_epoch"],
                      "state": "classic" if phase == "classic" else "retired"}
    finally:
        if stop and call_id:
            _stop_session(call_id, data["reason"])
    if save_error is not None:
        _quietly("agentLiveCloseSaveFail", _log_exception, "agentLiveCloseSaveFail", save_error,
                 f"call={call_id}")
        if not _retire_saved_again(call_id, data["reason"]):
            raise CallError(503, UNAVAILABLE)
    if changed:
        _quietly("agentLiveAuditFail", _audit_quietly, {"outcome": "live_closed", "ring_id": ring["ring_id"],
                                                        "by": principal, "live_call_id": call_id,
                                                        "reason": data["reason"]})
    return result


def _audit_quietly(entry: dict) -> None:
    """An audit after the fact: its failure is logged and never undoes or skips what already happened."""
    from . import agent_rings
    from .log import log_exception
    try:
        with agent_rings._lock:
            agent_rings._audit(entry)
    except Exception as exc:  # noqa: BLE001
        log_exception("agentLiveAuditFail", exc, json.dumps(entry)[:300])


def fallback(principal: str, data: Any) -> dict:
    """The one explicit switch of a Live-answered ring to the classic call.

    Allowed once, only before the Live opening was admitted and before any user
    audio went upstream, and only with the phone's word that its Live audio has
    stopped. In this order (WIRE-v2 §10): under the lock, claim it and retire
    Live with an epoch bump and save; then, in a finally that a failed save
    cannot skip, stop the Live session; then voice and start the classic call
    and record its exact id; the audit last, best effort. A failed claim save
    stops Live and starts nothing classic. A classic call whose id cannot be
    recorded is ended by that exact id, so no unrecorded audio path is left.
    A failure after the stop leaves Live retired and no classic call."""
    from . import agent_rings, group_calls
    from . import agents as agents_db
    call_id, stop, replay, save_error = None, False, None, None
    try:
        with agent_rings._lock:
            records = agent_rings._load()
            ring = _owned(records, principal, data)
            epoch = _epoch(data, "expected_epoch")
            if data.get("audio_stopped") is not True or not isinstance(data.get("request_id"), str) \
                    or not data["request_id"]:
                raise CallError(400, REQUEST_INVALID)
            call_id = ring["_live_call_id"]
            if ring.get("_live_phase") == "classic":
                stop = True
                if data["request_id"] != ring.get("_live_fallback_request_id"):
                    raise CallError(409, FALLBACK_NOT_ALLOWED)
                replay = {**agent_rings._answered(ring, group_calls.get_call(ring["call_id"]), changed=False),
                          "conversation_mode": MODE_CLASSIC}
            else:
                if ring.get("_live_fallback_request_id"):   # claimed by a fallback in progress or one that failed
                    raise CallError(409, FALLBACK_NOT_ALLOWED)
                if epoch != ring["_live_call_epoch"]:
                    raise CallError(409, CALL_STALE)
                if ring.get("_live_opening_consumed") or ring.get("_live_audio_forwarded"):
                    raise CallError(409, FALLBACK_NOT_ALLOWED)
                if not retire(ring, "fallback") and ring.get("_live_phase") != "retired":
                    raise CallError(409, FALLBACK_NOT_ALLOWED)
                stop = True
                ring.update(_live_retired_reason="fallback", _live_call_epoch=epoch + 1,
                            _live_fallback_request_id=data["request_id"])
                try:
                    agent_rings._save(records)
                except Exception as exc:  # noqa: BLE001 - the stop below runs first; nothing classic follows
                    save_error = exc
    finally:
        if stop and call_id:
            _stop_session(call_id, "fallback")
    if replay is not None:
        return replay
    if save_error is not None:
        _quietly("agentLiveFallbackSaveFail", _log_exception, "agentLiveFallbackSaveFail", save_error,
                 f"call={call_id}")
        _retire_saved_again(call_id, "fallback")
        raise CallError(503, UNAVAILABLE)
    classic_id = None
    try:
        agent = agents_db.get_by_agent_id(ring["agent_id"]) or {"session": ring["session"]}
        audio_url = ring.get("opening_audio_url") or agent_rings._voice(ring["opening_line"], agent)
        with agent_rings._lock:
            records = agent_rings._load()
            ring = next(r for r in records if r.get("_live_call_id") == call_id)
            if ring.get("_live_fallback_request_id") != data["request_id"] or ring.get("_live_phase") != "retired":
                raise CallError(409, FALLBACK_NOT_ALLOWED)
            started = group_calls.start(
                principal, [ring["session"]], request_id=ring["ring_id"], by=ring["session"],
                ring={"ring_id": ring["ring_id"], "reason": ring["reason"], "opening_line": ring["opening_line"]})
            call = started["call"]
            classic_id = call["call_id"]
            ring.update(_live_phase="classic", call_id=classic_id, opening_audio_url=audio_url)
            agent_rings._save(records)
    except CallError:
        raise
    except Exception as exc:  # noqa: BLE001 - Live is already retired and stopped; say so, never retry
        if classic_id is not None:      # started but not recorded: end exactly that call
            _quietly("agentLiveFallbackOrphanEndFail", lambda: group_calls.end(
                principal, call_id=classic_id, principal=principal, by="agent-live-fallback"))
        _quietly("agentLiveFallbackFail", _log_exception, "agentLiveFallbackFail", exc,
                 f"call={call_id} classic={classic_id}")
        raise CallError(503, UNAVAILABLE) from None
    _quietly("agentLiveAuditFail", _audit_quietly, {"outcome": "live_fallback", "ring_id": ring["ring_id"],
                                                    "by": principal, "live_call_id": call_id,
                                                    "call_id": classic_id})
    return {**agent_rings._answered(ring, call, changed=True), "conversation_mode": MODE_CLASSIC}


def reset_for_tests() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
    with _RETIRED_LOCK:
        _RETIRED.clear()
