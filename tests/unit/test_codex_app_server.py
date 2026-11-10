from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import agents as agents_db
from lib import codex_app_server
from lib.backend.codex import TurnState as _TurnState
from lib.backend.registry import by_id


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

    # This pure factory double has no process or actors. Restore its private
    # namespace before the independent shared-sink foreign-client gate runs.
    with monkeypatch.context() as factory:
        factory.setattr(codex_app_server, "_Client", FakeClient)
        # Its own registry, restored after the test: a FakeClient left in the shared one
        # (it has no `active`) broke later tests' active_handles() in the same worker.
        factory.setattr(codex_app_server, "_CLIENTS", {})
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
    first._lock = threading.Lock()      # as _Client.__init__ sets it
    second = object.__new__(codex_app_server._Client)
    second.active = None
    second.proc = _FakeProc()
    second._lock = threading.Lock()
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
    only._lock = threading.Lock()       # as _Client.__init__ sets it
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


def _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(home))
    monkeypatch.setattr(by_id("codex"), "required_binary", str(FAKE_CODEX))
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


def test_two_agents_share_one_fake_appserver(tmp_path, monkeypatch, codex_pool_lifecycle):
    _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
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


def test_recycle_releases_writer_so_resume_works(tmp_path, monkeypatch, codex_pool_lifecycle):
    _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
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


def test_external_leftover_writer_still_blocks_shared_client(tmp_path, monkeypatch, codex_pool_lifecycle):
    home = _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
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


def test_credentials_change_retires_idle_connection(tmp_path, monkeypatch, codex_pool_lifecycle):
    home = _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
    auth = home / 'auth.json'
    auth.write_text('old fixture credentials')
    first = codex_app_server._client('one', 'one')
    auth.write_text('new fixture credentials')
    second = codex_app_server._client('one', 'one')
    assert first.proc.poll() is not None
    assert second.proc.pid != first.proc.pid
    codex_app_server.recycle_clients()


def test_credentials_change_and_recycle_preserve_active_turn(tmp_path, monkeypatch, codex_pool_lifecycle):
    home = _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
    auth = home / 'auth.json'
    auth.write_text('old')
    agent = agents_db.create_agent(persona='Busy', voice_id='v', cwd=str(tmp_path), session='busy', backend='codex')
    handle = _spawn_fake_turn(tmp_path, agent_id=agent, session='busy', text='[qa-slow]')
    client = handle.client
    auth.write_text('new')
    assert codex_app_server.recycle_clients() == 0
    # BACKENDS-06b: the next turn gets a new generation on the new
    # credentials; the busy turn keeps its process until it ends.
    fresh = codex_app_server._client(agent, 'busy')
    assert fresh is not client
    assert client.proc.poll() is None
    handle.wait(8)
    for _ in range(200):
        if client.proc.poll() is not None:
            break
        threading.Event().wait(0.05)
    assert client.proc.poll() is not None, 'the drained generation did not exit'
    assert codex_app_server._client(agent, 'busy') is fresh
    codex_app_server.recycle_clients()


def test_unknown_quota_bucket_is_not_regular_quota():
    old = {'rateLimits': {'limitId': 'premium'}}
    regular = {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 100}}}}
    assert not codex_app_server._matching_bucket_blocked(old, regular)
    matching = {'rateLimitsByLimitId': {'premium': {'primary': {'usedPercent': 100}}}}
    assert codex_app_server._matching_bucket_blocked(old, matching)


def test_failed_other_agent_cannot_refresh_busy_shared_connection(tmp_path, monkeypatch, codex_pool_lifecycle):
    _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
    agent = agents_db.create_agent(persona='Busy', voice_id='v', cwd=str(tmp_path), session='busy', backend='codex')
    handle = _spawn_fake_turn(tmp_path, agent_id=agent, session='busy', text='[qa-slow]')
    failure = codex_app_server.CodexTurnFailure('usage limit', handle.client, False)
    assert not codex_app_server.recover_usage_failure(failure)
    assert handle.client.proc.poll() is None
    handle.wait(8)
    codex_app_server.recycle_clients()


