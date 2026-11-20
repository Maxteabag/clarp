"""R148: the user's compaction says what it did, from numbers, never from a clock.

Codex is compacted in the Host's app-server (the process that holds the
thread); Claude Code by its interactive /compact, proven by its own
compact_boundary record. Stand-ins only: no CLI, no provider.
"""
from __future__ import annotations

import ast
import json
import pathlib
import threading

import pytest

from lib import agents as agents_db
from lib import claude_transcript, codex_app_server, codex_transcript, compaction
from lib import turn_dispatch as td
from lib.backend.codex import TurnState

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"


# ---- Codex: the app-server's own compaction ---------------------------------

class _Handle:
    def __init__(self):
        self._done = threading.Event()


def _usage_params(thread, total, *, input_tokens=None):
    last = {"inputTokens": total if input_tokens is None else input_tokens,
            "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0,
            "totalTokens": total}
    return {"threadId": thread, "turnId": "c-turn",
            "tokenUsage": {"last": last, "total": last}}


def _client(monkeypatch, script):
    """A bare app-server client whose requests ``script`` answers; it may
    deliver notifications through the client, as the reader would."""
    client = object.__new__(codex_app_server._Client)
    client.agent_id = "agent-c"
    client.active = None
    client._actives = {}
    client._loaded_threads = {"thread-c"}
    client._lock = threading.Lock()
    client.requests = []

    def request(method, params, timeout=30, **_kw):
        client.requests.append(method)
        return script(client, method, params)

    client.request = request
    monkeypatch.setattr(client, "_adopt_server_turn",
                        lambda *_a: pytest.fail("a compaction must not be adopted as a turn"),
                        raising=False)
    monkeypatch.setattr(codex_app_server._backend(), "handle_item",
                        lambda *_a, **_k: pytest.fail("a compaction writes nothing to the chat"))
    return client


_AGENT = {"agent_id": "agent-c", "session": "cee", "cwd": "/tmp", "persona": ""}


def _compacting_server(status="completed", replace=True):
    def script(client, method, params):
        if method == "thread/compact/start":
            n = client._notification
            n("turn/started", {"threadId": "thread-c", "turn": {"id": "c-turn"}})
            # The summary call's own usage, then Codex's estimate of the new history.
            n("thread/tokenUsage/updated", _usage_params("thread-c", 410_000))
            if replace:
                n("thread/tokenUsage/updated", _usage_params("thread-c", 31_000, input_tokens=0))
                n("item/completed", {"threadId": "thread-c", "turnId": "c-turn",
                                     "item": {"type": "contextCompaction", "id": "i1"}})
            n("turn/completed", {"threadId": "thread-c",
                                 "turn": {"id": "c-turn", "status": status,
                                          **({"error": {"message": "boom"}} if status == "failed" else {})}})
        return {}
    return script


def test_codex_compaction_runs_in_the_app_server_and_reports_both_sizes(monkeypatch):
    client = _client(monkeypatch, _compacting_server())
    codex_app_server._remember_context_size(_usage_params("thread-c", 402_000))
    before = codex_app_server.last_context_size("thread-c")
    answer = client.compact(_AGENT, "thread-c", timeout=5)
    assert client.requests == ["thread/compact/start"]
    assert answer == {"status": "completed", "replaced": True, "after": 31_000, "error": ""}
    assert client._actives == {}                      # the slot is given back
    result = compaction.decide_codex(before, answer)
    assert result["outcome"] == compaction.COMPACTED
    assert (result["before_tokens"], result["after_tokens"]) == (402_000, 31_000)
    assert result["after_is_estimate"] is True
    assert codex_app_server.last_context_size("thread-c") == 31_000


def test_codex_without_a_replaced_history_is_not_compacted(monkeypatch):
    client = _client(monkeypatch, _compacting_server(status="failed", replace=False))
    answer = client.compact(_AGENT, "thread-c", timeout=5)
    result = compaction.decide_codex(402_000, answer)
    assert result["outcome"] == compaction.NOT_COMPACTED
    assert "boom" in result["detail"]


def test_codex_refusal_is_said_in_words(monkeypatch):
    def script(_client, method, _params):
        raise RuntimeError("thread/compact/start: {'code': -32601, 'message': 'Method not found'}")
    client = _client(monkeypatch, script)
    answer = client.compact(_AGENT, "thread-c", timeout=5)
    assert answer["status"] == "refused" and "Method not found" in answer["error"]
    assert client._actives == {}
    result = compaction.decide_codex(400_000, answer)
    assert result["outcome"] == compaction.NOT_COMPACTED
    assert "did not compact the thread" in result["detail"]


