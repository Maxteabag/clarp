from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents, message_store, oracle_delegations, prompt_admissions


def _agent(tmp_path):
    agents.create_agent(
        persona="Theo", voice_id="voice", cwd=str(tmp_path), session="theo")
    return agents.get_by_session("theo")


def test_delegation_lifecycle_is_durable_and_acknowledged(tmp_path):
    from lib import turn_queue
    agent = _agent(tmp_path)
    row, created = oracle_delegations.begin(
        delegation_id="d-1", trace_id="oracle-d-1",
        client_msg_id="oracle-d-1", agent_id=agent["agent_id"],
        session="theo", request_text="Investigate the failure",
    )
    assert created is True
    assert row["status"] == "accepted"

    turn_queue.enqueue(
        queue_id="oracle-d-1", agent_id=agent["agent_id"], session="theo",
        text="Investigate the failure", trace_id="oracle-d-1",
        client_msg_id="oracle-d-1", synthesize_audio=False,
        origin="oracle", sender_agent_id="")
    row = oracle_delegations.mark_dispatched(
        "d-1", backend_session_id="backend-1", queued=True)
    assert row and row["status"] == "queued"

    assert oracle_delegations.complete_for_trace(
        trace_id="oracle-d-1", message_id="answer-1",
        text="The worker lost its socket.")
    pending = oracle_delegations.undelivered()
    assert [(item["delegation_id"], item["result_text"])
            for item in pending] == [("d-1", "The worker lost its socket.")]

    assert oracle_delegations.acknowledge("d-1")
    assert oracle_delegations.undelivered() == []
    assert oracle_delegations.get("d-1")["delivered"] is True


def test_delegation_idempotency_rejects_changed_work(tmp_path):
    agent = _agent(tmp_path)
    kwargs = dict(
        delegation_id="same", trace_id="oracle-same",
        client_msg_id="oracle-same", agent_id=agent["agent_id"],
        session="theo", request_text="First request",
    )
    first, created = oracle_delegations.begin(**kwargs)
    retry, created_retry = oracle_delegations.begin(**kwargs)
    assert created is True and created_retry is False
    assert retry["delegation_id"] == first["delegation_id"]

    with pytest.raises(oracle_delegations.DelegationCollision):
        oracle_delegations.begin(**{**kwargs, "request_text": "Different"})