def test_foreign_subagent_answer_cannot_complete_current_primary_turn():
    agent_id=agents_db.create_agent(persona='Primary',voice_id='v',cwd='/tmp',session='primary',backend='codex')
    agents_db.open_turn(agent_id=agent_id,source='pwa',trace_id='primary-trace')
    client=object.__new__(codex_app_server._Client);client.agent_id=agent_id
    active=codex_app_server._ActiveTurn('primary-turn','primary-thread',agent_id,'primary','primary-trace',
        _TurnState(live_backend_session_id='primary-thread'),_Handle(),None,None,None,lambda **kw:0)
    client.active=active;client._actives={agent_id:active}
    client._notification('item/agentMessage/delta',{'threadId':'report-subagent-thread','turnId':'report-turn',
        'delta':'Updated report.html with482 links.'})
    client._notification('turn/completed',{'threadId':'report-subagent-thread',
        'turn':{'id':'report-turn','status':'completed'}})
    assert client.active is active and not active.handle._done.is_set()
    assert active.state.last_agent_message==''
    assert not agents_db.conn().execute('SELECT 1 FROM messages WHERE agent_id=?',(agent_id,)).fetchone()


def test_stale_explicit_turn_event_cannot_replace_current_turn_output():
    client=object.__new__(codex_app_server._Client);client.agent_id='primary'
    active=codex_app_server._ActiveTurn('current-turn','primary-thread','primary','primary','trace',
        _TurnState(),_Handle(),None,None,None,lambda **kw:0)
    client.active=active;client._actives={'primary':active}
    client._notification('item/agentMessage/delta',{'threadId':'primary-thread','turnId':'old-turn','delta':'old result'})
    client._notification('turn/completed',{'threadId':'primary-thread','turn':{'id':'old-turn','status':'completed'}})
    assert client.active is active and not active.handle._done.is_set()
    assert active.state.last_agent_message==''


class _LinesProc(_FakeProc):
    """A live app-server whose stdout yields the given lines, then ends."""

    def __init__(self, lines):
        super().__init__()
        self.stdout = iter(lines)
        self.stdin = None


def _reader_client(lines, agent_id="agent-reader"):
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = None
    client._actives = {}
    client._lock = threading.Lock()
    client._write_lock = threading.Lock()
    client._pending = {}
    client.proc = _LinesProc(lines)
    return client


def test_one_failing_notification_does_not_end_the_shared_reader(monkeypatch):
    # A database-locked error while handling one notification, or a JSON line
    # that is not an object, must not fail the other requests on the process.
    done = threading.Event()
    box = {}
    client = _reader_client([
        json.dumps({"method": "item/agentMessage/delta", "params": {"delta": "x"}}) + "\n",
        "5\n",
        json.dumps({"id": 7, "result": {"ok": True}}) + "\n",
    ])
    client._pending[7] = (done, box, None)

    def locked(_method, _params):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(client, "_notification", locked)
    client._read()

    assert done.is_set()
    assert box.get("result") == {"ok": True}
    assert "error" not in box


def test_a_live_app_server_whose_reader_ended_is_replaced(monkeypatch):
    def broken_stdout():
        yield json.dumps({"method": "noop", "params": {}}) + "\n"
        raise ValueError("stdout decode failed")

    client = _reader_client([])
    client.proc.stdout = broken_stdout()
    client.auth_fingerprint = codex_app_server._auth_fingerprint()
    client.refresh_requested = False
    monkeypatch.setattr(client, "_notification", lambda *_a: None)
    client._read()
    assert client.proc.poll() is None, "the process itself is still running"

    created = []

    class FakeClient:
        def __init__(self, agent_id, session, stream=None):
            self.proc = _FakeProc()
            created.append(self)

    monkeypatch.setattr(codex_app_server, "_Client", FakeClient)
    codex_app_server._CLIENTS.clear()
    codex_app_server._CLIENTS[codex_app_server._SHARED_KEY] = client
    try:
        fresh = codex_app_server._client("agent-reader", "reader")
        assert created == [fresh]
        assert fresh is not client
        assert client.proc.signals, "the orphaned process is stopped"
    finally:
        codex_app_server._CLIENTS.clear()


