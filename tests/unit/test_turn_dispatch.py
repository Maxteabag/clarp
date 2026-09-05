from pathlib import Path
from types import SimpleNamespace

from lib import agents as agents_db
from lib import heartbeat, team_leader, team_store
from lib import tts_queue
from lib.protocol import AgentState
from lib.turn_dispatch import (
    MAX_ATTEMPTS,
    TurnDispatchService,
    _spoken_failure_text,
    clear_for_agent,
)
import lib.turn_dispatch as _td


def test_clear_for_agent_frees_slot_and_drops_queue():
    """/stop uses this: a SIGTERM'd turn may not fire its terminal callback, so
    the in-flight slot + queue must be cleared explicitly (else the badge stays
    busy and the slot leaks)."""
    _td._INFLIGHT["ag"] = "trace-1"
    _td._QUEUED["ag"] = ["spec-a", "spec-b"]
    dropped = clear_for_agent("ag")
    assert dropped == 2
    assert "ag" not in _td._INFLIGHT
    assert "ag" not in _td._QUEUED
    # Idempotent / safe on an unknown agent.
    assert clear_for_agent("nobody") == 0


def test_clear_for_agent_can_preserve_durable_queue():
    from lib import turn_queue
    turn_queue.enqueue(
        queue_id="keep", agent_id="ag", session="mike", text="later",
        trace_id="trace", client_msg_id="keep", synthesize_audio=False,
        origin="user", sender_agent_id="")
    _td._INFLIGHT["ag"] = "trace-1"
    _td._QUEUED["ag"] = ["memory-copy"]

    dropped = clear_for_agent("ag", preserve_queue=True, pause_queue=True)

    assert dropped == 1
    assert turn_queue.get("keep") is not None
    assert turn_queue.is_paused("ag") is True
    assert _td._INFLIGHT["ag"] == _td._STOPPING_SENTINEL
    _td.finish_stop(SimpleNamespace(), "ag")
    assert "ag" not in _td._INFLIGHT


class _Backends:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned = []
        self.interrupted = []
        self.live = True   # whether active_handles reports a live in-flight turn

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        self.interrupted.append((backend, agent_id))
        return 0

    def active_handles(self, backend, agent_id):
        return ["handle"] if self.live else []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append((backend, kwargs))


class _SteerableBackends(_Backends):
    def __init__(self):
        super().__init__()
        self.steered = []

    def steer_turn(self, backend, agent_id, text, *, client_msg_id="",
                   synthesize_audio=False):
        self.steered.append(
            (backend, agent_id, text, client_msg_id, synthesize_audio))
        return True


class _CodexBackends(_Backends):
    CODEX = "codex"


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _make_service(tmp_path, *, retry_scheduler=None):
    """Build an agent + a mockable dispatch service. Returns (service,
    backends, agent_id)."""
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path),
        session="mike", backend="claude",
    )
    agents_db.start_runtime(agent_id, "mike")
    backends = _Backends()
    stream = _Stream()
    ctx = SimpleNamespace(
        default_session="mike",
        agents_path=tmp_path / "unused.json",
        stream=stream,
    )
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
        retry_scheduler=retry_scheduler,
    )
    return service, backends, agent_id


def _run_now(_delay, fn):
    """Synchronous retry scheduler: fire the retry immediately."""
    fn()


def test_dispatch_service_is_mockable_without_http_or_processes(tmp_path, monkeypatch):
    monkeypatch.delenv("CLARP_CACHE_DIR", raising=False)
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path),
        session="mike", backend="claude",
    )
    agents_db.start_runtime(agent_id, "mike")
    backends = _Backends()
    stream = _Stream()
    ctx = SimpleNamespace(
        default_session="mike",
        agents_path=tmp_path / "unused.json",
        stream=stream,
    )
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
    )

    result = service.dispatch(
        text="hello", requested_session="mike", trace_id="trace-1",
        synthesize_audio=False,
    )

    assert result.session == "mike"
    assert result.backend == "claude"
    # A normal send never preempts: the idle agent just spawns one turn.
    assert backends.interrupted == []
    assert len(backends.spawned) == 1
    _, call = backends.spawned[0]
    assert call["text"] == "hello"
    assert call["backend_session_id"] == "backend-session-1"
    assert call["trace_id"] == "trace-1"
    marker = Path(tmp_path, ".cache", "clarp", "source-markers", "mike")
    assert call["voice_preamble"] is False
    assert call["synthesize_audio"] is False
    assert marker.read_text() == "pwa-voice mike 12.500 trace-1 0\n"
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.THINKING
    assert state["detail"]["trace_id"] == "trace-1"
    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-session-1"
    )
    # The user row is now durable (client-authored id), not a placeholder.
    assert [(m["role"], m["text"], m["kind"]) for m in visible] == [
        ("user", "hello", None),
    ]
    assert [event["type"] for event in stream.events[-2:]] == [
        "transcript-updated",
        "agent-state",
    ]

    call["on_result"]({"duration_ms": 50})
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.DONE


def test_unheard_audio_context_reaches_provider_but_not_visible_user_row(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)

    service.dispatch(
        text="Here is the newer request",
        requested_session="mike",
        forced_session="mike",
        trace_id="fresh-turn",
        synthesize_audio=True,
        unheard_audio_sessions=("mike", "rachel"),
    )

    _, call = backends.spawned[0]
    assert "<clarp-delivery-context>" in call["text"]
    assert "Treat those replies as unheard" in call["text"]
    assert call["text"].endswith("Here is the newer request")
    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-session-1"
    )
    assert [message["text"] for message in visible] == ["Here is the newer request"]


