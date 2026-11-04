"""Per-key lock table for synthesized voice previews and controller narration:
one synthesis per cache key at a time, so two requests for the same voice
never call the provider twice. The table is this module's own state
(module-state guard: LOCKS); server.py imports the accessor only."""
from __future__ import annotations

import threading

_CARTESIA_PREVIEW_LOCKS: dict[str, threading.Lock] = {}
_CARTESIA_PREVIEW_LOCKS_GUARD = threading.Lock()


def _cartesia_preview_lock(voice_id: str) -> threading.Lock:
    with _CARTESIA_PREVIEW_LOCKS_GUARD:
        return _CARTESIA_PREVIEW_LOCKS.setdefault(voice_id, threading.Lock())
