"""Moved out of server/lib/agent_rings.py by tools/refactor/split_apply.py (split 'agent-rings', recipe tools/refactor/splits/s9-agent-rings.toml).
agent_rings.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from . import settings_store
from .log import log


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
_context: dict[str, Any] = {"stream": None, "tts": None, "ctx": None}
_ABSENT = object()


def now_ms() -> int:
    return int(time.time() * 1000)


def bind(ctx) -> None:
    """Remember where events go and how to voice the opening line."""
    _context["stream"] = getattr(ctx, "stream", None)
    _context["tts"] = getattr(ctx, "tts", None)
    _context["ctx"] = ctx


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
