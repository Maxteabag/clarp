from __future__ import annotations

from types import SimpleNamespace

from lib.runtime_startup import recover_runtime


def test_ephemeral_janitors_do_not_start_unused_chat_runtimes(tmp_path, monkeypatch):
    from lib import agents, janitors, janitor_builtins, runtime_startup
    agents.create_agent(persona="Sam", voice_id="", cwd=str(tmp_path), session="sam")
    agents.create_agent(persona="Rivet", voice_id="", cwd=str(tmp_path), session="rivet")
    janitors.create("rivet")
    janitor_builtins.ensure_builtins(cwd=str(tmp_path))
    restored = []
    monkeypatch.setattr(runtime_startup, "resume_missing_sessions",
        lambda rows, *args, **kwargs: restored.extend(rows) or [])
    runtime_startup.restore_persisted_agents(SimpleNamespace(agents_path=tmp_path / "unused.json"))
    assert set(restored) == {"sam", "rivet"}


def test_runtime_restart_marks_interrupted_work_without_submitting_continuity():
    from lib import agents, turn_lifecycle
    from lib.turn_lifecycle import TurnEvent

    aid = agents.create_agent(persona="Theo", voice_id="", cwd="/tmp", session="theo")
    agents.start_runtime(aid, "theo")
    agents.open_turn(agent_id=aid, source="pwa", trace_id="original-request")
    turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED,
                              {"trace_id": "original-request", "origin": "user"})
    submitted = []
    result = recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(submit=submitted.append, recover_queued=lambda: 3),
        restore_agents=lambda _ctx: None,
    )
    assert result["interrupted"] == 1
    assert agents.latest_state(aid)["detail"]["source"] == "server_restart"
    assert submitted == [], "the restarting agent, not startup, decides who to continue"
    assert result["queued"] == 3


def test_clean_runtime_handoff_does_not_invent_an_interruption():
    order = []
    result = recover_runtime(
        SimpleNamespace(stream="stream"),
        SimpleNamespace(recover_queued=lambda: order.append("queues") or 1),
        clean_handoff=True,
        restore_agents=lambda _ctx: order.append("restore"),
        mark_interrupted=lambda stream=None: order.append("interrupt") or [],
        reconcile=lambda: order.append("reconcile") or 0,
        repair_receipts=lambda: order.append("receipts") or {"done": 2},
    )

    assert order == ["restore", "reconcile", "queues", "receipts"]
    assert result["receipts"] == {"done": 2}
    assert result["interrupted"] == 0


def test_runtime_recovery_raises_sqlite_busy_timeout_during_boot(monkeypatch):
    from contextlib import contextmanager

    from lib import runtime_startup
    from lib.timing import SQLITE_RECOVERY_BUSY_TIMEOUT_MS

    seen: list[int] = []

    @contextmanager
    def fake_timeout(timeout_ms):
        seen.append(timeout_ms)
        yield

    monkeypatch.setattr(runtime_startup.db, "busy_timeout", fake_timeout)
    recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(recover_queued=lambda: 0),
        restore_agents=lambda _ctx: None,
        mark_interrupted=lambda stream=None: [],
        reconcile=lambda: 0,
    )
    # Boot recovery, then the queue receipt repair after queue recovery.
    assert seen == [SQLITE_RECOVERY_BUSY_TIMEOUT_MS] * 2


def test_runtime_recovery_ends_receipts_of_turns_the_restart_killed():
    from lib import agents, turn_lifecycle, turn_queue
    from lib.turn_lifecycle import TurnEvent

    aid = agents.create_agent(persona="Pebble", voice_id="", cwd="/tmp", session="pebble")
    agents.start_runtime(aid, "pebble")
    turn_queue.enqueue(queue_id="q1", agent_id=aid, session="pebble", text="hi",
                       trace_id="t1", client_msg_id="q1", synthesize_audio=False,
                       origin="agent", sender_agent_id="")
    turn_queue.mark_started("q1")
    agents.open_turn(agent_id=aid, source="pwa", trace_id="t1")
    turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED, {"trace_id": "t1"})
    turn_queue.enqueue(queue_id="q2", agent_id=aid, session="pebble", text="later",
                       trace_id="t2", client_msg_id="q2", synthesize_audio=False,
                       origin="user", sender_agent_id="")

    recover_runtime(SimpleNamespace(stream=None),
                    SimpleNamespace(recover_queued=lambda: 0),
                    restore_agents=lambda _ctx: None)

    assert turn_queue.status("q1") == "interrupted"
    assert turn_queue.status("q2") == "queued"


BAD_IDS = {"bad-agy": ("agy", "../escape"), "bad-claude": ("claude", "/etc/passwd"),
           "bad-codex": ("codex", "a*b")}
GOOD_AGY = "11111111-aaaa-4bbb-8ccc-000000000001"
GOOD_CLAUDE = "22222222-aaaa-4bbb-8ccc-000000000002"