def test_codex_that_never_finishes_is_unknown_not_done(monkeypatch):
    interrupted = []

    def script(client, method, _params):
        if method == "thread/compact/start":
            client._notification("turn/started", {"threadId": "thread-c", "turn": {"id": "c-turn"}})
        return {}
    client = _client(monkeypatch, script)
    monkeypatch.setattr(codex_app_server, "STOP_DETACH_SEC", 3600.0)
    monkeypatch.setattr(client, "interrupt_active",
                        lambda agent_id, handle=None, by_user=False: interrupted.append(agent_id),
                        raising=False)
    answer = client.compact(_AGENT, "thread-c", timeout=0.05)
    assert answer["status"] == "timeout" and interrupted == ["agent-c"]
    # Until Codex ends it the agent stays busy: no turn starts on top of it.
    assert client._actives["agent-c"].handle.is_alive()
    assert compaction.decide_codex(400_000, answer)["outcome"] == compaction.UNKNOWN


def test_a_send_is_never_steered_into_a_compaction(monkeypatch):
    held = threading.Event()

    def script(client, method, _params):
        if method == "thread/compact/start":
            client._notification("turn/started", {"threadId": "thread-c", "turn": {"id": "c-turn"}})
            assert client.steer("hello", agent_id="agent-c") is False
            held.set()
            client._notification("turn/completed", {"threadId": "thread-c",
                                                    "turn": {"id": "c-turn", "status": "interrupted"}})
        return {}
    client = _client(monkeypatch, script)
    answer = client.compact(_AGENT, "thread-c", timeout=5)
    assert held.is_set() and "turn/steer" not in client.requests
    assert compaction.decide_codex(400_000, answer)["outcome"] == compaction.NOT_COMPACTED


def test_codex_size_before_falls_back_to_the_rollout_tail_only(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    record = {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 9_999_999},
        "last_token_usage": {"input_tokens": 300_000, "cached_input_tokens": 0,
                             "output_tokens": 500, "reasoning_output_tokens": 0,
                             "total_tokens": 300_500}}}}
    rollout.write_text(json.dumps(record) + "\n" + '{"type":"response_item"}\n' * 10)
    assert codex_transcript.last_context_size(rollout) == 300_500
    far = tmp_path / "far.jsonl"
    far.write_text(json.dumps(record) + "\n" + ("x" * 200 + "\n") * 400)   # past 64 KB
    assert codex_transcript.last_context_size(far) is None


def test_codex_missing_before_is_unknown_in_words():
    result = compaction.decide_codex(None, {"status": "completed", "replaced": True, "after": 20_000})
    assert result["outcome"] == compaction.UNKNOWN
    assert "64 KB" in result["detail"]
    grew = compaction.decide_codex(20_000, {"status": "completed", "replaced": True, "after": 25_000})
    assert grew["outcome"] == compaction.NOT_COMPACTED


# ---- Claude Code: its own compaction record is the proof --------------------

def _usage(tokens):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-real",
        "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}}}) + "\n"


def _boundary(pre):
    return json.dumps({"type": "system", "subtype": "compact_boundary",
                       "compactMetadata": {"trigger": "manual", "preTokens": pre}}) + "\n"


