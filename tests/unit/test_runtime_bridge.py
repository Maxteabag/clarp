from __future__ import annotations

import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest

from lib import backends
from lib.prompt_admissions import PromptAdmission
from lib.runtime_bridge import (
    PROTOCOL_VERSION,
    RuntimeClient,
    RuntimeProtocolError,
    RuntimeRPCServer,
    RuntimeUnavailable,
    StopLease,
    decode_request,
    encode_request,
)
from lib import turn_dispatch
from lib.turn_dispatch import DispatchError, DispatchResult, TurnDispatchService


class RecordingRuntime:
    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(("dispatch", kwargs))
        return DispatchResult(
            session="theo", backend="codex", queued=True,
            queue_depth=2, queue_revision=9,
        )

    def recover_queued(self):
        self.calls.append(("recover_queued", {}))
        return 3

    def dispatch_queued(self, queue_id):
        self.calls.append(("dispatch_queued", {"queue_id": queue_id}))
        return DispatchResult(session="theo", backend="codex")


def test_turn_dispatch_forwards_complete_request_to_external_runtime():
    runtime = RecordingRuntime()
    ctx = SimpleNamespace(runtime_client=runtime)
    admission = PromptAdmission(
        admission_id="padm-1",
        admission_version=1,
        authenticated_at_admission=True,
        cooperative_principal="user",
        principal_id="user",
        origin="user",
        sender_agent_id="",
        channel="chat",
        observed_at=123,
        client_admission_id="client-1",
        trace_id="trace-1",
        original_text="keep working",
    )

    result = TurnDispatchService(ctx).dispatch(
        text="keep working",
        requested_session="theo",
        trace_id="trace-1",
        synthesize_audio=False,
        forced_session="theo",
        client_msg_id="client-1",
        origin="user",
        sender_agent_id="",
        prompt_admission=admission,
        prompt_admission_id="padm-1",
        queue_if_busy=True,
        skip_admission=False,
        durable_queue_id="queue-1",
        unheard_audio_sessions=("theo",),
        allow_paused_queue=True,
    )

    assert result == DispatchResult(
        session="theo", backend="codex", queued=True,
        queue_depth=2, queue_revision=9,
    )
    [(method, payload)] = runtime.calls
    assert method == "dispatch"
    assert payload["text"] == "keep working"
    assert payload["prompt_admission"] is admission
    assert payload["unheard_audio_sessions"] == ("theo",)


def test_queue_operations_are_owned_by_external_runtime():
    runtime = RecordingRuntime()
    service = TurnDispatchService(SimpleNamespace(runtime_client=runtime))

    assert service.recover_queued() == 3
    assert service.dispatch_queued("queue-1").session == "theo"
    assert runtime.calls == [
        ("recover_queued", {}),
        ("dispatch_queued", {"queue_id": "queue-1"}),
    ]


def test_runtime_wire_protocol_round_trips_prompt_admission():
    admission = PromptAdmission(
        admission_id="padm-wire",
        admission_version=1,
        authenticated_at_admission=True,
        cooperative_principal="user",
        principal_id="user",
        origin="user",
        sender_agent_id="",
        channel="voice",
        observed_at=456,
        client_admission_id="client-wire",
        trace_id="trace-wire",
        original_text="continue after the server restarts",
    )
    raw = encode_request("dispatch", {
        "text": "continue after the server restarts",
        "prompt_admission": admission,
        "unheard_audio_sessions": ("theo", "mike"),
    })

    decoded = decode_request(raw)

    assert decoded["version"] == PROTOCOL_VERSION
    assert decoded["method"] == "dispatch"
    assert decoded["params"]["prompt_admission"] == dataclasses.asdict(admission)
    assert decoded["params"]["unheard_audio_sessions"] == ["theo", "mike"]


def test_runtime_wire_protocol_rejects_incompatible_version():
    raw = json.dumps({
        "version": PROTOCOL_VERSION + 1,
        "method": "status",
        "params": {},
    }).encode()

    with pytest.raises(RuntimeProtocolError, match="protocol version"):
        decode_request(raw)


def test_runtime_client_surfaces_remote_dispatch_status(tmp_path, monkeypatch):
    client = RuntimeClient(tmp_path / "runtime.sock")
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {
        "ok": False,
        "status": 409,
        "error": "agent is still working",
    })

    with pytest.raises(DispatchError) as error:
        client.dispatch(text="next")

    assert error.value.status == 409
    assert str(error.value) == "agent is still working"


def test_turn_dispatch_translates_runtime_outage_to_retryable_503():
    class OfflineRuntime:
        def dispatch(self, **_kwargs):
            raise RuntimeUnavailable("runtime socket is offline")

    service = TurnDispatchService(SimpleNamespace(runtime_client=OfflineRuntime()))

    with pytest.raises(DispatchError) as error:
        service.dispatch(text="hello", requested_session="theo", trace_id="trace")

    assert error.value.status == 503


