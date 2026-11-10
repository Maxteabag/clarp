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

    def status_within(self, total_s):
        return self.status(timeout=total_s)

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


def test_snapshot_model_reads_are_bounded_and_reused_across_a_large_fleet(
        fleet, tmp_path, monkeypatch):
    """Read actual model files through the snapshot, with no mocked model result."""
    import builtins
    import os

    from lib import claude_transcript, session_models

    ctx, status = fleet
    backends.configure_runtime_client(Runtime(status))
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "-fixture"
    projects.mkdir(parents=True)
    caches = (session_models._read_model, session_models._transcript,
              session_models._indexed_model)
    for cache in caches:
        cache.cache_clear()
    paths, expected_models = {}, {}
    for i in range(513):
        aid, session, bsid = f"model-agent-{i}", f"model-session-{i}", f"model-native-{i}"
        db.conn().execute("""INSERT INTO agents
            (agent_id,persona,voice_id,cwd,session,backend,created_at)
            VALUES (?,?,?,?,?,'claude',?)""",
            (aid, f"Model fixture {i}", "V", str(tmp_path), session, NOW - 100000 - i))
        agents.start_runtime(aid, session)
        agents.bind_backend_session(aid, bsid)
        agents.record_state(aid, "done")
        model = f"model-{i}"
        path = projects / f"{bsid}.jsonl"
        # Only one file crosses the small read window; the fixture is <150 KiB,
        # rather than hundreds of megabytes of duplicate padding.
        prefix = (json.dumps({"type": "user", "message": {"content": "x" * 70000}})
                  + "\n") if i == 0 else ""
        path.write_text(prefix + json.dumps({"type": "assistant", "message": {"model": model}})
                        + "\n")
        os.utime(path, ns=(0, 0))  # settled files; preserve the context-cache racy rule
        paths[str(path)] = aid
        expected_models[aid] = model

    real_open = builtins.open
    opened, read_bytes = [], []

    class MeasuredFile:
        def __init__(self, stream, path):
            self.stream, self.path = stream, path

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, *args):
            data = self.stream.read(*args)
            read_bytes.append((self.path, len(data)))
            return data

    def measured_open(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        if mode == "rb" and str(path) in paths:
            opened.append(str(path))
            return MeasuredFile(stream, str(path))
        return stream

    # session_models uses binary open/read; context usage has its own Path.open
    # reader. These counters deliberately attribute model reads only.
    monkeypatch.setattr(builtins, "open", measured_open)
    try:
        cold = snapshot.build_agent_snapshot(ctx)
        actual_models = {a["agent_id"]: a["model"] for a in cold["agents"]
                         if a["agent_id"] in expected_models}
        assert actual_models == expected_models
        assert len(cold["agents"]) == 563  # retain the original mixed/archived fleet too
        assert len(opened) == 513
        small_bytes = sum(pathlib.Path(p).stat().st_size for p in paths
                          if paths[p] != "model-agent-0")
        assert sum(n for _, n in read_bytes) <= 64 * 1024 + small_bytes

        opened.clear()
        read_bytes.clear()
        warm = snapshot.build_agent_snapshot(ctx)
        assert json.dumps(warm).encode() == json.dumps(cold).encode()
        assert opened == [] and read_bytes == []

        changed_path = next(p for p, aid in paths.items() if aid == "model-agent-0")
        with real_open(changed_path, "a") as stream:
            stream.write(json.dumps({"type": "assistant",
                                     "message": {"model": "changed-in-session"}}) + "\n")
        changed = snapshot.build_agent_snapshot(ctx)
        expected = json.loads(json.dumps(cold))
        next(a for a in expected["agents"] if a["agent_id"] == "model-agent-0")["model"] = (
            "changed-in-session")
        assert json.dumps(changed).encode() == json.dumps(expected).encode()
        assert opened == [changed_path]
        assert 0 < sum(n for _, n in read_bytes) <= 64 * 1024
    finally:
        for cache in caches:
            cache.cache_clear()
        claude_transcript.reset_transcript_index()


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


def test_snapshot_unavailable_runtime_skips_reconcile_and_preserves_payload(fleet, monkeypatch):
    """An outage costs one status report and one summary, not one traceback per agent."""
    import copy
    from lib import eventlog, reconcile

    ctx, status = fleet
    # Independently frozen healthy bytes supply every field. Only these known
    # runtime-derived fields differ when the same status read fails: no live
    # activity, and no synthetic spawning/terminal handles for the two done rows.
    expected = json.loads(GOLDEN.read_bytes())
    expected_rows = {row["agent_id"]: row for row in expected["agents"]}
    expected_rows["fixture-01"]["live_activity"] = None
    for aid in ("fixture-04", "fixture-09"):
        expected_rows[aid].update(busy=False, latest_state="done", turn_started_at=0)
    # A bound missing transcript must stay bound while ownership is unknown.
    agents.start_runtime("fixture-15", "session-15")
    agents.bind_backend_session("fixture-15", "missing-native-session")
    expected_rows["fixture-15"].update(
        backend_session_id="missing-native-session", conversation_id="missing-native-session")
    db.conn().execute("""INSERT INTO background_jobs
        (job_id,agent_id,session,title,status,started_at,updated_at)
        VALUES ('outage-job','fixture-03','session-03','Synthetic work','running',?,?)""",
        (NOW, NOW))
    expected_rows["fixture-03"]["background_jobs"]["count"] = 1
    expected_rows["fixture-03"]["status_text"] = "Synthetic work"

    # Scale real stored rows without scaling fixture files or inventing another
    # projection oracle. These idle agents are copies of one frozen idle row;
    # their shared persona also exercises roster deduplication and ordering.
    extras = []
    for i in range(300):
        aid, session = f"outage-{i:03d}", f"outage-session-{i:03d}"
        created = NOW - 100000 - i
        db.conn().execute("""INSERT INTO agents
            (agent_id,persona,voice_id,cwd,session,backend,created_at)
            VALUES (?, 'Fixture 25','V','/fixture',?,'codex',?)""", (aid, session, created))
        agents.record_state(aid, "idle")
        row = copy.deepcopy(expected_rows["fixture-25"])
        row.update(agent_id=aid, session=session, last_activity=created)
        row["activity"].update(agent_id=aid, session=session)
        extras.insert(0, row)
    expected["agents"] = extras + expected["agents"]
    expected["roster"] = ["Fixture", "Fixture 25"] + [
        name for name in expected["roster"] if name not in {"Fixture", "Fixture 25"}]
    stored = {table: [tuple(row) for row in db.conn().execute(f"SELECT * FROM {table}")]
              for table in ("agents", "state_log", "runtimes", "turns", "queued_turns")}

    class UnavailableRuntime(Runtime):
        def status_within(self, total_s):
            self.timeouts.append(total_s)
            raise RuntimeUnavailable("synthetic unavailable runtime")

    runtime = UnavailableRuntime(status)
    backends.configure_runtime_client(runtime)
    reconciled, exception_events, summary_events = [], [], []
    original_reconcile = reconcile.reconcile_agent
    original_exception, original_emit = eventlog.emit_exception, eventlog.emit

    def observed_reconcile(aid, *args, **kwargs):
        reconciled.append(aid)
        return original_reconcile(aid, *args, **kwargs)

    def observed_exception(source, event, exc, **kwargs):
        exception_events.append(event)
        return original_exception(source, event, exc, **kwargs)

    def observed_emit(source, event, **kwargs):
        if event == "snapshotReconcileSkipped":
            summary_events.append(kwargs.get("detail"))
        return original_emit(source, event, **kwargs)

    monkeypatch.setattr(reconcile, "reconcile_agent", observed_reconcile)
    monkeypatch.setattr(eventlog, "emit_exception", observed_exception)
    monkeypatch.setattr(eventlog, "emit", observed_emit)
    actual = snapshot.build_agent_snapshot(ctx)
    assert actual == expected  # whole unavailable payload, before work-count RED
    assert len(actual["agents"]) == 350
    assert runtime.timeouts == [2.0]
    assert {table: [tuple(row) for row in db.conn().execute(f"SELECT * FROM {table}")]
            for table in stored} == stored
    assert reconciled == []
    assert exception_events == ["runtimeStatusUnavailable"]
    assert summary_events == [{"msg": "count=350 error_type=RuntimeUnavailable"}]

    # Failure is build-local. The next build must actually reconcile again,
    # including the missing binding once a fresh clear ownership read succeeds.
    reconciled.clear()
    exception_events.clear()
    summary_events.clear()
    recovered = Runtime(status)
    backends.configure_runtime_client(recovered)
    snapshot.build_agent_snapshot(ctx)
    assert len(reconciled) == 350
    assert recovered.timeouts == [2.0, 2.0]
    assert agents.live_backend_session("fixture-15") == ""
    assert exception_events == [] and summary_events == []


@pytest.mark.parametrize("build_fails", [False, True])
def test_non_snapshot_status_keeps_ttl_and_default_timeout(fleet, monkeypatch, build_fails):
    ctx, status = fleet
    clock = [1000.0]
    monkeypatch.setattr(backends, "_clock", lambda: clock[0])
    runtime = Runtime(status)
    backends.configure_runtime_client(runtime)
    if build_fails:
        def fail_roster():
            raise ValueError("synthetic projection failure")
        monkeypatch.setattr(agents, "list_agents", fail_roster)
        with pytest.raises(ValueError, match="synthetic projection failure"):
            snapshot.build_agent_snapshot(ctx)
    else:
        snapshot.build_agent_snapshot(ctx)
    runtime.timeouts.clear()
    backends.invalidate_runtime_status()
    assert backends.runtime_status() == status
    assert backends.runtime_status() == status
    assert runtime.timeouts == [None]
    clock[0] += backends.RUNTIME_STATUS_TTL + .01
    assert backends.runtime_status() == status
    assert runtime.timeouts == [None, None]


@pytest.mark.parametrize("fresh", ["clear", "admitted", "timeout"])
def test_snapshot_ghost_candidates_share_one_fresh_bounded_read(fleet, monkeypatch, fresh):
    ctx, status = fleet
    candidates = ["fixture-15", "fixture-00"]
    for aid in candidates:
        agents.bind_backend_session(aid, f"ghost-{aid}")
    # Resume-target lookup is a filesystem dependency; no real transcripts.
    monkeypatch.setattr(backends.by_id("claude"), "resume_target",
                        lambda *args: None)

    class AdmissionRuntime(Runtime):
        def status(self, *, timeout=None):
            self.timeouts.append(timeout)
            if len(self.timeouts) == 1:
                return self.sample
            if fresh == "timeout":
                raise RuntimeUnavailable("synthetic fresh-read timeout")
            return dict(self.sample, admitting=candidates if fresh == "admitted" else [])

    runtime = AdmissionRuntime(status)
    backends.configure_runtime_client(runtime)
    snapshot.build_agent_snapshot(ctx)
    assert runtime.timeouts == [2.0, 2.0]
    for aid in candidates:
        assert bool(agents.live_backend_session(aid)) == (fresh != "clear" or aid != candidates[0])
    if fresh == "timeout":
        runtime.timeouts.clear()
        snapshot.build_agent_snapshot(ctx)
        assert runtime.timeouts == [2.0, 2.0]
        assert all(agents.live_backend_session(aid) for aid in candidates)


def test_snapshot_admission_after_first_fresh_sample_keeps_second_binding(fleet, monkeypatch):
    ctx, status = fleet
    first, second = "fixture-15", "fixture-00"
    for aid in (first, second):
        agents.bind_backend_session(aid, f"ghost-{aid}")

    class RacingRuntime(Runtime):
        admitted = False

        def status(self, *, timeout=None):
            self.timeouts.append(timeout)
            # A detached status result: later admissions do not mutate it.
            return dict(self.sample, admitting=[second] if self.admitted else [])

    runtime = RacingRuntime(status)
    def missing_resume_target(bsid, *args):
        if bsid == f"ghost-{second}":
            assert runtime.timeouts == [2.0, 2.0]
            runtime.admitted = True
        return None
    monkeypatch.setattr(backends.by_id("claude"), "resume_target", missing_resume_target)
    backends.configure_runtime_client(runtime)
    snapshot.build_agent_snapshot(ctx)
    assert runtime.admitted
    assert agents.live_backend_session(first) == ""
    assert agents.live_backend_session(second) == f"ghost-{second}"
    assert runtime.timeouts == [2.0, 2.0]
