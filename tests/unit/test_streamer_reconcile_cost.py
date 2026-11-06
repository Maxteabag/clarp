"""Roster scans must not resolve executable configuration for every agent."""
import pytest

from lib import backends, config, db
from lib.transcript_streamer import TranscriptStreamer


@pytest.mark.parametrize("count", [1, 1059])
def test_idle_streamer_configuration_work_does_not_scale_with_roster(monkeypatch, count):
    c = db.conn()
    c.executemany("""INSERT INTO agents
        (agent_id, session, persona, voice_id, cwd, backend, created_at, archived_at)
        VALUES (?, ?, ?, '', '/fixture', 'claude', 1, ?)""",
        [(f"stream-cost-{i}", f"stream-cost-{i}", "Synthetic",
          1 if i % 5 == 0 else None) for i in range(count)])
    loads = []
    original = config.load

    def load(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(config, "load", load)
    streamer = TranscriptStreamer()
    streamer.reconcile_once()
    assert streamer._bound == {} and streamer.pool.agent_ids() == []
    # A scan reads a static support flag. Even an implementation that resolves
    # one shared configuration must not do executable I/O per idle agent.
    assert len(loads) <= 1, f"{count} agents caused {len(loads)} config loads"


@pytest.mark.parametrize("backend, supported", [
    (" CLAUDE ", True), ("unknown-fixture-backend", True), ("", True),
    ("codex", False), ("antigravity", False), ("open-code", False),
])
def test_reconcile_preserves_backend_selection_and_watch_transitions(
        monkeypatch, tmp_path, backend, supported):
    """Exercise real watcher ownership, including archived and deleted agents."""
    c = db.conn()
    c.execute("""INSERT INTO agents
        (agent_id, session, persona, voice_id, cwd, backend, created_at, archived_at)
        VALUES ('watch-cost', 'watch-cost', 'Synthetic', '', '/fixture', ?, 1, 1)""",
        (backend,))
    c.execute("""INSERT INTO runtimes
        (agent_id,session,backend_session_id,started_at)
        VALUES ('watch-cost','watch-cost','first',1)""")
    (tmp_path / "second.jsonl").touch()
    # File discovery is unrelated to the support decision. The watch, pool,
    # DB roster/runtime reads, and lifecycle transitions remain real.
    def find(self, sid):
        path = tmp_path / f"{sid}.jsonl"
        return path if path.exists() else None

    monkeypatch.setattr(type(backends.by_id("claude")), "find_transcript", find)
    streamer = TranscriptStreamer()
    try:
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == []
        (tmp_path / "first.jsonl").touch()
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == (["watch-cost"] if supported else [])
        if not supported:
            return
        first = streamer._bind_generation["watch-cost"]
        streamer.reconcile_once()
        assert streamer._bind_generation["watch-cost"] == first
        c.execute("UPDATE runtimes SET backend_session_id='second'")
        streamer.reconcile_once()
        assert streamer._bound == {"watch-cost": "second"}
        assert streamer._bind_generation["watch-cost"] > first
        c.execute("UPDATE runtimes SET ended_at=2")
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == [] and streamer._bound == {}
        c.execute("UPDATE runtimes SET ended_at=NULL")
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == ["watch-cost"]
        c.execute("UPDATE agents SET backend='codex'")
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == [] and streamer._bound == {}
        c.execute("UPDATE agents SET backend=?", (backend,))
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == ["watch-cost"]
        c.execute("UPDATE agents SET deleted_at=2")
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == [] and streamer._bound == {}
    finally:
        streamer.stop()


def test_live_instance_and_class_support_overrides_are_not_cached(monkeypatch, tmp_path):
    c = db.conn()
    c.execute("""INSERT INTO agents
        (agent_id,session,persona,voice_id,cwd,backend,created_at)
        VALUES ('override','override','Synthetic','','/fixture','codex',1)""")
    c.execute("""INSERT INTO runtimes(agent_id,session,backend_session_id,started_at)
        VALUES ('override','override','override',1)""")
    path = tmp_path / "override.jsonl"
    path.touch()
    runner = backends.by_id("codex")
    monkeypatch.setattr(type(runner), "find_transcript", lambda self, sid: path)
    streamer = TranscriptStreamer()
    # Restore the singleton's exact instance dictionary, including whether
    # it inherited the flag, so later tests do not inherit an instance shadow.
    original_instance = vars(runner).copy()
    try:
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == []
        monkeypatch.setattr(type(runner), "supports_transcript_streaming", True)
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == ["override"]
        runner.supports_transcript_streaming = False
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == []
        runner.supports_transcript_streaming = True
        streamer.reconcile_once()
        assert streamer.pool.agent_ids() == ["override"]
    finally:
        vars(runner).clear()
        vars(runner).update(original_instance)
        streamer.stop()


def test_support_flag_error_uses_existing_outer_health_and_log_path(monkeypatch):
    from lib import transcript_streamer as module

    db.conn().execute("""INSERT INTO agents
        (agent_id,session,persona,voice_id,cwd,backend,created_at)
        VALUES ('error','error','Synthetic','','/fixture','codex',1)""")
    problem = ValueError("synthetic support error")

    def fail(self):
        raise problem

    monkeypatch.setattr(type(backends.by_id("codex")), "supports_transcript_streaming",
                        property(fail))
    errors, logs, successes = [], [], []
    monkeypatch.setattr(module.health, "mark_error", lambda *args: errors.append(args))
    monkeypatch.setattr(module.health, "mark_success", lambda *args: successes.append(args))
    monkeypatch.setattr(module, "log_exception", lambda *args: logs.append(args))
    streamer = TranscriptStreamer()
    streamer._stop.set()  # Run the real initial reconcile, without a thread or timer.
    streamer._reconcile_loop()
    assert errors == [("transcript_streamer", problem)]
    assert logs == [("transcriptStreamerReconcileFail", problem)]
    assert successes == [] and streamer._bound == {}