def test_unheard_audio_context_is_scoped_to_the_landed_agent(tmp_path):
    service, backends, _ = _make_service(tmp_path)

    service.dispatch(
        text="Normal request", requested_session="mike", forced_session="mike",
        trace_id="normal-turn", synthesize_audio=False,
        unheard_audio_sessions=("rachel",),
    )

    _, call = backends.spawned[0]
    assert call["text"] == "Normal request"


def test_agy_terminal_callback_gate_is_reentrant_and_owner_atomic(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="hello", requested_session="mike", trace_id="trace-a",
                     synthesize_audio=False)
    _, call = backends.spawned[0]

    # AGY delivers its callback inside this gate. on_result calls _finish_turn,
    # so the lock must be reentrant while still making the ownership check and
    # callback one indivisible operation.
    assert call["run_if_owned"](
        lambda: call["on_result"]({"duration_ms": 1})) is True
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.DONE
    assert agent_id not in _td._INFLIGHT

    # A newer in-memory owner wins even before its DB trace row is written.
    service.dispatch(text="again", requested_session="mike", trace_id="trace-a2",
                     synthesize_audio=False)
    _, stale = backends.spawned[-1]
    with _td._TURN_LOCK:
        _td._INFLIGHT[agent_id] = "trace-b"
    assert stale["run_if_owned"](
        lambda: stale["on_result"]({"duration_ms": 2})) is False
    assert agents_db.latest_state(agent_id)["kind"] != AgentState.DONE


def test_new_codex_session_persists_client_message_after_init(tmp_path):
    """Codex learns its backend UUID asynchronously. The optimistic client id
    must be persisted when init arrives, or the imported transcript gets a
    different id and iOS leaves the original bubble stranded at the bottom."""
    agent_id = agents_db.create_agent(
        persona="Antoni", voice_id="V", cwd=str(tmp_path),
        session="antoni", backend="codex",
    )
    agents_db.start_runtime(agent_id, "antoni")
    backends = _CodexBackends()
    ctx = SimpleNamespace(
        default_session="antoni",
        agents_path=tmp_path / "unused.json",
        stream=_Stream(),
    )
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path)

    service.dispatch(
        text="I think I lost my kindle",
        requested_session="antoni",
        trace_id="trace-first",
        client_msg_id="u-first",
        synthesize_audio=False,
    )

    _, call = backends.spawned[0]
    assert call["backend_session_id"] == ""
    assert agents_db.list_messages(agent_id=agent_id, backend_session_id="") == []

    call["on_session_init"]("codex-conversation-1")

    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="codex-conversation-1"
    )
    assert [(m["id"], m["text"]) for m in visible] == [
        ("u-first", "I think I lost my kindle"),
    ]


def test_retry_after_init_resumes_initialized_backend_session(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Antoni", voice_id="V", cwd=str(tmp_path),
        session="antoni", backend="codex",
    )
    agents_db.start_runtime(agent_id, "antoni")
    backends = _CodexBackends()
    ctx = SimpleNamespace(default_session="antoni",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="antoni", trace_id="retry-init",
                     synthesize_audio=False)
    _, first = backends.spawned[0]
    first["on_session_init"]("conversation-1")
    first["on_error"]("read ECONNRESET")
    assert len(backends.spawned) == 2
    _, retry = backends.spawned[1]
    assert retry["backend_session_id"] == "conversation-1"
    assert retry["is_new_session"] is False


def test_session_bind_conflict_does_not_propagate_or_retry(tmp_path):
    owner = agents_db.create_agent(
        persona="Owner", voice_id="V", cwd=str(tmp_path),
        session="owner", backend="codex")
    agents_db.start_runtime(owner, "owner")
    agents_db.bind_backend_session(owner, "owned-conversation")
    agent_id = agents_db.create_agent(
        persona="Antoni", voice_id="V", cwd=str(tmp_path),
        session="antoni", backend="codex")
    agents_db.start_runtime(agent_id, "antoni")
    backends = _CodexBackends()
    ctx = SimpleNamespace(default_session="antoni",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="antoni", trace_id="bind-conflict",
                     synthesize_audio=False)
    _, first = backends.spawned[0]
    assert first["on_session_init"]("owned-conversation") is False
    first["on_error"]("read ECONNRESET")
    assert len(backends.spawned) == 1
    assert backends.interrupted == [("codex", agent_id)]
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.INTERRUPTED
    interrupted = agents_db.conn().execute(
        "SELECT COUNT(*) AS n FROM state_log WHERE agent_id=? AND kind=?",
        (agent_id, AgentState.INTERRUPTED),).fetchone()["n"]
    assert interrupted == 1


def test_automation_prompts_append_tagged_user_rows(tmp_path):
    service, _backends, agent_id = _make_service(tmp_path)

    service.dispatch(
        text=heartbeat.HEARTBEAT_PROMPT, requested_session="mike",
        trace_id="trace-heartbeat", synthesize_audio=False, origin="heartbeat",
    )
    assert agents_db.latest_state(agent_id)["detail"]["origin"] == "heartbeat"
    service.dispatch(
        text=team_leader.TICK_PROMPT, requested_session="mike",
        trace_id="trace-leader", synthesize_audio=False, origin="leader_tick",
    )
    assert agents_db.latest_state(agent_id)["detail"]["origin"] == "leader_tick"

    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-session-1")
    assert sorted(m["text"] for m in visible) == [
        "Automated heartbeat check",
        "Automated leader check",
    ]
    assert all(m["automated"] for m in visible)


