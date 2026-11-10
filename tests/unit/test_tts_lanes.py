"""VP-06: the TTS worker's per-agent lanes and the cancellation of queued speech.

One slow provider call must not hold up other agents' speech, an agent's own
chunks stay one at a time and in order, and a user Stop or a preempted turn
drops that agent's not-yet-synthesized speech instead of speaking it late.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                 # noqa: E402
from lib import eventlog, tts_queue                  # noqa: E402
from lib.db import conn                              # noqa: E402
from lib.protocol import TurnSource                  # noqa: E402
from lib import tts_registry


@pytest.fixture
def env(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    from lib import config
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(tts_provider="elevenlabs", eleven_api_key="test-key"))
    alice = agents_db.create_agent(persona="Alice", voice_id="V_A", cwd=str(tmp_path),
                                   session="alice", backend="claude")
    bob = agents_db.create_agent(persona="Bob", voice_id="V_B", cwd=str(tmp_path),
                                 session="bob", backend="claude")
    return SimpleNamespace(audio_dir=audio_dir, alice=alice, bob=bob, tmp_path=tmp_path)


def _fake_provider(monkeypatch, speak):
    """The ElevenLabs stream the worker calls, replaced by `speak(text, voice_id)`."""
    from lib import tts_worker

    def _ws(*, text, voice_id, out_path, api_key, model, on_chunk=None, **_kw):
        speak(text, voice_id)
        pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
        return 4
    monkeypatch.setattr(tts_registry, "synthesize_streaming", _ws)


def _enqueue(agent_id, text, *, trace_id="t", voice_id="V", session="s"):
    return tts_queue.enqueue(agent_id=agent_id, text=text, voice_id=voice_id, session=session,
                             source=TurnSource.PWA, trace_id=trace_id)


def _row(queue_id):
    return dict(conn().execute("SELECT status, error FROM tts_queue WHERE queue_id=?",
                               (queue_id,)).fetchone())


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def worker(env):
    from lib.tts_worker import TTSWorker
    started = []

    def start():
        w = TTSWorker(audio_dir=env.audio_dir, interval_sec=0.02)
        w.start()
        started.append(w)
        return w
    yield start
    for w in started:
        w.stop(timeout=5.0)


# ---- lanes ----------------------------------------------------------------

def test_two_agents_synthesize_at_once_and_each_keeps_its_own_trace(env, monkeypatch, worker):
    # Each provider call waits for the other agent's call to begin: a single
    # serial worker never gets there, and both rows fail.
    both_speaking = threading.Barrier(2, timeout=3.0)
    _fake_provider(monkeypatch, lambda text, voice_id: both_speaking.wait())
    a = _enqueue(env.alice, "from alice", trace_id="trace-a", voice_id="V_A", session="alice")
    b = _enqueue(env.bob, "from bob", trace_id="trace-b", voice_id="V_B", session="bob")
    worker()
    assert _wait_until(lambda: {_row(a)["status"], _row(b)["status"]} <= {"done", "failed"})
    assert (_row(a), _row(b)) == ({"status": "done", "error": None}, {"status": "done", "error": None})
    assert agents_db.get_trace(env.alice) == "trace-a"
    assert agents_db.get_trace(env.bob) == "trace-b"
    clips = conn().execute("SELECT agent_id, path FROM clips ORDER BY clip_id").fetchall()
    assert sorted(r["agent_id"] for r in clips) == sorted([env.alice, env.bob])
    assert len({r["path"] for r in clips}) == 2


def test_a_slow_agent_does_not_hold_up_another_agents_speech(env, monkeypatch, worker):
    release = threading.Event()

    def speak(text, voice_id):
        if voice_id == "V_A":
            release.wait(5.0)
    _fake_provider(monkeypatch, speak)
    slow = _enqueue(env.alice, "slow", voice_id="V_A", session="alice")
    fast = _enqueue(env.bob, "fast", voice_id="V_B", session="bob")
    try:
        worker()
        assert _wait_until(lambda: _row(fast)["status"] == "done", timeout=3.0), \
            "the other agent's clip waited behind the slow one"
        assert _row(slow)["status"] == "synthesizing"
    finally:
        release.set()
    assert _wait_until(lambda: _row(slow)["status"] == "done")


def test_one_agents_chunks_are_synthesized_one_at_a_time_in_order(env, monkeypatch, worker):
    lock = threading.Lock()
    active, most, order = [0], [0], []

    def speak(text, voice_id):
        with lock:
            active[0] += 1
            most[0] = max(most[0], active[0])
            order.append(text)
        time.sleep(0.05)
        with lock:
            active[0] -= 1
    _fake_provider(monkeypatch, speak)
    rows = [_enqueue(env.alice, text, voice_id="V_A", session="alice")
            for text in ("one", "two", "three", "four")]
    worker()
    assert _wait_until(lambda: all(_row(q)["status"] == "done" for q in rows))
    assert order == ["one", "two", "three", "four"]
    assert most[0] == 1


# ---- cancellation ---------------------------------------------------------

def test_cancel_drops_only_that_agents_queued_rows(env):
    a1 = _enqueue(env.alice, "a1", trace_id="t1")
    a2 = _enqueue(env.alice, "a2", trace_id="t2")
    b1 = _enqueue(env.bob, "b1", trace_id="t1")
    running = tts_queue.claim_next()
    assert running["queue_id"] == a1
    assert tts_queue.cancel(env.alice, reason="user stop") == 1
    assert _row(a1) == {"status": "synthesizing", "error": None}, "a clip already synthesizing finishes"
    assert _row(a2) == {"status": "failed", "error": "cancelled: user stop"}
    assert _row(b1) == {"status": "queued", "error": None}
    assert tts_queue.pending_count() == 1


def test_cancel_for_a_trace_keeps_the_agents_newer_turn(env):
    old = _enqueue(env.alice, "old", trace_id="t-old")
    new = _enqueue(env.alice, "new", trace_id="t-new")
    assert tts_queue.cancel(env.alice, "t-old", reason="preempted") == 1
    assert _row(old) == {"status": "failed", "error": "cancelled: preempted"}
    assert _row(new) == {"status": "queued", "error": None}


def test_a_cancelled_row_is_never_synthesized(env, monkeypatch):
    spoken = []
    _fake_provider(monkeypatch, lambda text, voice_id: spoken.append(text))
    _enqueue(env.alice, "dropped", voice_id="V_A", session="alice")
    tts_queue.cancel(env.alice, reason="user stop")
    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env.audio_dir) is False
    assert spoken == []


# ---- the hooks ------------------------------------------------------------

def _load_server():
    import importlib.util
    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("clarp_server_tts_lanes", root / "server/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_user_stop_cancels_that_agents_queued_speech(env, monkeypatch):
    import lib.turn_dispatch as _td
    from lib import backends
    _td.reset_for_tests()
    agents_db.start_runtime(env.alice, "alice")
    monkeypatch.setattr(backends, "interrupt", lambda backend, agent_id: 1)
    mine = _enqueue(env.alice, "late words", trace_id="t-run")
    other = _enqueue(env.bob, "bob's words")
    server = _load_server()
    handler = server.Handler.__new__(server.Handler)
    handler.server = SimpleNamespace(ctx=SimpleNamespace(stream=None, runtime_client=None,
                                                         default_session="alice"))
    server.Handler._stop_agent_session(handler, "alice", strict=False)
    assert _row(mine)["status"] == "failed"
    assert _row(mine)["error"].startswith("cancelled: ")
    assert _row(other) == {"status": "queued", "error": None}


def test_a_preempted_turns_queued_speech_is_cancelled_and_the_new_turns_kept(env):
    from lib.turn_dispatch import TurnDispatchService
    old = _enqueue(env.alice, "old answer", trace_id="t-old")
    new = _enqueue(env.alice, "new answer", trace_id="t-new")
    service = TurnDispatchService.__new__(TurnDispatchService)
    service.backends = SimpleNamespace(interrupt=lambda backend, agent_id: 1)
    spec = SimpleNamespace(backend="claude", agent_id=env.alice, trace_id="t-new",
                           context=eventlog.EventContext())
    service._preempt_for(spec, "t-old")
    assert _row(old)["status"] == "failed"
    assert _row(old)["error"].startswith("cancelled: ")
    assert _row(new) == {"status": "queued", "error": None}
