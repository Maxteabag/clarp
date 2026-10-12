"""TDD reproduction: Mic admission and plain-TTS capability boundaries. Implementation pending."""
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












def test_tts_auth_error_does_not_claim_an_elevenlabs_quota_failure():
    stream = _CaptureStream()
    tts_worker._publish_tts_error(  # noqa: SLF001
        stream,
        {"session": "voice", "agent_id": "agent"},
        {"persona": "Voice"},
        "Cartesia HTTP 401: invalid API key",
    )

    message = stream.events[0]["message"]
    assert "authentication" in message.lower()
    assert "quota" not in message.lower()
    assert "ElevenLabs" not in message
