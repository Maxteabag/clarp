from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from lib import agents as agents_db
from lib import codex_app_server
from lib import codex_runner
from lib.codex_runner import _TurnState


class _Handle:
    def __init__(self):
        self._done = threading.Event()


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def test_agent_message_deltas_grow_one_live_row_and_flush_on_completion():
    agent_id = agents_db.create_agent(
        persona="Caleb", voice_id="v", cwd="/tmp", session="caleb",
        backend="codex")
    agents_db.open_turn(
        agent_id=agent_id, source="pwa", trace_id="trace-1")
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1",
        thread_id="thread-1",
        agent_id=agent_id,
        session="caleb",
        trace_id="trace-1",
        state=_TurnState(live_backend_session_id="thread-1"),
        handle=_Handle(),
        on_result=None,
        on_error=None,
        stream=None,
        enqueue=lambda **_kwargs: 0,
    )

    client._notification("item/agentMessage/delta", {"delta": "Hel"})
    client._notification("item/agentMessage/delta", {"delta": "lo"})
    client._notification("turn/completed", {"turn": {"status": "completed"}})

    rows = agents_db.conn().execute(
        """SELECT text, kind FROM messages
             WHERE agent_id = ? AND source_file LIKE 'live:%'""",
        (agent_id,),
    ).fetchall()
    assert [(row["text"], row["kind"]) for row in rows] == [("Hello", "live")]


def test_normalize_item_keeps_official_activity_types_semantic():
    expected = {
        "dynamicToolCall": "dynamic_tool_call",
        "collabToolCall": "collab_tool_call",
        "collabAgentToolCall": "collab_agent_tool_call",
        "imageView": "image_view",
        "imageGeneration": "image_generation",
        "plan": "plan",
    }
    assert {
        source: codex_app_server._normalize_item({"type": source})["type"]
        for source in expected
    } == expected


def test_rate_limit_update_normalizes_and_broadcasts_new_events(monkeypatch):
    stream = _Stream()
    client = object.__new__(codex_app_server._Client)
    client.agent_id = "agent-1"
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id="agent-1",
        session="codex", trace_id="trace-1", state=_TurnState(),
        handle=_Handle(), on_result=None, on_error=None, stream=stream,
        enqueue=lambda **_kwargs: 0)
    seen = []

    def capture(payload):
        seen.append(payload)
        return {"limit_events": [{
            "type": "provider-limit", "kind": "warning",
            "provider_limit_event_id": "ple-1",
        }]}

    monkeypatch.setattr(
        codex_app_server.backend_usage, "capture_codex_rate_limits", capture)
    payload = {"rateLimits": {"primary": {"usedPercent": 82}}}
    client._notification("account/rateLimits/updated", payload)

    assert seen == [payload]
    assert stream.events == [{
        "type": "provider-limit", "kind": "warning",
        "provider_limit_event_id": "ple-1",
    }]

    client.active = None
    client.stream = stream
    client._notification("account/rateLimits/updated", payload)
    assert len(stream.events) == 2, "between-turn updates must still broadcast"


def test_client_factory_attaches_stream_before_initialization(monkeypatch):
    stream = _Stream()
    created = []

    class FakeClient:
        def __init__(self, agent_id, session, stream=None):
            self.agent_id = agent_id
            self.session = session
            self.stream = stream
            self.proc = type("Proc", (), {"poll": lambda _self: None})()
            created.append(self)

    monkeypatch.setattr(codex_app_server, "_Client", FakeClient)
    codex_app_server._CLIENTS.clear()
    client = codex_app_server._client("agent-1", "codex", stream=stream)
    assert created == [client]
    assert client.stream is stream
    second = codex_app_server._client("agent-2", "other")
    assert second is client
    assert created == [client]


class _FakeProc:
    def __init__(self):
        self.stdin = type("Stdin", (), {"close": lambda _self: None})()
        self.signals = []
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self.signals.append("term")
        self._code = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.signals.append("kill")
        self._code = 0


def test_recycle_clients_closes_stdio_and_drops_writer_slots():
    first = object.__new__(codex_app_server._Client)
    first.active = None
    first.proc = _FakeProc()
    second = object.__new__(codex_app_server._Client)
    second.active = None
    second.proc = _FakeProc()
    codex_app_server._CLIENTS.clear()
    codex_app_server._CLIENTS["a"] = first
    codex_app_server._CLIENTS["b"] = second
    assert codex_app_server.recycle_clients() == 2
    assert codex_app_server._CLIENTS == {}
    assert first.proc.signals == ["term"]
    assert second.proc.signals == ["term"]


def test_recycle_clients_closes_a_shared_client_once():
    only = object.__new__(codex_app_server._Client)
    only.active = None
    only.proc = _FakeProc()
    only._actives = {}
    codex_app_server._CLIENTS.clear()
    codex_app_server._CLIENTS[codex_app_server._SHARED_KEY] = only
    codex_app_server._CLIENTS["stale-agent"] = only
    assert codex_app_server.recycle_clients() == 1
    assert codex_app_server._CLIENTS == {}
    assert only.proc.signals == ["term"]


