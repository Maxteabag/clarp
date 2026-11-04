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


@pytest.fixture
def patch_eleven(monkeypatch):
    from lib import tts_worker
    # PWA-mode rows route through eleven_ws.synthesize_streaming; patch its
    # imported binding in tts_worker so no real WS opens.
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


def test_a_restart_fails_stale_in_flight_speech_instead_of_speaking_it_again(env, patch_eleven):
    """VP-05: a restart during synthesis re-spoke the reply, however old, and
    left its clip row streaming for ever."""
    from lib import db, tts_worker
    from lib.protocol import ClipProducerStatus

    stale = tts_queue.enqueue(agent_id=env["agent_id"], text="old answer", voice_id="V_MIKE",
                              session="claude", source=TurnSource.PWA)
    fresh = tts_queue.enqueue(agent_id=env["agent_id"], text="new answer", voice_id="V_MIKE",
                              session="claude", source=TurnSource.PWA)
    assert tts_queue.claim_next()["queue_id"] == stale
    assert tts_queue.claim_next()["queue_id"] == fresh
    now = db.now_ms()
    db.conn().execute("UPDATE tts_queue SET claimed_at = ? WHERE queue_id = ?",
                      (now - 2 * 60 * 60 * 1000, stale))
    clip = agents_db.record_clip(agent_id=env["agent_id"], path=str(env["audio_dir"] / "dead.pcm"),
                       voice_id="V_MIKE", trace_id="t-dead",
                       producer_status=ClipProducerStatus.STREAMING)

    tts_worker.recover_in_flight(now_ms=now)

    rows = {r["queue_id"]: r["status"] for r in db.conn().execute(
        "SELECT queue_id, status FROM tts_queue")}
    assert rows == {stale: tts_queue.FAILED, fresh: tts_queue.QUEUED}
    status = db.conn().execute(
        "SELECT producer_status FROM clips WHERE clip_id = ?", (clip,)).fetchone()[0]
    assert status == ClipProducerStatus.FAILED


def _fallback_clip(env, monkeypatch, *, primary_reads):
    """One Cartesia->ElevenLabs clip through _synthesize, with the real
    Cartesia adapter reading `primary_reads` from an in-memory response
    (an exception in it is the upstream drop) and providers calling the
    live sink as on_chunk(index, bytes)."""
    from lib import cartesia_tts, config, tts_worker
    cfg = config.Config(
        tts_provider="cartesia", tts_fallback="elevenlabs",
        cartesia_api_key="cartesia-key", eleven_api_key="eleven-key",
        cartesia_voices={"Mike": "cartesia-voice"})

    class Response:
        reads = iter(primary_reads)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _size):
            step = next(self.reads, b"")
            if isinstance(step, Exception):
                raise step
            return step

    monkeypatch.setattr(cartesia_tts.urllib.request, "urlopen",
                        lambda *_a, **_kw: Response())
    fallback_ran = []

    def eleven(*, on_chunk, out_path, **_kw):
        fallback_ran.append(True)
        for index, chunk in enumerate((b"ELEVEN-FULL-1", b"ELEVEN-FULL-2")):
            try:  # eleven_ws isolates callback errors the same way
                on_chunk(index, chunk)
            except Exception:  # noqa: BLE001
                pass
        pathlib.Path(out_path).write_bytes(b"ELEVEN-FULL-1ELEVEN-FULL-2")
        return 26

    monkeypatch.setattr(tts_worker, "synthesize_streaming", eleven)
    live: list[tuple[int, bytes]] = []
    row = {"voice_id": "eleven-voice", "text": "Sure, I'll check the logs",
           "agent_id": env["agent_id"], "session": "claude"}
    run = lambda: tts_worker._synthesize(  # noqa: E731
        cfg=cfg, row=row, agent={"persona": "Mike"},
        out_path=env["audio_dir"] / "clip.mp3",
        on_chunk=lambda index, chunk: live.append((index, chunk)),
        trace_id=None)
    return run, live, fallback_ran


def test_fallback_after_audio_reached_the_stream_fails_the_clip(env, monkeypatch):
    """VP-01: once the primary fed the live stream, a fallback would play a
    second full clip after the partial one; the clip fails instead."""
    from lib import tts_worker
    run, live, fallback_ran = _fallback_clip(
        env, monkeypatch, primary_reads=[b"CARTESIA-PART-1", OSError("dropped")])
    emitted = []
    monkeypatch.setattr(tts_worker, "_emit",
                        lambda _src, name, **kw: emitted.append((name, kw)))
    with pytest.raises(tts_worker.CartesiaError):
        run()
    assert live == [(0, b"CARTESIA-PART-1")]
    assert not fallback_ran
    skipped = [kw for name, kw in emitted if name == "providerFallbackSkipped"]
    assert len(skipped) == 1 and skipped[0]["level"] == "warn"
    assert skipped[0]["detail"]["fallback"] == "elevenlabs"


def test_fallback_before_any_audio_still_speaks(env, monkeypatch):
    run, live, fallback_ran = _fallback_clip(
        env, monkeypatch, primary_reads=[OSError("refused")])
    assert run() == 26
    assert live == [(0, b"ELEVEN-FULL-1"), (1, b"ELEVEN-FULL-2")]
    assert fallback_ran


def test_primary_streams_its_indexed_chunks_to_the_live_sink(env, monkeypatch):
    run, live, fallback_ran = _fallback_clip(
        env, monkeypatch, primary_reads=[b"CARTESIA-1", b"CARTESIA-2"])
    assert run() == 20
    assert live == [(0, b"CARTESIA-1"), (1, b"CARTESIA-2")]
    assert not fallback_ran
