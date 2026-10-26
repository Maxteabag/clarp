import pytest
from pathlib import Path
from types import SimpleNamespace

from lib import agents as agents_db
from lib import heartbeat, team_leader, team_store
from lib import tts_queue, turn_queue
from lib.protocol import AgentState
from lib.turn_dispatch import (
    MAX_ATTEMPTS,
    DispatchError,
    TurnDispatchService,
    clear_for_agent,
)
import lib.turn_dispatch as _td


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    # Integration tests may install an RPC client in this process. These unit
    # tests exercise the local runtime, not a socket from an earlier fixture.
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)


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
    team = team_store.create_team("Ops")
    team_store.add_member(team["team_id"], agent_id)
    team_store.set_leader(team["team_id"], agent_id)
    team_store.set_nudge_enabled(team["team_id"], True)

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
    team = team_store.create_team("Ops", communication_enabled=True)
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
    team = team_store.create_team("Ops", communication_enabled=True)
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


def test_paused_queue_holds_automation_but_allows_a_fresh_user_message(tmp_path):
    """Stop parks the follow-ups behind the killed turn. It must not swallow the
    conversation: recorded 2026-09-20, thirteen agents sat paused after one Stop
    each and every new message queued silently, with no Resume anywhere."""
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    turn_queue.set_paused(agent_id, True)

    # Automation waits behind the pause; fresh Oracle user intent is tested below.
    queued = service.dispatch(
        text="wait for me", requested_session="mike", trace_id="t-paused",
        client_msg_id="q-paused", synthesize_audio=False, queue_if_busy=True,
        origin="automation")
    assert queued.queued is True
    assert backends.spawned == []
    assert service.recover_queued() == 0
    assert turn_queue.is_paused(agent_id) is True

    # The user carrying on runs this new request, leaving old work parked.
    sent = service.dispatch(
        text="are you there", requested_session="mike", trace_id="t-user",
        client_msg_id="u-user", synthesize_audio=False, queue_if_busy=True)
    assert sent.queued is not True
    assert turn_queue.is_paused(agent_id) is True
    assert [call[1]["text"] for call in backends.spawned] == ["are you there"]
    # The parked automation item remains available for explicit queue resume.
    assert turn_queue.get("q-paused") is not None


def test_manual_send_of_a_paused_item_still_works(tmp_path):
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    turn_queue.set_paused(agent_id, True)
    service.dispatch(
        text="wait for me", requested_session="mike", trace_id="t-paused",
        client_msg_id="q-paused", synthesize_audio=False, queue_if_busy=True,
        origin="automation")
    sent = service.dispatch_queued("q-paused")
    assert sent.session == "mike"
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


def _queued_goal_wake(tmp_path):
    from lib import db, task_plans, task_goal_recovery

    _td.reset_for_tests()
    retries = []
    service, backends, agent_id = _make_service(
        tmp_path, retry_scheduler=lambda delay, fn: retries.append((delay, fn)))
    backends.live = False
    spawn = backends.spawn_turn

    def spawned(*args, **kwargs):
        spawn(*args, **kwargs)
        backends.live = True

    backends.spawn_turn = spawned
    agents_db.bind_backend_session(agent_id, "native-goal-test")
    transcript = tmp_path / ".claude" / "projects" / "-goal"
    transcript.mkdir(parents=True)
    (transcript / "native-goal-test.jsonl").write_text("{}\n")
    plan = task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True})
    sent = []

    def dispatch(session, text, request):
        # Real claim/admission race: user work arrives after the scheduler's
        # idle check, so canonical dispatch must durably queue the wake.
        service.dispatch(text="user active", requested_session=session,
                         trace_id="user-active", synthesize_audio=False)
        backends.live = True
        result = service.dispatch(
            text=text, requested_session=session, trace_id=request,
            client_msg_id=request, origin="automation", queue_if_busy=True,
            synthesize_audio=False)
        assert result.queued
        sent.append((request, text))
        return result

    assert task_goal_recovery.tick(dispatch, now=db.now_ms() + 130000) == 1
    return service, backends, plan, sent[0], retries


@pytest.mark.parametrize("change", ["checkpoint", "revision", "native", "archived"])
@pytest.mark.parametrize("path", ["restart", "drain", "paused_busy", "manual"])
def test_stale_goal_queue_is_retired_once_without_losing_user_or_peer_work(
        tmp_path, change, path):
    from lib import db, task_plans, task_goal_state

    service, backends, plan, (request, text), retries = _queued_goal_wake(tmp_path)
    peer_id = agents_db.create_agent(
        persona="Peer", session="peer", voice_id="", cwd=str(tmp_path))
    for client, origin in (("user-later", "user"), ("peer-later", "agent")):
        service.dispatch(text=client, requested_session="mike", trace_id=client,
                         forced_session="mike",
                         client_msg_id=client, origin=origin, queue_if_busy=True,
                         sender_agent_id=peer_id if origin == "agent" else "",
                         synthesize_audio=False)
    if change == "checkpoint":
        task_goal_state.mutate(plan["plan_id"], revision=plan["revision"],
            action="checkpoint", data={"progress": "New result", "next_work": "Reassess"})
    elif change == "revision":
        task_plans.update_item(plan["items"][0]["item_id"], "in_progress",
                               revision=plan["revision"])
    elif change == "native":
        agents_db.bind_backend_session(plan["agent_id"], "replacement-native")
    else:
        agents_db.set_archived(plan["agent_id"], True)
    if path == "restart":
        _td.reset_for_tests()
        backends.live = False
        service.recover_queued()
    elif path == "drain":
        backends.live = False
        backends.spawned[0][1]["on_result"]({"duration_ms": 5})
    elif path == "manual":
        with pytest.raises(DispatchError):
            service.dispatch_queued(request)
    else:
        backends.live = True
        turn_queue.set_paused(plan["agent_id"], True)
        service.recover_queued()

    assert turn_queue.status(request) == "cancelled"
    tombstone = db.conn().execute(
        "SELECT text,sender_agent_id FROM queued_turns WHERE queue_id=?", (request,)).fetchone()
    assert tuple(tombstone) == ("", "")
    assert not any(call["text"] == text for _, call in backends.spawned)
    assert not any(row["text"] == text for row in agents_db.list_messages(agent_id=plan["agent_id"]))
    assert turn_queue.status("user-later") in {"queued", "started"}
    assert turn_queue.status("peer-later") == "queued"
    revision = turn_queue.revision(plan["agent_id"])
    backends.live = True
    service.recover_queued()
    assert turn_queue.revision(plan["agent_id"]) == revision
    with pytest.raises(DispatchError):
        service.dispatch(text=text, requested_session="mike", trace_id=request,
                         client_msg_id=request, origin="automation", queue_if_busy=True,
                         synthesize_audio=False)
    assert turn_queue.status(request) == "cancelled"


@pytest.mark.parametrize("blocker", ["capacity", "approval", "busy", "native_owned", "host_paused"])
def test_current_goal_queue_survives_temporary_recovery_blockers(tmp_path, monkeypatch, blocker):
    from lib import artifacts, agent_goals

    service, backends, plan, (request, _text), retries = _queued_goal_wake(tmp_path)
    _td.reset_for_tests()
    backends.live = False
    if blocker == "capacity":
        agents_db.record_state(plan["agent_id"], "idle", {"account_recovery": "waiting"})
    elif blocker == "approval":
        artifacts.create_decision(session="mike", title="Permission", question="Publish?")
    elif blocker == "busy":
        backends.live = True
    elif blocker == "native_owned":
        agent_goals.upsert(plan["agent_id"], session="mike", backend="codex",
                           goal={"objective": "Native work", "status": "active"})
    else:
        monkeypatch.setenv("CLARP_HEARTBEATS_DISABLED", "1")
    revision = turn_queue.revision(plan["agent_id"])
    for _ in range(2):
        assert service.recover_queued() == 0
    assert turn_queue.status(request) == "queued"
    assert turn_queue.revision(plan["agent_id"]) == revision
    assert len(backends.spawned) == 1


def test_scheduler_ticks_do_not_duplicate_a_current_durable_wake_after_restart(tmp_path):
    from lib import db, task_goal_recovery, task_plans

    service, backends, plan, (request, _text), _retries = _queued_goal_wake(tmp_path)
    _td.reset_for_tests()
    backends.live = False
    revision = turn_queue.revision(plan["agent_id"])
    for offset in (999999, 1999999):
        assert task_goal_recovery.tick(
            lambda *args: pytest.fail("already queued wake must not re-enqueue"),
            now=db.now_ms() + offset) == 0
    assert [row["queue_id"] for row in turn_queue.pending(plan["agent_id"])] == [request]
    wake = task_plans.get(plan["plan_id"])["goal"]["continuation"]
    assert wake["request_id"] == request and wake["attempts"] == 1
    assert turn_queue.revision(plan["agent_id"]) == revision


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


def test_usage_limit_never_moves_the_agent_to_another_provider(tmp_path):
    """The agent keeps its own backend and session; the limit is simply surfaced."""
    import threading
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    before = agents_db.get_by_agent_id(agent_id)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("You've hit your usage limit. Try again at 3:29 PM.")

    assert len(backends.spawned) == 1
    assert [name for name in (t.name for t in threading.enumerate())
            if name.startswith("model-fallback")] == []
    after = agents_db.get_by_agent_id(agent_id)
    assert (after["backend"], after["model"], after["effort"]) == (
        before["backend"], before["model"], before["effort"])
    assert agents_db.latest_state(agent_id)["detail"]["reason"] == "usage_limit"
    assert not hasattr(service, "_start_model_fallback")