def _client_with_turn(agent_id="agent-turn"):
    from unittest.mock import Mock

    client = _reader_client([], agent_id=agent_id)
    handle = codex_app_server.AppTurnHandle(client, agent_id)
    active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id=agent_id,
        session="turn", trace_id="trace-1",
        state=_TurnState(live_backend_session_id="thread-1"), handle=handle,
        on_result=Mock(), on_error=Mock(), stream=None,
        enqueue=lambda **_kwargs: 0)
    client.active = active
    client._actives = {agent_id: active}
    return client, active


def test_a_failed_final_flush_still_settles_the_turn(monkeypatch):
    # The final live-text flush hits a locked database: the turn must still
    # end with its outcome while the shared reader keeps running.
    from unittest.mock import Mock

    client, active = _client_with_turn()
    monkeypatch.setattr(client, "_callback", lambda callback, value: callback(value))
    monkeypatch.setattr(by_id("codex"), "update_live_text",
                        Mock(side_effect=RuntimeError("database is locked")))
    seen = []

    def lines():
        yield json.dumps({"method": "turn/completed", "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed"}}}) + "\n"
        # Checked before EOF, whose _fail_all would hide a stranded turn.
        seen.append((active.handle.is_alive(), active.on_result.call_count))
        yield ""

    client.proc.stdout = lines()
    client._read()

    assert seen == [(False, 1)]
    active.on_error.assert_not_called()


def test_retiring_a_dead_reader_client_sends_no_request(monkeypatch):
    # Nothing reads the old process, so any request would wait out its
    # timeout while the pool lock is held.
    client, active = _client_with_turn("agent-retire")
    client.reader_dead = True
    client.auth_fingerprint = codex_app_server._auth_fingerprint()
    client.refresh_requested = False
    client.proc.stdin = None
    requests = []
    monkeypatch.setattr(client, "request",
                        lambda method, *_a, **_k: requests.append(method) or {})
    monkeypatch.setattr(client, "_callback", lambda callback, value: callback(value))

    class FakeClient:
        def __init__(self, agent_id, session, stream=None):
            self.proc = _FakeProc()

    monkeypatch.setattr(codex_app_server, "_Client", FakeClient)
    codex_app_server._CLIENTS.clear()
    codex_app_server._CLIENTS[codex_app_server._SHARED_KEY] = client
    try:
        fresh = codex_app_server._client("agent-retire", "retire")
        assert isinstance(fresh, FakeClient)
        assert requests == []
        assert client.proc.signals, "the old process is stopped"
        assert not active.handle.is_alive()
        active.on_error.assert_called_once()
    finally:
        codex_app_server._CLIENTS.clear()


def test_a_client_is_not_reused_while_its_reader_is_cleaning_up(monkeypatch):
    # Admissions overlap the ended reader's cleanup: the pool must already
    # treat the client as dead, and a request on it must fail at once
    # instead of waiting out its timeout under the pool lock.
    client, active = _client_with_turn("agent-cleanup")
    client.proc.stdin = None
    client.auth_fingerprint = codex_app_server._auth_fingerprint()
    client.refresh_requested = False
    client._next_id = 1
    monkeypatch.setattr(client, "_callback", lambda callback, value: callback(value))
    monkeypatch.setattr(client, "_send", lambda _message: None)
    entered, release = threading.Event(), threading.Event()

    class SlowWaiter(dict):
        def __setitem__(self, key, value):
            if key == "error":  # _fail_all has taken its pending snapshot
                entered.set()
                release.wait(3)
            super().__setitem__(key, value)

    client._pending[0] = (threading.Event(), SlowWaiter(), None)

    class FakeClient:
        def __init__(self, agent_id, session, stream=None):
            self.proc = _FakeProc()

    monkeypatch.setattr(codex_app_server, "_Client", FakeClient)
    codex_app_server._CLIENTS.clear()
    codex_app_server._CLIENTS[codex_app_server._SHARED_KEY] = client
    reader = threading.Thread(target=client._read)
    reader.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        try:
            client.request("thread/resume", {"threadId": "thread-1"}, timeout=2)
        except TimeoutError:
            pytest.fail("request on a client nobody reads waited out its timeout")
        except RuntimeError:
            pass
        assert time.monotonic() - started < 1
        fresh = codex_app_server._client("agent-cleanup", "cleanup")
        assert isinstance(fresh, FakeClient)
    finally:
        release.set()
        reader.join(3)
        codex_app_server._CLIENTS.clear()
    assert not reader.is_alive()
    assert active.on_error.call_count == 1, "one terminal outcome per turn"