def test_manual_queue_send_translates_runtime_outage_to_retryable_503():
    class OfflineRuntime:
        def dispatch_queued(self, _queue_id):
            raise RuntimeUnavailable("runtime socket is offline")

    service = TurnDispatchService(SimpleNamespace(runtime_client=OfflineRuntime()))

    with pytest.raises(DispatchError) as error:
        service.dispatch_queued("queue-1")

    assert error.value.status == 503


def test_runtime_rpc_owns_dispatch_across_client_replacement(tmp_path):
    class RuntimeDispatch:
        def __init__(self):
            self.calls = []

        def dispatch(self, **kwargs):
            self.calls.append(kwargs)
            return DispatchResult(session="theo", backend="codex")

        def dispatch_queued(self, queue_id):
            return DispatchResult(session=queue_id, backend="codex")

        def recover_queued(self):
            return 4

    dispatch = RuntimeDispatch()
    socket_path = tmp_path / "private" / "runtime.sock"
    runtime = RuntimeRPCServer(
        socket_path, dispatch_service=dispatch,
        status_provider=lambda: {"active": {"agent-1": "trace-1"}},
    )
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        first_server_process = RuntimeClient(socket_path)
        assert first_server_process.dispatch(
            text="keep working", requested_session="theo",
        ).session == "theo"

        # A replacement HTTP server creates a new client.  The runtime object,
        # active ownership, and previously accepted work remain untouched.
        replacement_server_process = RuntimeClient(socket_path)
        assert replacement_server_process.status()["active"] == {
            "agent-1": "trace-1"}
        assert replacement_server_process.recover_queued() == 4
        assert dispatch.calls == [{
            "text": "keep working", "requested_session": "theo"}]
        assert socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        runtime.shutdown()
        runtime.server_close()

    assert not socket_path.exists()


