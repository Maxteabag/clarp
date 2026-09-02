"""Phase B wiring tests — TTSWorker routes PWA-mode rows through the
ElevenLabs WebSocket path (lib.eleven_ws.synthesize_streaming) instead
of the HTTP path. Other modes (local/stream/off) keep their existing
behavior.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                  # noqa: E402
from lib import tts_queue                             # noqa: E402
from lib.protocol import TurnSource          # noqa: E402


# ---- Fixtures -----------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registered agent + paths the worker expects."""
    audio_dir = tmp_path / "audio"; audio_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    from lib import config
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(tts_provider="elevenlabs", eleven_api_key="simulated"))
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
def stub_streaming(monkeypatch):
    """Replace eleven_ws.synthesize_streaming with a deterministic stub
    that simulates streaming three small chunks then EOS."""
    captured = {"calls": []}

    def fake(*, text, voice_id, out_path, api_key,
             model, speed=1.2, stability=0.5, similarity_boost=0.75,
             timeout=30.0, on_chunk=None, **_kw):
        captured["calls"].append({
            "text": text, "voice_id": voice_id, "out_path": pathlib.Path(out_path),
            "api_key": api_key, "model": model,
        })
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        chunks = [b"\xff\xfb\x90\x00", b"AAAA", b"BBBB"]
        total = 0
        with out.open("wb") as f:
            for i, c in enumerate(chunks):
                if on_chunk is not None:
                    try: on_chunk(i, c)
                    except Exception: pass
                f.write(c); f.flush()
                total += len(c)
        return total

    from lib import tts_worker as _tw
    monkeypatch.setattr(_tw, "synthesize_streaming", fake)
    return captured


# ---- Tests --------------------------------------------------------------


def test_pwa_mode_uses_streaming_path(env, stub_streaming, monkeypatch):
    """A PWA-mode queue row routes through eleven_ws, NOT eleven_http."""
    # Belt-and-braces: if anything reaches the HTTP path, fail loudly.
    def boom(*a, **kw):
        raise AssertionError("HTTP synthesize_to_file must not be called "
                             "for PWA mode in Phase B")
    from lib import eleven_http
    monkeypatch.setattr(eleven_http, "synthesize_to_file", boom)

    tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="hello streaming world",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
        trace_id="trace-stream-1",
    )
    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is True

    assert len(stub_streaming["calls"]) == 1
    call = stub_streaming["calls"][0]
    assert call["text"] == "hello streaming world"
    assert call["voice_id"] == "V_MIKE"

    # Queue row done.
    assert tts_queue.pending_count() == 0
    row = tts_queue.recent(limit=1)[0]
    assert row["status"] == tts_queue.DONE

    # Clip on disk.
    clips = list(env["audio_dir"].glob("*__claude.mp3"))
    assert len(clips) == 1, [c.name for c in clips]
    assert clips[0].read_bytes() == b"\xff\xfb\x90\x00" + b"AAAA" + b"BBBB"

    # Sidecar remains as fallback/debug metadata, but the live stream URL is
    # now clip-id keyed instead of filename/watcher keyed.
    sidecar = clips[0].with_suffix(clips[0].suffix + ".json")
    assert sidecar.is_file()
    meta = json.loads(sidecar.read_text())
    assert meta.get("streamable") is True, (
        f"PWA-mode streaming clip should carry streamable=true; "
        f"sidecar = {meta}"
    )
    assert meta.get("trace_id") == "trace-stream-1"
    assert meta.get("clip_id")
    assert meta.get("stream_url") == f"/clips/{meta['clip_id']}/stream"


def test_pwa_streaming_failure_marks_row_failed(env, monkeypatch):
    """If eleven_ws raises, the row goes to FAILED with the error text."""
    from lib import tts_worker as _tw, eleven_ws
    def boom(**kw):
        raise eleven_ws.ElevenWSError("auth refused")
    monkeypatch.setattr(_tw, "synthesize_streaming", boom)

    tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="this should fail",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
    )
    from lib.tts_worker import synth_one
    assert synth_one(audio_dir=env["audio_dir"]) is True

    row = tts_queue.recent(limit=1)[0]
    assert row["status"] == tts_queue.FAILED
    assert row["error"] and "auth refused" in row["error"]
    db_row = agents_db.conn().execute(
        "SELECT producer_status, error FROM clips"
    ).fetchone()
    assert db_row["producer_status"] == "failed"
    assert "auth refused" in (db_row["error"] or "")
    # No partial clip should be left behind.
    assert list(env["audio_dir"].glob("*.mp3")) == []


