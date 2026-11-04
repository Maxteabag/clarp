"""Native low-latency raw PCM delivery."""
from __future__ import annotations

import pathlib

from .file_session import FileBrokerSession, begin_file_clip


RAW_PCM_FORMAT = {
    "container": "raw",
    "encoding": "pcm_f32le",
    "sample_rate": 44100,
    "channels": 1,
}


class RawPcmDelivery:
    name = "raw-pcm"

    def __init__(self, *, broker=None, encoding: str | None = None,
                 sample_rate: int | None = None):
        self._broker = broker
        # The advertised format must match what the TTS provider is asked
        # for (tts_worker passes the same cfg values to Cartesia); the client
        # configures its decoder from this dict, never from an assumption.
        self.format = dict(RAW_PCM_FORMAT)
        if encoding:
            self.format["encoding"] = encoding
        if sample_rate:
            self.format["sample_rate"] = int(sample_rate)

    def begin(self, *,
              audio_dir: pathlib.Path,
              agent: dict,
              voice_id: str,
              session: str,
              source: str,
              text_len: int,
              trace_id: str | None) -> "RawPcmSession":
        return begin_file_clip(
            RawPcmSession, broker=self._broker, suffix=".pcm",
            audio_dir=audio_dir, agent=agent, voice_id=voice_id,
            session=session, source=source, text_len=text_len,
            trace_id=trace_id, audio_format=self.format,
        )


def _extra_fields(clip_id: int, audio_format: dict) -> dict:
    stream_url = f"/clips/{clip_id}/stream"
    return {
        "delivery": "raw-pcm",
        "streamable": True,
        "stream_url": stream_url,
        "url": stream_url,
        "audio_format": dict(audio_format),
    }


class RawPcmSession(FileBrokerSession):
    """Per-clip session for RawPcmDelivery: raw PCM on disk, its format
    advertised with every clip."""

    def __init__(self, *, audio_format: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self._audio_format = dict(audio_format or RAW_PCM_FORMAT)

    @property
    def sse_fields(self) -> dict:
        return _extra_fields(self._clip_id, self._audio_format)

    def _finalized_url(self) -> str:
        return f"/clips/{self._clip_id}/stream"
