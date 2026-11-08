"""Runtime RPC behavior without opening a socket or launching a backend.

These cases characterize request results and effects before dispatch extraction.
The existing runtime_bridge tests own socket transport and client replacement.
"""
from types import SimpleNamespace
from unittest.mock import Mock, call
import threading

import pytest

from lib import agents, backends, runtime_rpc_server, turn_dispatch, turn_queue
from lib.runtime_rpc_server import RuntimeRPCServer
from lib.turn_dispatch import DispatchError, DispatchResult


@pytest.fixture
def rpc(monkeypatch):
    server = object.__new__(RuntimeRPCServer)
    server.dispatch_service = SimpleNamespace(ctx=SimpleNamespace(stream=object()), backends=object())
    server._admission_lock = threading.RLock()
    server._stop_lease_lock = threading.Lock()
    server._stop_leases = {}
    server._draining = False
    server.release_id = "release-test"
    server.instance = "instance-test"
    server.attestation = {"pid": 123}
    server.stop_lease_timeout = 30
    monkeypatch.setattr(agents, "get_by_agent_id", Mock(return_value=None))
    return server


@pytest.mark.parametrize("method", ["interrupt", "interrupt_any", "steer", "begin_stop", "release_agent"])
def test_maintenance_agent_control_is_refused_before_effects(rpc, monkeypatch, method):
    agents.get_by_agent_id.return_value = {"kind": "janitor"}
    monkeypatch.setattr(agents, "interaction_capabilities", lambda agent: {"can_restart": False})
    assert rpc.dispatch_request(method, {"agent_id": "maintenance"}) == {
        "ok": False, "status": 409,
        "error": "Janitors are managed from their maintenance configuration"}


def test_status_provider_cannot_replace_runtime_attestation(rpc):
    rpc.status_provider = lambda: {"active": {"a": "t"}, "attestation": {"pid": 999}}
    rpc._drain_status = lambda: {"sealed": False}
    assert rpc.dispatch_request("status", {}) == {"ok": True, "result": {
        "protocol_version": 1, "capabilities": {"janitor_runs": True, "send_delivery_state": True},
        "draining": False, "release_id": "release-test", "instance": "instance-test",
        "drain": {"sealed": False}, "active": {"a": "t"}, "attestation": {"pid": 123}}}


@pytest.mark.parametrize("method", ["interrupt", "interrupt_any", "steer", "begin_stop", "release_agent", "release_leaked_slots", "repair_slot"])
def test_agent_control_requires_agent_id(rpc, method):
    assert rpc.dispatch_request(method, {}) == {"ok": False, "status": 400, "error": "agent_id is required"}


@pytest.mark.parametrize("method,backend_name,args,kwargs,result", [
    ("interrupt", "interrupt", ("codex", "a"), {}, 2),
    ("interrupt_any", "interrupt_any", ("a",), {}, 3),
    ("steer", "steer_turn", ("codex", "a", "next"), {"client_msg_id": "m", "synthesize_audio": True}, True),
])
def test_control_results_and_receipt_fields(rpc, monkeypatch, method, backend_name, args, kwargs, result):
    effect = Mock(return_value=result)
    monkeypatch.setattr(backends, backend_name, effect)
    assert rpc.dispatch_request(method, {"agent_id": "a", "backend": "codex", "text": "next", "client_msg_id": "m", "synthesize_audio": True}) == {"ok": True, "result": result}
    effect.assert_called_once_with(*args, **kwargs)


def test_release_clears_work_before_interrupt_and_soft_delete(rpc, monkeypatch):
    effects = Mock()
    monkeypatch.setattr(turn_dispatch, "clear_for_agent", effects.clear)
    monkeypatch.setattr(backends, "interrupt_any", effects.interrupt)
    monkeypatch.setattr(agents, "soft_delete", effects.delete)
    effects.interrupt.return_value = 2
    assert rpc.dispatch_request("release_agent", {"agent_id": "a"}) == {"ok": True, "result": 2}
    assert effects.mock_calls == [call.clear("a"), call.interrupt("a"), call.delete("a")]


