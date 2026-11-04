"""Current production behavior moved behind the ClipDelivery interface.

The worker used to open-code this entire flow inline in
`_synth_pwa_streaming`:

  1. allocate clip_id (record_clip with producer_status=STREAMING)
  2. open the ClipStreamBroker entry for that clip_id
  3. write the pre-synth sidecar (streamable=true, no `bytes` field yet)
  4. publish the SSE event (handled by the worker, using `sse_fields`)
  5. synthesize_streaming writes the mp3 to target_path AND fires on_chunk
     → on_chunk fans bytes into the broker
  6. finalize: mark producer_status=COMPLETE, write final sidecar, close
     the broker

This module is that flow, lifted into a class so HlsDelivery can take
its place without rewriting the worker.

Two byte sinks per chunk: the on-disk file (for replay/fallback via
`clips.path` lookup) and the in-memory broker (for the live
`/clips/<id>/stream` endpoint). The mp3 file is no longer the live event
source — that role moved to the broker — but it remains a useful fallback
when a client reconnects after the broker has aged out the buffer.
"""
from __future__ import annotations

import pathlib

from .file_session import FileBrokerSession, begin_file_clip


class ChunkedFileDelivery:
    """Selected when `cfg.delivery = "chunked-file"` (the default).

    Stateless — all per-clip state lives in the session. `broker` is the
    process-wide ClipStreamBroker injected by `build_from_config` from
    the server context; it can be None in tests / older deployments, in
    which case the live path just falls back to the file."""

    name = "chunked-file"

    def __init__(self, *, broker=None):
        self._broker = broker

    def begin(self, *,
              audio_dir: pathlib.Path,
              agent: dict,
              voice_id: str,
              session: str,
              source: str,
              text_len: int,
              trace_id: str | None) -> "ChunkedFileSession":
        return begin_file_clip(
            ChunkedFileSession, broker=self._broker, suffix=".mp3",
            audio_dir=audio_dir, agent=agent, voice_id=voice_id,
            session=session, source=source, text_len=text_len,
            trace_id=trace_id,
        )


class ChunkedFileSession(FileBrokerSession):
    """Per-clip session for ChunkedFileDelivery: an MP3 on disk."""

    @property
    def sse_fields(self) -> dict:
        return {
            "streamable": True,
            "stream_url": f"/clips/{self._clip_id}/stream",
            "complete_url": f"/clips/{self._clip_id}/complete.mp3",
        }

    def _finalized_url(self) -> str:
        return f"/audio/{self._target.name}"
