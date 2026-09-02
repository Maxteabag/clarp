"""Per-turn trace id generation.

A trace id stitches together every event emitted during one user turn
(transcribe → /send → clarp spawn → hook fires → SSE broadcast). The
authoritative store is the `traces` table in SQLite (see
`agents.get_trace` / `agents.set_trace`); this module just mints fresh
ids.
"""
from __future__ import annotations

import secrets


def new_id() -> str:
    """16 hex chars — enough entropy for ~3 events/sec without collision."""
    return secrets.token_hex(8)