@pytest.mark.parametrize("method", ["release_leaked_slots", "repair_slot"])
def test_slot_operations_report_unsupported_runtime(rpc, method):
    response = rpc.dispatch_request(method, {"agent_id": "a"})
    assert response == {"ok": False, "status": 501, "error": (
        "this runtime cannot release leaked slots" if method == "release_leaked_slots" else "this runtime cannot repair slots")}


def test_slot_operations_preserve_actor_verification_and_validation(rpc):
    rpc.dispatch_service.release_leaked_slots = Mock(return_value={"a": "t"})
    rpc.dispatch_service.repair_slot = Mock(return_value={"released_trace": "t", "queue_recovered": 1})
    assert rpc.dispatch_request("release_leaked_slots", {"agent_id": "a"}) == {"ok": True, "result": {"a": "t"}}
    rpc.dispatch_service.release_leaked_slots.assert_called_once_with(agent_id="a")
    params = {"agent_id": "a", "actor": "owner", "reason": "repair", "actor_verified": 1}
    assert rpc.dispatch_request("repair_slot", params)["result"] == {"released_trace": "t", "queue_recovered": 1}
    rpc.dispatch_service.repair_slot.assert_called_once_with("a", actor="owner", reason="repair", actor_verified=False)
    rpc.dispatch_service.repair_slot.side_effect = ValueError("actor required")
    assert rpc.dispatch_request("repair_slot", params) == {"ok": False, "status": 400, "error": "actor required"}


def test_live_requests_missing_hub_and_invalid_fields(rpc):
    for method in ("live_snapshot", "live_observe", "live_patch"):
        assert rpc.dispatch_request(method, {}) == {"ok": False, "status": 404, "error": "no live items here"}
    rpc.live_hub = Mock()
    assert rpc.dispatch_request("live_patch", {"fields": []}) == {"ok": False, "status": 404, "error": "no live items here"}


def test_live_snapshot_observation_and_patch_results(rpc, monkeypatch):
    from lib import live_hub
    rpc.live_hub = Mock(epoch="epoch")
    rpc.live_hub.snapshot.return_value = {"items": ["item"]}
    assert rpc.dispatch_request("live_snapshot", {"session": "s", "agent_id": "a"}) == {"ok": True, "result": {"items": ["item"]}}
    rpc.live_hub.snapshot.assert_called_once_with(session="s", agent_id="a")
    rpc.live_hub.snapshot.return_value = None
    assert rpc.dispatch_request("live_snapshot", {}) == {"ok": True, "result": {"epoch": "epoch", "missing": True}}
    observe = Mock()
    monkeypatch.setattr(live_hub, "_observe", observe)
    assert rpc.dispatch_request("live_observe", {"agent_id": "a", "event": "done", "to_state": "idle", "detail": []}) == {"ok": True}
    observe.assert_called_once_with(rpc.live_hub, "a", "done", "idle", {})
    assert rpc.dispatch_request("live_patch", {"agent_id": "a", "item_id": "i", "fields": {"explanation": "summary"}}) == {"ok": True}
    rpc.live_hub.patch.assert_called_once_with("a", "i", {"explanation": "summary"})


def test_maintenance_and_explanation_requests(rpc, monkeypatch):
    from lib import janitor_autonomy, tool_explanation_commands
    recovery = Mock(return_value={"status": "recovered"})
    monkeypatch.setattr(janitor_autonomy, "runtime_recover", recovery)
    assert rpc.dispatch_request("recover_janitor_quota", {"provider": "codex", "owner_id": "a", "generation": 7, "approval_id": "approval"}) == {"ok": True, "result": {"status": "recovered"}}
    recovery.assert_called_once_with("codex", "a", 7, "approval")
    cancel = Mock(return_value={"cancelled": True})
    monkeypatch.setattr(turn_dispatch, "cancel_janitor_run", cancel)
    assert rpc.dispatch_request("cancel_janitor_run", {"run_id": "run"}) == {"ok": True, "result": {"cancelled": True}}
    cancel.assert_called_once_with(rpc.dispatch_service.ctx, "run", backend_registry=rpc.dispatch_service.backends)
    cancel.side_effect = DispatchError(409, "already ended")
    assert rpc.dispatch_request("cancel_janitor_run", {}) == {"ok": False, "status": 409, "error": "already ended"}
    answer = Mock(return_value="printf example")
    monkeypatch.setattr(tool_explanation_commands, "answer", answer)
    assert rpc.dispatch_request("explanation_command", {"agent_id": "a", "label": "printf"}) == {"ok": True, "result": "printf example"}
    answer.assert_called_once_with("a", "printf")


