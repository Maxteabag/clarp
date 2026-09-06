"""Maintenance capability boundaries using the real store and fake backends."""
from types import SimpleNamespace

import pytest

from lib import agents, db, janitors, message_store, turn_queue
from lib import compaction, oracle_delegations, turn_dispatch as td
from lib.agent_lifecycle import AgentLifecycleService, AgentLifecycleError
from lib.runtime_bridge import RuntimeRPCServer


class Backend:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned = []
        self.interrupted = []
        self.live = False

    def normalize(self, backend):
        return backend or "codex"

    def active_handles(self, _backend, _agent_id):
        return ["owned-handle"] if self.live else []

    def interrupt(self, backend, agent_id):
        # A durable write lock must not be held across backend interruption.
        assert not db.conn().in_transaction
        self.interrupted.append((backend, agent_id))
        self.live = False
        return 1

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append((backend, kwargs))
        self.live = True


def context(tmp_path):
    events, announcements = [], []
    return SimpleNamespace(
        default_session="worker", agents_path=tmp_path / "unused.json",
        stream=SimpleNamespace(broadcast=events.append), events=events,
        speak_announcement=lambda *a, **k: announcements.append((a, k)),
        announcements=announcements,
    )


@pytest.fixture
def setup(tmp_path):
    ctx = context(tmp_path)
    worker_id = agents.create_agent(persona="Worker", voice_id="worker-voice",
        cwd=str(tmp_path), session="worker", backend="codex")
    janitor_id = agents.create_agent(persona="Sam", voice_id="", cwd=str(tmp_path),
        session="sam", backend="codex")
    agents.start_runtime(janitor_id, "sam")
    config = janitors.create("sam")
    config = janitors.set_enabled("sam", config["revision"], True)
    run = janitors.create_run(config["attachments"][0]["attachment_id"],
        config["generation"], [{"session": "worker", "agent_id": worker_id,
        "state_id": 1, "fingerprint": "evidence", "task_key": "task", "change_key": "phase",
        "source_refs": []}], run_id="janitor-unit")
    backend, retries = Backend(), []
    service = td.TurnDispatchService(ctx, backend_registry=backend, home=tmp_path,
        retry_scheduler=lambda delay, fn: retries.append((delay, fn)))
    yield SimpleNamespace(ctx=ctx, worker_id=worker_id, janitor_id=janitor_id,
        config=config, run=run, backend=backend, service=service, retries=retries)
    for aid in (worker_id, janitor_id):
        td._INFLIGHT.pop(aid, None)
        td._QUEUED.pop(aid, None)
        td._CLAIMED_AT.pop(aid, None)


def dispatch(s, **overrides):
    params = dict(text="Review the bounded candidates", requested_session="sam",
        forced_session="sam", trace_id=s.run["run_id"], janitor_run_id=s.run["run_id"])
    params.update(overrides)
    return s.service.dispatch(**params)


def pause(s):
    janitors.set_enabled("sam", janitors.get("sam")["revision"], False)


def enqueue(s, *, queue_id=None, trace_id=None):
    rid = s.run["run_id"]
    turn_queue.enqueue(queue_id=queue_id or rid, agent_id=s.janitor_id, session="sam",
        text="Review the bounded candidates", trace_id=trace_id or rid,
        client_msg_id=rid, synthesize_audio=True, origin="automation", sender_agent_id="")


@pytest.mark.parametrize("origin", ["user", "agent", "oracle", "automation", "schedule", "janitor"])
def test_direct_and_forged_origins_rejected_before_side_effects(setup, monkeypatch, origin):
    s = setup
    agents.set_focus(s.worker_id)
    herald = []
    monkeypatch.setattr(s.service, "_notify_herald", herald.append)
    with pytest.raises(td.DispatchError, match="configured maintenance"):
        dispatch(s, origin=origin, janitor_run_id="")
    assert not s.backend.spawned and not herald
    assert agents.get_focus() == s.worker_id
    assert not message_store.has_client_message(s.run["run_id"])
    assert turn_queue.pending_count(s.janitor_id) == 0