def test_known_refusal_shows_its_plain_reason(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("Error from provider (Console): OpenCode's free tier can only "
                     "be used from within OpenCode")

    state = agents_db.latest_state(agent_id)
    assert state["detail"]["reason"] == "usage_limit"
    assert state["detail"]["message"].startswith("This free OpenCode model only runs")
    assert "OpenCode's free tier" in state["detail"]["error"]


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


def _failure_rows(agent_id):
    return agents_db.conn().execute(
        "SELECT role, origin, text, trace_id FROM messages "
        "WHERE agent_id = ? AND origin = 'system'", (agent_id,)).fetchall()


def test_unknown_error_interrupts_visibly_with_the_provider_message(tmp_path):
    # It used to flip to IDLE: the chat read "Worked for 1s" as if the turn
    # had answered, with the error nowhere a user would look.
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="hi", requested_session="mike", trace_id="t",
                     synthesize_audio=False)
    _, call = backends.spawned[0]
    call["on_error"]("some unexpected parser crash")

    assert len(backends.spawned) == 1  # not retried
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["message"] == "Turn failed"
    assert state["detail"]["error"] == "some unexpected parser crash"
    rows = _failure_rows(agent_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["trace_id"] == "t"
    assert "Turn failed" in rows[0]["text"]
    assert "some unexpected parser crash" in rows[0]["text"]
    assert any(event.get("type") == "transcript-updated"
               for event in service.ctx.stream.events)


def test_routine_and_deliberate_failures_add_no_chat_row(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="Heartbeat check", requested_session="mike", trace_id="hb",
                     origin="heartbeat", synthesize_audio=False)
    backends.spawned[-1][1]["on_error"]("some unexpected parser crash")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.INTERRUPTED
    service.dispatch(text="hi", requested_session="mike", trace_id="stop",
                     synthesize_audio=False)
    backends.spawned[-1][1]["on_error"]("turn_aborted: the user interrupted the previous turn")
    assert agents_db.latest_state(agent_id)["detail"]["reason"] == "interrupted"
    assert _failure_rows(agent_id) == []


_COMPACT_PLAN_ERROR = (
    'Error running remote compact task: {"type":"error","status":400,'
    '"error":{"type":"invalid_request_error","message":"The \'gpt-6-astra\' '
    'model is not supported when using Codex with a ChatGPT account."}}')


def _codex_plan_service(tmp_path, monkeypatch, selector):
    from dataclasses import replace
    cfg = replace(_td.config.load(), codex_account_switch_command=selector)
    monkeypatch.setattr(_td.config, "load", lambda *args, **kwargs: cfg)
    service, backend, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    agents_db.update_agent(agent_id, backend="codex", model="gpt-6-astra")
    service.dispatch(text="Continue", requested_session="mike", trace_id="plan",
                     synthesize_audio=False)
    backend.spawned[-1][1]["on_session_init"]("codex-thread")
    return service, backend, agent_id


def test_codex_plan_refusal_switches_accounts_like_a_usage_limit(tmp_path, monkeypatch):
    from lib.account_failover import AccountFailover
    from unittest.mock import Mock
    scheduled = []
    coordinator = AccountFailover(
        _td._TURN_LOCK, switch=Mock(return_value=True),
        schedule=lambda d, f: scheduled.append((d, f)), now=lambda: 100)
    monkeypatch.setitem(_td._FAILOVERS, "codex", coordinator)
    service, backend, agent_id = _codex_plan_service(
        tmp_path, monkeypatch, ("codex-selector",))

    backend.spawned[-1][1]["on_error"](_COMPACT_PLAN_ERROR)

    assert coordinator.recovering
    scheduled.pop()[1]()
    coordinator.switch.assert_called_once_with(("codex-selector",), ["gpt-6-astra"])
    resumed = backend.spawned[-1][1]
    assert len(backend.spawned) == 2
    assert resumed["trace_id"] == "plan"
    assert resumed["backend_session_id"] == "codex-thread"
    assert agents_db.latest_state(agent_id)["kind"] != AgentState.INTERRUPTED


def test_codex_plan_refusal_without_a_selector_says_which_model_the_plan_lacks(
        tmp_path, monkeypatch):
    service, backend, agent_id = _codex_plan_service(tmp_path, monkeypatch, ())

    backend.spawned[-1][1]["on_error"](_COMPACT_PLAN_ERROR)

    assert len(backend.spawned) == 1
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["reason"] == "account_plan"
    assert "plan" in state["detail"]["message"].lower()
    assert "gpt-6-astra" in state["detail"]["error"]
    rows = _failure_rows(agent_id)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "plan"
    assert ("The 'gpt-6-astra' model is not supported when using Codex with a "
            "ChatGPT account.") in rows[0]["text"]
    assert '"type":"error"' not in rows[0]["text"]


def _enable_account_failover(monkeypatch, *, available=True):
    from dataclasses import replace
    from lib.account_failover import AccountFailover
    from unittest.mock import Mock
    cfg = replace(_td.config.load(), claude_account_switch_command=("selector",))
    monkeypatch.setattr(_td.config, "load", lambda *args, **kwargs: cfg)
    scheduled = []
    coordinator = AccountFailover(
        _td.OwnershipLock(), switch=Mock(return_value=available),
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        now=lambda: 100)
    monkeypatch.setitem(_td._FAILOVERS, "claude", coordinator)
    return coordinator, scheduled


def test_account_failover_resumes_all_claude_turns_preserving_history_and_queue(tmp_path, monkeypatch):
    import uuid
    from lib import turn_queue
    coordinator, scheduled = _enable_account_failover(monkeypatch)
    service, backend, agent_id = _make_service(tmp_path)
    service.uuid_factory = lambda: str(uuid.uuid4())
    monkeypatch.setattr(_td.backends.by_id("claude"), "resume_target",
                        lambda *args, **kwargs: tmp_path / "native.jsonl")
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
    monkeypatch.setattr(_td.backends.by_id("claude"), "resume_target",
                        lambda *args, **kwargs: tmp_path / "native.jsonl")
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


def test_disabled_leader_tick_is_rejected_before_spawn(tmp_path):
    service, backend, agent_id = _make_service(tmp_path)
    with pytest.raises(DispatchError, match="disabled"):
        service.dispatch(text=team_leader.TICK_PROMPT, requested_session="mike",
                         trace_id="disabled", synthesize_audio=False, origin="leader_tick")
    assert backend.spawned == []


def test_retry_refreshes_team_context_after_communication_disabled(tmp_path):
    scheduled = []
    service, backend, agent_id = _make_service(tmp_path, retry_scheduler=lambda _, fn: scheduled.append(fn))
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], agent_id)
    service.dispatch(text="hello", requested_session="mike", trace_id="retry-team", synthesize_audio=False)
    _, first = backend.spawned[0]
    assert "Team feed" in first["text"]
    team_store.update_team(team["team_id"], communication_enabled=False)
    first["on_error"]("read ECONNRESET")
    assert scheduled
    scheduled.pop(0)()
    assert len(backend.spawned) == 2
    assert backend.spawned[1][1]["text"] == "hello"


@pytest.mark.parametrize('reject_peer',[False,True])
def test_peer_steering_keeps_the_active_oracle_assignment_visible(tmp_path,reject_peer):
    from lib import oracle_delegations
    agent_id=agents_db.create_agent(persona='Rowan',voice_id='V',cwd=str(tmp_path),session='rowan',backend='codex')
    peer_id=agents_db.create_agent(persona='Mira',voice_id='W',cwd=str(tmp_path),session='mira',backend='codex')
    agents_db.start_runtime(agent_id,'rowan')
    backend=_SteerableBackends()
    ctx=SimpleNamespace(default_session='rowan',agents_path=tmp_path/'unused',stream=_Stream())
    service=TurnDispatchService(ctx,backend_registry=backend,home=tmp_path)
    oracle_delegations.begin(delegation_id='stock',trace_id='oracle-stock',client_msg_id='oracle-stock',
        agent_id=agent_id,session='rowan',request_text='Inspect current stock. Read-only.')
    service.dispatch(text='Inspect current stock. Read-only.',requested_session='rowan',trace_id='oracle-stock',
        client_msg_id='oracle-stock',origin='oracle',queue_if_busy=True,synthesize_audio=False)
    if reject_peer:
        backend.steer_turn=lambda *args,**kwargs:False
        # Unsteerable now, the peer waits behind the assignment instead.
        sent=service.dispatch(text='Inspect prices independently.',requested_session='rowan',trace_id='peer-prices',
            origin='agent',sender_agent_id=peer_id,synthesize_audio=False)
        assert sent.queued is True
        assert backend.interrupted==[] and len(backend.spawned)==1
        assert oracle_delegations.get('stock')['status']=='accepted'
        return
    service.dispatch(text='Inspect prices independently.',requested_session='rowan',trace_id='peer-prices',
        origin='agent',sender_agent_id=peer_id,synthesize_audio=False)
    sent=backend.steered[-1][2]
    assert 'Inspect current stock. Read-only.' in sent
    assert 'Inspect prices independently.' in sent
    assert 'additional collaboration' in sent
    from lib.message_store import strip_injected_context
    assert strip_injected_context(sent)=='Inspect prices independently.'
    assert len(backend.spawned)==1 and backend.interrupted==[]
    assert oracle_delegations.get('stock')['status']=='accepted'


def test_oracle_followup_steers_and_tracks_actual_terminal_result(tmp_path):
    from lib import oracle_delegations, turn_queue
    agent_id = agents_db.create_agent(persona="Marcus", voice_id="V", cwd=str(tmp_path),
                                      session="marcus", backend="codex")
    agents_db.start_runtime(agent_id, "marcus")
    backend = _SteerableBackends()
    ctx = SimpleNamespace(default_session="marcus", agents_path=tmp_path / "unused", stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backend, home=tmp_path)
    service.dispatch(text="investigate", requested_session="marcus", trace_id="original",
                     synthesize_audio=False)
    oracle_delegations.begin(delegation_id="followup", trace_id="oracle-followup",
        client_msg_id="oracle-followup", agent_id=agent_id, session="marcus",
        request_text="also check cutoffs")
    result = service.dispatch(text="also check cutoffs", requested_session="marcus",
        trace_id="oracle-followup", client_msg_id="oracle-followup", origin="oracle",
        queue_if_busy=True, synthesize_audio=False)
    assert not result.queued
    assert len(backend.spawned) == 1
    assert backend.interrupted == []
    assert backend.steered == [("codex", agent_id, "also check cutoffs", "oracle-followup", False)]
    assert turn_queue.status("oracle-followup") == "started"
    assert turn_queue.pending_count(agent_id) == 0
    assert oracle_delegations.get("followup")["completion_trace_id"] == "original"
    assert oracle_delegations.complete_for_trace(trace_id="original", message_id="final", text="Checked both")
    row = oracle_delegations.get("followup")
    assert row["status"] == "completed"
    assert row["result_text"] == "Checked both"
    assert row["trace_id"] == "oracle-followup"
    assert not row["delivered"]
    assert oracle_delegations.acknowledge("followup")
    service.dispatch(text="also check cutoffs", requested_session="marcus",
        trace_id="oracle-followup", client_msg_id="oracle-followup", origin="oracle",
        queue_if_busy=True, synthesize_audio=False)
    assert len(backend.steered) == 1
    assert len(backend.spawned) == 1


@pytest.mark.parametrize('stop_during_probe', [False, True])
def test_stop_cancels_codex_connection_retry(tmp_path, monkeypatch, stop_during_probe):
    from lib import codex_app_server
    agent_id = agents_db.create_agent(persona='Stop', voice_id='v', cwd=str(tmp_path), session='stop', backend='codex')
    agents_db.start_runtime(agent_id, 'stop')
    backend = _CodexBackends()
    pending = []
    ctx = SimpleNamespace(default_session='stop', agents_path=tmp_path/'unused', stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backend, home=tmp_path,
                                 retry_scheduler=lambda _delay, fn: pending.append(fn))
    def recover(_message):
        if stop_during_probe:
            clear_for_agent(agent_id)
        return True
    monkeypatch.setattr(codex_app_server, 'recover_usage_failure', recover)
    service.dispatch(text='hi', requested_session='stop', trace_id='stop-retry')
    first = backend.spawned[0][1]
    first['on_session_init']('same-thread')
    first['on_error']("You've hit your usage limit.")
    clear_for_agent(agent_id)
    for callback in pending:
        callback()
    assert len(backend.spawned) == 1
    if stop_during_probe:
        assert pending == []

def test_codex_account_recovery_preserves_native_identity_and_user_stop(tmp_path,monkeypatch):
    from dataclasses import replace
    from lib.account_failover import AccountFailover
    from unittest.mock import Mock
    cfg=replace(_td.config.load(),codex_account_switch_command=('codex-selector',))
    monkeypatch.setattr(_td.config,'load',lambda *args,**kwargs:cfg)
    scheduled=[];coordinator=AccountFailover(_td._TURN_LOCK,switch=Mock(return_value=True),schedule=lambda d,f:scheduled.append((d,f)),now=lambda:100)
    monkeypatch.setitem(_td._FAILOVERS, "codex", coordinator)
    service,backend,aid=_make_service(tmp_path)
    agents_db.update_agent(aid,backend='codex')
    service.dispatch(text='Continue current task',requested_session='mike',trace_id='codex-owned',synthesize_audio=False)
    previous=backend.spawned[-1][1]
    previous['on_session_init']('codex-native-conversation')
    assert coordinator.request(aid,'codex-owned',('codex-selector',))
    scheduled.pop()[1]()
    resumed=backend.spawned[-1][1]
    assert resumed['backend_session_id']=='codex-native-conversation'
    assert resumed['trace_id']=='codex-owned'
    assert 'do not repeat work' in resumed['text']
    assert not resumed['is_new_session']


class _CodexSteerable(_SteerableBackends):
    CODEX = "codex"

    def active_handles(self, backend, agent_id):
        return ["handle"] if self.spawned else []