def test_the_shared_app_server_speaks_for_no_agent(tmp_path, monkeypatch, codex_pool_lifecycle):
    """BACKENDS-N1: one app-server process serves every Codex agent, so its
    environment must not name the agent that happened to start it. Tools in
    any Codex agent's shell that trust CLAUDE_PWA_SESSION would act as that
    agent; without it they refuse instead."""
    _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
    monkeypatch.setenv("CLAUDE_PWA_SESSION", "the-host-itself")
    seen = []
    real_popen = codex_app_server.subprocess.Popen

    def recording_popen(*args, **kwargs):
        seen.append(dict(kwargs.get("env") or {}))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(codex_app_server.subprocess, "Popen", recording_popen)
    try:
        codex_app_server._client("agent-one", "first-agent")
    finally:
        codex_app_server.recycle_clients()
    assert seen and all("CLAUDE_PWA_SESSION" not in env for env in seen)
    assert all("CLARP_PROVIDER_TURN" not in env for env in seen)


def test_each_codex_thread_carries_its_own_agent_session(tmp_path, monkeypatch, codex_pool_lifecycle):
    """BACKENDS-N1: the shared app-server has no agent identity; each thread
    gets its own agent's session for its shell through the thread's config
    (shell_environment_policy.set), when it starts and when it resumes."""
    _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
    sent = []
    real_request = codex_app_server._Client.request

    def recording(self, method, params, timeout=30, **kwargs):
        result = real_request(self, method, params, timeout=timeout, **kwargs)
        sent.append((method, dict(params), result))
        return result

    monkeypatch.setattr(codex_app_server._Client, "request", recording)
    agent_ids = {session: agents_db.create_agent(
        persona=session.title(), voice_id="v", cwd=str(tmp_path), session=session,
        backend="codex") for session in ("one", "two")}
    threads = {}
    try:
        for session, agent_id in agent_ids.items():
            handle = _spawn_fake_turn(tmp_path, agent_id=agent_id, session=session, text=session)
            assert handle.wait(timeout=8) == 0
            threads[session] = sent[-1][1].get("threadId") or [
                result.get("thread", {}).get("id") for method, _p, result in sent
                if method == "thread/start"][-1]
        for suffix in (" again", " once more"):
            for session, agent_id in agent_ids.items():
                handle = _spawn_fake_turn(tmp_path, agent_id=agent_id, session=session,
                                          text=session + suffix,
                                          backend_session_id=threads[session],
                                          is_new_session=False)
                assert handle.wait(timeout=8) == 0
    finally:
        codex_app_server.recycle_clients()

    def identity(params):
        return (params.get("config", {}).get("shell_environment_policy", {})
                .get("set", {}).get("CLAUDE_PWA_SESSION"))

    assert [(method, identity(params)) for method, params, _r in sent
            if method in ("thread/start", "thread/resume")] == [
        ("thread/start", "one"), ("thread/start", "two"),
        ("thread/resume", "one"), ("thread/resume", "two"),
        ("thread/resume", "one"), ("thread/resume", "two")]
    resumes = [(params, result) for method, params, result in sent
               if method == "thread/resume"]
    assert len(resumes) == 4
    assert all(params.get("excludeTurns") is True for params, _result in resumes)
    # This real fake-server transport returns only the thread ID: the reader,
    # binding and next-turn/callback path need no hydrated history.
    assert all("turns" not in result["thread"] for _params, result in resumes)
    assert all("excludeTurns" not in params for method, params, _result in sent
               if method == "thread/start")


