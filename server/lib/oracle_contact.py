"""The Host-owned Oracle contact: which agent Oracle hands investigations to.

The contact used to live only in the phone's UserDefaults and rode along on
every Oracle call as `oracle_session`. Nothing ever checked it against the
roster, so a session that had been deleted months earlier kept arriving on
every call: `AgentTools.resolve()` raised, the WebRTC create answered a bare
400 "Invalid Oracle call", and `list_agents` told Oracle about a contact that
matched no agent in the same list. She said she had no contact, and her
`investigate_with_oracle` tool could never run.

Storing the contact here gives one value across devices and reinstalls, checked
against the live roster on every read. A contact that stops resolving is
cleared and reported as `stale`, so a client can ask the user to pick again
instead of silently poisoning every call.
"""
from __future__ import annotations

from typing import Any

from . import agents as agents_db
from . import db
from .log import log

KEY = "oracle.contact_session"
MAX_LENGTH = 160


def roster() -> list[dict[str, Any]]:
    """Live agents Oracle may delegate to: not archived, not janitors."""
    return [a for a in agents_db.list_agents()
            if not a.get("archived_at") and not a.get("is_janitor")]


def resolve(name: str | None) -> dict[str, Any] | None:
    """The one live agent whose session or persona is `name`, else None.

    Mirrors `AgentTools.resolve` so the contact stored here is exactly what
    the tools will accept later. Ambiguity (two live agents sharing a persona)
    resolves to None on purpose: a contact must name one agent.
    """
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    found = [a for a in roster()
             if wanted in (str(a["session"]).casefold(), str(a["persona"]).casefold())]
    return found[0] if len(found) == 1 else None


def _stored() -> str:
    row = db.conn().execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    return str(row["value"] if row else "").strip()


def _store(session: str) -> None:
    con = db.conn()
    if session:
        con.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (KEY, session, db.now_ms()))
    else:
        con.execute("DELETE FROM settings WHERE key=?", (KEY,))


def get() -> dict[str, Any]:
    """The configured contact, validated against the live roster.

    Returns `{"session": ..., "persona": ..., "stale": ...}`. When the stored
    session no longer names one live agent it is cleared here and returned in
    `stale`, so the caller can tell the user which contact went away.
    """
    stored = _stored()
    if not stored:
        return {"session": "", "persona": "", "stale": ""}
    agent = resolve(stored)
    if agent is None:
        _store("")
        log("oracleContactStale", f"cleared={stored!r}")
        return {"session": "", "persona": "", "stale": stored}
    return {"session": str(agent["session"]), "persona": str(agent["persona"]), "stale": ""}


def set(value: str | None) -> dict[str, Any]:
    """Store the contact. Accepts a session or a persona; stores the session.

    An empty value clears the contact. A value naming no live agent raises
    ValueError rather than being stored: the whole point is never to persist a
    contact the tools cannot resolve.
    """
    wanted = str(value or "").strip()[:MAX_LENGTH]
    if not wanted:
        _store("")
        return {"session": "", "persona": "", "stale": ""}
    agent = resolve(wanted)
    if agent is None:
        raise ValueError(f"{wanted!r} is not a live agent; choose a contact from the roster")
    _store(str(agent["session"]))
    return {"session": str(agent["session"]), "persona": str(agent["persona"]), "stale": ""}


def effective(requested: str | None, *, source: str = "") -> str:
    """The contact an Oracle session should use, always resolvable or empty.

    The Host's contact wins when one is configured. Otherwise the client's
    `oracle_session` is honoured if it names a live agent. Anything else
    becomes "" so Oracle asks which contact should investigate, instead of a
    tool that raises. A stale client value is logged once per call so it is
    visible without breaking the call.
    """
    configured = get()
    if configured["session"]:
        return configured["session"]
    wanted = str(requested or "").strip()[:MAX_LENGTH]
    if not wanted:
        return ""
    agent = resolve(wanted)
    if agent is None:
        log("oracleContactUnresolved", f"requested={wanted!r} source={source or '-'}")
        return ""
    return str(agent["session"])