def _codex_service(tmp_path, backends):
    agent_id = agents_db.create_agent(
        persona="Cipher", voice_id="V", cwd=str(tmp_path),
        session="cipher", backend="codex")
    agents_db.start_runtime(agent_id, "cipher")
    # An established conversation, as Cipher's was: the durable user row is
    # written at admission, not deferred to the backend's init callback.
    agents_db.bind_backend_session(agent_id, "cipher-bsid")
    ctx = SimpleNamespace(default_session="cipher",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1")
    return service, agent_id


def _assert_retry_delivers_once(service, backends, agent_id):
    retried = service.dispatch(text="full control over the pipeline",
                               requested_session="cipher", trace_id="t-retry",
                               client_msg_id="u-lost")
    assert retried.queued is False
    assert backends.steered == [], "nothing may be steered into a turn that never ran"
    assert len(backends.spawned) == 1, "the retry launches exactly one turn"
    rows = agents_db.conn().execute(
        "SELECT message_id, trace_id FROM messages WHERE role = 'user' AND agent_id = ?",
        (agent_id,)).fetchall()
    assert [row["message_id"] for row in rows] == ["u-lost"], "one durable row, no duplicate"
    assert rows[0]["trace_id"] == "t-retry", "the row now belongs to the attempt that ran"
    # A further retry, after the launch succeeded, is the ordinary duplicate.
    again = service.dispatch(text="full control over the pipeline",
                             requested_session="cipher", trace_id="t-again",
                             client_msg_id="u-lost")
    assert again.queued is False
    assert len(backends.spawned) == 1
    assert backends.steered == []


def test_retry_after_a_launch_that_died_on_a_locked_database_is_delivered_once(tmp_path, monkeypatch):
    # Reported 2026-09-20: Cipher's user row was admitted and its turn opened;
    # the very next write ("database is locked" under a 6 s transcript import)
    # raised straight out of /send as a 500, leaving a phantom in-flight slot.
    # Codex is steerable, so the app's retries with the same client id were
    # steered into that phantom: never delivered, never reported.
    import sqlite3
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    real = agents_db.set_trace_for_session
    calls = {"n": 0}

    def locked_once(session, trace_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        real(session, trace_id)

    monkeypatch.setattr(agents_db, "set_trace_for_session", locked_once)
    with pytest.raises(sqlite3.OperationalError):
        service.dispatch(text="full control over the pipeline",
                         requested_session="cipher", trace_id="t-died",
                         client_msg_id="u-lost")
    assert backends.spawned == []
    assert _td._INFLIGHT.get(agent_id) is None, "a dead launch must not keep the slot"
    open_turns = agents_db.conn().execute(
        "SELECT count(*) AS n FROM turns WHERE agent_id = ? AND ended_at IS NULL",
        (agent_id,)).fetchone()["n"]
    assert open_turns == 0, "the turn opened for the dead launch is closed"
    _assert_retry_delivers_once(service, backends, agent_id)


def test_retry_after_a_spawn_failure_is_delivered_once(tmp_path):
    # The same guarantee when the backend itself fails to start (a clean 500).
    class _SpawnDiesOnce(_CodexSteerable):
        def __init__(self):
            super().__init__()
            self.failures_left = 1

        def spawn_turn(self, backend, **kwargs):
            if self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("codex app-server unreachable")
            super().spawn_turn(backend, **kwargs)

    backends = _SpawnDiesOnce()
    service, agent_id = _codex_service(tmp_path, backends)
    with pytest.raises(DispatchError):
        service.dispatch(text="full control over the pipeline",
                         requested_session="cipher", trace_id="t-died",
                         client_msg_id="u-lost")
    assert backends.spawned == []
    assert _td._INFLIGHT.get(agent_id) is None
    _assert_retry_delivers_once(service, backends, agent_id)


def test_phantom_inflight_slot_is_never_steered_into(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path),
        session="mike", backend="codex")
    agents_db.start_runtime(agent_id, "mike")
    backends = _SteerableBackends()
    backends.live = False  # the slot's process is gone
    ctx = SimpleNamespace(default_session="mike",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1")
    with _td._TURN_LOCK:
        _td._INFLIGHT[agent_id] = "t-phantom"
    service.dispatch(text="hello?", requested_session="mike", trace_id="t-new",
                     client_msg_id="u-new")
    assert backends.steered == []
    assert len(backends.spawned) == 1
    assert _td._INFLIGHT.get(agent_id) == "t-new"


def test_lock_while_opening_turn_releases_claim_and_retry_runs(tmp_path, monkeypatch):
    import sqlite3
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    from lib import turn_lifecycle
    real = turn_lifecycle.open_turn
    def locked(**kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(turn_lifecycle, 'open_turn', locked)
    with pytest.raises(sqlite3.OperationalError):
        service.dispatch(text='full control over the pipeline', requested_session='cipher',
                         trace_id='t-died', client_msg_id='u-lost')
    assert _td._INFLIGHT.get(agent_id) is None
    monkeypatch.setattr(turn_lifecycle, 'open_turn', real)
    _assert_retry_delivers_once(service, backends, agent_id)


def test_concurrent_retry_cannot_reclaim_admission_before_first_claim(tmp_path, monkeypatch):
    import threading
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    entered = threading.Event()
    release = threading.Event()
    retry_started = threading.Event()
    errors = []
    real = service._enqueue_if_busy
    def pause_before_claim(spec, **kwargs):
        if spec.trace_id == 'first':
            entered.set()
            assert release.wait(3)
        return real(spec, **kwargs)
    monkeypatch.setattr(service, '_enqueue_if_busy', pause_before_claim)
    def send(trace):
        try:
            if trace == 'retry': retry_started.set()
            service.dispatch(text='same message', requested_session='cipher',
                             trace_id=trace, client_msg_id='u-concurrent')
        except BaseException as exc:
            errors.append(exc)
    first = threading.Thread(target=send, args=('first',))
    retry = threading.Thread(target=send, args=('retry',))
    first.start()
    try:
        assert entered.wait(3)
        retry.start()
        assert retry_started.wait(3)
    finally:
        release.set()
        first.join(4)
        if retry.ident: retry.join(4)
    assert not errors
    assert len(backends.spawned) == 1
    assert not backends.steered


def test_missing_launch_telemetry_never_authorizes_duplicate_work(tmp_path):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    service.dispatch(text='run once', requested_session='cipher', trace_id='first', client_msg_id='u-once')
    # Simulate an already-completed turn whose state telemetry was unavailable.
    _td._INFLIGHT.pop(agent_id, None)
    agents_db.conn().execute('DELETE FROM state_log WHERE agent_id=?', (agent_id,))
    with pytest.raises(DispatchError, match='delivery is unconfirmed'):
        service.dispatch(text='run once', requested_session='cipher', trace_id='retry', client_msg_id='u-once')
    assert len(backends.spawned) == 1


def test_late_prelaunch_failure_does_not_replace_newer_owner_state(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    def superseded(session, trace):
        _td._INFLIGHT[agent_id] = 'newer'
        agents_db.record_state(agent_id, AgentState.THINKING, {'trace_id':'newer'})
        raise RuntimeError('old launch failed')
    monkeypatch.setattr(agents_db, 'set_trace_for_session', superseded)
    with pytest.raises(RuntimeError, match='old launch failed'):
        service.dispatch(text='old', requested_session='cipher', trace_id='old', client_msg_id='u-old')
    assert _td._INFLIGHT[agent_id] == 'newer'
    assert agents_db.latest_state(agent_id)['detail']['trace_id'] == 'newer'
    assert agents_db.trace_launch_status(agent_id, 'old') == 'retryable'


def test_failed_direct_send_can_retry_via_durable_queue(tmp_path, monkeypatch):
    import sqlite3
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    real = agents_db.set_trace_for_session
    def fail(*args): raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(agents_db, 'set_trace_for_session', fail)
    with pytest.raises(sqlite3.OperationalError):
        service.dispatch(text='request', requested_session='cipher', trace_id='failed', client_msg_id='u-queued-retry')
    monkeypatch.setattr(agents_db, 'set_trace_for_session', real)
    service.dispatch(text='request', requested_session='cipher', trace_id='retry', client_msg_id='u-queued-retry', queue_if_busy=True)
    assert len(backends.spawned) == 1
    assert agents_db.conn().execute("SELECT trace_id FROM messages WHERE message_id='u-queued-retry'").fetchone()[0] == 'retry'
    service.dispatch(text='request', requested_session='cipher', trace_id='again', client_msg_id='u-queued-retry', queue_if_busy=True)
    assert len(backends.spawned) == 1


def test_host_heartbeat_disable_blocks_dispatch(monkeypatch, tmp_path):
    from lib import heartbeat
    monkeypatch.setenv('CLARP_HEARTBEATS_DISABLED', '1')
    service, backends, agent_id = _make_service(tmp_path)
    assert not heartbeat.heartbeat_enabled({'heartbeat_enabled': True})
    with pytest.raises(DispatchError, match='Heartbeats are disabled'):
        service.dispatch(text='check', requested_session='mike', trace_id='disabled-wake', origin='heartbeat')
    assert not backends.spawned

@pytest.mark.parametrize('origin', ['user', 'oracle'])
def test_fresh_intended_send_bypasses_pause_without_draining_old_work(tmp_path, origin):
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    turn_queue.set_paused(agent_id, True)
    service.dispatch(text='old parked work', requested_session='mike', trace_id='parked',
                     client_msg_id='parked', origin='automation', queue_if_busy=True,
                     synthesize_audio=False)
    result = service.dispatch(text='fresh intended request', requested_session='mike',
                              trace_id='fresh', client_msg_id='fresh', origin=origin,
                              queue_if_busy=True, synthesize_audio=False)
    assert result.queued is not True
    assert [x[1]['text'] for x in backends.spawned] == ['fresh intended request']
    assert turn_queue.is_paused(agent_id) is True
    assert turn_queue.get('parked') is not None
    service.recover_queued()
    assert len(backends.spawned) == 1
    # Duplicate admission must not replay the fresh request.
    service.dispatch(text='fresh intended request', requested_session='mike',
                     trace_id='fresh', client_msg_id='fresh', origin=origin,
                     queue_if_busy=True, synthesize_audio=False)
    assert len(backends.spawned) == 1


def test_oracle_admission_runs_on_empty_stopped_queue_without_resuming_cancelled_operation(tmp_path, monkeypatch):
    from lib import oracle_delegations, turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    monkeypatch.setattr(_td, 'TurnDispatchService', lambda ctx: service)
    oracle_delegations.begin(delegation_id='cancelled-old', trace_id='oracle-cancelled-old',
                            client_msg_id='oracle-cancelled-old', agent_id=agent_id,
                            session='mike', request_text='old request')
    oracle_delegations.cancel_for_session('mike', stop=lambda: None)
    turn_queue.set_paused(agent_id, True)
    row = oracle_delegations.dispatch(ctx=service.ctx, delegation_id='fresh-oracle',
                                     session='mike', request_text='new voice handoff',
                                     authenticated_at_admission=True)
    assert row['status'] == 'accepted'
    assert turn_queue.status('oracle-fresh-oracle') == 'started'
    assert [x[1]['text'] for x in backends.spawned] == ['new voice handoff']
    assert turn_queue.is_paused(agent_id) is False
    old = oracle_delegations.dispatch(ctx=service.ctx, delegation_id='cancelled-old',
                                     session='mike', request_text='old request',
                                     authenticated_at_admission=True)
    assert old['status'] == 'cancelled'
    assert len(backends.spawned) == 1


class _OrderedTurnLock:
    """Stand-in for _TURN_LOCK that records any acquisition made while this
    thread's SQLite connection has a write transaction open. Lock order is
    _TURN_LOCK -> SQLite everywhere else (Stop, guarded callbacks, retries),
    so the reverse inside dispatch stalls those holders for the busy timeout."""

    def __init__(self, inner):
        self.inner = inner
        self.acquired = 0
        self.violations = []

    def _check(self):
        from lib import db
        if db.conn().in_transaction:
            import traceback
            self.violations.append("".join(traceback.format_stack(limit=8)))

    def __enter__(self):
        self._check()
        self.acquired += 1
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)

    def acquire(self, *args, **kwargs):
        self._check()
        self.acquired += 1
        return self.inner.acquire(*args, **kwargs)

    def release(self):
        return self.inner.release()


class _TransactionAwareStream(_Stream):
    def __init__(self):
        super().__init__()
        self.in_transaction = []

    def broadcast(self, event):
        from lib import db
        if db.conn().in_transaction:
            self.in_transaction.append(event["type"])
        super().broadcast(event)


def test_dispatch_never_takes_turn_lock_or_broadcasts_inside_its_write_transaction(tmp_path, monkeypatch):
    import sqlite3
    guard = _OrderedTurnLock(_td._TURN_LOCK)
    monkeypatch.setattr(_td, "_TURN_LOCK", guard)
    service, backends, agent_id = _make_service(tmp_path)
    stream = _TransactionAwareStream()
    service.ctx.stream = stream

    # 1. Ordinary admission that launches.
    service.dispatch(text="hello", requested_session="mike", trace_id="t-1",
                     client_msg_id="u-1")
    # 2. Duplicate of a launched, live message (dedup path checks the slot).
    service.dispatch(text="hello", requested_session="mike", trace_id="t-1b",
                     client_msg_id="u-1")
    # 3. A second message while the first is live queues behind it.
    service.dispatch(text="later", requested_session="mike", trace_id="t-2",
                     client_msg_id="u-2", queue_if_busy=True)
    # 4. A launch that dies after admission, then the client's retry of the
    #    same id, which relaunches through the never-launched check.
    _td._INFLIGHT.pop(agent_id, None)
    _td._CLAIMED_AT.pop(agent_id, None)
    _td._QUEUED.pop(agent_id, None)
    backends.live = False
    real = agents_db.set_trace_for_session
    dies = {"left": 1}

    def die_once(session, trace_id):
        if dies["left"]:
            dies["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        real(session, trace_id)

    monkeypatch.setattr(agents_db, "set_trace_for_session", die_once)
    with pytest.raises(sqlite3.OperationalError):
        service.dispatch(text="retry me", requested_session="mike",
                         trace_id="t-3", client_msg_id="u-3")
    _td._INFLIGHT.pop(agent_id, None)
    service.dispatch(text="retry me", requested_session="mike",
                     trace_id="t-3b", client_msg_id="u-3")

    assert guard.acquired > 0, "the guard did not observe the dispatch path"
    assert guard.violations == [], "\n---\n".join(guard.violations)
    assert stream.in_transaction == []
    assert any(e["type"] == "transcript-updated" for e in stream.events), \
        "the deferred transcript wake-up is still delivered after COMMIT"
    assert [t for _, kw in backends.spawned for t in [kw["trace_id"]]] == ["t-1", "t-3b"]


def test_janitor_spawn_locks_do_not_accumulate():
    import gc
    for i in range(500):
        _td._janitor_spawn_lock(f"janitor-{i}")
    gc.collect()
    assert len(_td._JANITOR_SPAWN_LOCKS) == 0

    held = _td._janitor_spawn_lock("busy")
    with held:
        assert _td._janitor_spawn_lock("busy") is held
        gc.collect()
        assert len(_td._JANITOR_SPAWN_LOCKS) == 1
    del held
    gc.collect()
    assert len(_td._JANITOR_SPAWN_LOCKS) == 0


# --- Stream A: durable slots, no writes under _TURN_LOCK, one dispatch -------

def test_send_parked_behind_stop_survives_a_runtime_restart(tmp_path):
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    _td.begin_stop(agent_id)
    result = service.dispatch(text="after stop", requested_session="mike",
                              trace_id="parked", client_msg_id="parked")
    assert result.queued and backends.spawned == []
    [row] = turn_queue.parked(agent_id)
    assert (row["trace_id"], row["text"]) == ("parked", "after stop")
    assert turn_queue.pending(agent_id) == []  # invisible to the queue UI

    # Restart: memory is gone, the row is not. The user lifts the Stop pause.
    _td.reset_for_tests()
    turn_queue.set_paused(agent_id, False)
    backends.live = False
    assert service.recover_queued() == 1
    [(_, call)] = backends.spawned
    assert call["trace_id"] == "parked"
    assert turn_queue.parked(agent_id) == []
    assert turn_queue.status("stop-park-parked") == "started"


def test_parked_send_is_unparked_when_the_stop_finishes(tmp_path):
    from lib import turn_queue
    service, backends, agent_id = _make_service(tmp_path)
    snapshot, _, _ = _td.begin_stop(agent_id)
    service.dispatch(text="after stop", requested_session="mike",
                     trace_id="parked", client_msg_id="parked")
    _td.complete_stop(service.ctx, agent_id, snapshot, set(),
                      backend_registry=backends)
    assert [call["trace_id"] for _, call in backends.spawned] == ["parked"]
    assert turn_queue.parked(agent_id) == []


def test_restart_rehydrates_an_open_turn_whose_process_is_alive(tmp_path):
    from lib import turn_lifecycle
    service, backends, agent_id = _make_service(tmp_path)
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="alive")
    agents_db.set_trace_for_session("mike", "alive")
    _td.reset_for_tests()
    service.recover_queued()
    assert _td._INFLIGHT[agent_id] == "alive"
    assert _td.live_work(agent_id).active_trace == "alive"


class _WritesUnderTurnLock:
    """sqlite authorizer that records every write compiled while this thread
    holds _TURN_LOCK."""
    WRITES = {9: "DELETE", 18: "INSERT", 23: "UPDATE"}

    def __init__(self):
        self.seen = []

    def __call__(self, action, arg1, *_rest):
        if action in self.WRITES and _td._TURN_LOCK.held():
            self.seen.append((self.WRITES[action], arg1))
        return 0  # SQLITE_OK

    def __enter__(self):
        from lib import db
        db.conn().set_authorizer(self)
        return self

    def __exit__(self, *exc):
        from lib import db
        db.conn().set_authorizer(None)


def test_enqueue_paths_write_nothing_under_the_turn_lock(tmp_path, monkeypatch):
    service, backends, agent_id = _make_service(tmp_path)
    with _WritesUnderTurnLock() as writes:
        service.dispatch(text="first", requested_session="mike", trace_id="t-1")
        # Spawning slot: queues. Live slot: queues. Terminal: holds.
        _td._CLAIMED_AT[agent_id] = 1.0
        service.dispatch(text="q1", requested_session="mike", trace_id="t-2",
                         client_msg_id="q1", queue_if_busy=True)
        _td._CLAIMED_AT.pop(agent_id, None)
        service.dispatch(text="q2", requested_session="mike", trace_id="t-3",
                         client_msg_id="q2", queue_if_busy=True)
        monkeypatch.setattr(_td, "_terminal_live", lambda _agent: True)
        service.dispatch(text="q3", requested_session="mike", trace_id="t-4",
                         client_msg_id="q3", queue_if_busy=True)
        monkeypatch.setattr(_td, "_terminal_live", lambda _agent: False)
        # Stop barrier: parks durably.
        _td.clear_for_agent(agent_id, preserve_queue=True)
        _td._INFLIGHT[agent_id] = _td._STOPPING_SENTINEL
        service.dispatch(text="p", requested_session="mike", trace_id="t-5")
    assert writes.seen == []
    assert [spec.trace_id for spec in _td._QUEUED[agent_id]] == ["t-5"]


def test_stop_paths_pause_the_queue_after_the_turn_lock(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="t-1")
    with _WritesUnderTurnLock() as writes:
        _snapshot, _dropped, was_paused = _td.begin_stop(agent_id)
    assert writes.seen == []
    assert was_paused is False
    assert turn_queue.is_paused(agent_id)
    assert _td._INFLIGHT[agent_id] == _td._STOPPING_SENTINEL
    turn_queue.set_paused(agent_id, False)
    with _WritesUnderTurnLock() as writes:
        _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)
    assert [w for w in writes.seen if w[1] == "queue_state_revisions"] == []
    assert turn_queue.is_paused(agent_id)


def test_account_recovery_pause_writes_its_state_after_the_locks(tmp_path, monkeypatch):
    coordinator, _scheduled = _enable_account_failover(monkeypatch)
    service, backends, agent_id = _make_service(tmp_path)
    coordinator.recovering = True  # a recovery for another agent is under way
    with _WritesUnderTurnLock() as writes:
        service.dispatch(text="work", requested_session="mike", trace_id="waits")
    assert writes.seen == []
    assert backends.spawned == []
    latest = agents_db.latest_state(agent_id)
    assert (latest["kind"], latest["detail"]["account_recovery"]) == (
        AgentState.THINKING, "waiting")
    assert agent_id in _td._CLAIMED_AT
    assert coordinator.lock is not _td._TURN_LOCK


def test_queued_spawn_after_a_callback_runs_outside_the_turn_lock(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="t-1")
    service.dispatch(text="next", requested_session="mike", trace_id="t-2",
                     client_msg_id="next", queue_if_busy=True)
    held = []
    original = backends.spawn_turn
    backends.spawn_turn = lambda backend, **kw: (
        held.append(_td._TURN_LOCK.held()), original(backend, **kw))
    backends.spawned[0][1]["on_result"]({"result": "done"})
    assert [call["trace_id"] for _, call in backends.spawned] == ["t-1", "t-2"]
    assert held == [False]


def _wire_runtime(server):
    """A RuntimeClient stand-in that crosses the real wire encoder, no socket."""
    from lib.runtime_bridge import RuntimeClient, decode_request, encode_request

    class Client:
        sent = []

        def dispatch_command(self, command):
            request = decode_request(encode_request("dispatch", command.to_wire()))
            self.sent.append(request["params"])
            return RuntimeClient._dispatch_result(
                server.dispatch_request("dispatch", request["params"]))

    return Client()


def test_rpc_round_trip_admits_the_command_once_on_the_owning_side(tmp_path, monkeypatch):
    from lib.prompt_admissions import PromptAdmission
    from lib.runtime_bridge import RuntimeRPCServer
    owner, backends, agent_id = _make_service(tmp_path)
    runtime = RuntimeRPCServer(tmp_path / "rt.sock", dispatch_service=owner)
    try:
        client = _wire_runtime(runtime)
        http = TurnDispatchService(
            SimpleNamespace(runtime_client=client), backend_registry=backends)
        owned = []
        real = TurnDispatchService._dispatch_owned
        monkeypatch.setattr(TurnDispatchService, "_dispatch_owned",
                            lambda self, command: (owned.append(self), real(self, command))[1])
        admission = PromptAdmission(
            admission_id="padm-1", admission_version=1,
            authenticated_at_admission=True, cooperative_principal="user",
            principal_id="user", origin="user", sender_agent_id="",
            channel="chat", observed_at=1, client_admission_id="c-1",
            trace_id="t-rpc", original_text="hello")
        result = http.dispatch(text="hello", requested_session="mike",
                               trace_id="t-rpc", client_msg_id="c-1",
                               prompt_admission=admission,
                               unheard_audio_sessions=("mike",))
    finally:
        runtime.server_close()
    assert result.session == "mike"
    assert owned == [owner]
    [wire] = client.sent
    assert wire["prompt_admission"]["admission_id"] == "padm-1"
    assert "janitor_run_id" not in wire
    [(_, call)] = backends.spawned
    assert call["trace_id"] == "t-rpc"


def test_rpc_runs_the_janitor_demand_authority_check_once(tmp_path, monkeypatch):
    from lib import janitor_autonomy
    from lib.runtime_bridge import RuntimeRPCServer
    owner, backends, _agent_id = _make_service(tmp_path)
    runtime = RuntimeRPCServer(tmp_path / "rt.sock", dispatch_service=owner)
    checks = []
    monkeypatch.setattr(janitor_autonomy, "validate_dispatch",
                        lambda *args: checks.append(args) or False)
    try:
        http = TurnDispatchService(
            SimpleNamespace(runtime_client=_wire_runtime(runtime)),
            backend_registry=backends)
        with pytest.raises(DispatchError) as error:
            http.dispatch(text="tidy", requested_session="mike", trace_id="t-j",
                          origin="heartbeat", client_msg_id="janitor-demand-1")
    finally:
        runtime.server_close()
    assert error.value.status == 409
    assert checks == [("mike", "janitor-demand-1")]


class _Process:
    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive


class _ProcessBackends(_Backends):
    """Every spawned turn is a process that runs until the test ends it."""

    def __init__(self):
        super().__init__()
        self.processes = []

    def spawn_turn(self, backend, **kwargs):
        super().spawn_turn(backend, **kwargs)
        self.processes.append(_Process())

    def active_handles(self, backend, agent_id):
        return [p for p in self.processes if p.alive]


def _process_service(tmp_path):
    service, _, agent_id = _make_service(tmp_path)
    backends = _ProcessBackends()
    service.backends = backends
    return service, backends, agent_id


def _wait_until(predicate, timeout=3.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_a_peer_message_to_a_busy_agent_waits_for_its_turn_instead_of_killing_it(tmp_path):
    service, backends, agent_id = _process_service(tmp_path)
    service.dispatch(text="build the chip", requested_session="mike",
                     trace_id="t-work", synthesize_audio=False)

    sent = service.dispatch(
        text="host shapes are on main", requested_session="mike",
        trace_id="t-peer", client_msg_id="clarp-admin-1",
        synthesize_audio=False, origin="agent", sender_agent_id="parent-1")

    assert sent.queued is True
    assert backends.interrupted == []
    assert [call["text"] for _, call in backends.spawned] == ["build the chip"]

    backends.processes[0].alive = False
    backends.spawned[0][1]["on_result"]({"duration_ms": 5})
    assert [call["text"] for _, call in backends.spawned] == [
        "build the chip", "host shapes are on main"]


def test_no_second_process_starts_while_the_agents_last_turn_process_still_runs(tmp_path):
    """Recorded 2026-10-03: the Host took a turn for finished while its Claude
    process went on working, and the next message started a second Claude on
    the same session. Whatever the Host believes, a live process for the agent
    keeps the next turn waiting until it exits."""
    service, backends, agent_id = _process_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="t1",
                     synthesize_audio=False)
    backends.spawned[0][1]["on_result"]({"duration_ms": 94})
    assert backends.processes[0].alive

    queued = service.dispatch(
        text="queued follow-up", requested_session="mike", trace_id="t2",
        client_msg_id="q-2", synthesize_audio=False, queue_if_busy=True)
    direct = service.dispatch(text="direct follow-up", requested_session="mike",
                              trace_id="t3", synthesize_audio=False)

    assert queued.queued is True and direct.queued is True
    assert len(backends.spawned) == 1
    assert backends.interrupted == []

    backends.processes[0].alive = False
    assert _wait_until(lambda: len(backends.spawned) == 2)
    assert backends.spawned[1][1]["text"] == "queued follow-up"
    assert backends.processes[1].alive
    backends.processes[1].alive = False
    backends.spawned[1][1]["on_result"]({"duration_ms": 5})
    assert [call["text"] for _, call in backends.spawned] == [
        "first", "queued follow-up", "direct follow-up"]


def test_a_goal_wake_keeps_running_when_its_conversation_is_carried_over(tmp_path):
    from lib import db, task_plans, task_goal_recovery
    _td.reset_for_tests()
    service, backends, agent_id = _make_service(tmp_path)
    agents_db.bind_backend_session(agent_id, "native-goal-test")
    transcript = tmp_path / ".claude" / "projects" / "-goal"
    transcript.mkdir(parents=True)
    (transcript / "native-goal-test.jsonl").write_text("{}\n")
    task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True})

    def dispatch(session, text, request):
        return service.dispatch(
            text=text, requested_session=session, trace_id=request,
            client_msg_id=request, origin="automation", queue_if_busy=True,
            synthesize_audio=False)

    assert task_goal_recovery.tick(dispatch, now=db.now_ms() + 130000) == 1
    _backend, spawn = backends.spawned[-1]
    owned = spawn["run_if_owned"]
    # The backend lost the conversation and continued it in a new one, which
    # it binds and carries the goal over to (agy after a prune).
    assert owned(lambda: spawn["on_session_init"]("continued-native"))
    task_plans.carry_native_session(agent_id, "native-goal-test", "continued-native",
                                    "pruned")
    ran = []
    assert owned(lambda: ran.append("event")) and ran == ["event"]