def test_pick_active_routes_by_thread_id():
    client = object.__new__(codex_app_server._Client)
    client.active = None
    client._actives = {}
    first = codex_app_server._ActiveTurn(
        "turn-a", "thread-a", "agent-a", "one", "tr-a", _TurnState(),
        _Handle(), None, None, None, lambda **_kwargs: 0)
    second = codex_app_server._ActiveTurn(
        "turn-b", "thread-b", "agent-b", "two", "tr-b", _TurnState(),
        _Handle(), None, None, None, lambda **_kwargs: 0)
    client._actives = {"agent-a": first, "agent-b": second}
    client.active = second
    assert client._pick_active({"threadId": "thread-a"}) is first
    assert client._pick_active({"turn": {"threadId": "thread-b"}}) is second
    assert client._pick_active({}) is second


def test_interrupt_before_turn_id_does_not_kill_shared_process():
    client = object.__new__(codex_app_server._Client)
    client.proc = _FakeProc()
    client.active = None
    first = codex_app_server._ActiveTurn(
        "", "thread-a", "agent-a", "one", "tr-a", _TurnState(),
        codex_app_server.AppTurnHandle(client, "agent-a"),
        None, None, None, lambda **_kwargs: 0)
    second = codex_app_server._ActiveTurn(
        "", "thread-b", "agent-b", "two", "tr-b", _TurnState(),
        codex_app_server.AppTurnHandle(client, "agent-b"),
        None, None, None, lambda **_kwargs: 0)
    client._actives = {"agent-a": first, "agent-b": second}
    client.interrupt_active("agent-a")
    assert client.proc.signals == []


FAKE_CODEX = Path(__file__).resolve().parents[2] / "tests/qa/fake_codex.py"


def _install_fake_codex(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(home))
    monkeypatch.setattr(codex_runner, "CODEX_BIN", str(FAKE_CODEX))
    codex_app_server._CLIENTS.clear()
    return home


class _NullStream:
    def broadcast(self, event):
        return None


def _spawn_fake_turn(tmp_path, *, agent_id, session, text, backend_session_id="",
                     is_new_session=True, on_result=None, on_error=None):
    return codex_app_server.spawn_turn(
        text=text, cwd=tmp_path, backend_session_id=backend_session_id,
        is_new_session=is_new_session, session=session, agent_id=agent_id,
        on_result=on_result, on_error=on_error, trace_id=f"tr-{session}",
        stream=_NullStream(), enqueue=lambda **_kwargs: 0, voice_preamble=False,
    )


def test_two_agents_share_one_fake_appserver(tmp_path, monkeypatch):
    _install_fake_codex(tmp_path, monkeypatch)
    results, errors = {}, {}
    done = threading.Event()

    def capture(key):
        def on_result(payload):
            results[key] = payload
            if len(results) + len(errors) >= 2:
                done.set()

        def on_error(message):
            errors[key] = message
            if len(results) + len(errors) >= 2:
                done.set()

        return on_result, on_error

    first_id = agents_db.create_agent(
        persona="One", voice_id="v", cwd=str(tmp_path), session="one",
        backend="codex")
    second_id = agents_db.create_agent(
        persona="Two", voice_id="v", cwd=str(tmp_path), session="two",
        backend="codex")
    on_a, err_a = capture("a")
    on_b, err_b = capture("b")
    handle_a = _spawn_fake_turn(
        tmp_path, agent_id=first_id, session="one", text="alpha",
        on_result=on_a, on_error=err_a)
    handle_b = _spawn_fake_turn(
        tmp_path, agent_id=second_id, session="two", text="beta",
        on_result=on_b, on_error=err_b)
    assert handle_a.pid == handle_b.pid
    assert len(codex_app_server._CLIENTS) == 1
    assert handle_a.wait(timeout=8) == 0
    assert handle_b.wait(timeout=8) == 0
    assert done.wait(5)
    assert errors == {}
    assert "alpha" in (results["a"].get("last_agent_message") or "")
    assert "beta" in (results["b"].get("last_agent_message") or "")
    codex_app_server.recycle_clients()