def test_dispatch_is_silent_forced_and_durable(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    calls = []

    class FakeDispatch:
        def __init__(self, _ctx):
            pass

        def dispatch(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(session="theo", queued=False)

    from lib import turn_dispatch
    monkeypatch.setattr(turn_dispatch, "TurnDispatchService", FakeDispatch)

    result = oracle_delegations.dispatch(
        ctx=object(), delegation_id="work-1", session="theo",
        request_text="Check the logs", authenticated_at_admission=True)

    assert result["agent_id"] == agent["agent_id"]
    assert len(calls) == 1
    assert calls[0]["forced_session"] == "theo"
    assert calls[0]["synthesize_audio"] is False
    assert calls[0]["queue_if_busy"] is True
    assert calls[0]["origin"] == "oracle"
    assert calls[0]["client_msg_id"] == "oracle-work-1"
    assert calls[0]["prompt_admission"].authenticated_at_admission is True


def test_dispatch_preserves_unauthenticated_request_authority(tmp_path, monkeypatch):
    _agent(tmp_path)
    calls = []

    class FakeDispatch:
        def __init__(self, _ctx):
            pass

        def dispatch(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(session="theo", queued=False)

    from lib import turn_dispatch
    monkeypatch.setattr(turn_dispatch, "TurnDispatchService", FakeDispatch)

    oracle_delegations.dispatch(
        ctx=object(), delegation_id="untrusted", session="theo",
        request_text="Auth-disabled request", authenticated_at_admission=False)

    admission = calls[0]["prompt_admission"]
    assert admission.authenticated_at_admission is False
    assert admission.cooperative_principal == "automation"
    assert admission.cooperative_principal != "user"


def test_dispatch_retry_does_not_reopen_terminal_failure(tmp_path, monkeypatch):
    _agent(tmp_path)
    calls = []

    class FailingDispatch:
        def __init__(self, _ctx):
            pass

        def dispatch(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("backend rejected the turn")

    from lib import turn_dispatch
    monkeypatch.setattr(turn_dispatch, "TurnDispatchService", FailingDispatch)

    with pytest.raises(RuntimeError, match="backend rejected"):
        oracle_delegations.dispatch(
            ctx=object(), delegation_id="failed-retry", session="theo",
            request_text="Perform this exactly once",
            authenticated_at_admission=True)

    retry = oracle_delegations.dispatch(
        ctx=object(), delegation_id="failed-retry", session="theo",
        request_text="Perform this exactly once",
        authenticated_at_admission=True)

    assert len(calls) == 1
    assert retry["status"] == "failed"
    assert retry["error"] == "backend rejected the turn"


def test_invalid_delegation_id_is_rejected():
    with pytest.raises(ValueError, match="invalid delegation_id"):
        oracle_delegations.normalize_id("contains spaces")


def test_codex_terminal_result_uses_last_agent_message():
    from lib.turn_dispatch import _result_assistant_text
    assert _result_assistant_text({
        "type": "result", "subtype": "success",
        "last_agent_message": "Codex finished the investigation",
    }) == "Codex finished the investigation"


def test_authenticated_oracle_admission_keeps_user_authority():
    admission = prompt_admissions.create(
        authenticated_at_admission=True, origin="oracle",
        sender_agent_id="", channel="oracle", observed_at=1,
        client_admission_id="oracle-1", trace_id="oracle-1",
        original_text="Authorized from the driving session")
    assert admission.cooperative_principal == "user"
    assert admission.principal_id == "user"


def test_terminal_turn_failure_becomes_deliverable(tmp_path):
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="failed-1", trace_id="oracle-failed-1",
        client_msg_id="oracle-failed-1", agent_id=agent["agent_id"],
        session="theo", request_text="Fail safely",
    )
    assert oracle_delegations.fail_for_trace(
        "oracle-failed-1", "Backend unavailable")
    pending = oracle_delegations.undelivered()
    assert pending[0]["status"] == "failed"
    assert pending[0]["error"] == "Backend unavailable"


def test_started_turn_lost_on_restart_fails_without_replay(tmp_path):
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="orphaned", trace_id="oracle-orphaned",
        client_msg_id="oracle-orphaned", agent_id=agent["agent_id"],
        session="theo", request_text="Potentially consequential")
    message_store.record_user_message(
        agent_id=agent["agent_id"], backend_session_id="backend-1",
        client_msg_id="oracle-orphaned", text="Potentially consequential")

    changed = oracle_delegations.reconcile_orphans(
        is_live=lambda _agent_id, _trace_id: False)

    assert changed == 1
    row = oracle_delegations.get("orphaned")
    assert row["status"] == "failed"
    assert "delegate it again" in row["error"]


def test_orphan_reconciliation_recovers_committed_assistant_text(tmp_path):
    agent = _agent(tmp_path)
    agent_id = agent["agent_id"]
    agents.start_runtime(agent_id, "theo")
    agents.bind_backend_session(agent_id, "backend-1")
    agents.open_turn(
        agent_id=agent_id, source="pwa", trace_id="oracle-committed",
        synthesize_audio=False)
    oracle_delegations.begin(
        delegation_id="committed", trace_id="oracle-committed",
        client_msg_id="oracle-committed", agent_id=agent_id,
        session="theo", request_text="Return durable text")
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="backend-1",
        client_msg_id="oracle-committed", text="Return durable text")
    message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="backend-1",
        trace_id="oracle-committed", text="Committed before restart")

    changed = oracle_delegations.reconcile_orphans(
        is_live=lambda _agent_id, _trace_id: False)

    assert changed == 1
    row = oracle_delegations.get("committed")
    assert row["status"] == "completed"
    assert row["result_text"] == "Committed before restart"


def test_unstarted_queued_turn_survives_orphan_reconciliation(tmp_path):
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="still-queued", trace_id="oracle-still-queued",
        client_msg_id="oracle-still-queued", agent_id=agent["agent_id"],
        session="theo", request_text="Wait safely")

    changed = oracle_delegations.reconcile_orphans(
        is_live=lambda _agent_id, _trace_id: False)

    assert changed == 0
    assert oracle_delegations.get("still-queued")["status"] == "accepted"


