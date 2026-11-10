"""Native goal responses and notifications must mirror in wire order."""
import threading
from types import SimpleNamespace

import pytest

from lib import agent_goals, agents, codex_app_server as cas
from tests.unit.test_agent_goals import _Stream, _agent, _client


@pytest.mark.parametrize("action", ["start", "resume", "pause", "get", "clear", "stop",
                                   "move", "resume_stopped", "repause"])
@pytest.mark.parametrize("newer_status", ["complete", None])
def test_newer_notification_survives_a_waiting_goal_response(tmp_path, action, newer_status):
    agent_id = _agent(tmp_path)
    stream = _Stream()
    client = _client(stream)
    client._lock = threading.Lock()
    client._pending = {}
    client._next_id = 1
    client.generation = 1
    client._loaded_threads.add("thread-theo")
    agent_goals.upsert(agent_id, session="theo", backend="codex",
                       goal={"objective": "synthetic", "status": "active"})

    def deliver(message):
        if not message["method"].startswith("thread/goal/"):
            client._handle_message({"id": message["id"], "result": {}})
            return
        # The reader may consume both lines before the request waiter runs.
        # Equal whole-second timestamps cannot distinguish these versions.
        response_goal = {"objective": "synthetic", "status": "paused" if action in
                         ("pause", "stop", "move", "resume_stopped", "repause") else "active", "updatedAt": 7}
        client._handle_message({"id": message["id"], "result":
                                {} if action == "clear" else {"goal": response_goal}})
        params = {"threadId": "thread-theo"}
        if newer_status:
            params["goal"] = {**response_goal, "status": newer_status}
        client._handle_message({"method": "thread/goal/updated" if newer_status else
                                "thread/goal/cleared", "params": params})

    client._send = deliver
    if action == "stop":
        client._pause_goal_for_stop(SimpleNamespace(agent_id=agent_id, thread_id="thread-theo"))
    elif action == "move":
        client._pause_for_move("thread-theo")
        assert cas._move_get("thread-theo")["phase"] == "paused"
    elif action in ("resume_stopped", "repause"):
        cas._stop_pause_put("thread-theo")
        if action == "resume_stopped":
            assert client._resume_stopped_safely("thread-theo", lambda: {"resumed": True}) == {"resumed": True}
            assert not client.pausing_stopped
        else:
            client._interrupt_and_repause("thread-theo", "synthetic-continuation")
            assert not client.repausing
        assert cas._stop_pause_get("thread-theo") is None
    else:
        result = client.goal(agents.get_by_agent_id(agent_id), action, "synthetic")
        assert (result or {}).get("status") == newer_status
    assert (agent_goals.get(agent_id) or {}).get("status") == newer_status
    assert (stream.events[-1]["goal"] or {}).get("status") == newer_status
    assert len(stream.events) == 2, "the response and notification each mirror once"
    assert not client._goal_activations(), "every successful answer must release its bookkeeping"


def test_failed_result_callback_logs_and_releases_the_waiter(monkeypatch):
    client = _client()
    client._lock = threading.Lock()
    client._pending = {}
    client._next_id = 1
    logged = []
    monkeypatch.setattr(cas, "log_exception", lambda code, exc, **_kw: logged.append(code))
    client._send = lambda message: client._handle_message({
        "id": message["id"], "result": {"ok": True}})

    def failed(_result):
        raise RuntimeError("synthetic callback failure")

    assert client.request("thread/goal/get", {}, timeout=0, on_result=failed) == {"ok": True}
    assert logged == ["codexGoalMirrorFail"]
    assert client.request("next", {}, timeout=0) == {"ok": True}


@pytest.mark.parametrize("failure", ["timeout", "error", "reader_end"])
def test_unsuccessful_request_never_mirrors_a_result(failure):
    client = _client()
    client._lock = threading.Lock()
    client._pending = {}
    client._next_id = 1
    callbacks = []
    sent = []

    def deliver(message):
        sent.append(message)
        if failure == "error":
            client._handle_message({"id": message["id"], "error": {"code": -1}})
        elif failure == "reader_end":
            client._fail_all("synthetic reader exit")

    client._send = deliver
    with pytest.raises(TimeoutError if failure == "timeout" else RuntimeError):
        client.request("thread/goal/get", {}, timeout=0, on_result=callbacks.append)
    client._handle_message({"id": sent[0]["id"], "result": {"goal": None}})
    assert callbacks == []