def test_recycle_releases_writer_so_resume_works(tmp_path, monkeypatch):
    _install_fake_codex(tmp_path, monkeypatch)
    agent_id = agents_db.create_agent(
        persona="Gordon", voice_id="v", cwd=str(tmp_path), session="gordon",
        backend="codex")
    bound = {}

    def on_session_init(thread_id):
        bound["thread"] = thread_id
        return True

    first = codex_app_server.spawn_turn(
        text="hello", cwd=tmp_path, is_new_session=True, session="gordon",
        agent_id=agent_id, on_session_init=on_session_init,
        stream=_NullStream(), enqueue=lambda **_kwargs: 0)
    first.wait(timeout=8)
    thread_id = bound["thread"]
    assert thread_id
    leftover_pid = first.pid
    assert codex_app_server.recycle_clients() == 1
    resumed = []
    second = codex_app_server.spawn_turn(
        text="again", cwd=tmp_path, backend_session_id=thread_id,
        is_new_session=False, session="gordon", agent_id=agent_id,
        on_result=lambda payload: resumed.append(payload),
        stream=_NullStream(), enqueue=lambda **_kwargs: 0)
    second.wait(timeout=8)
    deadline = time.time() + 5
    while time.time() < deadline and not resumed:
        time.sleep(0.05)
    assert second.pid != leftover_pid
    assert resumed and "again" in (resumed[0].get("last_agent_message") or "")
    codex_app_server.recycle_clients()


def test_external_leftover_writer_still_blocks_shared_client(tmp_path, monkeypatch):
    home = _install_fake_codex(tmp_path, monkeypatch)
    leftover = subprocess.Popen(
        [sys.executable, str(FAKE_CODEX), "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        env={**os.environ, "CODEX_HOME": str(home),
             "CLARP_QA_PROVIDER_ROOT": str(home)},
    )
    try:
        leftover.stdin.write(json.dumps({
            "method": "initialize", "id": 1,
            "params": {"clientInfo": {"name": "ghostty"}},
        }) + "\n")
        leftover.stdin.flush()
        leftover.stdout.readline()
        leftover.stdin.write(json.dumps({
            "method": "thread/start", "id": 2,
            "params": {"cwd": str(tmp_path)},
        }) + "\n")
        leftover.stdin.flush()
        started = json.loads(leftover.stdout.readline())
        thread_id = started["result"]["thread"]["id"]
        agent_id = agents_db.create_agent(
            persona="Ext", voice_id="v", cwd=str(tmp_path), session="ext",
            backend="codex")
        try:
            _spawn_fake_turn(
                tmp_path, agent_id=agent_id, session="ext", text="blocked",
                backend_session_id=thread_id, is_new_session=False)
            raise AssertionError("shared client resumed a thread another process holds")
        except RuntimeError as exc:
            assert "already has an active writer" in str(exc)
    finally:
        if leftover.stdin:
            leftover.stdin.close()
        leftover.kill()
        leftover.wait(timeout=3)
        codex_app_server.recycle_clients()


def test_credentials_change_retires_idle_connection(tmp_path, monkeypatch):
    home = _install_fake_codex(tmp_path, monkeypatch)
    auth = home / 'auth.json'
    auth.write_text('old fixture credentials')
    first = codex_app_server._client('one', 'one')
    auth.write_text('new fixture credentials')
    second = codex_app_server._client('one', 'one')
    assert first.proc.poll() is not None
    assert second.proc.pid != first.proc.pid
    codex_app_server.recycle_clients()


def test_credentials_change_and_recycle_preserve_active_turn(tmp_path, monkeypatch):
    home = _install_fake_codex(tmp_path, monkeypatch)
    auth = home / 'auth.json'
    auth.write_text('old')
    agent = agents_db.create_agent(persona='Busy', voice_id='v', cwd=str(tmp_path), session='busy', backend='codex')
    handle = _spawn_fake_turn(tmp_path, agent_id=agent, session='busy', text='[qa-slow]')
    client = handle.client
    auth.write_text('new')
    assert codex_app_server.recycle_clients() == 0
    assert codex_app_server._client(agent, 'busy') is client
    assert client.proc.poll() is None
    handle.wait(8)
    fresh = codex_app_server._client(agent, 'busy')
    assert fresh is not client
    assert client.proc.poll() is not None
    codex_app_server.recycle_clients()


def test_unknown_quota_bucket_is_not_regular_quota():
    old = {'rateLimits': {'limitId': 'premium'}}
    regular = {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 100}}}}
    assert not codex_app_server._matching_bucket_blocked(old, regular)
    matching = {'rateLimitsByLimitId': {'premium': {'primary': {'usedPercent': 100}}}}
    assert codex_app_server._matching_bucket_blocked(old, matching)


def test_failed_other_agent_cannot_refresh_busy_shared_connection(tmp_path, monkeypatch):
    _install_fake_codex(tmp_path, monkeypatch)
    agent = agents_db.create_agent(persona='Busy', voice_id='v', cwd=str(tmp_path), session='busy', backend='codex')
    handle = _spawn_fake_turn(tmp_path, agent_id=agent, session='busy', text='[qa-slow]')
    failure = codex_app_server.CodexTurnFailure('usage limit', handle.client, False)
    assert not codex_app_server.recover_usage_failure(failure)
    assert handle.client.proc.poll() is None
    handle.wait(8)
    codex_app_server.recycle_clients()
