import pytest
from pathlib import Path
from types import SimpleNamespace

from lib import agents as agents_db
from lib import heartbeat, team_leader, team_store
from lib import tts_queue
from lib.protocol import AgentState
from lib.turn_dispatch import (
    MAX_ATTEMPTS,
    DispatchError,
    TurnDispatchService,
    _spoken_failure_text,
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
        with pytest.raises(DispatchError) as error:
            service.dispatch(text='Inspect prices independently.',requested_session='rowan',trace_id='peer-prices',
                origin='agent',sender_agent_id=peer_id,synthesize_audio=False)
        assert error.value.status==503
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
    from lib.claude_failover import ClaudeFailover
    from unittest.mock import Mock
    cfg=replace(_td.config.load(),codex_account_switch_command=('codex-selector',))
    monkeypatch.setattr(_td.config,'load',lambda *args,**kwargs:cfg)
    scheduled=[];coordinator=ClaudeFailover(_td._TURN_LOCK,switch=Mock(return_value=True),schedule=lambda d,f:scheduled.append((d,f)),now=lambda:100)
    monkeypatch.setattr(_td,'_CODEX_FAILOVER',coordinator)
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
    real = agents_db.open_turn
    def locked(**kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(agents_db, 'open_turn', locked)
    with pytest.raises(sqlite3.OperationalError):
        service.dispatch(text='full control over the pipeline', requested_session='cipher',
                         trace_id='t-died', client_msg_id='u-lost')
    assert _td._INFLIGHT.get(agent_id) is None
    monkeypatch.setattr(agents_db, 'open_turn', real)
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


def test_host_heartbeat_disable_blocks_dispatch_and_restart(monkeypatch, tmp_path):
    from lib import heartbeat
    monkeypatch.setenv('CLARP_HEARTBEATS_DISABLED', '1')
    service, backends, agent_id = _make_service(tmp_path)
    assert not heartbeat.heartbeat_enabled({'heartbeat_enabled': True})
    assert heartbeat.restart_heartbeat_agents() == []
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
