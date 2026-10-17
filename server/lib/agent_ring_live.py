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
import json
import pathlib
import re
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
OWNER_CONFLICT = "live_owner_conflict"
UNAVAILABLE = "live_unavailable"
FALLBACK_NOT_ALLOWED = "live_fallback_not_allowed"

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
    try:
        raw = (folder / pin["file"]).read_bytes()
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
