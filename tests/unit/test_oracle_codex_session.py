import json
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from lib import oracle_router
from lib.oracle_codex_session import CodexRouterSession

BODY = {"model": oracle_router.MODEL, "instructions": "Route only the current snapshot", "input": "{}", "tools": []}


def fake_session(monkeypatch, unexpected=False):
    stop = threading.Event(); session = CodexRouterSession(stop); calls = []
    session.directory = tempfile.TemporaryDirectory()
    monkeypatch.setattr(session, "_ensure", lambda deadline: True)
    class Writer:
        def write(self, line):
            value = json.loads(line); method = value.get("method"); calls.append(method)
            if method == "thread/start": result = {"thread": {"id": "thread-"+str(calls.count(method))}}
            elif method == "turn/start":
                ident = "turn-"+str(calls.count(method)); thread = value["params"]["threadId"]
                # Real app-server may deliver output before the turn/start reply.
                session.events.put({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": "old-turn", "status": "completed"}}})
                session.events.put({"method": "item/agentMessage/delta", "params": {"threadId": thread,"turnId": ident,"delta": '{"message":"Current answer","actions":[]}'}})
                if unexpected: session.events.put({"method":"item/completed","params":{"threadId":thread,"turnId":ident,"item":{"type":"commandExecution"}}})
                session.events.put({"method":"turn/completed","params":{"threadId":thread,"turn":{"id":ident,"status":"completed"}}})
                result = {"turn":{"id":ident}}
            else: result = {}
            if "id" in value:session.events.put({"id":value["id"],"result":result})
        def flush(self): pass
        def close(self): pass
    session.process = SimpleNamespace(stdin=Writer(),wait=lambda timeout: 0)
    return session,stop,calls


def test_output_before_rpc_reply_is_retained_and_old_turn_is_ignored(monkeypatch):
    session, stop, calls = fake_session(monkeypatch)
    try:
        first = session.route(BODY, 1)
        second = session.route(BODY, 1)
        assert first["output"][0]["content"][0]["text"] == "Current answer"
        assert second["transport_metrics"]["thread_reused"] is True
        assert calls.count("thread/start") == 1
    finally: stop.set(); session.close()


def test_changed_policy_retires_previous_model_context(monkeypatch):
    session, stop, calls = fake_session(monkeypatch)
    try:
        session.route(BODY, 1)
        session.route({**BODY,"instructions":"New current policy"},1)
        assert calls.count("thread/start") == 2
        assert calls.count("thread/unsubscribe") == 1
    finally: stop.set(); session.close()


def test_unexpected_tool_execution_fails_and_never_uses_api(monkeypatch):
    session, stop, _ = fake_session(monkeypatch, unexpected=True)
    calls=[]; monkeypatch.setattr(oracle_router,"urlopen",lambda *a,**k:calls.append(True))
    try:
        with pytest.raises(oracle_router.RouterError,match="unexpected_codex_tool"):
            oracle_router.route(BODY,backend="codex",api_key="present",stop=stop,session=session)
        assert calls == [] and session.process is None
    finally: stop.set(); session.close()


def test_voice_stop_closes_an_idle_router(monkeypatch):
    session, stop, _ = fake_session(monkeypatch)
    stop.set()
    for _ in range(100):
        if session.process is None: break
        time.sleep(.01)
    assert session.closed.is_set() and session.process is None
    with pytest.raises(oracle_router.RouterError,match="router_cancelled"):
        session.route(BODY,1)
