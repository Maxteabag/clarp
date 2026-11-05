"""SNAP-N1: freeze snapshot bytes while bounding runtime outage work per build."""
import json
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents, agent_goals, backends, db, snapshot, turn_queue
from lib import agent_ring_live, config, personas, turn_lifecycle
from lib.runtime_bridge import RuntimeUnavailable

# Captured from origin/main 74114894cf35458137937da1cf4f184ad0c8c60b,
# before any production changes, with this synthetic database and fixed status.
GOLDEN = pathlib.Path(__file__).with_name("fixtures") / "snapshot-runtime.json"
NOW = 1_800_000_000_000


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    # Synthetic persisted rows: all stay in the snapshot, including archived
    # agents. No Host, sockets, provider processes or transcripts are started.
    monkeypatch.setattr(db, "now_ms", lambda: NOW)
    monkeypatch.setattr(agents, "now_ms", lambda: NOW)
    monkeypatch.setattr(turn_lifecycle, "now_ms", lambda: NOW)
    monkeypatch.setattr(snapshot, "now_ms", lambda: NOW)
    monkeypatch.setattr(personas, "ensure_builtins", lambda: None)
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    monkeypatch.setattr(snapshot, "_exhausted_backends", lambda: {})
    monkeypatch.setattr(agent_ring_live, "user_call_profiles", lambda **kw: {})
    c = db.conn()
    c.execute("INSERT INTO personas (persona_id,name,voice_id,personality,builtin,created_at) "
              "VALUES ('persona-fixture','Fixture','V','',0,?)", (NOW,))
    active, compacting = {}, []
    for i in range(50):
        aid, session = f"fixture-{i:02d}", f"session-{i:02d}"
        backend = ("claude", "codex", "agy")[i % 3]
        kind = ("idle", "thinking", "compacting", "background", "done")[i % 5]
        c.execute("""INSERT INTO agents
            (agent_id,persona,voice_id,cwd,session,backend,created_at,
             archived_at,is_janitor,parent_agent_id,role,helper_state)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, f"Fixture {i:02d}", "V", "/fixture", session, backend,
             NOW - i * 1000, NOW if i == 49 else None, int(i == 48),
             "fixture-00" if i == 47 else None,
             "helper" if i == 47 else "janitor" if i == 48 else "agent",
             "running" if i == 47 else None))
        agents.record_state(aid, kind)
        if kind in {"thinking", "compacting"}:
            active[aid] = f"trace-{i:02d}"
        if kind == "compacting":
            compacting.append(session)
    agents.set_focus("fixture-00")
    turn_queue.enqueue(queue_id="queue-fixture", agent_id="fixture-01", session="session-01",
                       text="synthetic", trace_id="queued-trace", client_msg_id="queued-message",
                       synthesize_audio=False, origin="user", sender_agent_id="")
    agent_goals.upsert("fixture-01", session="session-01", backend="codex",
                       goal={"objective": "Synthetic goal", "status": "active", "token_budget": 500})
    status = {"active": active, "spawning": ["fixture-04"], "terminals": ["fixture-09"],
              "admitting": [], "queued": {}, "compactions": compacting,
              "live_activities": {"fixture-01": {"summary": "Synthetic activity"}}}
    # Simulate a build lasting many 250 ms windows without making tests slow.
    clock = [1000.0]
    def tick():
        clock[0] += 0.3
        return clock[0]
    monkeypatch.setattr(backends, "_clock", tick)
    yield SimpleNamespace(static=tmp_path / "static"), status
    backends.configure_runtime_client(None)


class Runtime:
    def __init__(self, status, *, offline=False):
        self.sample = status
        self.offline = offline
        self.timeouts = []

    def status(self, *, timeout=None):
        self.timeouts.append(timeout)
        if self.offline:
            if timeout is not None:
                time.sleep(timeout)
            raise RuntimeUnavailable("synthetic status timeout")
        return self.sample


def test_snapshot_fixed_runtime_json_bytes(fleet):
    ctx, status = fleet
    runtime = Runtime(status)
    backends.configure_runtime_client(runtime)
    actual = json.dumps(snapshot.build_agent_snapshot(ctx)).encode()
    assert actual == GOLDEN.read_bytes()


@pytest.mark.parametrize("offline", [False, True], ids=["healthy", "timeout"])
def test_snapshot_one_bounded_status_attempt_per_build(fleet, offline):
    ctx, status = fleet
    runtime = Runtime(status, offline=offline)
    backends.configure_runtime_client(runtime)
    started = time.monotonic()
    first = snapshot.build_agent_snapshot(ctx)
    elapsed = time.monotonic() - started
    assert runtime.timeouts == [2.0]
    assert elapsed < 3.0  # one 2 s wait plus the small synthetic projection
    assert len(first["agents"]) == 50
    if offline:
        by_id = {row["agent_id"]: row for row in first["agents"]}
        assert by_id["fixture-01"]["busy"] is True
        assert by_id["fixture-02"]["compacting"] is True
        assert by_id["fixture-00"]["busy"] is False
        assert agents.latest_state("fixture-01")["kind"] == "thinking"
    snapshot.build_agent_snapshot(ctx)
    assert runtime.timeouts == [2.0, 2.0]


def test_non_snapshot_status_keeps_ttl_and_default_timeout(fleet, monkeypatch):
    ctx, status = fleet
    clock = [1000.0]
    monkeypatch.setattr(backends, "_clock", lambda: clock[0])
    runtime = Runtime(status)
    backends.configure_runtime_client(runtime)
    snapshot.build_agent_snapshot(ctx)
    runtime.timeouts.clear()
    backends.invalidate_runtime_status()
    assert backends.runtime_status() == status
    assert backends.runtime_status() == status
    assert runtime.timeouts == [None]
    clock[0] += backends.RUNTIME_STATUS_TTL + .01
    assert backends.runtime_status() == status
    assert runtime.timeouts == [None, None]