def test_sidecar_exists_BEFORE_first_audio_byte_is_visible(env, monkeypatch):
    """Race-condition regression. The audio_stream watcher scans for new
    .mp3 files at a fixed cadence; when it finds one it reads the
    sidecar to pick up streamable + agent_id + trace_id. If the worker
    writes the .mp3 file FIRST and only writes the sidecar after
    synthesis completes, there's a window where the watcher detects the
    .mp3 with no sidecar — meta is empty, streamable is missing from
    the SSE broadcast, and the client falls back to <audio src>.

    Pinned: when the synthesize_streaming callback fires (i.e. while the
    mp3 file is being written), the sidecar must already be on disk.
    """
    observed = {"sidecar_present_during_synth": None}

    def fake_streaming(*, text, voice_id, out_path, **kw):
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Simulate the first ElevenLabs chunk landing. The watcher's
        # `glob("*.mp3")` would discover this file now. Check whether the
        # sidecar is on disk at this exact instant.
        out.write_bytes(b"\xff\xfb")
        side = out.with_suffix(out.suffix + ".json")
        observed["sidecar_present_during_synth"] = side.is_file()
        return 2

    from lib import tts_worker as _tw
    monkeypatch.setattr(_tw, "synthesize_streaming", fake_streaming)

    tts_queue.enqueue(
        agent_id=env["agent_id"], text="hello",
        voice_id="V_MIKE", session="claude",
        source=TurnSource.PWA,
        trace_id="trace-race",
    )
    from lib.tts_worker import synth_one
    synth_one(audio_dir=env["audio_dir"])

    assert observed["sidecar_present_during_synth"] is True, (
        "race condition: the watcher would have seen the .mp3 without a "
        "sidecar, so the SSE broadcast strips streamable/stream_url and the "
        "client falls back to <audio src>. Write the sidecar BEFORE the "
        "mp3 is created (or use a temp-rename), so the watcher's read is "
        "consistent."
    )


def test_clip_row_exists_BEFORE_first_audio_byte(env, monkeypatch):
    """The migrated live path is SQLite-owned: by the time the first
    ElevenLabs byte lands, the clips row already exists and is marked
    producer_status=streaming. The client stream URL is keyed off that
    clip_id, so this must not be delayed until EOS."""
    observed = {"row": None}

    def fake_streaming(*, text, voice_id, out_path, **kw):
        from lib.db import conn
        rows = conn().execute(
            "SELECT clip_id, path, producer_status FROM clips"
        ).fetchall()
        observed["row"] = dict(rows[0]) if rows else None
        pathlib.Path(out_path).write_bytes(b"\xff\xfb")
        return 2

    from lib import tts_worker as _tw
    monkeypatch.setattr(_tw, "synthesize_streaming", fake_streaming)

    tts_queue.enqueue(
        agent_id=env["agent_id"], text="hello",
        voice_id="V_MIKE", session="claude",
        source=TurnSource.PWA,
        trace_id="trace-row-first",
    )
    from lib.tts_worker import synth_one
    synth_one(audio_dir=env["audio_dir"])

    assert observed["row"] is not None
    assert observed["row"]["producer_status"] == "streaming"
    assert observed["row"]["path"].endswith("__claude.mp3")


def test_pwa_streaming_writes_clips_row_with_trace_id(env, stub_streaming):
    """The clips DB row should carry the trace_id from the queue. This
    pins the trace-end-to-end path through the streaming producer."""
    tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="hi",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
        trace_id="trace-streaming-end-to-end",
    )
    from lib.tts_worker import synth_one
    synth_one(audio_dir=env["audio_dir"])

    from lib.db import conn
    rows = conn().execute(
        "SELECT path, trace_id, status, producer_status, bytes FROM clips"
    ).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["trace_id"] == "trace-streaming-end-to-end"
    assert row["producer_status"] == "complete"
    assert row["bytes"] == 12   # \xff\xfb\x90\x00 + AAAA + BBBB


def test_worker_publishes_clip_event_directly_without_watcher(env, stub_streaming):
    """The live audio notification path is no longer AudioStream's
    directory watcher. The worker should broadcast the SSE audio event
    directly once it has created the clip row, keyed by clip_id."""
    from lib.audio_stream import AudioStream

    stream = AudioStream(env["audio_dir"])
    q = stream.subscribe()

    tts_queue.enqueue(
        agent_id=env["agent_id"],
        text="direct event",
        voice_id="V_MIKE",
        session="claude",
        source=TurnSource.PWA,
        trace_id="trace-direct-event",
    )
    from lib.tts_worker import synth_one
    synth_one(
        audio_dir=env["audio_dir"],
        stream=stream,
    )

    ev = json.loads(q.get(timeout=1))
    assert ev["type"] == "audio"
    assert ev["trace_id"] == "trace-direct-event"
    assert ev["clip_id"]
    assert ev["stream_url"] == f"/clips/{ev['clip_id']}/stream"
    assert ev["streamable"] is True