def test_spoken_name_cannot_grant_run_permission(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(td, "resolve_send_target", lambda **kw: td.SendTarget("sam", "hello", True))
    with pytest.raises(td.DispatchError):
        dispatch(s, forced_session="", requested_session="worker", janitor_run_id="")
    assert not s.backend.spawned


@pytest.mark.parametrize("override", [{"janitor_run_id": "bogus"}, {"trace_id": "wrong"},
    {"client_msg_id": "wrong"}, {"durable_queue_id": "wrong"}])
def test_capability_requires_current_run_and_matching_stable_ids(setup, override):
    with pytest.raises(td.DispatchError):
        dispatch(setup, **override)
    assert not setup.backend.spawned


def test_valid_run_is_silent_queued_idempotent_and_does_not_change_focus(setup, monkeypatch):
    s = setup
    agents.set_focus(s.worker_id)
    herald = []
    monkeypatch.setattr(s.service, "_notify_herald", herald.append)
    result = dispatch(s, synthesize_audio=True, origin="user", queue_if_busy=False)
    assert result.queued is False
    assert len(s.backend.spawned) == 1
    kwargs = s.backend.spawned[0][1]
    assert kwargs["synthesize_audio"] is False and kwargs["voice_preamble"] is False
    assert agents.get_focus() == s.worker_id and not herald
    assert janitors.get_run(s.run["run_id"])["status"] == "running"
    kwargs["on_session_init"]("native-unit")
    row = db.conn().execute("SELECT origin FROM messages WHERE message_id=?",
        ("u-" + s.run["run_id"],)).fetchone()
    assert row["origin"] == "janitor"
    dispatch(s)
    assert len(s.backend.spawned) == 1


def test_stale_sticky_janitor_focus_is_ignored(setup):
    agents.set_focus(setup.janitor_id)
    assert setup.service._sticky_session() == ""


def test_recovery_derives_capability_from_run_and_durable_queue_not_origin(setup):
    s = setup
    enqueue(s)
    assert s.service.recover_queued() == 1
    assert len(s.backend.spawned) == 1
    assert s.backend.spawned[0][1]["synthesize_audio"] is False


@pytest.mark.parametrize("bad_queue,bad_trace", [("forged-queue", None), (None, "forged-trace")])
def test_recovery_rejects_queue_identity_mismatch(setup, bad_queue, bad_trace):
    enqueue(setup, queue_id=bad_queue, trace_id=bad_trace)
    assert setup.service.recover_queued() == 0
    assert not setup.backend.spawned
    assert turn_queue.pending_count(setup.janitor_id) == 0


def test_manual_queued_send_cannot_start_janitor(setup):
    enqueue(setup)
    with pytest.raises(td.DispatchError, match="managed by their configuration"):
        setup.service.dispatch_queued(setup.run["run_id"])
    assert not setup.backend.spawned
    assert turn_queue.get(setup.run["run_id"]) is not None


def test_pause_prevents_durable_recovery(setup):
    enqueue(setup)
    pause(setup)
    assert setup.service.recover_queued() == 0
    assert not setup.backend.spawned


def test_pause_prevents_in_memory_queued_resume(setup):
    s = setup
    td._INFLIGHT[s.janitor_id] = "prior-unrelated-turn"
    s.backend.live = True
    assert dispatch(s).queued is True
    spec = td._QUEUED[s.janitor_id][0]
    pause(s)
    s.backend.live = False
    td._INFLIGHT[s.janitor_id] = spec.trace_id
    s.service._resume_and_spawn(s.janitor_id, spec)
    assert not s.backend.spawned
    assert s.janitor_id not in td._INFLIGHT


def test_pause_during_retry_backoff_prevents_another_model_call(setup):
    s = setup
    dispatch(s)
    s.backend.spawned[0][1]["on_error"]("API Error: The socket connection was closed unexpectedly.")
    assert len(s.retries) == 1
    pause(s)
    s.retries[0][1]()
    assert len(s.backend.spawned) == 1
    assert s.janitor_id not in td._INFLIGHT


def test_cancel_requires_configuration_fence(setup):
    with pytest.raises(td.DispatchError, match="Pause"):
        td.cancel_janitor_run(setup.ctx, setup.run["run_id"], backend_registry=setup.backend)
    assert not setup.backend.interrupted


def test_cancel_interrupts_only_exact_active_run_after_fence(setup):
    s = setup
    dispatch(s)
    pause(s)
    result = td.cancel_janitor_run(s.ctx, s.run["run_id"], backend_registry=s.backend)
    assert result["interrupted"] is True
    assert s.backend.interrupted == [("codex", s.janitor_id)]
    assert s.janitor_id not in td._INFLIGHT
    assert janitors.get_run(s.run["run_id"])["status"] == "cancelled"
    assert agents.latest_state(s.janitor_id)["kind"] == "interrupted"


def test_cancel_preserves_unrelated_active_work_and_queue(setup):
    s = setup
    enqueue(s)
    td._INFLIGHT[s.janitor_id] = "unrelated-turn"
    s.backend.live = True
    pause(s)
    result = td.cancel_janitor_run(s.ctx, s.run["run_id"], backend_registry=s.backend)
    assert result["interrupted"] is False
    assert td._INFLIGHT[s.janitor_id] == "unrelated-turn"
    assert not s.backend.interrupted


@pytest.mark.parametrize("method", ["interrupt", "interrupt_any", "steer", "begin_stop", "release_agent"])
def test_runtime_shared_controls_reject_janitors(setup, tmp_path, method):
    s = setup
    runtime = RuntimeRPCServer(tmp_path / "runtime.sock", dispatch_service=s.service)
    try:
        result = runtime.dispatch_request(method, {"agent_id": s.janitor_id, "backend": "codex"})
        assert result["ok"] is False and result["status"] == 409
        assert agents.get_by_agent_id(s.janitor_id) is not None
    finally:
        runtime.server_close()


def test_runtime_cancel_rpc_reaches_exact_run_helper(setup, tmp_path):
    s = setup
    dispatch(s)
    pause(s)
    runtime = RuntimeRPCServer(tmp_path / "runtime.sock", dispatch_service=s.service)
    try:
        result = runtime.dispatch_request("cancel_janitor_run", {"run_id": s.run["run_id"]})
        assert result["ok"] is True
        assert result["result"]["interrupted"] is True
    finally:
        runtime.server_close()


def test_oracle_rejects_before_creating_delegation(setup):
    with pytest.raises(ValueError, match="Janitors"):
        oracle_delegations.dispatch(ctx=setup.ctx, delegation_id="unit-delegation",
            session="sam", request_text="Do this", authenticated_at_admission=True)
    assert oracle_delegations.get("unit-delegation") is None


def test_compaction_and_delete_reject_before_runtime_control(setup):
    assert compaction.compact_session("sam")["ok"] is False
    with pytest.raises(AgentLifecycleError) as exc:
        AgentLifecycleService(setup.ctx).delete("sam")
    assert exc.value.status == 409
    assert agents.get_by_agent_id(setup.janitor_id) is not None


def test_private_creation_needs_no_voice_and_preserves_focus(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    worker = agents.create_agent(persona="Worker", voice_id="reserved", cwd=str(tmp_path), session="worker")
    agents.set_focus(worker)
    from lib import voice
    monkeypatch.setattr(voice, "resolve_voice", lambda *_: pytest.fail("Janitor should not resolve or reserve a voice"))
    result = AgentLifecycleService(ctx).create_janitor({"name": "Housekeeping", "backend": "codex", "cwd": str(tmp_path)})
    assert result.voice_id == ""
    assert agents.get_by_session(result.session)["voice_id"] == ""
    assert agents.get_focus() == worker
    assert not ctx.announcements and not ctx.events
    assert agents.current_runtime_id(agents.get_by_session(result.session)["agent_id"])


def test_janitor_flag_in_ordinary_create_json_does_not_enable_private_mode(tmp_path):
    ctx = context(tmp_path)
    result = AgentLifecycleService(ctx).create({"name": "Ordinary", "is_janitor": True, "janitor": True,
        "voice_id": "v-ordinary", "backend": "codex", "cwd": str(tmp_path)})
    assert result.voice_id == "v-ordinary"
    assert not agents.get_by_session(result.session)["is_janitor"]
    assert ctx.announcements


def test_late_backend_callback_cannot_revive_paused_run(setup):
    s = setup
    dispatch(s)
    callbacks = s.backend.spawned[0][1]
    pause(s)
    td.cancel_janitor_run(s.ctx, s.run["run_id"], backend_registry=s.backend)
    assert callbacks["on_session_init"]("late-native-session") is False
    callbacks["on_result"]({"type": "result", "result": "Finished", "is_error": False})
    assert agents.latest_state(s.janitor_id)["kind"] == "interrupted"
    assert janitors.get_run(s.run["run_id"])["status"] == "cancelled"
    assert not agents.live_backend_session(s.janitor_id)


def test_pause_before_spawn_rechecks_capability_without_model_call(setup, monkeypatch):
    s = setup
    original = s.service._spawn_attempt

    def pause_before_spawn(spec, *, attempt):
        pause(s)
        return original(spec, attempt=attempt)

    monkeypatch.setattr(s.service, "_spawn_attempt", pause_before_spawn)
    with pytest.raises(td.DispatchError):
        dispatch(s)
    assert not s.backend.spawned
    assert s.janitor_id not in td._INFLIGHT
    assert agents.latest_state(s.janitor_id)["kind"] == "interrupted"


def test_janitor_spawn_leaves_global_turn_lock_available_to_callbacks(setup):
    import threading
    s = setup
    entered, release = threading.Event(), threading.Event()
    failures = []
    original = s.backend.spawn_turn

    def blocking_start(backend, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(backend, **kwargs)

    def dispatch_thread():
        try:
            dispatch(s)
        except BaseException as exc:
            failures.append(exc)
        finally:
            db.conn().close()

    s.backend.spawn_turn = blocking_start
    thread = threading.Thread(target=dispatch_thread)
    thread.start()
    try:
        assert entered.wait(2)
        acquired = td._TURN_LOCK.acquire(timeout=0.2)
        if acquired:
            td._TURN_LOCK.release()
        assert acquired, "Backend callbacks and other agents must not wait behind a start RPC"
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert failures == []


def test_failed_exact_run_interrupt_restores_slot_without_touching_store_fence(setup):
    s = setup
    dispatch(s)
    pause(s)

    def fail_interrupt(*args):
        assert not db.conn().in_transaction
        raise RuntimeError("backend unavailable")

    s.backend.interrupt = fail_interrupt
    with pytest.raises(RuntimeError, match="backend unavailable"):
        td.cancel_janitor_run(s.ctx, s.run["run_id"], backend_registry=s.backend)
    assert td._INFLIGHT[s.janitor_id] == s.run["run_id"]
    assert janitors.get_run(s.run["run_id"])["status"] == "cancelled"
    assert janitors.get("sam")["enabled"] is False
    assert not turn_queue.is_paused(s.janitor_id)


def test_private_janitor_creation_does_not_mint_a_second_reserved_identity(setup):
    with pytest.raises(AgentLifecycleError, match="reserved Janitor identity"):
        AgentLifecycleService(setup.ctx).create_janitor({"name": "Different", "session": "sam",
            "backend": "codex", "cwd": str(setup.service.home)})
    assert db.conn().execute("SELECT count(*) FROM agents WHERE persona=?", ("Different",)).fetchone()[0] == 0
