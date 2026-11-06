"""Freeze runtime projection semantics while removing roster-squared work."""
import json
from lib import agents, db
from test_snapshot_n2 import seed


# The pre-fix projection is the independent differential oracle for ordering,
# NULLs and output key order; no production helper constructs this expectation.
LEGACY = """SELECT a.agent_id,
    (SELECT backend_session_id FROM runtimes r
     WHERE r.agent_id=a.agent_id AND ended_at IS NULL
     ORDER BY started_at DESC LIMIT 1) AS backend_session_id,
    (SELECT started_at FROM turns t
     WHERE t.agent_id=a.agent_id AND ended_at IS NULL
     ORDER BY started_at DESC LIMIT 1) AS open_turn_started_at
    FROM agents a WHERE a.deleted_at IS NULL"""


def legacy_bytes():
    return json.dumps({r['agent_id']:dict(r) for r in db.conn().execute(LEGACY)}).encode()


def test_dashboard_runtime_reads_do_not_rescan_live_roster_per_agent():
    ids = seed(250)
    db.conn().executemany("""INSERT INTO runtimes
        (agent_id,session,backend_session_id,started_at) VALUES (?,?,?,1000)""",
        [(a,a,a) for a in ids])
    ticks = [0]
    def progress():
        ticks[0] += 1000
        return 0
    db.conn().set_progress_handler(progress,1000)
    try:
        result = agents.dashboard_runtimes()
    finally:
        db.conn().set_progress_handler(None,0)
    assert len(result) == 250
    # Deterministic SQLite VM-work budget, not a load-sensitive wall clock.
    # The old unindexed correlated runtime lookup walks250live rows250times.
    assert ticks[0] <= 250 * 300, ticks[0]


def test_runtime_projection_matches_legacy_through_binding_and_turn_changes():
    ids = seed(4)
    c = db.conn()
    c.execute("UPDATE agents SET archived_at=1 WHERE agent_id=?", (ids[2],))
    c.execute("UPDATE agents SET deleted_at=1 WHERE agent_id=?", (ids[3],))
    c.executemany("""INSERT INTO runtimes
        (agent_id,session,backend_session_id,started_at,ended_at) VALUES(?,?,?,?,?)""", [
        (ids[0],ids[0],'z-tied',10,None),
        (ids[0],ids[0],'a-tied',10,None),
        (ids[0],ids[0],'older',9,None),
        (ids[0],ids[0],'closed',30,31),
        (ids[1],ids[1],'bound-older',9,None),
        (ids[1],ids[1],None,10,None),
    ])
    c.executemany("""INSERT INTO turns
        (agent_id,source,trace_id,started_at,ended_at) VALUES(?,'user',?,?,?)""", [
        (ids[0],'old-open',100,None),(ids[0],'new-open',200,None),
        (ids[0],'closed',300,301)])
    assert json.dumps(agents.dashboard_runtimes()).encode() == legacy_bytes()
    assert ids[2] in agents.dashboard_runtimes() and ids[3] not in agents.dashboard_runtimes()
    # Each fixture change must appear immediately; no new cache hides a write.
    for sql, args in [
        ("UPDATE runtimes SET backend_session_id='new-binding' WHERE backend_session_id='a-tied'",()),
        ("UPDATE turns SET ended_at=400 WHERE trace_id='new-open'",()),
        ("UPDATE runtimes SET ended_at=500 WHERE agent_id=?",(ids[0],)),
    ]:
        c.execute(sql,args)
        assert json.dumps(agents.dashboard_runtimes()).encode() == legacy_bytes()
    agents.start_runtime(ids[2],ids[2])
    agents.bind_backend_session(ids[2],'ghost-bound')
    assert json.dumps(agents.dashboard_runtimes()).encode() == legacy_bytes()
    agents.end_runtime_bound_to(ids[2],'ghost-bound')
    assert json.dumps(agents.dashboard_runtimes()).encode() == legacy_bytes()
