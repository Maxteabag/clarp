"""/log pages seek an index instead of sorting the whole conversation.

The tail and `before` pages take the newest rows of one conversation in
display order, ``COALESCE(timestamp, ''), seq``. Without an index in that
order SQLite read every row of the conversation and sorted it in a temporary
B-tree on every page (about 40 ms per open on a 12k-row chat). The plan is
asserted on the SQL `list_messages` actually runs, like T3 Code's
OrchestrationEventStore.sequence.test.ts does for its cursor reads.
"""
from __future__ import annotations

import pytest

from lib import db, message_store

AGENT, BSID = "a1", "bs-1"


def _seed(n=300):
    con = db.conn()
    con.execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", (AGENT, "Mike", "v", "/tmp", "mike", db.now_ms()))
    con.executemany(
        "INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role,"
        " timestamp, text, updated_at, revision) VALUES (?,?,?,?,?,?,?,?,?)",
        [(f"m{i}", AGENT, BSID, i, "assistant" if i % 2 else "user",
          f"2026-09-01T10:{i // 60:02d}:{i % 60:02d}.000Z", f"row {i}", i, i)
         for i in range(n)])


def _plan_of_list_messages(**kw):
    con = db.conn()
    statements: list[str] = []
    con.set_trace_callback(statements.append)
    try:
        rows = message_store.list_messages(agent_id=AGENT, backend_session_id=BSID, **kw)
    finally:
        con.set_trace_callback(None)
    page = [s for s in statements if "FROM messages m" in s and "LIMIT" in s][-1]
    plan = [tuple(r) for r in con.execute("EXPLAIN QUERY PLAN " + page)]
    return rows, plan


def _subtree(plan, root_id):
    ids, out = {root_id}, []
    for row in plan:
        if row[1] in ids:
            ids.add(row[0])
            out.append(row[3])
    return out


@pytest.mark.parametrize("include_automated", [True, False])
@pytest.mark.parametrize("page", ["tail", "before"])
def test_log_pages_read_the_newest_rows_from_the_index(page, include_automated):
    _seed()
    kw = {"limit": 50, "include_automated": include_automated}
    if page == "before":
        kw["before_message_id"] = "m200"
    rows, plan = _plan_of_list_messages(**kw)

    newest = 199 if page == "before" else 299
    assert [r["id"] for r in rows] == [f"m{i}" for i in range(newest - 49, newest + 1)]

    inner = next(r for r in plan if r[3].startswith("CO-ROUTINE"))
    inner_plan = _subtree(plan, inner[0])
    assert any("USING INDEX idx_messages_log_order" in d for d in inner_plan), plan
    # Only the outer re-sort of the page itself may use a temporary B-tree.
    assert not any("TEMP B-TREE" in d for d in inner_plan), plan
