"""Which chat each paired client last opened.

The global `focus` row is shared by every client and is also moved by the
server itself (name-addressed or orchestrated messages make that agent the
sticky default). A recording belongs to the chat open on the device that
recorded it, so `/select` and `/focus` also remember the session per
authenticated principal, and `/transcribe` reads it back.
"""
from __future__ import annotations

import hashlib
import json

from . import db, settings_store
from .log import log_exception

PREFIX = "client-chat:"
_MAX_AGE_MS = 12 * 3600 * 1000


def _key(principal: str) -> str:
    return PREFIX + hashlib.sha256(principal.encode()).hexdigest()


def record(principal: str, session: str) -> None:
    if not principal or not session:
        return
    try:
        settings_store.set_text(_key(principal), json.dumps(
            {"session": session, "at": db.now_ms()}, separators=(",", ":")))
    except Exception as e:  # noqa: BLE001 — never fail a selection over this
        log_exception("clientChatRecordFail", e, detail=session)


def current(principal: str) -> str:
    """The session this principal last opened in the last 12 h, or ''."""
    if not principal:
        return ""
    try:
        value = json.loads(settings_store.get_text(_key(principal)) or "{}")
        if db.now_ms() - int(value.get("at", 0)) <= _MAX_AGE_MS:
            return str(value.get("session") or "")
    except (ValueError, TypeError) as e:
        log_exception("clientChatReadFail", e)
    return ""
