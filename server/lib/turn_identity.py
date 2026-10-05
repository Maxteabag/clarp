"""Who is calling: the Host-issued identity of the turn a Clarp tool runs in.

Every per-turn provider process (Claude, AGY, Grok, OpenCode, Codex exec) gets
a fresh token in its environment, recorded in ``provider_turns`` with its agent
(lib.provider_background_jobs). Clarp tools inside that turn inherit it, and so
do detached workers launched from it. The goal store uses it to decide who is
acting, instead of a session name the caller types.

Limits: agents share one Unix user, so a process that deliberately reads
another process's environment, or writes the database directly, is not stopped
by this. The shared Codex app-server has no per-turn process and so no token.
"""
from __future__ import annotations

import os

from . import db
from .provider_background_jobs import TURN_ENV, _TOKEN

# Backends whose every turn carries a token. Codex runs through one shared
# app-server, so its agents cannot prove who they are.
IDENTITY_BACKENDS = frozenset({"claude", "agy", "grok", "opencode", "deepseek"})


def caller_agent_id(environ=None) -> str:
    """The agent whose turn issued this process's token, or ""."""
    token = (os.environ if environ is None else environ).get(TURN_ENV, "")
    if not _TOKEN.fullmatch(token or ""):
        return ""
    row = db.conn().execute("SELECT agent_id FROM provider_turns WHERE turn_token=?",
                            (token,)).fetchone()
    return str(row[0]) if row else ""
