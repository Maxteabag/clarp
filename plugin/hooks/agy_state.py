#!/usr/bin/env python3
"""Observe AGY lifecycle events without changing its execution decisions."""
import json
import os
import sqlite3
import sys


def _clarp_conversation(conversation_id: str) -> bool:
    """Cheap read-only check before loading Clarp: agy fires these hooks on
    every model call of every agy run on the machine (Clarp's own one-shots
    included), and only conversations a live Clarp agent owns matter. Unsure
    means yes, so the full check decides."""
    database = os.environ.get("CLAUDE_PWA_DB", "")
    if not conversation_id or not database:
        return bool(conversation_id)
    try:
        con = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.5)
        try:
            return con.execute(
                "SELECT 1 FROM runtimes WHERE backend_session_id = ? AND ended_at IS NULL"
                " LIMIT 1", (conversation_id,)).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return True


try:
    payload = json.load(sys.stdin)
    # stream-json owns every turn Clarp starts; they carry this marker.
    if os.environ.get("CLARP_AGY_MANAGED_TURN") == "1":
        payload = None
    if isinstance(payload, dict) and _clarp_conversation(
            str(payload.get("conversationId") or "")):
        import _clarp_lib  # noqa: F401
        from lib.backend.agy_hooks import record_event

        record_event(sys.argv[1] if len(sys.argv) > 1 else "", payload)
except Exception:
    # An unavailable Clarp install must never prevent terminal work.
    pass

# No permission, context injection, or continuation decisions.
print("{}")