# --- leaked slot: a hook-minted trace must not strand the turn's slot --------
#
# 2026-10-05: a helper's `claude -p` turn ran past the agents trace TTL (1h).
# Background-task notifications then fired UserPromptSubmit inside that same
# process; with no source marker and an expired trace the hook minted
# "local-…" and stored it as the agent's trace. When the process finished,
# on_result saw a trace other than its own, treated the turn as superseded and
# returned before _finish_turn. Nothing else owned the slot, so it stayed held
# for 30h and blocked every idle runtime handoff.

def _hook_mints_local_trace(agent_id):
    agents_db.set_trace(agent_id, "local-1a10d256179")


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_turn_outcome_after_a_hook_minted_trace_releases_its_slot(tmp_path, outcome):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="review the PR", requested_session="mike",
                     trace_id="19f671f72ccfa877", synthesize_audio=False)
    _, call = backends.spawned[0]
    _hook_mints_local_trace(agent_id)

    if outcome == "result":
        call["on_result"]({"duration_ms": 5})
    else:
        call["on_error"]("Turn interrupted")

    assert agent_id not in _td.runtime_status()["active"]


def test_queued_turn_runs_after_a_hook_minted_trace(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    service.dispatch(text="second", requested_session="mike", trace_id="tB",
                     client_msg_id="u-second", synthesize_audio=False,
                     queue_if_busy=True)
    _hook_mints_local_trace(agent_id)

    backends.spawned[0][1]["on_result"]({"duration_ms": 5})

    assert [kw["text"] for _, kw in backends.spawned] == ["first", "second"]
    assert _td.runtime_status()["active"][agent_id] == "tB"


def test_superseded_turn_outcome_leaves_the_newer_owner_alone(tmp_path):
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    service.dispatch(text="second", requested_session="mike", trace_id="tB",
                     synthesize_audio=False)

    backends.spawned[0][1]["on_result"]({"duration_ms": 5})

    assert _td.runtime_status()["active"][agent_id] == "tB"
    assert agents_db.latest_state(agent_id)["kind"] != AgentState.DONE


# --- self-heal: a slot without a process whose turn settled is released -----

def _leak_slot(tmp_path, *, settled_ms_ago):
    from lib import db, turn_lifecycle
    service, backends, agent_id = _make_service(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="tA",
                     synthesize_audio=False)
    turn_lifecycle.try_transition(
        agent_id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {})
    db.conn().execute(
        "UPDATE state_log SET ts = ts - ? WHERE agent_id = ?",
        (settled_ms_ago, agent_id))
    backends.live = False
    return service, backends, agent_id


def test_leaked_slot_of_a_settled_turn_is_released_and_logged(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(_td, "log", lambda event, msg="": logged.append((event, msg)))
    service, _backends, agent_id = _leak_slot(tmp_path, settled_ms_ago=10 * 60_000)

    released = service.release_leaked_slots()

    assert released == {agent_id: "tA"}
    assert agent_id not in _td.runtime_status()["active"]
    assert [event for event, _ in logged].count("leakedSlotReleased") == 1
    assert "trace=tA" in dict(logged)["leakedSlotReleased"]


def test_leaked_slot_hands_over_to_queued_work(tmp_path):
    service, backends, agent_id = _leak_slot(tmp_path, settled_ms_ago=10 * 60_000)
    backends.live = True
    service.dispatch(text="queued", requested_session="mike", trace_id="tQ",
                     client_msg_id="u-q", synthesize_audio=False,
                     queue_if_busy=True)
    backends.live = False

    assert service.release_leaked_slots() == {agent_id: "tA"}

    assert [kw["text"] for _, kw in backends.spawned] == ["first", "queued"]
    assert _td.runtime_status()["active"][agent_id] == "tQ"


@pytest.mark.parametrize("case", ["live_process", "busy_state", "fresh_settle",
                                  "spawning"])
def test_slot_with_live_work_is_never_released(tmp_path, case):
    from lib import turn_lifecycle
    service, backends, agent_id = _leak_slot(
        tmp_path, settled_ms_ago=1_000 if case == "fresh_settle" else 10 * 60_000)
    if case == "live_process":
        backends.live = True
    elif case == "busy_state":
        turn_lifecycle.try_transition(
            agent_id, turn_lifecycle.TurnEvent.RETRY_SCHEDULED, {})
    elif case == "spawning":
        _td._SLOTS.touch_claim(agent_id)

    assert service.release_leaked_slots() == {}
    assert _td.runtime_status()["active"][agent_id] == "tA"


def test_a_failed_turn_keeps_its_prompt_and_runs_what_queued_behind_it(tmp_path):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    service.dispatch(text="first", requested_session="mike", trace_id="t1",
                     client_msg_id="first", synthesize_audio=False)
    queued = service.dispatch(text="second", requested_session="mike", trace_id="t2",
                              client_msg_id="second", queue_if_busy=True,
                              synthesize_audio=False)
    assert queued.queued
    backends.spawned[0][1]["on_error"](_COMPACT_PLAN_ERROR)

    users = agents_db.conn().execute(
        "SELECT text, trace_id FROM messages WHERE agent_id = ? AND role = 'user' "
        "ORDER BY updated_at", (agent_id,)).fetchall()
    assert [(row["text"], row["trace_id"]) for row in users] == [
        ("first", "t1"), ("second", "t2")]
    assert [row["trace_id"] for row in _failure_rows(agent_id)] == ["t1"]
    # The queued prompt is its own turn, running, not failed with the first.
    assert len(backends.spawned) == 2
    assert backends.spawned[1][1]["trace_id"] == "t2"
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING


def test_a_plan_refusal_waiting_for_an_account_is_paused_not_failed(tmp_path, monkeypatch):
    from lib.account_failover import AccountFailover
    from unittest.mock import Mock
    scheduled = []
    coordinator = AccountFailover(
        _td._TURN_LOCK, switch=Mock(return_value=False),
        schedule=lambda d, f: scheduled.append((d, f)), now=lambda: 100)
    monkeypatch.setitem(_td._FAILOVERS, "codex", coordinator)
    service, backend, agent_id = _codex_plan_service(
        tmp_path, monkeypatch, ("codex-selector",))

    backend.spawned[-1][1]["on_error"](_COMPACT_PLAN_ERROR)
    scheduled.pop(0)[1]()

    assert coordinator.status()["waiting"] == [agent_id]
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING
    assert _failure_rows(agent_id) == []


# --- a Stop's queue pause must not fence a goal authorised after it ----------
#
# 2026-10-06 (Omar): Peter stopped the agent (07:05, 15:40), which paused its
# queue and every active goal. The goal was resumed, its dependency finished,
# and the scheduler sent the continuation (07:35:56, 16:35:25). Canonical
# dispatch wrote the wake's own durable queue receipt, which bumped the queue
# revision; the spawn-time fence then compared that revision with the one saved
# at resume, called the goal "Stopped by user; queue is paused", and the wake
# never started. Every later tick saw the same gate, so the goal (and one
# created after the Stop) slept until Peter asked the next morning.

def _goal_owner(tmp_path):
    _td.reset_for_tests()
    service, backends, agent_id = _make_service(tmp_path)
    spawn = backends.spawn_turn

    def spawned(*args, **kwargs):
        spawn(*args, **kwargs)
        backends.live = True

    backends.spawn_turn = spawned
    agents_db.bind_backend_session(agent_id, "native-goal-test")
    transcript = tmp_path / ".claude" / "projects" / "-goal"
    transcript.mkdir(parents=True)
    (transcript / "native-goal-test.jsonl").write_text("{}\n")
    return service, backends, agent_id


def _user_stop(agent_id):
    from lib import task_plans
    # What /stop records durably: the queue pause and the goals' pause.
    turn_queue.set_paused(agent_id, True)
    task_plans.pause_recovery_for_agent(agent_id, "User stopped this agent")


def _goal_tick(service, now_offset=130000):
    """The scheduler's goal tick through the DispatchAdapters wiring."""
    from lib import db, task_goal_recovery

    def goal_continuation(session, text, request):
        return service.dispatch(
            text=text, requested_session=session, trace_id=request,
            client_msg_id=request, origin="automation", queue_if_busy=True,
            synthesize_audio=False,
            allow_paused_queue=task_goal_recovery.allows_paused_queue(request))

    return task_goal_recovery.tick(goal_continuation, now=db.now_ms() + now_offset)


def _goal(session="mike", title="Finish the outcome"):
    from lib import task_plans
    return task_plans.create(
        session=session, title=title, items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True})


def _wakes(backends):
    return [call for _, call in backends.spawned
            if "Continue your durable outcome commitment" in call["text"]]


def _unlaunched(agent_id):
    from lib import db
    rows = db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id=? AND detail LIKE '%dispatch_not_started%'",
        (agent_id,)).fetchall()
    return [row["detail"] for row in rows]