def test_leader_heartbeat_dispatch_uses_lean_team_protocol(tmp_path):
    service, backends, leader_id = _make_service(tmp_path)
    worker_id = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path),
        session="omar", backend="claude",
    )
    team = team_store.create_team("Ops")
    team_store.add_member(team["team_id"], leader_id)
    team_store.add_member(team["team_id"], worker_id)
    team_store.set_leader(team["team_id"], leader_id)
    agents_db.record_state(worker_id, AgentState.WAITING, {"reason": "blocked"})

    service.dispatch(
        text=heartbeat.HEARTBEAT_PROMPT, requested_session="mike",
        trace_id="trace-heartbeat", synthesize_audio=False, origin="heartbeat",
    )

    _, call = backends.spawned[0]
    assert "You lead this team; decide/delegate/track" in call["text"]
    assert "Team 'Ops' live member states" in call["text"]
    assert "Omar (omar): waiting" in call["text"]
    assert "LEADER STANDING ORDERS v2" not in call["text"]
    assert "Compact User Values" not in call["text"]
    assert call["text"].rstrip().endswith(heartbeat.HEARTBEAT_PROMPT)


def test_dispatch_injects_pending_team_digest_without_showing_it(tmp_path):
    mike_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )
    rachel_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    agents_db.start_runtime(rachel_id, "rachel")
    team = team_store.create_team("Ops")
    team_store.add_member(team["team_id"], mike_id)
    team_store.add_member(team["team_id"], rachel_id)
    team_store.capture_assistant_message(
        agent_id=mike_id,
        source_message_id="m1",
        text="<team>Prod deploy completed.</team>",
    )
    backends = _Backends()
    ctx = SimpleNamespace(
        default_session="rachel",
        agents_path=tmp_path / "unused.json",
        stream=_Stream(),
    )
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
    )

    service.dispatch(
        text="what changed?", requested_session="rachel", trace_id="trace-2",
        synthesize_audio=False,
    )

    _, call = backends.spawned[0]
    assert call["text"].startswith("--- Clarp team context ---")
    assert "[Ops] Mike: Prod deploy completed." in call["text"]
    assert call["text"].rstrip().endswith("what changed?")
    visible = agents_db.list_messages(
        agent_id=rachel_id, backend_session_id="backend-session-1"
    )
    assert [(m["role"], m["text"]) for m in visible] == [
        ("user", "what changed?"),
    ]
    assert team_store.pending_digest(rachel_id) == ("", [])


def test_dispatch_threads_origin_and_sender_onto_user_row(tmp_path):
    omar_id = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    agents_db.start_runtime(omar_id, "omar")
    backends = _Backends()
    ctx = SimpleNamespace(
        default_session="omar",
        agents_path=tmp_path / "unused.json",
        stream=_Stream(),
    )
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
    )

    service.dispatch(
        text="rebase onto main", requested_session="omar", trace_id="trace-3",
        synthesize_audio=False, origin="agent", sender_agent_id="lena-123",
        client_msg_id="cmid-1",
    )

    row = next(
        m for m in agents_db.list_messages(
            agent_id=omar_id, backend_session_id="backend-session-1")
        if m["role"] == "user"
    )
    assert row["origin"] == "agent"
    assert row["sender_agent_id"] == "lena-123"


def test_forced_session_ignores_sticky_focus(tmp_path):
    mike_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path),
        session="mike", backend="claude",
    )
    rachel_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path),
        session="rachel", backend="claude",
    )
    agents_db.start_runtime(mike_id, "mike")
    agents_db.start_runtime(rachel_id, "rachel")
    agents_db.set_focus(rachel_id)
    backends = _Backends()
    ctx = SimpleNamespace(
        default_session="mike",
        agents_path=tmp_path / "unused.json",
        stream=_Stream(),
    )
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-1", now=lambda: 12.5,
    )

    result = service.dispatch(
        text="continue", requested_session="mike", forced_session="mike",
        trace_id="trace-1", synthesize_audio=False,
    )

    assert result.session == "mike"
    _, call = backends.spawned[0]
    assert call["session"] == "mike"
    assert call["text"] == "continue"


