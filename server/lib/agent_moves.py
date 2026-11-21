"""An agent that moved to another Host.

The Rust Host's import takes up an agent's native session (for Codex, the same
thread id). After it succeeded it calls this Host once (``POST /agent-moved``)
and the agent is marked moved, with where it went (that Host's name and its id
for the agent) and when. From then on no turn of the agent starts here: every
door that starts one goes through admission, and admission refuses a moved agent
with ``409 agent_moved`` (``turn_admission``), so a stray turn never writes the
session the other Host now writes. Its queued items are held, not dispatched,
and run again when the move is taken back (an import rolled back, an agent that
returns) by the same route.

A turn already running when the mark is set is not cut: the mark refuses new
turns only. It is read where a turn would start (admission, the in-memory
handover, queue recovery), from the database, so the HTTP process and the
runtime see the same answer with no message between them.

Nothing here touches the agent's native session, transcripts or files.
"""
from __future__ import annotations

from typing import Any

from . import db

# The one refusal every door answers (docs/protocol.md): 409 with this error
# and ``moved_to``.
AGENT_MOVED = "agent_moved"


class MoveConflict(ValueError):
    """The agent is already marked moved somewhere else."""


def _clean(value: Any, name: str, limit: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} required")
    if len(text) > limit:
        raise ValueError(f"{name} too long")
    return text


def _public(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"to": {"host": row["to_host"], "id": row["to_id"]}, "at": int(row["moved_at"])}


def from_row(row) -> dict[str, Any] | None:
    """The mark from a roster row read with ``agents.list_agents(with_moves=True)``."""
    if not row.get("moved_to_host"):
        return None
    return {"to": {"host": row["moved_to_host"], "id": row["moved_to_id"]},
            "at": int(row["moved_at"])}


def moved(agent_id: str) -> dict[str, Any] | None:
    """``{"to": {"host", "id"}, "at"}`` when the agent moved, else None."""
    if not agent_id:
        return None
    return _public(db.conn().execute(
        "SELECT to_host, to_id, moved_at FROM agent_moves WHERE agent_id = ?",
        (agent_id,)).fetchone())


def all_moved() -> dict[str, dict[str, Any]]:
    """Every moved agent's mark, for the snapshot (one read)."""
    return {str(row["agent_id"]): _public(row) for row in db.conn().execute(
        "SELECT agent_id, to_host, to_id, moved_at FROM agent_moves")}


def set_moved(agent_id: str, *, host: Any, remote_id: Any) -> tuple[dict[str, Any], bool]:
    """Mark the agent moved to ``host`` (its id there ``remote_id``). Answers
    the mark and whether this call set it: the same call again changes nothing
    and says so. A mark to another place is refused (MoveConflict): take the
    move back first."""
    host = _clean(host, "moved_to.host")
    remote_id = _clean(remote_id, "moved_to.id")
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        current = con.execute(
            "SELECT to_host, to_id, moved_at FROM agent_moves WHERE agent_id = ?",
            (agent_id,)).fetchone()
        if current is not None:
            con.execute("COMMIT")
            if (current["to_host"], current["to_id"]) != (host, remote_id):
                raise MoveConflict(
                    f"already moved to {current['to_host']} ({current['to_id']})")
            return _public(current), False
        now = db.now_ms()
        con.execute(
            "INSERT INTO agent_moves (agent_id, to_host, to_id, moved_at) VALUES (?, ?, ?, ?)",
            (agent_id, host, remote_id, now))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return {"to": {"host": host, "id": remote_id}, "at": now}, True


def clear(agent_id: str) -> bool:
    """Take the move back. Answers whether a mark was cleared."""
    return bool(db.conn().execute(
        "DELETE FROM agent_moves WHERE agent_id = ?", (agent_id,)).rowcount)
