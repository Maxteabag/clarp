"""Rotation reserves an idle owner without holding a lock across provider work."""
from __future__ import annotations

import threading

import pytest

from lib import agents, native_rotation_admission as admission, native_rotation_journal as journal
from lib.turn_command import DispatchError
from lib.turn_dispatch_state import _SLOTS
from tests.unit.test_agent_goals import _agent


@pytest.fixture(autouse=True)
def _owned_process_bookkeeping(monkeypatch):
    monkeypatch.setattr(admission, "_owners", {})


def test_running_admission_refuses_rotation_before_durable_or_native_effects(tmp_path):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    entered, finish = threading.Event(), threading.Event()
    errors = []

    def send_in_progress():
        try:
            with admission.admitting(agent):
                entered.set()
                if not finish.wait(5):
                    raise RuntimeError("test owner did not finish its admission")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=send_in_progress)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(journal.RotationConflict):
            admission.reserve(agent, old, "operation-a")
        assert journal.get(agent) is None
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and errors == []
    # The rejected reservation neither wedges later work nor needs a reset.
    with admission.admitting(agent):
        pass
    record = admission.reserve(agent, old, "operation-a")
    assert record["phase"] == "reserved"
    with pytest.raises(DispatchError) as caught:
        with admission.admitting(agent):
            pytest.fail("no later admission may enter")
    assert caught.value.status == 503
    assert admission.reserve(agent, old, "operation-a") == record


@pytest.mark.parametrize("busy", ["slot", "queue", "launch"])
def test_preselected_retry_handoff_or_launch_refuses_rotation(tmp_path, busy):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    context = None
    if busy == "slot":
        _SLOTS.claim(agent, "selected-trace")
    elif busy == "queue":
        _SLOTS.enqueue(agent, object())
    else:
        context = _SLOTS.launching(agent, "selected-trace")
        context.__enter__()
    try:
        with pytest.raises(journal.RotationConflict):
            admission.reserve(agent, old, "operation-a")
        assert journal.get(agent) is None
        # No global admission fence was installed on unrelated agents.
        assert not _SLOTS.fenced
    finally:
        if context is not None:
            context.__exit__(None, None, None)
        _SLOTS.release(agent, "selected-trace")
        _SLOTS.take_queued(agent)


def test_restart_without_memory_flag_is_still_fenced_by_durable_intent(tmp_path):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    journal.reserve(agent, old, "operation-a")
    with pytest.raises(DispatchError) as caught:
        with admission.admitting(agent):
            pytest.fail("durable pending intent cannot be ignored")
    assert caught.value.status == 503
    assert journal.get(agent)["operation_id"] == "operation-a"