# These controls exercise the fixture boundary, not a replacement pool model.
# Negative cleanup cases use a private namespace/gate so their deliberately
# FAILED owner remains quarantined without poisoning pytest's shared owner.
def _private_pool_owner(*, timeout=1.0, mp=None):
    from types import SimpleNamespace
    from tests.codex_pool_lifecycle import OwnerGate, PoolOwner
    namespace = SimpleNamespace(
        _Client=codex_app_server._Client, threading=threading,
        _LOCK=threading.RLock(), _CLIENTS={}, _DRAINING={}, _RETIRING={})
    mp = mp if mp is not None else pytest.MonkeyPatch()
    gate = OwnerGate()
    return PoolOwner(namespace, mp, gate=gate, timeout=timeout), namespace, mp, gate


def test_fake_pool_close_joins_running_backstop_and_terminal_callback(
        tmp_path, monkeypatch, codex_pool_lifecycle):
    from lib import eventlog
    from tests.codex_pool_lifecycle import QuiescenceError
    _install_fake_codex(tmp_path, monkeypatch, codex_pool_lifecycle)
    owner = codex_pool_lifecycle
    client = codex_app_server._client("fixture-owner", "fixture-owner")
    # A foreign native turn legitimately keeps the real drain registration
    # alive, so the actual backstop body must attempt its next timer.
    client.foreign_turns.add("held-child")
    entered, release, retiring = threading.Event(), threading.Event(), threading.Event()
    callback_entered, callback_release = threading.Event(), threading.Event()
    close_done = threading.Event()
    callbacks, failures = [], []
    real_exit, real_retire = client._exit_if_drained, client.retire

    def blocked_exit(*, backstop=False):
        if backstop:
            entered.set()
            assert release.wait(5)
        real_exit(backstop=backstop)

    def terminal(_value):
        callback_entered.set()
        assert callback_release.wait(5)
        callbacks.append("terminal")
        codex_app_server.log("codexFixtureTerminal", "owned callback finished")

    def retire():
        retiring.set()  # close is already CLOSING before this worker runs
        real_retire()
        client._callback(terminal, None)

    monkeypatch.setattr(client, "_exit_if_drained", blocked_exit)
    monkeypatch.setattr(client, "retire", retire)
    monkeypatch.setattr(codex_app_server, "DRAIN_BACKSTOP_SEC", 0)

    def close():
        try:
            owner.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            close_done.set()

    closer = threading.Thread(target=close)
    try:
        with codex_app_server._LOCK:
            codex_app_server._begin_drain(client)
        assert entered.wait(5)
        closer.start()
        assert retiring.wait(5) and callback_entered.wait(5)
        assert not close_done.is_set(), "running backstop/callback must hold cleanup"
        release.set()
        callback_release.set()
        closer.join(10)
        assert close_done.is_set() and not failures
        assert callbacks == ["terminal"]
        assert not client.reader.is_alive() and not client.stderr_reader.is_alive()
        timers = [actor for actor in owner.actors if isinstance(actor, threading.Timer)]
        assert len(timers) >= 2, "the actual backstop rearm was not exercised"
        assert all(not actor.is_alive() for actor in owner.actors)
        assert id(client) not in codex_app_server._DRAINING
        # Make a later sink eligible only AFTER proven close. Even a retained
        # direct timer body invocation cannot write there.
        later = tmp_path / "later-logs"
        eventlog.reset_for_tests(later)
        before = {path.name: path.read_bytes() for path in later.glob("*") if path.is_file()}
        with pytest.raises(QuiescenceError, match="body after"):
            timers[0].run()
        with pytest.raises(QuiescenceError, match="body after"):
            timers[0].function()
        with pytest.raises(QuiescenceError, match="body after"):
            client._callback(terminal, None)
        after = {path.name: path.read_bytes() for path in later.glob("*") if path.is_file()}
        assert after == before and callbacks == ["terminal"]
    finally:
        release.set()
        callback_release.set()
        if closer.ident is not None:
            closer.join(10)