@pytest.mark.parametrize("error,status", [(None, 200), (backends.GoalUnsupported("unsupported"), 501), (ValueError("bad action"), 400), (RuntimeError("provider failed"), 502)])
def test_goal_success_and_error_translation(rpc, monkeypatch, error, status):
    from lib import log
    agents.get_by_agent_id.return_value = {"backend": "codex"}
    effect = Mock(return_value={"state": "active"}, side_effect=error)
    logger = Mock()
    monkeypatch.setattr(backends, "goal", effect)
    monkeypatch.setattr(log, "log_exception", logger)
    response = rpc.dispatch_request("goal", {"agent_id": "a", "action": "set", "objective": "synthetic"})
    assert response == ({"ok": True, "result": {"state": "active"}} if error is None else {"ok": False, "status": status, "error": str(error)})
    effect.assert_called_once_with("codex", "a", "set", objective="synthetic", stream=rpc.dispatch_service.ctx.stream)
    if status == 502:
        logger.assert_called_once_with("runtimeGoalFail", error, detail="a")


def test_missing_goal_and_compaction_validation(rpc, monkeypatch):
    from lib import compaction
    assert rpc.dispatch_request("goal", {}) == {"ok": False, "status": 404, "error": "no such agent"}
    assert rpc.dispatch_request("compact", {"session": "  "}) == {"ok": False, "status": 400, "error": "session is required"}
    effect = Mock(return_value={"compacted": True})
    monkeypatch.setattr(compaction, "compact_session", effect)
    assert rpc.dispatch_request("compact", {"session": " s "}) == {"ok": True, "result": {"compacted": True}}
    effect.assert_called_once_with("s")


def test_dispatch_submission_errors_and_delivery_identity(rpc):
    rpc.dispatch_service.submit = Mock(return_value=DispatchResult(session="s", backend="codex", delivery={"state": "accepted"}))
    response = rpc.dispatch_request("dispatch", {"text": "synthetic", "requested_session": "s"})
    assert response["ok"] is True
    assert response["result"]["delivery"] == {"state": "accepted"}
    assert response["result"]["delivery_runtime"] == {"release_id": "release-test", "instance": "instance-test", "draining": False, "capabilities": {"send_delivery_state": True}}
    command = rpc.dispatch_service.submit.call_args.args[0]
    assert command.text == "synthetic" and command.requested_session == "s"
    rpc.dispatch_service.submit.side_effect = DispatchError(409, "busy")
    assert rpc.dispatch_request("dispatch", {"text": "synthetic"}) == {"ok": False, "status": 409, "error": "busy"}
    rpc.dispatch_service.submit.side_effect = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        rpc.dispatch_request("dispatch", {"text": "synthetic"})
    assert rpc.dispatch_request("dispatch", {"admission": "invalid"})["status"] == 400


def test_legacy_dispatch_and_queue_results(rpc):
    rpc.dispatch_service.dispatch = Mock(return_value=DispatchResult(session="s", backend="codex"))
    rpc.dispatch_service.dispatch_queued = Mock(return_value=DispatchResult(session="queued", backend="codex"))
    rpc.dispatch_service.recover_queued = Mock(return_value="3")
    assert rpc.dispatch_request("dispatch", {"text": "synthetic"})["result"]["session"] == "s"
    assert rpc.dispatch_service.dispatch.call_args.kwargs["text"] == "synthetic"
    assert rpc.dispatch_request("dispatch_queued", {"queue_id": "q"})["result"]["session"] == "queued"
    rpc.dispatch_service.dispatch_queued.assert_called_once_with("q")
    assert rpc.dispatch_request("recover_queued", {}) == {"ok": True, "result": 3}
    assert rpc.dispatch_request("unknown", {}) == {"ok": False, "status": 404, "error": "unknown runtime method: unknown"}