def test_the_gauge_is_unknown_after_a_compaction_until_the_next_call(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(_usage(480_000))
    assert claude_transcript.context_tokens_from_jsonl(path) == 480_000
    path.write_text(_usage(480_000) + _boundary(480_000)
                    + json.dumps({"type": "user", "isCompactSummary": True}) + "\n")
    assert claude_transcript.context_tokens_from_jsonl(path) is None
    path.write_text(_usage(480_000) + _boundary(480_000) + _usage(22_000))
    assert claude_transcript.context_tokens_from_jsonl(path) == 22_000


def test_claude_is_compacted_on_a_boundary_written_after_the_send(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(_boundary(100) + _usage(480_000))
    offset = path.stat().st_size
    assert claude_transcript.compact_boundary_since(path, offset) is None   # the old one does not count
    with path.open("a") as f:
        f.write(_boundary(481_234))
    proof = claude_transcript.compact_boundary_since(path, offset)
    assert proof == 481_234
    result = compaction.decide_claude(480_000, proof, transcript=True, ending="settled")
    assert result["outcome"] == compaction.COMPACTED
    assert result["before_tokens"] == 481_234 and result["after_tokens"] is None
    assert "next call" in result["detail"]


@pytest.mark.parametrize("ending,expected", [
    ("settled", compaction.NOT_COMPACTED), ("stopped", compaction.NOT_COMPACTED),
    ("cap", compaction.UNKNOWN),
])
def test_claude_without_its_record_is_never_compacted(ending, expected):
    result = compaction.decide_claude(480_000, None, transcript=True, ending=ending)
    assert result["outcome"] == expected and result["detail"]
    assert compaction.decide_claude(None, None, transcript=False, ending="settled")["outcome"] \
        == compaction.UNKNOWN


# ---- the whole button, on stand-ins -----------------------------------------

@pytest.fixture()
def _fresh_slots(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    compaction._active.clear()
    yield
    td.reset_for_tests()
    compaction._active.clear()


class _Inline:
    """threading.Thread stand-in that runs the target at start()."""
    def __init__(self, target=None, args=(), kwargs=None, **_kw):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def test_the_codex_button_records_its_outcome_on_the_agent_row(monkeypatch, _fresh_slots):
    agent_id = agents_db.create_agent(persona="Cee", voice_id="v", cwd="/tmp",
                                      session="cee", backend="codex")
    agents_db.start_runtime(agent_id, "cee")
    monkeypatch.setattr(compaction.agents_db, "live_backend_session", lambda _a: "thread-c")
    monkeypatch.setattr(compaction.threading, "Thread", _Inline)
    codex_app_server._remember_context_size(_usage_params("thread-c", 390_000))
    seen = {}

    def compact_thread(agent, thread_id, *, timeout):
        seen["holds"] = td.compaction_holds_slot(agent["agent_id"])
        return {"status": "completed", "replaced": True, "after": 28_000, "error": ""}
    monkeypatch.setattr(codex_app_server, "compact_thread", compact_thread)
    answer = compaction.compact_session("cee")
    assert answer == {"ok": True, "status": "started", "backend": "codex"}
    assert seen == {"holds": True}
    assert td.compaction_holds_slot(agent_id) is False          # given back
    row = compaction.last_outcomes([agent_id])[agent_id]
    assert row["outcome"] == "compacted"
    assert (row["before_tokens"], row["after_tokens"]) == (390_000, 28_000)
    assert row["backend"] == "codex" and row["at"] > 0


def test_a_refused_codex_compaction_is_recorded_in_words(monkeypatch, _fresh_slots):
    agent_id = agents_db.create_agent(persona="Dee", voice_id="v", cwd="/tmp",
                                      session="dee", backend="codex")
    agents_db.start_runtime(agent_id, "dee")
    monkeypatch.setattr(compaction.agents_db, "live_backend_session", lambda _a: "thread-d")
    monkeypatch.setattr(compaction.threading, "Thread", _Inline)
    monkeypatch.setattr(codex_app_server, "compact_thread", lambda *_a, **_k: {
        "status": "refused", "replaced": False, "after": None,
        "error": "this Codex app-server did not compact the thread (Method not found)"})
    compaction.compact_session("dee")
    row = compaction.last_outcomes([agent_id])[agent_id]
    assert row["outcome"] == "not_compacted" and "Method not found" in row["detail"]


def test_the_snapshot_row_carries_last_compaction():
    from lib import snapshot
    text = (SERVER / "lib" / "snapshot.py").read_text()
    assert '"last_compaction": last_compactions.get(agent_id)' in text
    assert snapshot  # imported cleanly


# ---- the PR adds no automation ------------------------------------------------

# Every call site of the compaction, before this change and after it: the
# user's POST /compact (routes/send.py) and the runtime's "compact" RPC that
# the server process forwards that request to (runtime_rpc_server.py).
# compaction.py itself reaches the app-server's compaction; nothing else does.
_ALLOWED = {
    "compact_session": {"lib/routes/send.py", "lib/runtime_rpc_server.py"},
    "compact_thread": {"lib/compaction.py"},
    "claim_for_compaction": {"lib/compaction.py"},
}


def test_nothing_compacts_an_agent_except_the_users_request():
    found: dict[str, set[str]] = {name: set() for name in _ALLOWED}
    rpc_compact: set[str] = set()
    for path in SERVER.rglob("*.py"):
        rel = str(path.relative_to(SERVER))
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in found:
                    found[name].add(rel)
            if isinstance(node, ast.Constant) and node.value == "thread/compact/start":
                rpc_compact.add(rel)
    assert found == _ALLOWED
    # The app-server method is sent from one place, and named in the strategy.
    assert rpc_compact == {"lib/codex_app_server.py", "lib/backend/codex.py"}
    # The runtime's compact RPC is only the server's forward of POST /compact.
    bridge = (SERVER / "lib" / "runtime_bridge.py").read_text()
    assert bridge.count('self._request("compact"') == 1
    callers = {str(p.relative_to(SERVER)) for p in SERVER.rglob("*.py")
               if "_runtime_client.compact(" in p.read_text()}
    assert callers == {"lib/compaction.py"}
