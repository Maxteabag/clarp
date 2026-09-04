"""Unit tests for the TTS worker.

The worker is the executor side of the Phase A split: hooks enqueue,
worker drains. These tests exercise the drain path directly with the
FakeTTSEngine pattern that test_server_di uses, so no ElevenLabs HTTP
goes out.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                 # noqa: E402
from lib import tts_queue                            # noqa: E402
from lib.protocol import ClipStatus, TurnSource  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A registered agent + a HOME-aware audio dir."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    from lib import config
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(tts_provider="elevenlabs", eleven_api_key="test-key"))

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V_MIKE",
        cwd=str(tmp_path), session="claude",
    )
    return {
        "tmp_path": tmp_path,
        "audio_dir": audio_dir,
        "agent_id": agent_id,
    }


def _fake_synthesize_to_file(text, voice_id, out_path, **kw):
    """Stand-in for eleven_http.synthesize_to_file — writes 4 bytes."""
    pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
    return 4


@pytest.fixture
def patch_eleven(monkeypatch):
    from lib import eleven_http, tts_worker
    monkeypatch.setattr(eleven_http, "synthesize_to_file",
                        _fake_synthesize_to_file)
    # PWA-mode rows route through eleven_ws.synthesize_streaming now;
    # patch its imported binding in tts_worker too so no real WS opens.
    def _fake_ws(*, text, voice_id, out_path, api_key, model,
                 speed=1.2, stability=0.5, similarity_boost=0.75,
                 timeout=30.0, on_chunk=None, **_kw):
        import pathlib
        pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
        return 4
    monkeypatch.setattr(tts_worker, "synthesize_streaming", _fake_ws)


def test_enqueue_then_drain_lands_clip_on_disk(env, patch_eleven):
    qid = tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="hello",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
        trace_id="trace-1",
    )
    assert qid > 0
    assert tts_queue.pending_count() == 1

    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is True

    # Queue row should be done now.
    assert tts_queue.pending_count() == 0
    assert tts_queue.in_flight_count() == 0
    recent = tts_queue.recent(limit=1)
    assert recent[0]["status"] == tts_queue.DONE
    assert recent[0]["error"] is None

    # And an mp3 should be on disk.
    clips = list(env["audio_dir"].glob("*__claude.mp3"))
    assert len(clips) == 1, [c.name for c in clips]

    # …with a sidecar carrying the trace_id we enqueued.
    import json
    sidecar = clips[0].with_suffix(clips[0].suffix + ".json")
    meta = json.loads(sidecar.read_text())
    assert meta["trace_id"] == "trace-1"
    assert meta["agent_id"] == env["agent_id"]
    assert meta["voice_id"] == "V_MIKE"


def test_drain_when_queue_empty_returns_false(env, patch_eleven):
    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is False


def test_enqueue_rejects_deleted_session_owner(env):
    agents_db.soft_delete(env["agent_id"])

    with pytest.raises(ValueError, match="session changed"):
        tts_queue.enqueue(
            agent_id=env["agent_id"], text="stale", voice_id="V_MIKE",
            session="claude", source=TurnSource.PWA)

    assert tts_queue.pending_count() == 0


def test_claim_marks_orphaned_queued_audio_failed(env):
    tts_queue.enqueue(
        agent_id=env["agent_id"], text="queued", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA)
    agents_db.soft_delete(env["agent_id"])

    assert tts_queue.claim_next() is None

    [row] = tts_queue.recent(limit=1)
    assert row["status"] == tts_queue.FAILED


def test_synth_failure_marks_row_failed(env, monkeypatch):
    from lib import eleven_http, eleven_ws, tts_worker
    def _boom_http(text, voice_id, out_path, **kw):
        raise eleven_http.ElevenError("rate limited")
    def _boom_ws(**kw):
        raise eleven_ws.ElevenWSError("rate limited")
    monkeypatch.setattr(eleven_http, "synthesize_to_file", _boom_http)
    monkeypatch.setattr(tts_worker, "synthesize_streaming", _boom_ws)

    tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="hello",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
    )
    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is True

    row = tts_queue.recent(limit=1)[0]
    assert row["status"] == tts_queue.FAILED
    assert row["error"] and "rate limited" in row["error"]
    # No clip on disk.
    assert list(env["audio_dir"].glob("*.mp3")) == []


def test_claim_next_is_atomic_across_calls(env, patch_eleven):
    """Two consecutive claims must not return the same row."""
    qid1 = tts_queue.enqueue(
        agent_id=env["agent_id"], text="one", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
    )
    qid2 = tts_queue.enqueue(
        agent_id=env["agent_id"], text="two", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
    )
    r1 = tts_queue.claim_next()
    r2 = tts_queue.claim_next()
    assert r1 is not None and r2 is not None
    assert {r1["queue_id"], r2["queue_id"]} == {qid1, qid2}
    assert r1["queue_id"] != r2["queue_id"]
    # Third claim has nothing left.
    assert tts_queue.claim_next() is None


def test_reset_in_flight_recovers_stuck_rows(env, patch_eleven):
    """If the worker dies mid-claim, the row should re-queue on next start."""
    qid = tts_queue.enqueue(
        agent_id=env["agent_id"], text="hello", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
    )
    r = tts_queue.claim_next()
    assert r is not None and r["queue_id"] == qid
    assert tts_queue.in_flight_count() == 1

    # Simulate a worker crash by NOT marking done/failed. Now restart.
    n = tts_queue.reset_in_flight()
    assert n == 1
    assert tts_queue.in_flight_count() == 0
    assert tts_queue.pending_count() == 1
    # And it's claimable again.
    r2 = tts_queue.claim_next()
    assert r2 is not None and r2["queue_id"] == qid


def test_silent_turn_is_suppressed_before_it_reaches_worker(env):
    assert tts_queue.enqueue(
        agent_id=env["agent_id"], text="silent", voice_id="V_MIKE",
        session="claude", source=TurnSource.PWA,
        synthesize_audio=False,
    ) == 0
    assert tts_queue.pending_count() == 0


def test_cartesia_fallback_is_used_only_when_explicit(env, monkeypatch):
    from lib import config, tts_worker
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="cartesia", tts_fallback="elevenlabs",
        cartesia_api_key="cartesia-key", eleven_api_key="eleven-key",
        cartesia_voices={"Mike": "cartesia-voice"}))
    monkeypatch.setattr(
        tts_worker, "cartesia_synthesize",
        lambda **_kwargs: (_ for _ in ()).throw(
            tts_worker.CartesiaError("outage")))

    def eleven(**kwargs):
        pathlib.Path(kwargs["out_path"]).write_bytes(b"mp3")
        return 3

    monkeypatch.setattr(tts_worker, "synthesize_streaming", eleven)
    tts_queue.enqueue(
        agent_id=env["agent_id"], text="Fallback",
        voice_id="eleven-voice", session="claude", source=TurnSource.PWA)
    assert tts_worker.synth_one(audio_dir=env["audio_dir"]) is True
    assert tts_queue.recent(limit=1)[0]["status"] == tts_queue.DONE