def test_a_goal_resumed_after_a_stop_wakes_despite_its_own_queue_receipt(tmp_path):
    from lib import task_plans, task_goal_state
    service, backends, agent_id = _goal_owner(tmp_path)
    plan = _goal()
    _user_stop(agent_id)
    plan = task_plans.get(plan["plan_id"])
    assert plan["status"] == "paused"
    task_goal_state.mutate(plan["plan_id"], revision=plan["revision"], action="resume",
                           data={"reason": "Peter: continue"})

    assert _goal_tick(service) == 1, task_plans.get(plan["plan_id"])["goal"]["history"][-2:]

    wakes = _wakes(backends)
    assert len(wakes) == 1, "the resumed goal's continuation never started"
    assert _unlaunched(agent_id) == []
    request = task_plans.get(plan["plan_id"])["goal"]["continuation"]["request_id"]
    assert turn_queue.status(request) == "started"


def test_a_goal_created_after_a_stop_wakes(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    _user_stop(agent_id)
    # Peter speaks again after the Stop; the agent opens a new goal in that turn.
    service.dispatch(text="continue until v2 wins", requested_session="mike",
                     trace_id="user-after-stop", client_msg_id="user-after-stop",
                     synthesize_audio=False)
    backends.live = False
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})
    _goal(title="Run the next comparison")

    assert _goal_tick(service) == 1
    assert len(_wakes(backends)) == 1
    assert _unlaunched(agent_id) == []