def test_preconstructed_pool_actors_refuse_start_and_body_after_undo():
    from tests.codex_pool_lifecycle import QuiescenceError
    owner, namespace, mp, gate = _private_pool_owner()
    calls = []
    client = namespace._Client.__new__(namespace._Client)
    deferred_dispatch = client._callback
    with owner.producing():
        timer = namespace.threading.Timer(60, lambda: calls.append("timer"))
        thread = namespace.threading.Thread(target=lambda: calls.append("thread"))
    owner.close()
    owner.close()  # only positive CLOSED is idempotent
    mp.undo()
    assert namespace.threading is threading
    for actor in (timer, thread):
        with pytest.raises(QuiescenceError, match="start after"):
            actor.start()
        with pytest.raises(QuiescenceError, match="body after"):
            actor.run()
    with pytest.raises(QuiescenceError, match="body after"):
        deferred_dispatch(lambda _v: calls.append("late callback"), None)
    assert calls == []
    gate.check(namespace)


def test_fake_pool_owner_preserves_foreign_replacement_and_refuses_live_foreign():
    from tests.codex_pool_lifecycle import PoolOwner, QuiescenceError
    owner, namespace, mp, gate = _private_pool_owner()
    # Explicit __new__ path registers identity before initialization/actors.
    owned = namespace._Client.__new__(namespace._Client)
    owned.proc = _FakeProc()
    owned.active, owned._actives, owned._pending = None, {}, {}
    owned._lock = threading.Lock()
    foreign = object.__new__(codex_app_server._Client)
    foreign.proc = _FakeProc()
    namespace._DRAINING[id(owned)] = foreign  # same key, different identity
    owner.close()
    assert owned.proc.poll() == 0
    assert namespace._DRAINING[id(owned)] is foreign
    assert foreign.proc.signals == []
    with pytest.raises(QuiescenceError, match="unowned"):
        gate.check(namespace)
    fresh_mp = pytest.MonkeyPatch()
    with pytest.raises(QuiescenceError, match="unowned"):
        PoolOwner(namespace, fresh_mp, gate=gate)
    assert vars(fresh_mp).get("undo") is None, "refuse before patch/actor setup"
    # Foreign bound targets remain foreign even under owned producer context.
    with owner.producing():
        actor = namespace.threading.Thread(target=foreign.shutdown)
    assert type(actor) is threading.Thread
    assert foreign.proc.signals == []
    mp.undo()


def test_partially_started_pool_actor_stays_quarantined():
    from tests.codex_pool_lifecycle import QuiescenceError
    owner, namespace, mp, gate = _private_pool_owner()
    entered, release = threading.Event(), threading.Event()
    with owner.producing():
        actor = namespace.threading.Thread(target=lambda: (entered.set(), release.wait(5)))

    def uncertain_start():
        threading.Thread.start(actor)
        raise RuntimeError("start raised after native launch")

    try:
        with pytest.raises(RuntimeError, match="after native launch"):
            owner.start(actor, uncertain_start)
        assert entered.wait(5)
        release.set()
        actor.join(5)
        with pytest.raises(QuiescenceError, match="partially started"):
            owner.close()
        assert owner in gate.owners and owner.state == "FAILED"
        with pytest.raises(QuiescenceError):
            mp.undo()
        with pytest.raises(QuiescenceError):
            owner.close()  # must not erase the earlier uncertainty
    finally:
        release.set()
        actor.join(5)
    # FAILED owner and its untouched patch stack remain strongly held by gate.
    assert owner in gate.owners and mp._setattr


