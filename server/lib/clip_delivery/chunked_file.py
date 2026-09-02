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
import time

from .. import agents as agents_db
from .. import clips as clips_lib
from ..log import log_exception
from ..protocol import ClipProducerStatus
from . import FinalizeResult


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
        audio_dir.mkdir(parents=True, exist_ok=True)
        target = audio_dir / f"{int(time.time() * 1000)}__{session}.mp3"

        # 1. Allocate the durable clip identity BEFORE the first byte. The
        #    live URL is keyed by clip_id; the file is just a replay artifact.
        clip_id = agents_db.record_clip(
            agent_id=agent["agent_id"],
            path=str(target),
            voice_id=voice_id,
            trace_id=trace_id,
            producer_status=ClipProducerStatus.STREAMING,
        )
        if not clip_id:
            raise RuntimeError("record_clip returned no clip_id")

        # 2. Open the broker entry so late subscribers see the buffered
        #    chunks. Failure to open is non-fatal — the file fallback path
        #    still works when the live broker is absent.
        if self._broker is not None:
            try:
                self._broker.open(clip_id)
            except Exception as e:  # noqa: BLE001
                log_exception("clipBrokerOpenFail", e, detail=str(clip_id))

        # 3. Pre-synth sidecar. The audio_stream watcher / herald look at
        #    this when they discover the mp3; without it they'd broadcast a
        #    bare clip and the client would fall back to the static URL.
        clips_lib.write_sidecar(
            target,
            clip_id=clip_id,
            agent_id=agent["agent_id"],
            persona=agent.get("persona"),
            voice_id=voice_id,
            session=session,
            source=source,
            text_len=text_len,
            trace_id=trace_id,
            extra={"streamable": True,
                   "stream_url": f"/clips/{clip_id}/stream",
                   "complete_url": f"/clips/{clip_id}/complete.mp3"},
        )

        return ChunkedFileSession(
            broker=self._broker,
            target=target, clip_id=clip_id, agent=agent,
            voice_id=voice_id, session=session,
            source=source, text_len=text_len, trace_id=trace_id,
        )


class ChunkedFileSession:
    """Per-clip session for ChunkedFileDelivery.

    `feed` fans bytes into the broker. `synthesize_streaming` is what
    writes them to the file (via target_path). Two sinks, one source."""

    def __init__(self, *,
                 broker,
                 target: pathlib.Path,
                 clip_id: int,
                 agent: dict,
                 voice_id: str,
                 session: str,
                 source: str,
                 text_len: int,
                 trace_id: str | None):
        self._broker = broker
        self._target = target
        self._clip_id = int(clip_id)
        self._agent = agent
        self._voice_id = voice_id
        self._session = session
        self._source = source
        self._text_len = text_len
        self._trace_id = trace_id

    publish_after_finalize = False   # broker streams bytes live

    @property
    def clip_id(self) -> int:
        return self._clip_id

    @property
    def target_path(self) -> pathlib.Path:
        # synthesize_streaming writes the mp3 here directly.
        return self._target

    @property
    def sse_fields(self) -> dict:
        return {
            "streamable": True,
            "stream_url": f"/clips/{self._clip_id}/stream",
            "complete_url": f"/clips/{self._clip_id}/complete.mp3",
        }

    def feed(self, chunk_idx: int, chunk: bytes) -> None:
        if self._broker is None:
            return
        try:
            self._broker.append(self._clip_id, chunk)
        except Exception as e:  # noqa: BLE001
            log_exception("clipBrokerAppendFail", e, detail=str(self._clip_id))

    def finalize(self, *, total_bytes: int) -> FinalizeResult:
        size = (self._target.stat().st_size if self._target.exists()
                else total_bytes)

        # 1. Producer-side state: synthesis is done.
        try:
            agents_db.mark_clip_producer_status(
                clip_id=self._clip_id,
                producer_status=ClipProducerStatus.COMPLETE,
                byte_count=size,
            )
        except Exception as e:  # noqa: BLE001
            log_exception("clipMarkCompleteFail", e, detail=str(self._clip_id))

        # 2. Final sidecar — includes `bytes` so `audio_growing.is_in_progress`
        #    now returns False and any /audio/<file> fetches use the static
        #    path. Pinned by tests/integration/test_audio_pipeline_seams.py.
        clips_lib.write_sidecar(
            self._target,
            clip_id=self._clip_id,
            agent_id=self._agent["agent_id"],
            persona=self._agent.get("persona"),
            voice_id=self._voice_id,
            session=self._session,
            source=self._source,
            bytes_=size,
            text_len=self._text_len,
            trace_id=self._trace_id,
            extra={"streamable": True,
                   "stream_url": f"/clips/{self._clip_id}/stream",
                   "complete_url": f"/clips/{self._clip_id}/complete.mp3"},
        )

        # 3. Close the live broker stream — late subscribers get the
        #    buffered bytes + EOF, then fall back to the file path.
        if self._broker is not None:
            try:
                self._broker.finish(self._clip_id)
            except Exception as e:  # noqa: BLE001
                log_exception("clipBrokerFinishFail", e,
                              detail=str(self._clip_id))

        return FinalizeResult(
            path=self._target,
            clip_id=self._clip_id,
            sse_url=f"/audio/{self._target.name}",
            ok=True,
        )

    def fail(self, error: str) -> None:
        # Tear down partial artifacts so the watcher / herald can't
        # broadcast a half-written clip. Pinned by
        # test_failure_path_leaves_no_partial_mp3.
        try: self._target.unlink()
        except OSError: pass
        try: clips_lib.sidecar_path(self._target).unlink()
        except OSError: pass

        try:
            agents_db.mark_clip_producer_status(
                clip_id=self._clip_id,
                producer_status=ClipProducerStatus.FAILED,
                error=error,
            )
        except Exception as e:  # noqa: BLE001
            log_exception("clipMarkFailedFail", e, detail=str(self._clip_id))

        if self._broker is not None:
            try:
                self._broker.fail(self._clip_id, error)
            except Exception as e:  # noqa: BLE001
                log_exception("clipBrokerFailCleanup", e,
                              detail=str(self._clip_id))
