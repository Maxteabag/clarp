"""Terminal handle removal does not mean its canonical writers have finished."""
from __future__ import annotations

import threading

import pytest

from lib import agents, native_rotation_admission as admission, native_rotation_journal as journal
from lib import codex_app_server
from tests.unit.test_agent_goals import _agent, _client


@pytest.fixture(autouse=True)
def _owned_bookkeeping(monkeypatch):
    monkeypatch.setattr(admission, "_owners", {})


def _adopted(tmp_path):
    agent = _agent(tmp_path)
    thread = agents.live_backend_session(agent)
    client = _client()
    client._notification("turn/started", {"threadId": thread, "turn": {"id": "native-owned-turn"}})
    return agent, thread, client, client._actives[agent]


def test_rotation_waits_for_real_native_terminal_callback_after_handle_is_done(tmp_path, monkeypatch):
    agent, thread, client, active = _adopted(tmp_path)
    callback_entered, finish_callback = threading.Event(), threading.Event()
    workers = []
    original_thread = threading.Thread
    original_result = active.on_result

    def recorded_thread(*args, **kwargs):
        worker = original_thread(*args, **kwargs)
        if kwargs.get("name") == "codex-callback":
            workers.append(worker)
        return worker

    def held_result(value):
        callback_entered.set()
        if not finish_callback.wait(5):
            raise RuntimeError("native callback test barrier expired")
        original_result(value)

    monkeypatch.setattr(threading, "Thread", recorded_thread)
    active.on_result = held_result
    client._notification("turn/completed", {"threadId": thread,
                                           "turn": {"id": "native-owned-turn", "status": "completed"}})
    try:
        assert callback_entered.wait(5)
        assert active.handle._done.is_set() and agent not in client._actives
        with pytest.raises(journal.RotationConflict):
            admission.reserve(agent, thread, "rotation-a")
        assert journal.get(agent) is None
    finally:
        finish_callback.set()
        for worker in workers:
            worker.join(5)
    assert workers and all(not worker.is_alive() for worker in workers)
    assert agents.latest_state(agent)["kind"] == "done"
    assert admission.reserve(agent, thread, "rotation-a")["phase"] == "reserved"


def test_unknown_callback_publication_is_retained_as_specific_failure_owner(tmp_path, monkeypatch):
    agent, thread, client, active = _adopted(tmp_path)
    failure = RuntimeError("callback thread could not be published")

    def denied_start(self):
        raise failure

    monkeypatch.setattr(threading.Thread, "start", denied_start)
    with pytest.raises(RuntimeError) as caught:
        client._notification("turn/completed", {"threadId": thread,
                                               "turn": {"id": "native-owned-turn", "status": "completed"}})
    assert caught.value is failure
    assert active.handle._done.is_set()
    with pytest.raises(journal.RotationConflict, match="uncertain ownership"):
        admission.reserve(agent, thread, "rotation-a")
    assert journal.get(agent) is None