def test_pool_cleanup_deadline_blocks_reset_next_setup_and_exact_undo(tmp_path, monkeypatch):
    from tests import conftest
    from tests import codex_pool_lifecycle as lifecycle
    from lib import db, eventlog, revisioned_cache
    owner, namespace, mp, gate = _private_pool_owner(timeout=0.2)
    paths = {"DB": "old-test-db", "LOG": "old-test-log"}
    mp.setitem(paths, "DB", "owned-test-db")
    mp.setitem(paths, "LOG", "owned-test-log")
    release, retiring = threading.Event(), threading.Event()
    entered = [threading.Event() for _ in range(4)]
    with owner.producing():
        actors = [namespace.threading.Thread(
            target=lambda event=event: (event.set(), release.wait(5))) for event in entered]
    for actor in actors:
        actor.start()
    assert all(event.wait(5) for event in entered)
    client = namespace._Client.__new__(namespace._Client)
    client.proc = _FakeProc()
    # A blocking stop is part of the ONE budget, not an unbounded preliminary
    # shutdown followed by a separate join allowance per actor.
    client.retire = lambda: (retiring.set(), release.wait(5), client.proc.terminate())
    # Drive the actual DB fixture generator only through its setup, against
    # this test's existing paths. Its teardown must independently fence reset.
    db_fixture = conftest._isolated_db.__wrapped__(None, tmp_path, monkeypatch)
    next(db_fixture)
    resets = []
    try:
        started = time.monotonic()
        with pytest.raises(lifecycle.QuiescenceError, match="deadline"):
            owner.close()
        assert time.monotonic() - started < 1.0
        assert retiring.is_set()
        assert owner.state == "FAILED" and owner in gate.owners
        stacks = (list(mp._setattr), list(mp._setitem))
        with pytest.raises(lifecycle.QuiescenceError, match="undo"):
            mp.undo()
        assert (mp._setattr, mp._setitem) == stacks
        assert paths == {"DB": "owned-test-db", "LOG": "owned-test-log"}
        # This context changes ONLY negative-control gate/reset sentinels,
        # never an actor's CLI/config dependencies. Release/join before unwind.
        with monkeypatch.context() as control:
            control.setattr(lifecycle, "GATE", gate)
            control.setattr(db, "reset_for_tests", lambda *_a: resets.append("db"))
            control.setattr(eventlog, "reset_for_tests", lambda *_a: resets.append("log"))
            control.setattr(revisioned_cache, "reset_all", lambda: resets.append("cache"))
            try:
                with pytest.raises(lifecycle.QuiescenceError):
                    next(db_fixture)
                with pytest.raises(lifecycle.QuiescenceError):
                    conftest._transcript_importer_quiescent.__wrapped__()
                assert resets == []
            finally:
                release.set()
                for actor in owner.actors:
                    if actor.ident is not None:
                        actor.join(5)
        assert all(not actor.is_alive() for actor in owner.actors)
        assert owner.state == "FAILED" and owner in gate.owners
        with pytest.raises(lifecycle.QuiescenceError):
            mp.undo()  # completion is not an implicit failed-owner release
    finally:
        release.set()
        for actor in owner.actors:
            if actor.ident is not None:
                actor.join(5)


def test_healthy_pool_undo_delegates_once_and_preserves_native_errors():
    calls = []

    class RecordingPatch(pytest.MonkeyPatch):
        def undo(self):
            calls.append("undo")
            return super().undo()

    owner, namespace, mp, gate = _private_pool_owner(mp=RecordingPatch())
    owner.close()
    mp.undo()
    assert calls == ["undo"] and namespace.threading is threading
    gate.check(namespace)

    class FailingPatch(pytest.MonkeyPatch):
        def undo(self):
            raise ValueError("native undo failed")

    owner, namespace, mp, gate = _private_pool_owner(mp=FailingPatch())
    owner.close()
    with pytest.raises(ValueError, match="native undo failed"):
        mp.undo()
    assert owner.state == "CLOSED"