def test_a_goal_the_stop_paused_stays_asleep_after_a_later_user_turn(tmp_path):
    from lib import task_plans
    service, backends, agent_id = _goal_owner(tmp_path)
    plan = _goal()
    _user_stop(agent_id)
    service.dispatch(text="unrelated question", requested_session="mike",
                     trace_id="user-after-stop", client_msg_id="user-after-stop",
                     synthesize_audio=False)
    backends.live = False
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})

    assert _goal_tick(service, now_offset=9_999_999) == 0
    assert _wakes(backends) == []
    assert task_plans.get(plan["plan_id"])["status"] == "paused"


def test_a_stop_after_the_resume_fences_the_goal_again(tmp_path):
    from lib import task_plans, task_goal_state
    service, backends, agent_id = _goal_owner(tmp_path)
    plan = _goal()
    _user_stop(agent_id)
    plan = task_plans.get(plan["plan_id"])
    task_goal_state.mutate(plan["plan_id"], revision=plan["revision"], action="resume",
                           data={"reason": "Peter: continue"})
    _user_stop(agent_id)

    assert _goal_tick(service, now_offset=9_999_999) == 0
    assert _wakes(backends) == []


def test_a_resumed_goals_wake_queued_behind_user_work_still_runs(tmp_path):
    from lib import task_plans, task_goal_state
    service, backends, agent_id = _goal_owner(tmp_path)
    plan = _goal()
    _user_stop(agent_id)
    plan = task_plans.get(plan["plan_id"])
    task_goal_state.mutate(plan["plan_id"], revision=plan["revision"], action="resume",
                           data={"reason": "Peter: continue"})
    service.dispatch(text="user turn", requested_session="mike",
                     trace_id="user-busy", client_msg_id="user-busy",
                     synthesize_audio=False)
    assert _goal_tick(service) == 1
    request = task_plans.get(plan["plan_id"])["goal"]["continuation"]["request_id"]
    assert turn_queue.status(request) == "queued"

    backends.live = False
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})

    assert len(_wakes(backends)) == 1
    assert turn_queue.status(request) == "started"


def test_a_resumed_goals_queued_wake_runs_after_a_restart(tmp_path):
    from lib import task_plans, task_goal_state
    service, backends, agent_id = _goal_owner(tmp_path)
    plan = _goal()
    _user_stop(agent_id)
    plan = task_plans.get(plan["plan_id"])
    task_goal_state.mutate(plan["plan_id"], revision=plan["revision"], action="resume",
                           data={"reason": "Peter: continue"})
    service.dispatch(text="user turn", requested_session="mike",
                     trace_id="user-busy", client_msg_id="user-busy",
                     synthesize_audio=False)
    assert _goal_tick(service) == 1
    request = task_plans.get(plan["plan_id"])["goal"]["continuation"]["request_id"]
    _td.reset_for_tests()  # the Host restarts; the user turn is gone
    backends.live = False

    assert service.recover_queued() == 1
    assert len(_wakes(backends)) == 1
    assert turn_queue.status(request) == "started"


def test_a_stop_while_a_goal_wake_waits_keeps_it_from_running(tmp_path):
    """A user Stop pauses the goal, so its waiting wake never runs, even after
    a restart; the goal wakes again only once it is resumed."""
    from lib import task_plans
    service, backends, plan, (request, _text), _retries = _queued_goal_wake(tmp_path)
    _user_stop(plan["agent_id"])
    _td.reset_for_tests()
    backends.live = False
    spawned = len(backends.spawned)

    assert service.recover_queued() == 0
    assert len(backends.spawned) == spawned
    assert turn_queue.status(request) != "started"
    assert task_plans.get(plan["plan_id"])["status"] == "paused"


def test_leaked_slot_release_can_be_scoped_to_one_agent(tmp_path):
    service, _backends, agent_id = _leak_slot(tmp_path, settled_ms_ago=10 * 60_000)

    assert service.release_leaked_slots(agent_id="someone-else") == {}
    assert _td.runtime_status()["active"][agent_id] == "tA"
    assert service.release_leaked_slots(agent_id=agent_id) == {agent_id: "tA"}


# ---- listen-first initiatives: the dispatcher's claim (docs/agent-rings.md) -------------

def _call_open_admitted(tmp_path, monkeypatch):
    """A listen_first ring answered by a capable phone, then a silence claim:
    the initiative is admitted as a queued call-open row the drain has not
    picked up yet. Returns (service, backends, ring opening, queue id)."""
    from lib import agent_rings, group_calls
    _td.reset_for_tests()
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    for name, value in (("_kick_queue", lambda: None), ("_push", lambda ring, kind: {}),
                        ("_push_voip", lambda ring: {}), ("_schedule_expiry", lambda ring: None)):
        monkeypatch.setattr(agent_rings, name, value)
    ctx = SimpleNamespace(stream=None, tts=None)
    agent_rings.bind(ctx)
    group_calls.bind(ctx)
    monkeypatch.setattr(group_calls, "resolve_name", lambda name, participants=(): {
        "session": "mike", "persona": "Mike", "agent_id": agent_id})
    ring = agent_rings.ring(agent="mike", reason="Leak at pump 01", opening_line="Line",
                            opening_mode="listen_first")["ring"]
    op = agent_rings.answer("device_phone", ring["ring_id"], opening_mode="listen_first")["opening"]
    out = agent_rings.opening_claim("device_phone", {
        "token": op["token"], "epoch": 1, "claim": "silence", "request_id": "rq-silence",
        "capture_gen": 1, "at_ms": 1250, "watermark_ms": 1290})
    queue_id = out["opening"]["intent"]["admission_id"]
    assert out["opening"]["intent"]["state"] == "admitted" and turn_queue.status(queue_id) == "queued"
    return service, backends, op, queue_id


def _greet(op):
    from lib import agent_rings
    return agent_rings.greeting("device_phone", token=op["token"], epoch=1, client_msg_id="u-hallo",
                                trace_id="tr-hallo", session="mike", call_id=op["call_id"])


def test_a_greeting_that_withdraws_first_means_the_dispatcher_never_spawns(tmp_path, monkeypatch):
    from lib import agent_rings
    service, backends, op, queue_id = _call_open_admitted(tmp_path, monkeypatch)
    claim = agent_rings.claim_initiative
    seen = []

    def greeting_wins(qid):
        # The drain has picked the row; the user's greeting commits first.
        seen.append(_greet(op)["route"])
        result = claim(qid)
        seen.append(result)
        return result
    monkeypatch.setattr(agent_rings, "claim_initiative", greeting_wins)
    service.recover_queued()
    assert seen == ["greeting", False]
    assert backends.spawned == []                                  # no spawn on loss
    assert turn_queue.status(queue_id) == "withdrawn"
    assert _td._INFLIGHT.get(op["session"] and agents_db.get_by_session("mike")["agent_id"]) is None
    assert agents_db.conn().execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0] == 0


def test_a_dispatcher_claim_that_commits_first_spawns_once_and_the_greeting_is_ordinary(tmp_path, monkeypatch):
    from lib import agent_rings
    service, backends, op, queue_id = _call_open_admitted(tmp_path, monkeypatch)
    claim = agent_rings.claim_initiative
    seen = []

    def claim_wins(qid):
        result = claim(qid)
        seen.extend([result, _greet(op)["route"]])               # the greeting lands right after
        return result
    monkeypatch.setattr(agent_rings, "claim_initiative", claim_wins)
    service.recover_queued()
    assert seen == [True, "ordinary"]
    assert len(backends.spawned) == 1                             # the initiative runs exactly once
    assert "Open the conversation" in backends.spawned[0][1]["text"]
    assert turn_queue.status(queue_id) == "started"
    assert agent_rings.opening_state(op["ring_id"])["state"] == "initiative_started"
    assert agents_db.conn().execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0] == 0
    backends.live = False
    service.recover_queued()
    assert len(backends.spawned) == 1


def test_ordinary_queued_rows_drain_exactly_as_before_without_the_opening_claim(tmp_path, monkeypatch):
    from lib import agent_rings
    calls = []
    # raising=False: the same test runs unchanged on 006e9e70, which has no claim hook.
    monkeypatch.setattr(agent_rings, "claim_initiative", lambda qid: calls.append(qid) or True, raising=False)
    service, backends, agent_id = _make_service(tmp_path)
    backends.live = False
    for n in (1, 2):
        turn_queue.enqueue(
            queue_id=f"u-plain-{n}", agent_id=agent_id, session="mike", text=f"plain {n}",
            trace_id=f"t-plain-{n}", client_msg_id=f"u-plain-{n}", synthesize_audio=False,
            origin="user", sender_agent_id="")
    service.recover_queued()
    # This fake backend reports no live turn, so recovery drains both, in FIFO order.
    assert [s[1]["text"] for s in backends.spawned] == ["plain 1", "plain 2"]
    assert [turn_queue.status(f"u-plain-{n}") for n in (1, 2)] == ["started", "started"]
    rows = agents_db.conn().execute("SELECT message_id FROM messages WHERE role='user' ORDER BY seq DESC").fetchall()
    assert sorted(r[0] for r in rows) == ["u-plain-1", "u-plain-2"]           # their user rows, as before
    assert calls == []                                                        # never consulted


# ---- review L1: a claimed opening that never ran is retried exactly once --------------------

