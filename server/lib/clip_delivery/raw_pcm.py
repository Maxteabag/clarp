"""Native low-latency raw PCM delivery."""
from __future__ import annotations

from .. import clip_store

import pathlib
import time

from .. import agents as agents_db
from .. import clips as clips_lib
from ..log import log_exception
from ..protocol import ClipProducerStatus
from . import FinalizeResult


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
        audio_dir.mkdir(parents=True, exist_ok=True)
        target = audio_dir / f"{int(time.time() * 1000)}__{session}.pcm"
        clip_id = agents_db.record_clip(
            agent_id=agent["agent_id"],
            path=str(target),
            voice_id=voice_id,
            trace_id=trace_id,
            producer_status=ClipProducerStatus.STREAMING,
        )
        if not clip_id:
            raise RuntimeError("record_clip returned no clip_id")

        if self._broker is not None:
            try:
                self._broker.open(clip_id)
            except Exception as e:  # noqa: BLE001
                log_exception("clipBrokerOpenFail", e, detail=str(clip_id))

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
            extra=_extra_fields(clip_id, self.format),
        )

        return RawPcmSession(
            broker=self._broker,
            target=target,
            clip_id=clip_id,
            agent=agent,
            voice_id=voice_id,
            session=session,
            source=source,
            text_len=text_len,
            trace_id=trace_id,
            audio_format=self.format,
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


class RawPcmSession:
    publish_after_finalize = False

    def __init__(self, *,
                 broker,
                 target: pathlib.Path,
                 clip_id: int,
                 agent: dict,
                 voice_id: str,
                 session: str,
                 source: str,
                 text_len: int,
                 trace_id: str | None,
                 audio_format: dict | None = None):
        self._broker = broker
        self._audio_format = dict(audio_format or RAW_PCM_FORMAT)
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
        return _extra_fields(self._clip_id, self._audio_format)

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
        try:
            clip_store.mark_clip_producer_status(
                clip_id=self._clip_id,
                producer_status=ClipProducerStatus.COMPLETE,
                byte_count=size,
            )
        except Exception as e:  # noqa: BLE001
            log_exception("clipMarkCompleteFail", e, detail=str(self._clip_id))

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
            extra=_extra_fields(self._clip_id, self._audio_format),
        )

        if self._broker is not None:
            try:
                self._broker.finish(self._clip_id)
            except Exception as e:  # noqa: BLE001
                log_exception("clipBrokerFinishFail", e,
                              detail=str(self._clip_id))

        return FinalizeResult(
            path=self._target,
            clip_id=self._clip_id,
            sse_url=f"/clips/{self._clip_id}/stream",
            ok=True,
        )

    def fail(self, error: str) -> None:
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
