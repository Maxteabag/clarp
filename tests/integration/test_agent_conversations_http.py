"""Pair conversations over the real authenticated HTTP routes."""
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from lib import agents, message_store

_spec = importlib.util.spec_from_file_location(
    "agent_conversations_http_server", Path(__file__).resolve().parents[2] / "server/server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
TOKEN = "isolated-pair-http-test"


@pytest.fixture
def host(tmp_path):
    hugo = agents.create_agent(persona="Hugo", voice_id="V1", cwd=str(tmp_path), session="hugo")
    cpp = agents.create_agent(persona="C++ Agent", voice_id="V2", cwd=str(tmp_path), session="cagent")
    agents.set_focus(hugo)
    ctx = SimpleNamespace(auth_token=TOKEN, default_session="hugo", agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda *_: None))
    srv = server.ContextHTTPServer(("127.0.0.1", 0), server.Handler, ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{srv.server_port}", hugo=hugo, cpp=cpp)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def request(host, path, *, auth=True):
    headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
    req = urllib.request.Request(host.base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, (json.load(exc) if exc.headers.get("Content-Type", "").startswith("application/json") else {})


def test_pair_routes_require_authentication(host):
    assert request(host, "/agent-conversations", auth=False)[0] == 401


def test_pair_list_and_log_share_the_log_shape(host):
    rec = message_store.record_user_message(agent_id=host.hugo, backend_session_id="bs1", client_msg_id="c1",
                                            text="Status update", origin="agent", sender_agent_id=host.cpp)
    agents.open_turn(agent_id=host.hugo, source="pwa", trace_id="t1")
    message_store.store_transcript_turns(agent_id=host.hugo, backend_session_id="bs1", source_file="f",
                                         turns=[{"role": "assistant", "text": "Good, thanks.", "timestamp": "2099-01-01T00:00:00Z"}])
    status, body = request(host, "/agent-conversations")
    assert status == 200 and len(body["conversations"]) == 1
    room = body["conversations"][0]
    assert room["conversation_id"] == f"pair:{min(host.hugo, host.cpp)}:{max(host.hugo, host.cpp)}"
    assert room["title"] == "C++ Agent & Hugo"
    status, log = request(host, f'/log?session={room["conversation_id"]}&limit=50')
    assert status == 200
    assert {"turns", "latest_revision", "has_more", "missing", "conversation_id"} <= log.keys()
    assert [t["sender_name"] for t in log["turns"]] == ["C++ Agent", "Hugo"]
    assert log["turns"][0]["id"] == rec["id"] and log["turns"][0]["delivery"] == "sent"
    assert log["turns"][1]["reply_to_name"] == "C++ Agent" and log["turns"][1]["delivery"] == "private"
    status, delta = request(host, f'/log?session={room["conversation_id"]}&after_revision={log["latest_revision"]}')
    assert status == 200 and delta["turns"] == []
    status, missing = request(host, "/log?session=pair:x:y")
    assert status == 200 and missing["missing"] is True and missing["turns"] == []


def test_pair_list_shares_response_without_weakening_auth_or_hiding_new_messages(host, monkeypatch):
    from lib import agent_conversations
    original = agent_conversations.list_conversations
    calls = []
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(agent_conversations, 'list_conversations', counted)
    for _ in range(8):
        assert request(host, '/agent-conversations') == (200, {'conversations': []})
    assert len(calls) == 1
    assert request(host, '/agent-conversations', auth=False)[0] == 401
    message_store.record_user_message(agent_id=host.hugo, backend_session_id='bs1',
        client_msg_id='new-pair', text='hello', origin='agent', sender_agent_id=host.cpp)
    status, body = request(host, '/agent-conversations')
    assert status == 200 and len(body['conversations']) == 1
    assert len(calls) == 2