def test_a_spawn_failure_after_the_backend_was_invoked_is_unknown_and_never_retried(tmp_path, monkeypatch):
    # D2: a failure inside spawn_turn may follow a launch; only a failure before
    # the backend is invoked is evidence that nothing was sent.
    from lib import agent_rings
    service, backends, op, queue_id = _call_open_admitted(tmp_path, monkeypatch)
    calls = {"n": 0}
    spawn = backends.spawn_turn

    def fails_once(backend, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the backend did not start")
        spawn(backend, **kwargs)
    backends.spawn_turn = fails_once
    service.recover_queued()
    assert backends.spawned == []
    _td._RECOVERY_BACKOFF.clear(agents_db.get_by_session("mike")["agent_id"])
    turn_queue.reset_stale_claims(max_age_ms=0)
    service.recover_queued()
    turn_queue.reset_stale_claims(max_age_ms=0)
    service.recover_queued()
    assert calls["n"] == 1 and backends.spawned == []            # never a second launch
    assert agent_rings.opening_state(op["ring_id"])["state"] == "unknown"


def test_a_claim_left_by_a_previous_process_is_unknown_never_run_again(tmp_path, monkeypatch):
    # D3: a process boundary is never proof that nothing was sent.
    from lib import agent_rings
    service, backends, op, queue_id = _call_open_admitted(tmp_path, monkeypatch)
    assert agent_rings.claim_initiative(queue_id) is True     # claimed, then the process died
    _td.reset_for_tests()
    service.recover_queued()
    service.recover_queued()
    assert backends.spawned == []
    assert turn_queue.status(queue_id) == "unknown"
    assert agent_rings.opening_state(op["ring_id"])["state"] == "unknown"


def test_a_retry_after_a_real_start_never_spawns_again(tmp_path, monkeypatch):
    from lib import agent_rings
    service, backends, op, queue_id = _call_open_admitted(tmp_path, monkeypatch)
    service.recover_queued()
    assert len(backends.spawned) == 1 and turn_queue.status(queue_id) == "started"
    assert agent_rings.claim_initiative(queue_id) is False
    turn_queue.reset_stale_claims(max_age_ms=0)
    service.recover_queued()
    assert len(backends.spawned) == 1


def test_a_retry_rechecks_the_fence(tmp_path, monkeypatch):
    from lib import agent_rings
    service, backends, op, queue_id = _call_open_admitted(tmp_path, monkeypatch)
    assert agent_rings.claim_initiative(queue_id) is True
    turn_queue.set_paused(agents_db.get_by_session("mike")["agent_id"], True)   # a Stop after the claim
    assert agent_rings.claim_initiative(queue_id) is False
    assert agent_rings.opening_state(op["ring_id"])["retired"]["reason"] == "stopped_after_answer"


# ---- 2026-10-25 Lumi: an admitted message whose launch never started -----------------------
# The user row was committed, then the post-COMMIT transcript broadcast (an sse_events INSERT)
# hit "database is locked" and escaped before any turn opened: no turn, no state, and every
# same-id retry was refused as "delivery is unconfirmed". The admission now writes a pending
# launch receipt in the same transaction; it leaves `pending` (fail-closed) right before a
# backend is handed the prompt, so only a provably unsent message is ever launched again.

def _receipt_state(trace_id):
    import json
    from lib import settings_store
    raw = settings_store.get("launch-receipt:" + trace_id)
    return json.loads(raw)["state"] if raw else None


def _user_rows(agent_id):
    return agents_db.conn().execute(
        "SELECT message_id, trace_id FROM messages WHERE role = 'user' AND agent_id = ?",
        (agent_id,)).fetchall()


def _die_once_after_admission(monkeypatch, service):
    """The process dies after the admission COMMIT and before any launch step."""
    real = service._enqueue_if_busy
    calls = {"n": 0}

    def dies(spec, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process died after admission")
        return real(spec, **kwargs)
    monkeypatch.setattr(service, "_enqueue_if_busy", dies)


def test_locked_transcript_broadcast_after_admission_still_launches(tmp_path, monkeypatch):
    import sqlite3
    from lib.protocol import SSEType
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    real = _td.events.broadcast

    def locked_transcript(stream, event):
        if event.get("type") == SSEType.TRANSCRIPT_UPDATED:
            raise sqlite3.OperationalError("database is locked")
        return real(stream, event)
    monkeypatch.setattr(_td.events, "broadcast", locked_transcript)

    result = service.dispatch(text="what did the build log say?", requested_session="cipher",
                              trace_id="t-lumi", client_msg_id="u-lumi")

    assert result.queued is False
    assert len(backends.spawned) == 1, "a failed projection must not stop the launch"
    assert [r["message_id"] for r in _user_rows(agent_id)] == ["u-lumi"]
    assert _receipt_state("t-lumi") == "attempted"


def test_crash_between_admission_and_spawn_relaunches_once_on_same_id_retry(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError, match="process died"):
        service.dispatch(text="are you there?", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-lost")
    _td.reset_for_tests()                         # the process is gone
    assert backends.spawned == []
    assert _receipt_state("t-first") == "pending"

    _assert_retry_delivers_once(service, backends, agent_id)
    assert _receipt_state("t-first") == "relaunched"
    assert _receipt_state("t-retry") == "attempted"


def test_crash_after_spawn_is_never_launched_again(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)

    def died_after_spawn(spec):
        raise SystemExit("process died after the backend took the prompt")
    monkeypatch.setattr(service, "_mark_spawned", died_after_spawn)
    with pytest.raises(SystemExit):
        service.dispatch(text="run once", requested_session="cipher",
                         trace_id="t-ran", client_msg_id="u-ran")
    assert len(backends.spawned) == 1
    _td.reset_for_tests()
    # Its post-launch state never landed either.
    agents_db.conn().execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))
    assert _receipt_state("t-ran") == "attempted"

    with pytest.raises(DispatchError, match="delivery is unconfirmed"):
        service.dispatch(text="run once", requested_session="cipher",
                         trace_id="t-again", client_msg_id="u-ran")
    assert len(backends.spawned) == 1


def test_unrecorded_launch_receipt_refuses_the_handoff_and_retry_runs_once(tmp_path, monkeypatch):
    import sqlite3
    from lib import launch_receipts
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    real = launch_receipts.mark_attempted
    calls = {"n": 0}

    def locked_once(trace_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(trace_id, **kwargs)
    monkeypatch.setattr(launch_receipts, "mark_attempted", locked_once)
    with pytest.raises(DispatchError, match="was not sent"):
        service.dispatch(text="full control over the pipeline", requested_session="cipher",
                         trace_id="t-died", client_msg_id="u-lost")
    assert backends.spawned == [], "never hand over a prompt the receipt cannot record"
    assert _td._INFLIGHT.get(agent_id) is None
    _assert_retry_delivers_once(service, backends, agent_id)


def test_concurrent_same_id_retries_of_an_unlaunched_message_launch_once(tmp_path, monkeypatch):
    import threading
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="same message", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-concurrent")
    _td.reset_for_tests()
    start = threading.Barrier(3)
    errors, results = [], []

    def retry(trace):
        try:
            start.wait(3)
            results.append(service.dispatch(text="same message", requested_session="cipher",
                                            trace_id=trace, client_msg_id="u-concurrent"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=retry, args=(f"t-retry-{n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    start.wait(3)
    for thread in threads:
        thread.join(5)
    assert not errors
    assert len(results) == 2
    assert len(backends.spawned) == 1, "exactly one launch for one message"
    assert len(_user_rows(agent_id)) == 1


def test_stop_after_an_unlaunched_admission_fences_the_resend(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="never mind", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-stopped")
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)   # the user's Stop
    _td.finish_stop(SimpleNamespace(), agent_id)
    assert _receipt_state("t-first") == "fenced"

    # Not "sent" (no 200 dedupe) and not relaunched: the legacy unconfirmed 503.
    with pytest.raises(DispatchError, match="delivery is unconfirmed") as refused:
        service.dispatch(text="never mind", requested_session="cipher",
                         trace_id="t-retry", client_msg_id="u-stopped")
    assert refused.value.status == 503
    assert backends.spawned == [], "an automatic resend after Stop is not new intent"


def test_retry_while_the_message_waits_behind_a_busy_turn_is_not_launched_twice(tmp_path, monkeypatch):
    backends = _Backends()
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike", backend="claude")
    agents_db.start_runtime(agent_id, "mike")
    ctx = SimpleNamespace(default_session="mike",
                          agents_path=tmp_path / "unused.json", stream=_Stream())
    service = TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                                  uuid_factory=lambda: "backend-session-1")
    # An interactive terminal holds the agent: the send waits in this process's memory.
    monkeypatch.setattr(_td, "_terminal_live", lambda agent: agent == agent_id)
    service.dispatch(text="later", requested_session="mike", trace_id="t-held",
                     client_msg_id="u-held")
    assert _receipt_state("t-held") == "pending"
    assert _td._SLOTS.has_queued(agent_id, lambda s: getattr(s, "client_msg_id", "") == "u-held")

    service.dispatch(text="later", requested_session="mike", trace_id="t-retry",
                     client_msg_id="u-held")
    assert backends.spawned == [], "the held original is the only copy that may launch"
    assert _td._SLOTS.queue_depth(agent_id) == 1


def test_a_message_admitted_without_a_receipt_stays_unknown(tmp_path):
    # Rows admitted before this receipt existed (e.g. 2026-10-25 u-D0524D85) are never
    # launched automatically: missing evidence is still not proof.
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    from lib import message_store, settings_store
    service.dispatch(text="historic", requested_session="cipher", trace_id="t-old",
                     client_msg_id="u-old")
    _td._INFLIGHT.pop(agent_id, None)
    agents_db.conn().execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))
    settings_store.delete("launch-receipt:t-old")
    assert message_store.client_message_trace("u-old") == "t-old"
    with pytest.raises(DispatchError, match="delivery is unconfirmed"):
        service.dispatch(text="historic", requested_session="cipher", trace_id="t-new",
                         client_msg_id="u-old")
    assert len(backends.spawned) == 1


def test_a_different_principal_cannot_relaunch_a_pending_message(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="mine", requested_session="cipher", trace_id="t-first",
                         client_msg_id="u-mine")
    _td.reset_for_tests()
    with pytest.raises(DispatchError, match="delivery is unconfirmed"):
        service.dispatch(text="mine", requested_session="cipher", trace_id="t-peer",
                         client_msg_id="u-mine", origin="agent", sender_agent_id="peer")
    assert backends.spawned == []
    assert _receipt_state("t-first") == "pending"


# ---- launch receipts: deterministic barriers (handoff vs retry vs Stop) ---------------------

def _block_handoff(monkeypatch, trace_id):
    """Hold the dispatch of `trace_id` right before its receipt leaves `pending`."""
    import threading
    from lib import launch_receipts
    real = launch_receipts.mark_attempted
    entered, release = threading.Event(), threading.Event()

    def blocked(trace, **kwargs):
        if trace == trace_id:
            entered.set()
            assert release.wait(5)
        return real(trace, **kwargs)
    monkeypatch.setattr(launch_receipts, "mark_attempted", blocked)
    return entered, release


def _in_thread(fn):
    import threading
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_retry_between_commit_and_handoff_waits_and_launches_once(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    entered, release = _block_handoff(monkeypatch, "t-first")
    first, first_box = _in_thread(lambda: service.dispatch(
        text="once", requested_session="cipher", trace_id="t-first", client_msg_id="u-once"))
    try:
        assert entered.wait(5), "the first send is committed and parked before its handoff"
        assert _receipt_state("t-first") == "pending"
        # Even without the per-client lock, the slot proves the launch is in progress.
        slot = service._slot_snapshot(SimpleNamespace(agent_id=agent_id))
        assert slot == ("t-first", True)
        assert service._admitted_but_never_launched(
            SimpleNamespace(agent_id=agent_id, origin="user", sender_agent_id=""),
            "u-once", slot=slot, held=False) is False
        retry, retry_box = _in_thread(lambda: service.dispatch(
            text="once", requested_session="cipher", trace_id="t-retry", client_msg_id="u-once"))
        retry.join(0.5)
        assert retry.is_alive(), "the same-id retry waits for the first attempt"
    finally:
        release.set()
    first.join(5)
    retry.join(5)
    assert "error" not in first_box and "error" not in retry_box
    assert len(backends.spawned) == 1
    assert len(_user_rows(agent_id)) == 1
    assert _receipt_state("t-first") == "attempted"


def test_stop_fence_before_spawn_handoff_refuses_it(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    entered, release = _block_handoff(monkeypatch, "t-first")
    first, box = _in_thread(lambda: service.dispatch(
        text="stop me", requested_session="cipher", trace_id="t-first", client_msg_id="u-stop"))
    try:
        assert entered.wait(5)
        # The durable fence lands while this attempt still owns the slot: the
        # compare-and-set then changes nothing and must not authorize the spawn.
        from lib import launch_receipts
        assert launch_receipts.fence_agent(agent_id) == 1
    finally:
        release.set()
    first.join(5)
    assert isinstance(box.get("error"), DispatchError)
    assert (box["error"].status, str(box["error"])) == (503, _td.UNCONFIRMED_DELIVERY)
    assert box["error"].reason == "fenced"                   # internal, diagnostics only
    assert backends.spawned == []
    assert _receipt_state("t-first") == "fenced"
    with pytest.raises(DispatchError, match="delivery is unconfirmed"):
        service.dispatch(text="stop me", requested_session="cipher", trace_id="t-again",
                         client_msg_id="u-stop")
    assert backends.spawned == []


def test_real_stop_during_handoff_never_launches_after_stop(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    entered, release = _block_handoff(monkeypatch, "t-first")
    first, box = _in_thread(lambda: service.dispatch(
        text="stop me", requested_session="cipher", trace_id="t-first", client_msg_id="u-stop"))
    try:
        assert entered.wait(5)
        _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)
    finally:
        release.set()
    first.join(5)
    assert "error" in box
    assert backends.spawned == [], "no provider handoff after Stop"
    assert _receipt_state("t-first") == "fenced"


def test_stop_fence_before_steer_refuses_it(tmp_path, monkeypatch):
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    service.dispatch(text="long task", requested_session="cipher", trace_id="t-live",
                     client_msg_id="u-live")
    assert len(backends.spawned) == 1                       # a live, steerable turn
    entered, release = _block_handoff(monkeypatch, "t-steer")
    second, box = _in_thread(lambda: service.dispatch(
        text="also this", requested_session="cipher", trace_id="t-steer",
        client_msg_id="u-steer"))
    try:
        assert entered.wait(5)
        from lib import launch_receipts
        assert launch_receipts.fence_agent(agent_id) == 1
    finally:
        release.set()
    second.join(5)
    assert isinstance(box.get("error"), DispatchError)
    assert (box["error"].status, str(box["error"])) == (503, _td.UNCONFIRMED_DELIVERY)
    assert backends.steered == []
    assert len(backends.spawned) == 1, "a refused steer never falls through to a preempt"


def test_a_failed_stop_fence_write_still_blocks_the_resend(tmp_path, monkeypatch):
    import sqlite3
    from lib import launch_receipts
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="never mind", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-stopped")

    def locked(agent):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(launch_receipts, "fence_agent", locked)
    monkeypatch.setattr(_td, "_FENCE_RETRY_DELAYS", ())
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)   # Stop still completes
    _td.finish_stop(SimpleNamespace(), agent_id)
    assert _receipt_state("t-first") == "pending", "the durable write failed"

    with pytest.raises(DispatchError, match="delivery is unconfirmed"):
        service.dispatch(text="never mind", requested_session="cipher",
                         trace_id="t-retry", client_msg_id="u-stopped")
    assert backends.spawned == []


def test_a_send_held_behind_stop_still_runs_after_a_racing_fence(tmp_path, monkeypatch):
    from lib import launch_receipts
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)   # Stop barrier up
    service.dispatch(text="do this instead", requested_session="cipher",
                     trace_id="t-newer", client_msg_id="u-newer")
    assert backends.spawned == []                            # held behind the barrier
    assert launch_receipts.fence_agent(agent_id) == 1        # a fence that raced it
    _td.finish_stop(service.ctx, agent_id, backend_registry=backends)
    assert len(backends.spawned) == 1, "Stop decided it runs; the receipt does not override that"
    assert _receipt_state("t-newer") == "attempted"


def test_handoff_verdicts_never_trust_an_unchanged_row(tmp_path):
    from lib import launch_receipts
    launch_receipts.record_pending(agent_id="a", trace_id="t1", client_msg_id="u",
                                   origin="user")
    launch_receipts.record_pending(agent_id="a", trace_id="t2", client_msg_id="u",
                                   origin="user", supersedes="t1")
    with pytest.raises(launch_receipts.HandoffRefused) as refused:
        launch_receipts.mark_attempted("t1")
    assert refused.value.state == "relaunched"
    with pytest.raises(launch_receipts.HandoffRefused):
        launch_receipts.mark_attempted("t1", override_fence=True)   # superseded always refuses
    assert launch_receipts.mark_attempted("t2") == "attempted"
    assert launch_receipts.mark_attempted("t2") == "already_attempted"
    assert launch_receipts.mark_attempted("no-such-trace") == "no_receipt"
    launch_receipts.record_pending(agent_id="a", trace_id="t3", client_msg_id="v",
                                   origin="user")
    launch_receipts.fence_agent("a")
    with pytest.raises(launch_receipts.HandoffRefused):
        launch_receipts.mark_attempted("t3")
    assert launch_receipts.mark_attempted("t3", override_fence=True) == "attempted"


def test_pruning_is_bounded_and_never_revives_a_fenced_receipt(tmp_path):
    import json
    from lib import launch_receipts, maintenance, settings_store
    launch_receipts.reset_for_tests()
    old = 1_000
    for trace, state in (("p", "pending"), ("a", "attempted"), ("f", "fenced")):
        settings_store.set_text("launch-receipt:" + trace, json.dumps(
            {"state": state, "agent_id": "ag", "trace_id": trace, "client_msg_id": "u",
             "origin": "user", "sender_agent_id": "", "admitted_at": old}), updated_at=old)
    settings_store.set_text("unrelated-key", "keep", updated_at=old)
    plan = " ".join(str(row[-1]) for row in agents_db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT key FROM settings WHERE key >= ? AND key < ?",
        ("launch-receipt:", "launch-receipt;")))
    assert "INDEX" in plan.upper(), plan
    # An admission never prunes (F4 N7): the Host maintenance pass does.
    launch_receipts.record_pending(agent_id="ag", trace_id="fresh", client_msg_id="w",
                                   origin="user")
    assert _receipt_state("p") == "pending"
    maintenance.prune_database(now_ms=launch_receipts.RETENTION_MS + 10 * old)
    assert _receipt_state("p") is None and _receipt_state("a") is None
    assert _receipt_state("f") == "fenced", "a fenced receipt is never pruned"
    assert _receipt_state("fresh") == "pending", "a recent receipt is kept"
    assert settings_store.get("unrelated-key") == "keep"
    assert launch_receipts.status("ag", "p") == "unknown", "expiry never yields retryable"


def test_a_failed_receipt_write_rolls_back_the_admission(tmp_path, monkeypatch):
    import sqlite3
    from lib import launch_receipts
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    real = launch_receipts.record_pending
    calls = {"n": 0}

    def locked_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(**kwargs)
    monkeypatch.setattr(launch_receipts, "record_pending", locked_once)
    with pytest.raises(sqlite3.OperationalError):
        service.dispatch(text="atomic", requested_session="cipher", trace_id="t-died",
                         client_msg_id="u-lost")
    assert _user_rows(agent_id) == [], "no user row without its receipt"
    _assert_retry_delivers_once(service, backends, agent_id)


def test_a_stop_across_a_restart_still_fences_the_resend(tmp_path, monkeypatch):
    # Owner boundary + restart: the fence write failed and the process that
    # recorded the in-memory epoch is gone. The durable Stop generation, moved
    # in the same transaction as Stop's pause, still refuses the relaunch.
    import sqlite3
    from lib import launch_receipts
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="never mind", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-stopped")

    def locked(agent):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(launch_receipts, "fence_agent", locked)
    monkeypatch.setattr(_td, "_FENCE_RETRY_DELAYS", ())
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)   # the user's Stop
    _td.finish_stop(SimpleNamespace(), agent_id)
    _td.reset_for_tests()                                   # the runtime restarts
    assert _receipt_state("t-first") == "pending", "no durable fence was written"

    with pytest.raises(DispatchError, match="delivery is unconfirmed") as refused:
        service.dispatch(text="never mind", requested_session="cipher",
                         trace_id="t-retry", client_msg_id="u-stopped")
    assert refused.value.status == 503
    assert backends.spawned == []