def test_terminal_assistant_message_completes_exact_delegation(tmp_path):
    agent = _agent(tmp_path)
    agent_id = agent["agent_id"]
    agents.start_runtime(agent_id, "theo")
    agents.bind_backend_session(agent_id, "backend-1")
    agents.open_turn(
        agent_id=agent_id, source="pwa", trace_id="oracle-result-1",
        synthesize_audio=False)
    oracle_delegations.begin(
        delegation_id="result-1", trace_id="oracle-result-1",
        client_msg_id="oracle-result-1", agent_id=agent_id,
        session="theo", request_text="Return a result",
    )
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="backend-1",
        client_msg_id="oracle-result-1", text="Return a result")

    row = agents.finalize_live_assistant_message(
        agent_id=agent_id, backend_session_id="backend-1",
        trace_id="oracle-result-1", text="Exact durable result")

    assert row is not None
    delegation = oracle_delegations.get("result-1")
    assert delegation["status"] == "completed"
    assert delegation["result_message_id"] == row["id"]
    assert delegation["result_text"] == "Exact durable result"


def test_single_queued_delegation_requires_agent_cancellation(tmp_path):
    from lib import turn_queue
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="queued-cancel", trace_id="oracle-queued-cancel",
        client_msg_id="oracle-queued-cancel", agent_id=agent["agent_id"],
        session="theo", request_text="Do not run later",
    )
    turn_queue.enqueue(
        queue_id="oracle-queued-cancel", agent_id=agent["agent_id"],
        session="theo", text="Do not run later",
        trace_id="oracle-queued-cancel", client_msg_id="oracle-queued-cancel",
        synthesize_audio=False, origin="oracle", sender_agent_id="")
    oracle_delegations.mark_dispatched(
        "queued-cancel", backend_session_id="backend-1", queued=True)

    with pytest.raises(
        oracle_delegations.DelegationNotCancellable,
        match="requires agent cancellation",
    ):
        oracle_delegations.cancel("queued-cancel")

    assert oracle_delegations.get("queued-cancel")["status"] == "queued"
    assert turn_queue.contains("oracle-queued-cancel")


def test_started_queued_delegation_requires_agent_cancellation(tmp_path):
    from lib import turn_queue
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="started-queue", trace_id="oracle-started-queue",
        client_msg_id="oracle-started-queue", agent_id=agent["agent_id"],
        session="theo", request_text="Now running")
    turn_queue.enqueue(
        queue_id="oracle-started-queue", agent_id=agent["agent_id"],
        session="theo", text="Now running",
        trace_id="oracle-started-queue", client_msg_id="oracle-started-queue",
        synthesize_audio=False, origin="oracle", sender_agent_id="")
    oracle_delegations.mark_dispatched(
        "started-queue", backend_session_id="backend-1", queued=True)

    assert oracle_delegations.mark_started_for_trace("oracle-started-queue")
    with pytest.raises(oracle_delegations.DelegationNotCancellable):
        oracle_delegations.cancel("started-queue")
    assert oracle_delegations.get("started-queue")["status"] == "accepted"


def test_late_queued_receipt_does_not_overwrite_started_state(tmp_path):
    from lib import turn_queue
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="late-receipt", trace_id="oracle-late-receipt",
        client_msg_id="oracle-late-receipt", agent_id=agent["agent_id"],
        session="theo", request_text="Already claimed")
    turn_queue.enqueue(
        queue_id="oracle-late-receipt", agent_id=agent["agent_id"],
        session="theo", text="Already claimed",
        trace_id="oracle-late-receipt", client_msg_id="oracle-late-receipt",
        synthesize_audio=False, origin="oracle", sender_agent_id="")
    turn_queue.mark_started("oracle-late-receipt")

    row = oracle_delegations.mark_dispatched(
        "late-receipt", backend_session_id="backend-1", queued=True)

    assert row["status"] == "accepted"


