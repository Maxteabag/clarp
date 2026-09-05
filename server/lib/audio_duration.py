"""How long an uploaded clip actually is, before anything transcribes it.

Routing long recordings to a stronger model has to happen *before* inference,
so the only signal available is the audio itself. The processing time the STT
classes return is unrelated and must not be confused with this.

Three tiers, cheapest first: a WAV header is pure arithmetic, a container
header (MP4/M4A, and WebM when the muxer wrote a duration) is one open, and
only a live-recorded stream with no duration in its header falls through to
counting decoded packets.
"""
from __future__ import annotations

import io
import wave

from .log import log_exception

# MediaRecorder WebM often carries no duration, so the fallback decode is not
# an edge case. It stays bounded by the caller's upload cap.
_UNKNOWN = 0.0


def seconds(audio_bytes: bytes) -> float:
    """Duration of `audio_bytes`, or 0.0 when it cannot be determined.

    Never raises: an undetermined duration means "do not route", which is the
    safe answer — a failed probe must not stop someone being transcribed.
    """
    if not audio_bytes:
        return _UNKNOWN
    wav = _wav_seconds(audio_bytes)
    if wav > 0:
        return wav
    try:
        return _container_seconds(audio_bytes)
    except Exception as exc:  # noqa: BLE001 - probing never blocks STT
        log_exception("audioDurationProbeFail", exc)
        return _UNKNOWN


def _wav_seconds(audio_bytes: bytes) -> float:
    if not (audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE"):
        return _UNKNOWN
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as source:
            rate = source.getframerate()
            if rate <= 0:
                return _UNKNOWN
            return source.getnframes() / float(rate)
    except (EOFError, wave.Error):
        return _UNKNOWN


def _container_seconds(audio_bytes: bytes) -> float:
    import av  # type: ignore

    with av.open(io.BytesIO(audio_bytes), mode="r") as container:
        stream = next(
            (candidate for candidate in container.streams
             if candidate.type == "audio"), None)
        if stream is None:
            return _UNKNOWN
        if container.duration:
            return float(container.duration) / av.time_base
        if stream.duration and stream.time_base:
            return float(stream.duration * stream.time_base)
        # Live-recorded WebM: no duration was written, so count what decodes.
        samples = 0
        rate = int(getattr(stream, "rate", 0) or 0)
        for frame in container.decode(stream):
            samples += int(getattr(frame, "samples", 0) or 0)
        if rate <= 0 or samples <= 0:
            return _UNKNOWN
        return samples / float(rate)