def _stored(agent_id, backend_session_id):
    """A binding stored before the shared rule existed: written as it was, past today's check."""
    from lib import agents, db
    agents.start_runtime(agent_id, agents.get_by_agent_id(agent_id)["session"])
    db.conn().execute("UPDATE runtimes SET backend_session_id = ? WHERE agent_id = ? AND ended_at IS NULL",
                      (backend_session_id, agent_id))


def _fleet_with_bad_ids(tmp_path):
    import pathlib
    from lib import agents
    from lib.fork import encoded_project_dir
    ids = {}
    for session, (backend, bad) in BAD_IDS.items():
        ids[session] = agents.create_agent(persona=session, voice_id="", cwd=str(tmp_path),
                                           session=session, backend=backend)
        _stored(ids[session], bad)
    ids["good-agy"] = agents.create_agent(persona="Ada", voice_id="", cwd=str(tmp_path),
                                          session="good-agy", backend="agy")
    _stored(ids["good-agy"], GOOD_AGY)
    ids["good-claude"] = agents.create_agent(persona="Cy", voice_id="", cwd=str(tmp_path),
                                             session="good-claude", backend="claude")
    _stored(ids["good-claude"], GOOD_CLAUDE)
    project = encoded_project_dir(str(tmp_path), pathlib.Path.home() / ".claude" / "projects")
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{GOOD_CLAUDE}.jsonl").write_text('{"type":"user","sessionId":"%s"}\n' % GOOD_CLAUDE)
    return ids


def _logged(monkeypatch):
    from lib import resume, runtime_startup, transcript_paths
    lines = []
    for module in (resume, runtime_startup, transcript_paths):
        monkeypatch.setattr(module, "log", lambda event, detail="", **_: lines.append((event, detail)))
    monkeypatch.setattr(resume, "log_exception", lambda event, exc, detail="", **_: lines.append((event, detail)))
    return lines


def test_a_stored_id_that_fails_the_rule_starts_that_agent_fresh_and_restores_every_other(tmp_path, monkeypatch):
    """R70 review: such an id once stopped the runtime from starting (ValueError out of the binding,
    the agents after it unrestored, recovery never run). It is a missing transcript: that agent
    starts fresh, one line names the backend, everyone else is restored and recovery runs on."""
    from lib import agents, db
    import pytest
    ids = _fleet_with_bad_ids(tmp_path)
    lines = _logged(monkeypatch)
    ran = []
    try:
        result = recover_runtime(
            SimpleNamespace(stream=None, agents_path=tmp_path / "unused.json"),
            SimpleNamespace(recover_queued=lambda: ran.append("queued") or 0),
            mark_interrupted=lambda stream=None: ran.append("interrupted") or [],
            reconcile=lambda: ran.append("reconciled") or 0)
    except ValueError as exc:
        pytest.fail(f"a stored id stopped the runtime from starting: {exc!r}")
    assert ran == ["interrupted", "reconciled", "queued"] and result["queued"] == 0
    assert agents.live_backend_session(ids["good-agy"]) == GOOD_AGY
    assert agents.live_backend_session(ids["good-claude"]) == GOOD_CLAUDE
    for session, (backend, bad) in BAD_IDS.items():
        assert agents.live_backend_session(ids[session]) == "", session
        kept = [row[0] for row in db.conn().execute(
            "SELECT backend_session_id FROM runtimes WHERE agent_id = ? AND ended_at IS NOT NULL",
            (ids[session],))]
        assert kept == [bad], session        # ended as a fresh start ends it, not rewritten
    refused = [detail for event, detail in lines if event == "transcriptSessionIdRefused"]
    assert sorted(refused) == ["backend=agy", "backend=claude", "backend=codex"]
    assert not any(bad in detail for event, detail in lines for _, bad in BAD_IDS.values())


def test_a_binding_the_rule_refuses_at_restore_leaves_that_agent_fresh_and_goes_on(tmp_path, monkeypatch):
    """However a refused id reaches the binding at restore, the loop goes on and that agent is fresh."""
    from lib import agents, runtime_startup
    import pytest
    bad = agents.create_agent(persona="Bo", voice_id="", cwd=str(tmp_path), session="bo", backend="agy")
    _stored(bad, "../escape")
    good = agents.create_agent(persona="Al", voice_id="", cwd=str(tmp_path), session="al", backend="agy")
    lines = _logged(monkeypatch)
    monkeypatch.setattr(runtime_startup, "resume_missing_sessions", lambda *a, **k: [
        {"sid": "bo", "action": "resumed", "ok": True, "backend_session_id": "../escape"},
        {"sid": "al", "action": "resumed", "ok": True, "backend_session_id": GOOD_AGY}])
    try:
        runtime_startup.restore_persisted_agents(SimpleNamespace(agents_path=tmp_path / "unused.json"))
    except ValueError as exc:
        pytest.fail(f"a refused binding stopped the restore: {exc!r}")
    assert agents.live_backend_session(bad) == ""
    assert agents.current_runtime_id(bad) is not None
    assert agents.live_backend_session(good) == GOOD_AGY
    assert ("startupSessionRefused", "bo leaving fresh") in lines
