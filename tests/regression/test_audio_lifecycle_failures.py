"""TDD reproduction: Queued audio ownership and durable held delivery. Implementation pending."""
from __future__ import annotations

import os
from pathlib import Path
import time

from lib import agents, clip_store, config, tts_queue, tts_worker
from lib.audio_stream import AudioStream
from lib.herald import HeraldManager
from lib.protocol import ClipStatus, TurnSource


class _FakeHeraldTTS:
    def __init__(self, root: Path):
        self.root = root

    def synthesize_herald(self, _text, _voice_id, *, session=None):
        path = self.root / f"herald-{session or 'agent'}.mp3"
        path.write_bytes(b"herald")
        return path


class _OneJanitorPass:
    def __init__(self):
        self.calls = 0

    def wait(self, _timeout):
        self.calls += 1
        return self.calls > 1


class _CaptureStream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _agent(session: str) -> str:
    return agents.create_agent(
        persona=session.capitalize(),
        voice_id="voice",
        cwd="/tmp",
        session=session,
    )


def test_old_tts_work_cannot_overwrite_a_newer_turns_trace(tmp_path, monkeypatch):
    agent_id = _agent("trace-owner")
    agents.set_trace(agent_id, "new-live-turn")
    tts_queue.enqueue(
        agent_id=agent_id,
        text="An older reply",
        voice_id="voice",
        session="trace-owner",
        source=TurnSource.PWA,
        trace_id="old-finished-turn",
    )
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="none"))

    assert tts_worker.synth_one(audio_dir=tmp_path) is True

    assert agents.get_trace(agent_id) == "new-live-turn"




def test_server_held_herald_clip_has_a_durable_recovery_record(tmp_path):
    focused_id = _agent("focused")
    background_id = _agent("background")
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    clip_path = audio_dir / "background-reply.mp3"
    clip_path.write_bytes(b"reply")
    clip_id = clip_store.record_clip(
        agent_id=background_id,
        path=str(clip_path),
        trace_id="held-trace",
        producer_status="complete",
        runtime_id=lambda _agent_id: None,
    )
    stream = AudioStream(audio_dir)
    manager = HeraldManager(
        stream=stream,
        tts=_FakeHeraldTTS(audio_dir),
        agents=lambda: {
            "focused": {"name": "Focused", "voice_id": "voice",
                        "agent_id": focused_id},
            "background": {"name": "Background", "voice_id": "voice",
                           "agent_id": background_id},
        },
    )
    manager.set_focus("focused")

    outcome = manager.ingest_clip(
        "background",
        url=f"/audio/{clip_path.name}",
        ts=1,
        meta={
            "clip_id": clip_id,
            "agent_id": background_id,
            "persona": "Background",
            "trace_id": "held-trace",
            "text_len": 500,
        },
    )

    assert outcome.broadcast is False
    row = agents.conn().execute(
        "SELECT status FROM clips WHERE clip_id=?", (clip_id,)
    ).fetchone()
    assert row["status"] == ClipStatus.HELD
    recoverable = clip_store.recoverable_events(session="background")
    assert [event["clip_id"] for event in recoverable] == [clip_id]




def test_terminal_playback_ack_cannot_move_back_to_queued(tmp_path):
    agent_id = _agent("playback-state")
    clip_id = clip_store.record_clip(
        agent_id=agent_id,
        path=str(tmp_path / "played.mp3"),
        producer_status="complete",
        runtime_id=lambda _agent_id: None,
    )
    assert clip_store.mark_clip_status(
        clip_id=clip_id, status=ClipStatus.PLAY_OK)

    clip_store.mark_clip_status(clip_id=clip_id, status=ClipStatus.QUEUED)

    row = agents.conn().execute(
        "SELECT status FROM clips WHERE clip_id=?", (clip_id,)
    ).fetchone()
    assert row["status"] == ClipStatus.PLAY_OK


