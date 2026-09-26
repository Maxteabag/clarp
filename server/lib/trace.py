"""Per-turn trace id: the single minting point and the single parser.

A trace id stitches together every event emitted during one user turn
(transcribe → /send → clarp spawn → hook fires → SSE broadcast). The
authoritative store is the `traces` table in SQLite (see
`agents.get_trace` / `agents.set_trace`); this module mints fresh ids and
recognises the ones already on disk.

Canonical format: 16 lowercase hex characters (64 bits). Two older shapes
still exist in stored rows and are accepted by ``parse_trace_id`` so history
stays readable; nothing mints them any more:

* a bare uuid4 (restart prompts before the architecture program)
* ``dream-<16 hex>`` (dream runs before the architecture program)

``LEGACY_TRACE_ID_RE`` can go once no stored row carries either shape.
"""
from __future__ import annotations

import re
import secrets

TRACE_ID_HEX_CHARS = 16
TRACE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
LEGACY_TRACE_ID_RE = re.compile(
    r"^(?:dream-[0-9a-f]{16}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def new_trace_id() -> str:
    """16 hex chars — enough entropy for ~3 events/sec without collision."""
    return secrets.token_hex(TRACE_ID_HEX_CHARS // 2)


def parse_trace_id(value: object) -> str | None:
    """Return the trace id carried by ``value`` or None.

    Accepts the canonical 16-hex form and the two legacy forms still present
    in stored rows. Whitespace is stripped and hex is lower-cased; anything
    else (empty, wrong length, non-hex, non-string) is None so callers never
    thread an unparsed string further in.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if TRACE_ID_RE.match(candidate) or LEGACY_TRACE_ID_RE.match(candidate):
        return candidate
    return None


def is_canonical(value: object) -> bool:
    return isinstance(value, str) and bool(TRACE_ID_RE.match(value))