def test_single_active_delegation_cannot_report_false_cancellation(tmp_path):
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="active-cancel", trace_id="oracle-active-cancel",
        client_msg_id="oracle-active-cancel", agent_id=agent["agent_id"],
        session="theo", request_text="Already running")

    with pytest.raises(
        oracle_delegations.DelegationNotCancellable,
        match="requires agent cancellation",
    ):
        oracle_delegations.cancel("active-cancel")

    assert oracle_delegations.get("active-cancel")["status"] == "accepted"


def test_cancel_for_session_uses_durable_owner_state(tmp_path):
    agent = _agent(tmp_path)
    for owner, suffix in (("phone-a", "1"), ("phone-a", "2"),
                          ("phone-b", "other")):
        oracle_delegations.begin(
            delegation_id=f"cancel-{suffix}",
            trace_id=f"oracle-cancel-{suffix}",
            client_msg_id=f"oracle-cancel-{suffix}",
            agent_id=agent["agent_id"], session="theo",
            request_text=f"Work {suffix}", owner_principal=owner)
    oracle_delegations.mark_dispatched(
        "cancel-2", backend_session_id="backend-1", queued=True)

    stops = []
    cancelled = oracle_delegations.cancel_for_session(
        "theo", stop=lambda: stops.append("stopped"),
        owner_principal="phone-a")

    assert stops == ["stopped"]
    assert {row["delegation_id"] for row in cancelled} == {
        "cancel-1", "cancel-2"}
    assert all(row["status"] == "cancelled" for row in cancelled)
    assert oracle_delegations.get("cancel-other")["status"] == "accepted"
    assert not oracle_delegations.fail_for_trace(
        "oracle-cancel-1", "stop callback arrived late")
    assert oracle_delegations.get("cancel-1")["status"] == "cancelled"


def test_cancel_for_session_preserves_rows_when_stop_fails(tmp_path):
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="stop-failed", trace_id="oracle-stop-failed",
        client_msg_id="oracle-stop-failed", agent_id=agent["agent_id"],
        session="theo", request_text="Keep result recoverable",
        owner_principal="phone-a")

    def fail_stop():
        raise RuntimeError("backend stop failed")

    with pytest.raises(RuntimeError, match="backend stop failed"):
        oracle_delegations.cancel_for_session(
            "theo", stop=fail_stop, owner_principal="phone-a")

    assert oracle_delegations.get("stop-failed")["status"] == "accepted"


def test_cancel_for_session_counts_only_rows_it_transitions(tmp_path):
    agent = _agent(tmp_path)
    oracle_delegations.begin(
        delegation_id="finished-race", trace_id="oracle-finished-race",
        client_msg_id="oracle-finished-race", agent_id=agent["agent_id"],
        session="theo", request_text="Finishes during stop",
        owner_principal="phone-a")

    def finish_during_stop():
        assert oracle_delegations.complete_for_trace(
            trace_id="oracle-finished-race", message_id="answer-race",
            text="Already completed")

    cancelled = oracle_delegations.cancel_for_session(
        "theo", stop=finish_during_stop, owner_principal="phone-a")

    assert cancelled == []
    assert oracle_delegations.get("finished-race")["status"] == "completed"


def test_result_delivery_is_scoped_to_creating_device(tmp_path):
    agent = _agent(tmp_path)
    for owner, suffix in (("phone-a", "a"), ("phone-b", "b")):
        oracle_delegations.begin(
            delegation_id=f"owned-{suffix}", trace_id=f"oracle-owned-{suffix}",
            client_msg_id=f"oracle-owned-{suffix}", agent_id=agent["agent_id"],
            session="theo", request_text=f"Private work {suffix}",
            owner_principal=owner)
        oracle_delegations.complete_for_trace(
            trace_id=f"oracle-owned-{suffix}", message_id=f"answer-{suffix}",
            text=f"Private result {suffix}")

    assert [row["delegation_id"] for row in oracle_delegations.undelivered(
        owner_principal="phone-a")] == ["owned-a"]
    assert [row["delegation_id"] for row in oracle_delegations.undelivered(
        owner_principal="phone-b")] == ["owned-b"]
    assert not oracle_delegations.acknowledge(
        "owned-a", owner_principal="phone-b")
    assert oracle_delegations.acknowledge(
        "owned-a", owner_principal="phone-a")
