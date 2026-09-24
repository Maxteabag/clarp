"""Which agent the user currently has open.

Focus is stored in the `focus` table (`agents.set_focus`); `/select` also
mirrors it into the `current-session` cache file for anything outside the
server process. Readers inside the server must use the DB so that transcribe,
upload and the herald all agree on one answer.
"""
from __future__ import annotations

from . import agents as agents_db
from .log import log_exception


def current_focus_session() -> str:
    """Session id of the focused agent, or '' when there is none.

    Never raises: a focus lookup failure must not fail a voice request, the
    same way an unreadable focus file never did.
    """
    try:
        return agents_db.get_focus_session() or ""
    except Exception as e:  # noqa: BLE001
        log_exception("focusReadFail", e)
        return ""