@pytest.fixture
def stop_effects(rpc, monkeypatch):
    from lib import log
    effects = Mock()
    effects.begin.return_value = ({"trace_id": "t"}, 2, False)
    effects.interrupt.return_value = 1
    monkeypatch.setattr(turn_dispatch, "begin_stop", effects.begin)
    monkeypatch.setattr(turn_dispatch, "fence_user_stop", effects.fence)
    monkeypatch.setattr(turn_dispatch, "restore_stop_state", effects.restore)
    monkeypatch.setattr(turn_dispatch, "complete_stop", effects.complete)
    monkeypatch.setattr(turn_queue, "set_paused", effects.pause)
    monkeypatch.setattr(backends, "interrupt", effects.interrupt)
    monkeypatch.setattr(log, "log_exception", effects.log)
    rpc._handoff_after_failed_stop = effects.handoff
    return effects


@pytest.mark.parametrize("user_stop", [True, False])
def test_stop_without_hold_completes_with_correct_receipt_fence(rpc, stop_effects, user_stop):
    response = rpc.dispatch_request("begin_stop", {"agent_id": "a", "backend": "codex", "hold": False, "fence_user_receipts": user_stop})
    assert response == {"ok": True, "result": {"lease_id": "", "trace_id": "t", "terminated": 1, "dropped": 2}}
    expected = [call.begin("a", user_stop=user_stop)]
    if user_stop:
        expected.append(call.fence("a"))
    expected += [call.interrupt("codex", "a"), call.complete(rpc.dispatch_service.ctx, "a", {"trace_id": "t"}, set(), backend_registry=rpc.dispatch_service.backends)]
    assert stop_effects.mock_calls == expected


def test_stop_hold_lease_is_finished_once_with_cancelled_ids(rpc, stop_effects, monkeypatch):
    timer = Mock()
    monkeypatch.setattr(runtime_rpc_server.threading, "Timer", timer)
    response = rpc.dispatch_request("begin_stop", {"agent_id": "a", "backend": "codex"})
    lease_id = response["result"]["lease_id"]
    assert lease_id.startswith("stop-")
    assert rpc._stop_leases == {lease_id: ("a", {"trace_id": "t"})}
    timer.assert_called_once_with(30, rpc._expire_stop_lease, args=(lease_id,))
    assert timer.return_value.daemon is True
    timer.return_value.start.assert_called_once_with()
    assert rpc.dispatch_request("finish_stop", {"lease_id": lease_id, "cancelled_trace_ids": ["t", 9, "t"]}) == {"ok": True, "result": True}
    stop_effects.complete.assert_called_once_with(rpc.dispatch_service.ctx, "a", {"trace_id": "t"}, {"t", "9"}, backend_registry=rpc.dispatch_service.backends)
    assert rpc._stop_leases == {}
    assert rpc.dispatch_request("finish_stop", {"lease_id": lease_id}) == {"ok": False, "status": 404, "error": "runtime stop lease not found"}


@pytest.mark.parametrize("failure", ["begin", "fence", "fence_rollback", "interrupt", "strict"])
def test_stop_failure_rollback_and_error_codes(rpc, stop_effects, failure):
    error = RuntimeError("synthetic failure")
    if failure == "strict":
        stop_effects.interrupt.return_value = 0
    elif failure == "fence_rollback":
        stop_effects.fence.side_effect = error
        stop_effects.pause.side_effect = RuntimeError("rollback failure")
    else:
        getattr(stop_effects, failure).side_effect = error
    response = rpc.dispatch_request("begin_stop", {"agent_id": "a", "backend": "codex", "strict": True})
    status = 503 if failure in {"begin", "fence", "fence_rollback"} else 502
    message = "backend did not confirm interruption" if failure == "strict" else "synthetic failure"
    assert response == {"ok": False, "status": status, "error": message}
    assert rpc._stop_leases == {}
    if failure == "begin":
        stop_effects.log.assert_called_once_with("runtimeStopBeginFail", error, detail="a")
        stop_effects.restore.assert_not_called()
    else:
        stop_effects.restore.assert_called_once_with("a", {"trace_id": "t"})
        stop_effects.pause.assert_called_once_with("a", False)
    if status == 503:
        stop_effects.handoff.assert_called_once_with("a")
    if failure == "fence_rollback":
        assert stop_effects.log.call_args.args[0] == "runtimeStopRollbackFail"