def test_a_maintenance_cancel_barrier_does_not_withdraw_a_user_send(tmp_path, monkeypatch):
    # begin_stop is also the janitor-cancel barrier: it must not fence the
    # user's admitted send that is about to be handed over.
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    entered, release = _block_handoff(monkeypatch, "t-user")
    first, box = _in_thread(lambda: service.dispatch(
        text="mine", requested_session="cipher", trace_id="t-user", client_msg_id="u-user"))
    try:
        assert entered.wait(5)
        snapshot, _dropped, _was_paused = _td.begin_stop(agent_id)   # maintenance barrier
        assert _receipt_state("t-user") == "pending"
    finally:
        release.set()
    first.join(5)
    assert "error" not in box
    assert len(backends.spawned) == 1
    assert _receipt_state("t-user") == "attempted"
    _td.restore_stop_state(agent_id, snapshot)


def test_retry_after_a_relaunch_is_never_a_200_for_the_superseded_attempt(tmp_path, monkeypatch):
    # The original attempt is superseded by a relaunch; its own late handoff is
    # refused with the legacy unconfirmed 503, never "sent" and never a launch.
    from lib import launch_receipts
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="hello", requested_session="cipher", trace_id="t-old",
                         client_msg_id="u-same")
    _td.reset_for_tests()
    service.dispatch(text="hello", requested_session="cipher", trace_id="t-new",
                     client_msg_id="u-same")
    assert len(backends.spawned) == 1 and _receipt_state("t-old") == "relaunched"
    with pytest.raises(launch_receipts.HandoffRefused):
        launch_receipts.mark_attempted("t-old")
    spec = SimpleNamespace(origin="user", client_msg_id="u-same", trace_id="t-old",
                           agent_id=agent_id, session="cipher", queue_id="", park_id="",
                           after_stop=False, prompt_admission_id="")
    with pytest.raises(DispatchError) as refused:
        _td._record_launch_attempt(spec)
    assert (refused.value.status, str(refused.value)) == (503, _td.UNCONFIRMED_DELIVERY)
    assert refused.value.reason == "relaunched"
    assert len(backends.spawned) == 1


def test_a_maintenance_cancel_and_restore_never_strands_a_pending_user_retry(tmp_path, monkeypatch):
    # The maintenance cancel installs the Stop barrier and pauses, then restores
    # the queue; it moves the shared stop generation but is not the user's Stop.
    # An admitted-but-unlaunched user send must still launch exactly once.
    from lib import turn_queue
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="full control over the pipeline", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-lost")
    before_shared = turn_queue.stop_generation(agent_id)
    before_user = turn_queue.user_stop_generation(agent_id)
    snapshot, _dropped, was_paused = _td.begin_stop(agent_id)     # maintenance cancel
    _td.restore_stop_state(agent_id, snapshot)
    turn_queue.set_paused(agent_id, was_paused)                   # its restore
    assert turn_queue.stop_generation(agent_id) > before_shared, "shared consumers unchanged"
    assert turn_queue.user_stop_generation(agent_id) == before_user
    assert _receipt_state("t-first") == "pending"

    _assert_retry_delivers_once(service, backends, agent_id)
    assert _receipt_state("t-retry") == "attempted"


def test_only_the_users_stop_moves_the_user_stop_generation(tmp_path):
    from lib import turn_queue
    backends = _CodexSteerable()
    _service, agent_id = _codex_service(tmp_path, backends)
    shared, user = turn_queue.stop_generation(agent_id), turn_queue.user_stop_generation(agent_id)
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)   # local user Stop
    _td.finish_stop(SimpleNamespace(), agent_id)
    after_user = turn_queue.stop_generation(agent_id)
    assert after_user > shared, "the shared generation still moves for its consumers"
    assert turn_queue.user_stop_generation(agent_id) == user + 1
    snapshot, _dropped, was_paused = _td.begin_stop(agent_id)            # maintenance
    _td.restore_stop_state(agent_id, snapshot)
    turn_queue.set_paused(agent_id, was_paused)
    assert turn_queue.stop_generation(agent_id) > after_user
    assert turn_queue.user_stop_generation(agent_id) == user + 1, "maintenance never moves it"
    snapshot, _dropped, _was = _td.begin_stop(agent_id, user_stop=True)   # the RPC user route
    _td.restore_stop_state(agent_id, snapshot)
    assert turn_queue.user_stop_generation(agent_id) == user + 2


def test_a_failed_stop_fence_write_retries_through_the_tracked_scheduler(monkeypatch):
    # The retry must use the dispatcher's scheduler (tracked and cancelled per
    # test), never a private timer that could outlive the test that armed it.
    import sqlite3
    from lib import launch_receipts
    calls, scheduled = {"n": 0}, []

    def flaky(agent, **_hooks):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return 0
    monkeypatch.setattr(launch_receipts, "fence_agent", flaky)
    monkeypatch.setattr(_td, "_default_retry_scheduler",
                        lambda delay, fn: scheduled.append((delay, fn)))
    _td.fence_user_stop("agent-retry")
    assert [d for d, _fn in scheduled] == [_td._FENCE_RETRY_DELAYS[0]]
    scheduled[0][1]()                                       # the timer fires
    assert calls["n"] == 2 and len(scheduled) == 1, "a successful retry schedules nothing more"
    _td.reset_for_tests()


def test_the_stop_override_runs_the_newer_send_but_keeps_the_older_one_fenced(tmp_path, monkeypatch):
    # The user's Stop fences what was admitted before it; the override only lets
    # a send that Stop itself held as newer work run. It never unfences the old one.
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="old", requested_session="cipher", trace_id="t-old",
                         client_msg_id="u-old")
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True)   # the user's Stop
    assert _receipt_state("t-old") == "fenced"
    service.dispatch(text="new", requested_session="cipher", trace_id="t-new",
                     client_msg_id="u-new")                                # held behind it
    _td.finish_stop(service.ctx, agent_id, backend_registry=backends)
    assert [s[1]["trace_id"] for s in backends.spawned] == ["t-new"]
    assert _receipt_state("t-new") == "attempted" and _receipt_state("t-old") == "fenced"
    with pytest.raises(DispatchError, match="delivery is unconfirmed"):
        service.dispatch(text="old", requested_session="cipher", trace_id="t-old-retry",
                         client_msg_id="u-old")
    assert len(backends.spawned) == 1


def test_an_agent_stop_then_resume_and_restart_keeps_the_pending_user_retry(tmp_path, monkeypatch):
    from lib import turn_queue
    backends = _CodexSteerable()
    service, agent_id = _codex_service(tmp_path, backends)
    _die_once_after_admission(monkeypatch, service)
    with pytest.raises(RuntimeError):
        service.dispatch(text="full control over the pipeline", requested_session="cipher",
                         trace_id="t-first", client_msg_id="u-lost")
    user_gen = turn_queue.user_stop_generation(agent_id)
    _td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True, user_stop=False)
    _td.finish_stop(SimpleNamespace(), agent_id)
    assert turn_queue.is_paused(agent_id)
    assert turn_queue.user_stop_generation(agent_id) == user_gen
    assert _receipt_state("t-first") == "pending"
    turn_queue.set_paused(agent_id, False)                 # the user resumes
    _td.reset_for_tests()                                  # and the runtime restarts
    _assert_retry_delivers_once(service, backends, agent_id)