def test_connection_drop_is_silently_retried(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    assert len(backends.spawned) == 1

    # First attempt drops on a socket error → a retry spawns immediately.
    _, call1 = backends.spawned[0]
    call1["on_error"]("API Error: The socket connection was closed unexpectedly.")
    assert len(backends.spawned) == 2
    # Across the gap the agent stays busy, not idle.
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING

    # The retry succeeds → DONE, no interruption.
    _, call2 = backends.spawned[1]
    call2["on_result"]({"duration_ms": 10})
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.DONE


def test_busy_agent_send_preempts_inflight_turn(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    assert len(backends.spawned) == 1      # first turn is running
    assert backends.interrupted == []      # nothing killed yet

    # A backend without steering retains legacy preemption.
    service.dispatch(text="second", requested_session="mike", trace_id="tB",
                     synthesize_audio=False)
    assert len(backends.interrupted) == 1
    assert len(backends.spawned) == 2
    _, second_call = backends.spawned[1]

    # The superseded callback is ignored.
    _, first_call = backends.spawned[0]
    first_call["on_result"]({"duration_ms": 5})
    assert len(backends.spawned) == 2
    assert second_call["text"] == "second"
    assert second_call["trace_id"] == "tB"

    # The new turn completes normally → done.
    second_call["on_result"]({"duration_ms": 7})
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.DONE


def test_explicit_queue_waits_for_current_turn_without_interrupting(tmp_path):
    from lib import turn_queue
    service, backends, _agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    queued = service.dispatch(
        text="second", requested_session="mike", trace_id="tB",
        client_msg_id="u-second", synthesize_audio=False, queue_if_busy=True)
    assert queued.queued is True
    assert queued.queue_depth == 1
    queue_event = service.ctx.stream.events[-1]
    assert queue_event["type"] == "queue-updated"
    assert queue_event["queue_depth"] == 1
    assert queue_event["queue_started"] is False
    assert backends.interrupted == []
    assert len(backends.spawned) == 1
    assert turn_queue.contains("u-second") is True
    assert agents_db.conn().execute(
        "SELECT 1 FROM messages WHERE message_id = 'u-second'"
    ).fetchone() is None
    retried = service.dispatch(
        text="second", requested_session="mike", trace_id="tB-retry",
        client_msg_id="u-second", synthesize_audio=False, queue_if_busy=True)
    assert retried.queued is True
    assert len(backends.spawned) == 1
    assert turn_queue.update_text("u-second", "edited second") is True

    _, first_call = backends.spawned[0]
    first_call["on_result"]({"duration_ms": 5})
    assert len(backends.spawned) == 2
    assert backends.spawned[1][1]["text"] == "edited second"
    assert turn_queue.status("u-second") == "started"
    assert turn_queue.pending_count(_agent_id) == 0
    started_event = next(
        event for event in reversed(service.ctx.stream.events)
        if event["type"] == "queue-updated")
    assert started_event["queue_depth"] == 0
    assert started_event["queue_started"] is True
    assert agents_db.conn().execute(
        "SELECT text FROM messages WHERE message_id = 'u-second'"
    ).fetchone()["text"] == "edited second"


def test_stopped_agent_queue_stays_paused_until_manual_send(tmp_path):
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    turn_queue.set_paused(agent_id, True)

    queued = service.dispatch(
        text="wait for me", requested_session="mike", trace_id="t-paused",
        client_msg_id="q-paused", synthesize_audio=False, queue_if_busy=True)

    assert queued.queued is True
    assert backends.spawned == []
    assert service.recover_queued() == 0
    assert turn_queue.get("q-paused") is not None

    sent = service.dispatch_queued("q-paused")

    assert sent.session == "mike"
    assert len(backends.spawned) == 1
    assert backends.spawned[0][1]["text"] == "wait for me"
    assert turn_queue.status("q-paused") == "started"
    assert turn_queue.is_paused(agent_id) is False


def test_normal_send_admitted_during_stop_runs_after_interrupt(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    _td._INFLIGHT[agent_id] = _td._STOPPING_SENTINEL

    result = service.dispatch(
        text="new work after stop", requested_session="mike", trace_id="t-new",
        client_msg_id="u-new", synthesize_audio=False)

    assert result.queued is True
    assert backends.spawned == []
    _td.finish_stop(service.ctx, agent_id, backend_registry=backends)
    assert len(backends.spawned) == 1
    assert backends.spawned[0][1]["text"] == "new work after stop"


def test_explicit_queue_does_not_steer_busy_codex_turn(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path),
        session="mike", backend="codex")
    agents_db.start_runtime(agent_id, "mike")
    backends = _SteerableBackends()
    ctx = SimpleNamespace(default_session="mike",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1")
    service.dispatch(text="first", requested_session="mike", trace_id="tA")
    result = service.dispatch(text="later", requested_session="mike", trace_id="tB",
                              queue_if_busy=True)
    assert result.queued is True
    assert backends.steered == []
    assert len(backends.spawned) == 1


def test_explicit_queue_behind_terminal_is_durable(tmp_path, monkeypatch):
    from lib import turn_dispatch as module, turn_queue
    service, backends, _agent_id = _make_service(tmp_path)
    monkeypatch.setattr(module, "_terminal_live", lambda _agent_id: True)
    result = service.dispatch(
        text="after terminal", requested_session="mike", trace_id="t-terminal",
        client_msg_id="u-terminal", queue_if_busy=True)
    assert result.queued is True
    assert backends.spawned == []
    assert turn_queue.contains("u-terminal") is True


def test_durable_queue_recovers_after_dispatch_state_loss(tmp_path):
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    turn_queue.enqueue(
        queue_id="u-recover", agent_id=agent_id, session="mike", text="recover me",
        trace_id="t-recover", client_msg_id="u-recover", synthesize_audio=False,
        origin="user", sender_agent_id="")
    assert service.recover_queued() == 1
    assert len(backends.spawned) == 1
    assert backends.spawned[0][1]["text"] == "recover me"
    assert turn_queue.status("u-recover") == "started"


def test_queue_during_claim_to_spawn_window_stays_serial(tmp_path):
    import threading
    service, backends, _agent_id = _make_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_spawn = backends.spawn_turn

    def blocked_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_spawn(*args, **kwargs)

    backends.spawn_turn = blocked_spawn
    first = threading.Thread(target=lambda: service.dispatch(
        text="first", requested_session="mike", trace_id="t-first"))
    first.start()
    assert entered.wait(timeout=2)
    queued = service.dispatch(
        text="second", requested_session="mike", trace_id="t-second",
        client_msg_id="u-second-race", queue_if_busy=True)
    assert queued.queued is True
    assert backends.spawned == []
    release.set()
    first.join(timeout=2)
    assert len(backends.spawned) == 1


def test_idle_queue_request_retry_does_not_duplicate_started_turn(tmp_path):
    service, backends, _agent_id = _make_service(tmp_path)
    first = service.dispatch(
        text="run once", requested_session="mike", trace_id="t-once",
        client_msg_id="u-once", queue_if_busy=True)
    assert first.queued is False
    retried = service.dispatch(
        text="run once", requested_session="mike", trace_id="t-once",
        client_msg_id="u-once", queue_if_busy=True)
    assert retried.queued is False
    assert len(backends.spawned) == 1


def test_client_message_id_is_idempotent_across_normal_and_queue_modes(tmp_path):
    service, backends, _agent_id = _make_service(tmp_path)
    service.dispatch(
        text="run once", requested_session="mike", trace_id="t-normal",
        client_msg_id="u-cross-mode", queue_if_busy=False)
    duplicate = service.dispatch(
        text="run once", requested_session="mike", trace_id="t-queue-retry",
        client_msg_id="u-cross-mode", queue_if_busy=True)
    assert duplicate.queued is False
    assert len(backends.spawned) == 1


def test_busy_codex_send_steers_active_turn_without_interrupting(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path),
        session="mike", backend="codex",
    )
    agents_db.start_runtime(agent_id, "mike")
    backends = _SteerableBackends()
    ctx = SimpleNamespace(default_session="mike",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1")

    service.dispatch(text="original request", requested_session="mike",
                     trace_id="tA", synthesize_audio=True)
    service.dispatch(text="also preserve this", requested_session="mike",
                     trace_id="tB", client_msg_id="u-followup",
                     synthesize_audio=False)

    assert len(backends.spawned) == 1
    assert backends.interrupted == []
    assert backends.steered == [
        ("codex", agent_id, "also preserve this", "u-followup", False)
    ]
    turns = agents_db.conn().execute(
        "SELECT synthesize_audio FROM turns WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    assert [row["synthesize_audio"] for row in turns] == [1]


def test_preempted_turn_failure_is_ignored(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    service.dispatch(text="second", requested_session="mike", trace_id="tB",
                     synthesize_audio=False)
    assert len(backends.spawned) == 2
    _, first_call = backends.spawned[0]
    first_call["on_error"]("read ECONNRESET")
    assert len(backends.spawned) == 2
    _, second_call = backends.spawned[1]
    second_call["on_result"]({"duration_ms": 4})
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.DONE


def test_ghost_session_resets_to_fresh(tmp_path):
    """A bound session whose transcript is missing on disk (a turn crashed before
    creating it) must NOT be resumed — resuming a ghost exits instantly and
    wedges the agent on every turn. Dispatch detects the missing transcript and
    starts a fresh session instead."""
    service, backends, agent_id = _make_service(tmp_path)
    agents_db.bind_backend_session(agent_id, "ghost-xyz")
    assert agents_db.live_backend_session(agent_id) == "ghost-xyz"

    service.dispatch(text="hello", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    assert len(backends.spawned) == 1
    _, call = backends.spawned[0]
    assert call["is_new_session"] is True, "ghost session must start fresh"
    assert call["backend_session_id"] != "ghost-xyz"


def test_existing_session_is_resumed(tmp_path):
    """Control for the ghost guard: when the bound session's transcript DOES
    exist on disk, resume it (don't reset)."""
    service, backends, agent_id = _make_service(tmp_path)
    agents_db.bind_backend_session(agent_id, "real-sess")
    proj = tmp_path / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    (proj / "real-sess.jsonl").write_text("{}\n")

    service.dispatch(text="hello", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    assert call["is_new_session"] is False, "an existing session must be resumed"
    assert call["backend_session_id"] == "real-sess"


def test_stale_inflight_slot_is_freed_on_next_send(tmp_path):
    """If a turn dies without firing its terminal callback (e.g. killed
    mid-flight), its in-flight slot leaks. The next send must NOT queue behind
    that phantom forever — it detects the slot has no live process and takes it
    over. Event-driven (on send), no timer."""
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    assert len(backends.spawned) == 1   # in-flight, live

    # Simulate the turn's process dying WITHOUT a terminal callback: the slot
    # stays marked in-flight, but there's no live process anymore.
    backends.live = False

    # Next send must NOT queue behind the phantom — it spawns immediately.
    service.dispatch(text="second", requested_session="mike", trace_id="tB",
                     synthesize_audio=False)
    assert len(backends.spawned) == 2, \
        "a send must self-heal a leaked in-flight slot, not queue behind a dead turn"
    _, second = backends.spawned[1]
    assert second["text"] == "second"


def test_retry_rearms_pwa_source_marker(tmp_path, monkeypatch):
    """A retry (or redispatch) must re-write the single-use pwa-voice marker,
    so the retried turn stays pwa and the Stop hook still speaks the reply."""
    monkeypatch.delenv("CLARP_CACHE_DIR", raising=False)
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=True)
    marker = Path(tmp_path, ".cache", "clarp", "source-markers", "mike")
    assert marker.exists()

    # The first attempt's UserPromptSubmit hook consumes (unlinks) the marker.
    marker.unlink()

    # Connection drop → retry. The retry must re-arm the marker.
    _, call1 = backends.spawned[0]
    call1["on_error"]("API Error: The socket connection was closed unexpectedly.")
    assert len(backends.spawned) == 2
    assert marker.exists(), "retry did not re-arm the pwa source marker"
    assert marker.read_text() == "pwa-voice mike 12.500 t 1\n"


def test_connection_drop_exhausts_retries_then_interrupts(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)

    # Every attempt drops the connection. After MAX_ATTEMPTS spawns we stop.
    for _ in range(MAX_ATTEMPTS + 2):
        _, call = backends.spawned[-1]
        before = len(backends.spawned)
        call["on_error"]("read ECONNRESET")
        if len(backends.spawned) == before:
            break  # no further retry was scheduled

    assert len(backends.spawned) == MAX_ATTEMPTS
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "connection"


def test_transient_api_error_notifies_without_retry(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("overloaded_error: Overloaded")

    assert len(backends.spawned) == 1  # never retried
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "transient"


def test_usage_limit_notifies_without_retry(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("RESOURCE_EXHAUSTED: exceeded your current quota")

    assert len(backends.spawned) == 1
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "usage_limit"
    assert state["detail"]["message"] == "Usage limit reached"


def test_heartbeat_turn_failure_records_heartbeat_noop(tmp_path, monkeypatch):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    recorded = []
    def _fake_noop(aid, *, is_interrupted=None):
        state = agents_db.latest_state(aid)
        recorded.append((aid, state["kind"], is_interrupted))
    monkeypatch.setattr(
        "lib.heartbeat.record_heartbeat_noop",
        _fake_noop,
    )
    service.dispatch(text="Heartbeat check", requested_session="mike", trace_id="t",
                     origin="heartbeat", synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("RESOURCE_EXHAUSTED: exceeded your current quota")

    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert recorded == [(agent_id, AgentState.INTERRUPTED, True)]



def test_codex_usage_limit_terminal_references_provider_event(
    tmp_path, monkeypatch,
):
    agent_id = agents_db.create_agent(
        persona="Codex", voice_id="V", cwd=str(tmp_path),
        session="codex", backend="codex")
    agents_db.start_runtime(agent_id, "codex")
    backends = _CodexBackends()
    stream = _Stream()
    service = TurnDispatchService(
        SimpleNamespace(
            default_session="codex", agents_path=tmp_path / "unused.json",
            stream=stream),
        backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: "backend-session-codex", now=lambda: 12.5)
    monkeypatch.setattr(
        _td.backend_usage, "record_classified_usage_limit",
        lambda _provider: {
            "type": "provider-limit", "provider_limit_event_id": "ple-1",
            "episode_id": "plp-1", "kind": "hard_limit", "_new": True,
        })

    service.dispatch(text="hi", requested_session="codex", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("RESOURCE_EXHAUSTED: exceeded your current quota")

    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "usage_limit"
    assert state["detail"]["provider_limit_event_id"] == "ple-1"
    assert any(event.get("provider_limit_event_id") == "ple-1"
               for event in stream.events)


def test_interruption_is_recorded_but_not_spoken(tmp_path):
    """Interruptions still flip the agent to INTERRUPTED (so the UI can show
    it) but are muted — no spoken failure clip is queued (hearing raw error
    text read aloud was jarring)."""
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=True)
    _, call = backends.spawned[0]
    call["on_error"]("You've hit your usage limit. Try again at 3:29 PM.")

    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "usage_limit"
    # Muted: nothing is queued for TTS.
    assert tts_queue.recent(5) == []


def test_spoken_usage_limit_includes_reset_time():
    text = _spoken_failure_text(
        persona="Mike",
        category="usage_limit",
        human="Usage limit reached",
        message="You've hit your usage limit. Try again at 3:29 PM.",
    )

    assert text == "Mike is out of usage. Try again at 3:29 PM."


def test_nonzero_runner_exit_notifies_without_retry(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("codex exited rc=1")

    assert len(backends.spawned) == 1
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "runner_exit"
    assert state["detail"]["message"] == "Agent process exited unexpectedly"


_TIMEOUT_MSG = ("clarp turn timed out — backend produced no output before the "
                "idle watchdog fired")


def test_wedged_before_init_is_redelivered_then_notifies(tmp_path):
    """At-least-once delivery: a turn that times out BEFORE producing any
    output (wedged on spawn — no init) never delivered the message, so it is
    re-dispatched up to MAX_ATTEMPTS (the user turn is durably recorded, so
    this is safe), then notifies once attempts are exhausted."""
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    # Every attempt wedges before init (no on_session_init call) → re-delivered.
    for _ in range(MAX_ATTEMPTS + 2):
        _, call = backends.spawned[-1]
        before = len(backends.spawned)
        call["on_error"](_TIMEOUT_MSG)
        if len(backends.spawned) == before:
            break  # no further re-delivery scheduled

    assert len(backends.spawned) == MAX_ATTEMPTS, \
        "a wedged-before-init turn re-delivers up to MAX_ATTEMPTS"
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "timeout"


def test_timeout_after_init_is_also_redelivered(tmp_path):
    """A turn that inits then stalls with no reply output (a hung model/context
    call — the watchdog's post-init deadline) is re-delivered too: the message
    was never answered, and it's durably recorded, so retry is safe."""
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_session_init"]("backend-session-1")  # init landed
    call["on_error"](_TIMEOUT_MSG)                # then stalled → timeout

    assert len(backends.spawned) == 2, "a post-init stall re-delivers the message"


def test_deliberate_interrupt_notifies_without_retry(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("turn_aborted: the user interrupted the previous turn")

    assert len(backends.spawned) == 1
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "interrupted"


def test_error_result_with_connection_text_is_retried(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_result"]({
        "is_error": True,
        "subtype": "error_during_execution",
        "result": "API Error: The socket connection was closed unexpectedly.",
    })
    assert len(backends.spawned) == 2


def test_unknown_error_keeps_legacy_idle_flip(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("some unexpected parser crash")

    assert len(backends.spawned) == 1  # not retried
    # Legacy behaviour: idle flip, not the interrupted badge.
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.IDLE


def _enable_account_failover(monkeypatch, *, available=True):
    from dataclasses import replace
    from lib.claude_failover import ClaudeFailover
    from unittest.mock import Mock
    cfg = replace(_td.config.load(), claude_account_switch_command=("selector",))
    monkeypatch.setattr(_td.config, "load", lambda *args, **kwargs: cfg)
    scheduled = []
    coordinator = ClaudeFailover(
        _td._TURN_LOCK, switch=Mock(return_value=available),
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        now=lambda: 100)
    monkeypatch.setattr(_td, "_CLAUDE_FAILOVER", coordinator)
    return coordinator, scheduled


def test_account_failover_resumes_all_claude_turns_preserving_history_and_queue(tmp_path, monkeypatch):
    import uuid
    from lib import turn_queue
    coordinator, scheduled = _enable_account_failover(monkeypatch)
    service, backend, agent_id = _make_service(tmp_path)
    service.uuid_factory = lambda: str(uuid.uuid4())
    monkeypatch.setattr(_td, "find_latest_jsonl", lambda *args, **kwargs: tmp_path / "native.jsonl")
    for session, provider in (("bella", "claude"), ("codex-agent", "codex")):
        new_id = agents_db.create_agent(persona=session, voice_id="V", cwd=str(tmp_path),
                                        session=session, backend=provider)
        agents_db.start_runtime(new_id, session)
    service.dispatch(text="finish the feature", requested_session="mike", trace_id="a",
                     client_msg_id="original", synthesize_audio=False)
    service.dispatch(text="other Claude task", requested_session="bella", trace_id="b")
    service.dispatch(text="Codex task", requested_session="codex-agent", trace_id="c")
    first, second, codex = [call for _, call in backend.spawned]
    first["on_session_init"](first["backend_session_id"])
    second["on_session_init"](second["backend_session_id"])
    service.dispatch(text="then run checks", requested_session="mike", forced_session="mike", trace_id="queued",
                     client_msg_id="queued", queue_if_busy=True)
    first["on_result"]({"is_error": True, "result": "You've hit your session limit"})
    second["on_error"]("You've hit your session limit")
    assert len(scheduled) == 1
    assert len(backend.spawned) == 3
    scheduled.pop()[1]()
    assert len(backend.spawned) == 5
    resumed = {call["session"]: call for _, call in backend.spawned[3:]}
    for previous in (first, second):
        current = resumed[previous["session"]]
        assert current["backend_session_id"] == previous["backend_session_id"]
        assert current["trace_id"] == previous["trace_id"]
        assert current["model"] == previous["model"]
        assert current["effort"] == previous["effort"]
        assert current["is_new_session"] is False
        assert "Continue the unfinished request" in current["text"]
    assert "codex-agent" not in resumed
    # Late callbacks from terminated attempts cannot complete the new attempt.
    first["on_result"]({"result": "old completion"})
    first["on_error"]("SIGTERM")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING
    assert turn_queue.status("queued") == "queued"
    assert resumed["mike"]["on_session_init"](first["backend_session_id"]) is True
    messages = agents_db.list_messages(agent_id=agent_id,
                                      backend_session_id=first["backend_session_id"])
    assert [m["text"] for m in messages if m["role"] == "user"] == ["finish the feature"]
    assert not coordinator.attempts[agent_id].state.get("account_recovery")
    assert not coordinator.attempts[agent_id].state.get("outcome_seen")
    assert _td._INFLIGHT.get(agent_id) == "a"
    assert agents_db.get_trace(agent_id) == "a"
    resumed["mike"]["on_result"]({"result": "feature finished"})
    assert backend.spawned[-1][1]["text"] == "then run checks"
    assert turn_queue.status("queued") == "started"
    assert len(backend.spawned) == 6


def test_stop_during_account_recovery_prevents_continuation(tmp_path, monkeypatch):
    coordinator, scheduled = _enable_account_failover(monkeypatch)
    service, backend, agent_id = _make_service(tmp_path)
    service.dispatch(text="work", requested_session="mike", trace_id="stop-me")
    backend.spawned[0][1]["on_error"]("usage limit reached")
    clear_for_agent(agent_id)
    scheduled.pop()[1]()
    assert len(backend.spawned) == 1
    coordinator.switch.assert_not_called()
    assert not coordinator.recovering


def test_unavailable_accounts_hold_new_messages_behind_unfinished_work(tmp_path, monkeypatch):
    coordinator, scheduled = _enable_account_failover(monkeypatch, available=False)
    service, backend, agent_id = _make_service(tmp_path)
    service.dispatch(text="work", requested_session="mike", trace_id="pending")
    backend.spawned[0][1]["on_error"]("usage limit reached")
    scheduled.pop()[1]()
    backend.live = False
    result = service.dispatch(text="follow up", requested_session="mike", trace_id="next",
                              queue_if_busy=True)
    assert result.queued
    assert len(backend.spawned) == 1
    assert _td._INFLIGHT[agent_id] == "pending"
    assert agent_id in _td._CLAIMED_AT
    assert coordinator.status()["waiting"] == [agent_id]


def test_temporary_429_does_not_switch_accounts(tmp_path, monkeypatch):
    coordinator, scheduled = _enable_account_failover(monkeypatch)
    service, backend, agent_id = _make_service(tmp_path)
    service.dispatch(text="work", requested_session="mike", trace_id="temporary")
    backend.spawned[0][1]["on_error"]("429 too many requests")
    assert not scheduled
    coordinator.switch.assert_not_called()
    assert agents_db.latest_state(agent_id)["detail"]["reason"] == "transient"


def test_account_recovery_before_native_session_exists_keeps_original_request(tmp_path, monkeypatch):
    coordinator, scheduled = _enable_account_failover(monkeypatch)
    service, backend, _ = _make_service(tmp_path)
    service.dispatch(text="not yet delivered", requested_session="mike", trace_id="fresh")
    first = backend.spawned[0][1]
    first["on_error"]("usage limit reached")
    scheduled.pop()[1]()
    resumed = backend.spawned[-1][1]
    assert resumed["is_new_session"] is True
    assert resumed["backend_session_id"] == first["backend_session_id"]
    assert resumed["text"] == "not yet delivered"


def test_account_recovery_invalidates_an_already_scheduled_connection_retry(tmp_path, monkeypatch):
    import uuid
    coordinator, recovery = _enable_account_failover(monkeypatch)
    retries = []
    service, backend, _ = _make_service(
        tmp_path, retry_scheduler=lambda delay, callback: retries.append(callback))
    service.uuid_factory = lambda: str(uuid.uuid4())
    other = agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path),
                                   session="bella", backend="claude")
    agents_db.start_runtime(other, "bella")
    service.dispatch(text="work", requested_session="mike", forced_session="mike", trace_id="a")
    service.dispatch(text="work", requested_session="bella", forced_session="bella", trace_id="b")
    first, second = [call for _, call in backend.spawned]
    first["on_error"]("connection reset")
    assert len(retries) == 1
    second["on_error"]("usage limit reached")
    recovery.pop()[1]()
    assert len(backend.spawned) == 4
    retries.pop()()
    assert len(backend.spawned) == 4


def test_new_user_turn_parked_on_an_existing_session_keeps_native_user_boundary(tmp_path, monkeypatch):
    coordinator, recovery = _enable_account_failover(monkeypatch)
    service, backend, _ = _make_service(tmp_path)
    monkeypatch.setattr(_td, "find_latest_jsonl", lambda *args, **kwargs: tmp_path / "native.jsonl")
    other = agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path),
                                   session="bella", backend="claude")
    agents_db.start_runtime(other, "bella")
    agents_db.bind_backend_session(other, "older-conversation")
    service.dispatch(text="working", requested_session="mike", forced_session="mike", trace_id="a")
    backend.spawned[0][1]["on_error"]("usage limit reached")
    service.dispatch(text="a new user request", requested_session="bella", forced_session="bella", trace_id="new")
    assert len(backend.spawned) == 1
    recovery.pop()[1]()
    delivered = [call for _, call in backend.spawned if call["session"] == "bella"]
    assert len(delivered) == 1
    assert delivered[0]["text"] == "a new user request"
    assert delivered[0]["backend_session_id"] == "older-conversation"
    assert delivered[0]["is_new_session"] is False


def test_strict_runtime_cancellation_accepts_reaped_recovery_work(tmp_path, monkeypatch):
    from lib import backends as registry
    from lib.runtime_bridge import RuntimeRPCServer
    coordinator, recovery = _enable_account_failover(monkeypatch, available=False)
    service, backend, agent_id = _make_service(tmp_path)
    service.dispatch(text="delegated work", requested_session="mike", trace_id="oracle-work",
                     origin="oracle")
    monkeypatch.setattr(registry, "interrupt", lambda *_: 0)
    runtime = RuntimeRPCServer(tmp_path / "runtime.sock", dispatch_service=service)
    try:
        params = {"agent_id": agent_id, "backend": "claude", "strict": True, "hold": True}
        # Ordinary in-flight work still requires confirmed interruption.
        denied = runtime.dispatch_request("begin_stop", params)
        assert denied["status"] == 502
        assert _td._INFLIGHT[agent_id] == "oracle-work"
        backend.spawned[0][1]["on_error"]("usage limit reached")
        recovery.pop()[1]()
        assert coordinator.parked(agent_id, "oracle-work")
        accepted = runtime.dispatch_request("begin_stop", params)
        assert accepted["ok"] is True
        assert accepted["result"]["terminated"] == 0
        runtime.dispatch_request("finish_stop", {
            "lease_id": accepted["result"]["lease_id"],
            "cancelled_trace_ids": ["oracle-work"],
        })
        recovery.pop()[1]()
        assert len(backend.spawned) == 1
        assert not coordinator.recovering
    finally:
        runtime.server_close()
