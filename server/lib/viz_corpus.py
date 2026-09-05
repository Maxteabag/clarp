"""Bounded, snapshot-fenced reads shared by the map and its cold author."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from .protocol import AgentState


def tool_rows(con: sqlite3.Connection, since: int = 0, until: int = 1 << 62,
              page_size: int = 128) -> Iterator[sqlite3.Row]:
    """Keyset pages; release each cursor before yielding or invoking a model.

    The high-water mark makes a full-corpus scan finite while agents keep
    writing. No OFFSET, long-lived read transaction, or corpus-sized fetch.
    Ordering is insertion order; callers needing event time sort projections.
    """
    if not 1 <= page_size <= 4096:
        raise ValueError("page_size must be between 1 and 4096")
    end = con.execute("SELECT coalesce(max(state_id), 0) FROM state_log").fetchone()[0]
    after = 0
    while after < end:
        cursor = con.execute(
            "SELECT state_id, agent_id, ts, detail FROM state_log "
            "WHERE state_id > ? AND state_id <= ? AND kind = ? "
            "AND ts >= ? AND ts <= ? ORDER BY state_id LIMIT ?",
            (after, end, AgentState.TOOL, since, until, page_size))
        page = cursor.fetchall()
        cursor.close()
        if not page:
            break
        after = page[-1]["state_id"]
        yield from page
