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


@pytest.mark.parametrize("entry", ["finish", "retry", "stop", "terminal"])
def test_selected_callback_survives_stop_removing_its_slot_before_rotation(tmp_path, monkeypatch, entry):
    from lib import db, turn_dispatch as td
    from lib.turn_command import _TurnSpec
    from tests.unit.test_graceful_release_drain import _service

    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    service, _provider = _service(tmp_path)
    head = _TurnSpec(backend="codex", text="selected work", cwd=tmp_path,
                     backend_session_id=old, is_new_session=False, session="theo",
                     agent_id=agent, trace_id="selected-trace", context=None,
                     synthesize_audio=False, bypass_pause=True)
    selected, finish = threading.Event(), threading.Event()
    completions, errors = [], []

    def retained_callback(self, agent_id, spec):
        selected.set()
        if not finish.wait(5):
            raise RuntimeError("selected callback did not finish")
        completions.append(spec.trace_id)

    monkeypatch.setattr(td.TurnDispatchService, "_resume_and_spawn", retained_callback)
    _SLOTS.enqueue(agent, head)
    if entry == "finish":
        _SLOTS.claim(agent, "prior-trace")
        action = lambda: service._finish_slot(agent, "prior-trace", "codex")
    elif entry == "retry":
        action = lambda: service._retry_handoff(agent)
    elif entry == "stop":
        _SLOTS.claim(agent, td._STOPPING_SENTINEL)
        action = lambda: td.finish_stop(service.ctx, agent)
    else:
        _SLOTS.claim(agent, td._TERMINAL_SENTINEL)
        action = lambda: td.drain_after_terminal(service.ctx, agent)

    def run():
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close_local()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert selected.wait(5), "real selector never published its callback"
        # Actual Stop clears the selected slot. Its absence is not absence of
        # the callback that was already published outside TURN.
        td.clear_for_agent(agent, preserve_queue=True, pause_queue=True)
        td.finish_stop(service.ctx, agent)
        assert _SLOTS.get(agent) is None and _SLOTS.queue_depth(agent) == 0
        assert not _SLOTS.launch_running(agent)
        with pytest.raises(journal.RotationConflict):
            admission.reserve(agent, old, "operation-a")
        assert journal.get(agent) is None
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and errors == [] and completions == ["selected-trace"]
    assert admission.reserve(agent, old, "operation-a")["phase"] == "reserved"


def test_overlapping_selected_tokens_retire_only_their_own_lifetime(tmp_path):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    parent = admission.begin_selected(agent)
    child = admission.begin_selected(agent)
    assert parent is not None and child is not None and parent is not child
    assert admission.finish_selected(agent, parent)
    assert not admission.finish_selected(agent, parent)
    assert not admission.finish_selected(agent, object())
    with pytest.raises(journal.RotationConflict):
        admission.reserve(agent, old, "operation-a")
    assert admission.finish_selected(agent, child)
    assert admission.reserve(agent, old, "operation-a")["phase"] == "reserved"