def test_second_runtime_refuses_to_steal_live_socket(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    first = RuntimeRPCServer(
        socket_path, dispatch_service=RecordingRuntime())
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        assert RuntimeClient(socket_path).ping()
        with pytest.raises(RuntimeProtocolError, match="already running"):
            RuntimeRPCServer(
                socket_path, dispatch_service=RecordingRuntime())
        assert RuntimeClient(socket_path).ping()
    finally:
        first.shutdown()
        first.server_close()


def test_runtime_rpc_rehydrates_prompt_admission(tmp_path):
    captured = {}

    class RuntimeDispatch:
        def dispatch(self, **kwargs):
            captured.update(kwargs)
            return DispatchResult(session="theo", backend="claude")

        def dispatch_queued(self, _queue_id):
            raise AssertionError("not called")

        def recover_queued(self):
            return 0

    socket_path = tmp_path / "runtime.sock"
    runtime = RuntimeRPCServer(socket_path, dispatch_service=RuntimeDispatch())
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    admission = PromptAdmission(
        admission_id="padm-rpc", admission_version=1,
        authenticated_at_admission=True, cooperative_principal="user",
        principal_id="user", origin="user", sender_agent_id="",
        channel="chat", observed_at=1, client_admission_id="client-rpc",
        trace_id="trace-rpc", original_text="hello",
    )
    try:
        RuntimeClient(socket_path).dispatch(
            text="hello", prompt_admission=admission,
            unheard_audio_sessions=("theo",),
        )
    finally:
        runtime.shutdown()
        runtime.server_close()

    assert captured["prompt_admission"] == admission
    assert captured["unheard_audio_sessions"] == ("theo",)


def test_runtime_drain_waits_for_idle_and_fences_new_dispatch(tmp_path):
    status = {"active": {"agent-1": "trace-1"}, "queued": {}}
    socket_path = tmp_path / "runtime.sock"
    runtime = RuntimeRPCServer(
        socket_path, dispatch_service=RecordingRuntime(),
        status_provider=lambda: status,
    )
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        client = RuntimeClient(socket_path)
        assert runtime.begin_drain_if_idle() is False
        assert client.dispatch(text="still accepted").session == "theo"

        status["active"] = {}
        assert runtime.begin_drain_if_idle() is True
        assert client.status()["draining"] is True
        with pytest.raises(DispatchError) as error:
            client.dispatch(text="retry on the next runtime")
        assert error.value.status == 503
        assert "draining" in str(error.value)
    finally:
        runtime.shutdown()
        runtime.server_close()


def test_backend_control_uses_runtime_owner_from_server_process():
    class RuntimeOwner:
        def __init__(self):
            self.calls = []

        def status(self):
            return {
                "active": {"agent-1": "trace-1"},
                "spawning": [], "terminals": [],
            }

        def interrupt(self, backend, agent_id):
            self.calls.append(("interrupt", backend, agent_id))
            return 1

        def interrupt_any(self, agent_id):
            self.calls.append(("interrupt_any", agent_id))
            return 2

        def steer(self, backend, agent_id, text, **kwargs):
            self.calls.append(("steer", backend, agent_id, text, kwargs))
            return True

    runtime = RuntimeOwner()
    backends.configure_runtime_client(runtime)
    turn_dispatch.configure_runtime_client(runtime)
    try:
        handles = backends.active_handles("codex", "agent-1")
        assert len(handles) == 1
        assert handles[0].is_alive()
        assert backends.active_handles("codex", "agent-2") == []
        assert backends.interrupt("codex", "agent-1") == 1
        assert backends.interrupt_any("agent-1") == 2
        assert backends.steer_turn(
            "codex", "agent-1", "more", client_msg_id="c-1",
            synthesize_audio=True) is True
        assert turn_dispatch.owns_inflight_trace("agent-1", "trace-1")
        assert not turn_dispatch.owns_inflight_trace("agent-1", "other")
    finally:
        backends.configure_runtime_client(None)
        turn_dispatch.configure_runtime_client(None)

    assert runtime.calls == [
        ("interrupt", "codex", "agent-1"),
        ("interrupt_any", "agent-1"),
        ("steer", "codex", "agent-1", "more", {
            "client_msg_id": "c-1", "synthesize_audio": True,
        }),
    ]


def test_runtime_status_outage_preserves_persisted_busy_ownership(tmp_path):
    from lib import agents as agents_db

    agent_id = agents_db.create_agent(
        persona="Theo", voice_id="voice", cwd=str(tmp_path),
        session="theo", backend="codex")
    agents_db.start_runtime(agent_id, "theo")
    agents_db.set_trace_for_session("theo", "trace-busy")
    agents_db.record_state(
        agent_id, "thinking", {"trace_id": "trace-busy"})

    class OfflineRuntime:
        def status(self):
            raise RuntimeUnavailable("runtime restarting")

    backends.configure_runtime_client(OfflineRuntime())
    turn_dispatch.configure_runtime_client(OfflineRuntime())
    try:
        handles = backends.active_handles("codex", agent_id)
        assert len(handles) == 1
        assert handles[0].is_alive()
        assert turn_dispatch.owns_inflight_trace(agent_id, "trace-busy")
    finally:
        backends.configure_runtime_client(None)
        turn_dispatch.configure_runtime_client(None)


def test_runtime_stop_lease_keeps_queue_barrier_in_process_owner(
    tmp_path, monkeypatch,
):
    from lib import turn_dispatch, turn_queue

    calls = []
    snapshot = {"trace_id": "trace-stop", "queued": ["private-spec"]}
    monkeypatch.setattr(
        turn_dispatch, "begin_stop",
        lambda agent_id: calls.append(("begin", agent_id)) or
        (snapshot, 2, False))
    monkeypatch.setattr(
        backends, "interrupt",
        lambda backend, agent_id: calls.append(
            ("interrupt", backend, agent_id)) or 1)
    monkeypatch.setattr(
        turn_dispatch, "complete_stop",
        lambda ctx, agent_id, state, cancelled_trace_ids, backend_registry:
        calls.append((
            "finish", ctx, agent_id, state, cancelled_trace_ids,
            backend_registry)))
    monkeypatch.setattr(turn_queue, "set_paused", lambda *_args: None)

    socket_path = tmp_path / "runtime.sock"
    ctx = SimpleNamespace(name="runtime-context")
    dispatch = RecordingRuntime()
    dispatch.ctx = ctx
    dispatch.backends = backends
    runtime = RuntimeRPCServer(socket_path, dispatch_service=dispatch)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        client = RuntimeClient(socket_path)
        lease = client.begin_stop("agent-1", "codex", strict=True)
        assert lease == StopLease(
            lease_id=lease.lease_id,
            trace_id="trace-stop",
            terminated=1,
            dropped=2,
        )
        client.finish_stop(lease.lease_id, {"trace-cancelled"})
    finally:
        runtime.shutdown()
        runtime.server_close()

    assert calls[0:2] == [
        ("begin", "agent-1"),
        ("interrupt", "codex", "agent-1"),
    ]
    assert calls[2][0] == "finish"
    assert calls[2][2:5] == (
        "agent-1", snapshot, {"trace-cancelled"})


def test_runtime_stop_lease_self_releases_if_http_server_disappears(
    tmp_path, monkeypatch,
):
    from lib import turn_dispatch, turn_queue

    finished = threading.Event()
    monkeypatch.setattr(
        turn_dispatch, "begin_stop",
        lambda _agent_id: ({"trace_id": "trace", "queued": []}, 0, False))
    monkeypatch.setattr(backends, "interrupt", lambda *_args: 1)
    monkeypatch.setattr(turn_queue, "set_paused", lambda *_args: None)
    monkeypatch.setattr(
        turn_dispatch, "complete_stop",
        lambda *_args, **_kwargs: finished.set())

    dispatch = RecordingRuntime()
    dispatch.ctx = SimpleNamespace()
    dispatch.backends = backends
    socket_path = tmp_path / "runtime.sock"
    runtime = RuntimeRPCServer(
        socket_path, dispatch_service=dispatch, stop_lease_timeout=0.05,
        status_provider=lambda: {
            "active": {}, "spawning": [], "terminals": [], "queued": {},
            "compactions": [],
        })
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        lease = RuntimeClient(socket_path).begin_stop(
            "agent-1", "codex", strict=True, hold=True)
        assert lease.lease_id
        assert finished.wait(1.0)
        assert runtime.begin_drain_if_idle() is True
    finally:
        runtime.shutdown()
        runtime.server_close()
