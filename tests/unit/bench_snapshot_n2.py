"""Explicit buildbox benchmark (not part of normal test collection).

uv run --frozen --group dev python -m pytest -n 0 -s tests/unit/bench_snapshot_n2.py
"""
import json
import statistics
import time
import pytest
from lib import agents, agent_ring_live, backends, config, db, personas, snapshot
from lib import message_previews, message_store


@pytest.mark.parametrize("count", [250, 1059])
def test_snapshot_benchmark(monkeypatch, count):
    ids = [f"bench-n2-{i:04d}" for i in range(count)]
    c = db.conn()
    c.executemany("""INSERT INTO agents
        (agent_id,persona,voice_id,cwd,session,backend,created_at,archived_at)
        VALUES (?,?, 'V','/fixture',?,'codex',1000,?)""",
        [(a,a,a,1000 if i % 5 == 0 else None) for i,a in enumerate(ids)])
    c.executemany("INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES(?,?,?,1000)",
                  [(a,a,a) for a in ids])
    c.executemany("INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES(?,'user',?,1000)",
                  [(a,'trace-'+a) for a in ids])
    c.executemany("""INSERT INTO messages(message_id,agent_id,backend_session_id,
        seq,role,text,revision,updated_at) VALUES(?,?,?,?,'assistant',?, ?,1000)""",
        [(f'{a}-{j}',a,a,j,'Synthetic fixture reply '+str(j),j+1)
         for a in ids for j in range(20)])
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    monkeypatch.setattr(personas, "ensure_builtins", lambda: None)
    monkeypatch.setattr(snapshot, "_exhausted_backends", lambda: {})
    monkeypatch.setattr(agent_ring_live, "user_call_profiles", lambda **kw: {})
    class Runtime:
        def status_within(self, timeout):
            return {"active":{a:'trace-'+a for a in ids}}
    backends.configure_runtime_client(Runtime())
    calls = []
    original = message_previews._agent_preview
    def compute(aid, *args):
        calls.append(aid)
        return original(aid, *args)
    monkeypatch.setattr(message_previews, "_agent_preview", compute)
    try:
        snapshot.build_agent_snapshot(None)
        durations, recomputes = [], []
        for i in range(7):
            message_store.record_user_message(agent_id=ids[0], backend_session_id=ids[0],
                client_msg_id=f'changed-{i}',text='Synthetic import-like update '+str(i))
            calls.clear()
            start = time.perf_counter()
            body = json.dumps(snapshot.build_agent_snapshot(None)).encode()
            durations.append((time.perf_counter()-start)*1000)
            recomputes.append(len(calls))
        print('SNAP_N2_BENCH '+json.dumps({'agents':count,'messages_per_agent':20,
            'iterations':7,'median_ms':round(statistics.median(durations),3),
            'min_ms':round(min(durations),3),'max_ms':round(max(durations),3),
            'recomputes':recomputes,'body_bytes':len(body)}),flush=True)
    finally:
        backends.configure_runtime_client(None)
