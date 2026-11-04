"""The clip session both file-backed deliveries share (S7 step 1c, VP-19).

A clip is a file on disk plus the live ClipStreamBroker entry:

  1. allocate clip_id (record_clip with producer_status=STREAMING)
  2. open the broker entry for that clip_id
  3. write the pre-synth sidecar (no `bytes` field yet)
  4. the worker publishes the SSE event from `sse_fields`
  5. the provider writes the file at target_path and fires on_chunk, which
     `feed` fans into the broker
  6. finalize: mark producer_status=COMPLETE, write the final sidecar, close
     the broker; fail: remove the partial file and sidecar, mark FAILED

Raw PCM and chunked MP3 differ only in the file suffix, the clip fields
(`sse_fields`, also the sidecar's extra) and the URL finalize reports;
subclasses give those.
"""
from __future__ import annotations

from .. import clip_store

import pathlib

from .. import agents as agents_db
from .. import clips as clips_lib
from ..clock import now_ms
from ..log import log_exception
from ..protocol import ClipProducerStatus
from . import FinalizeResult


def begin_file_clip(session_cls, *,
                    broker,
                    suffix: str,
                    audio_dir: pathlib.Path,
                    agent: dict,
                    voice_id: str,
                    session: str,
                    source: str,
                    text_len: int,
                    trace_id: str | None,
                    **session_kwargs) -> "FileBrokerSession":
    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / f"{now_ms()}__{session}{suffix}"

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
    if broker is not None:
        try:
            broker.open(clip_id)
        except Exception as e:  # noqa: BLE001
            log_exception("clipBrokerOpenFail", e, detail=str(clip_id))

    clip = session_cls(
        broker=broker,
        target=target, clip_id=clip_id, agent=agent,
        voice_id=voice_id, session=session,
        source=source, text_len=text_len, trace_id=trace_id,
        **session_kwargs,
    )

    # 3. Pre-synth sidecar. The audio_stream watcher / herald look at
    #    this when they discover the file; without it they'd broadcast a
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
        extra=clip.sse_fields,
    )
    return clip


class FileBrokerSession:
    """Per-clip session: `feed` fans bytes into the broker; the provider
    writes them to the file (via target_path). Two sinks, one source."""

    publish_after_finalize = False   # broker streams bytes live

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

    @property
    def clip_id(self) -> int:
        return self._clip_id

    @property
    def target_path(self) -> pathlib.Path:
        return self._target

    @property
    def sse_fields(self) -> dict:
        raise NotImplementedError

    def _finalized_url(self) -> str:
        raise NotImplementedError

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
            clip_store.mark_clip_producer_status(
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
            extra=self.sse_fields,
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
            sse_url=self._finalized_url(),
            ok=True,
        )

    def fail(self, error: str) -> None:
        # Tear down partial artifacts so the watcher / herald can't
        # broadcast a half-written clip. Pinned by
        # test_failure_path_leaves_no_partial_mp3.
        try:
            self._target.unlink()
        except OSError:
            pass
        try:
            clips_lib.sidecar_path(self._target).unlink()
        except OSError:
            pass
        try:
            clip_store.mark_clip_producer_status(
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
