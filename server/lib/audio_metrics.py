"""Level and integrity metrics for one uploaded utterance.

Decodes the capture the client sent to /transcribe (WAV straight from the
header, anything else through PyAV like the STT path does) and answers the
questions a voice-timeline investigation asks: how loud was it, did it clip,
how much of it was silence, and did it decode at all. Numbers are dBFS; a
digital-silence file reads -120.
"""
from __future__ import annotations

import io
import math
import wave

import numpy as np

SILENCE_FLOOR_DB = -50.0
CLIP_LEVEL = 0.985
FRAME_MS = 20
# Flags that mark a capture as suspect. Thresholds are deliberately loose:
# the timeline wants to catch the obviously broken upload, not judge a
# quiet speaker.
QUIET_PEAK_DB = -40.0
CLIP_RATIO_LIMIT = 0.01
SILENT_RATIO_LIMIT = 0.97
MIN_SPEECH_MS = 150
DC_OFFSET_LIMIT = 0.1


def _db(x: float) -> float:
    return 20.0 * math.log10(x) if x > 1e-6 else -120.0


def _decode_wav(audio: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(audio), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    if channels > 1:
        usable = samples.size - samples.size % channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return samples, rate


def _decode_av(audio: bytes) -> tuple[np.ndarray, int]:
    import av  # type: ignore
    from av.audio.resampler import AudioResampler  # type: ignore

    chunks: list[np.ndarray] = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio stream")
        resampler = AudioResampler(format="s16", layout="mono", rate=16_000)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(np.frombuffer(
                    bytes(converted.planes[0])[:converted.samples * 2], dtype="<i2"))
        for converted in resampler.resample(None):
            chunks.append(np.frombuffer(
                bytes(converted.planes[0])[:converted.samples * 2], dtype="<i2"))
    if not chunks:
        return np.zeros(0, dtype=np.float32), 16_000
    return np.concatenate(chunks).astype(np.float32) / 32768.0, 16_000


def decode(audio: bytes, content_type: str = "") -> tuple[np.ndarray, int]:
    """Mono float32 samples in [-1, 1] plus sample rate."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return _decode_wav(audio)
    return _decode_av(audio)


def analyze(audio: bytes, content_type: str = "") -> dict:
    """Metrics for one capture. Never raises; decode failures are a result."""
    out: dict = {"bytes": len(audio), "content_type": content_type or ""}
    try:
        samples, rate = decode(audio, content_type)
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        out["decode_error"] = str(exc)[:200]
        return out
    n = int(samples.size)
    out["sample_rate"] = int(rate)
    out["samples"] = n
    if n == 0 or rate <= 0:
        out["decode_error"] = "no samples"
        out["duration_ms"] = 0.0
        return out
    out["duration_ms"] = round(n / rate * 1000.0, 1)
    absolute = np.abs(samples)
    out["peak_db"] = round(_db(float(absolute.max())), 1)
    out["rms_db"] = round(_db(float(np.sqrt(np.mean(np.square(samples))))), 1)
    out["clip_ratio"] = round(float(np.mean(absolute >= CLIP_LEVEL)), 4)
    out["dc_offset"] = round(float(np.mean(samples)), 4)
    frame = max(1, int(rate * FRAME_MS / 1000))
    frames = n // frame
    if frames:
        block = samples[: frames * frame].reshape(frames, frame)
        frame_rms = np.sqrt(np.mean(np.square(block), axis=1))
        silent = frame_rms < 10 ** (SILENCE_FLOOR_DB / 20.0)
        out["silence_ratio"] = round(float(np.mean(silent)), 3)
        loud = np.flatnonzero(~silent)
        if loud.size:
            out["leading_silence_ms"] = int(loud[0]) * FRAME_MS
            out["trailing_silence_ms"] = int(frames - 1 - loud[-1]) * FRAME_MS
        else:
            out["leading_silence_ms"] = frames * FRAME_MS
            out["trailing_silence_ms"] = frames * FRAME_MS
    else:
        out["silence_ratio"] = 1.0
        out["leading_silence_ms"] = out["trailing_silence_ms"] = 0
    return out


def corruption_reasons(metrics: dict, *, transcript: str | None = None) -> list[str]:
    """Why a capture looks broken. Empty when it looks fine."""
    reasons: list[str] = []
    if metrics.get("decode_error"):
        return ["decode_error"]
    duration = float(metrics.get("duration_ms") or 0.0)
    silent_text = not (transcript or "").strip()
    if duration < MIN_SPEECH_MS:
        reasons.append("too_short")
    if float(metrics.get("clip_ratio") or 0.0) > CLIP_RATIO_LIMIT:
        reasons.append("clipping")
    if float(metrics.get("peak_db", 0.0)) < QUIET_PEAK_DB:
        reasons.append("too_quiet")
    if float(metrics.get("silence_ratio") or 0.0) > SILENT_RATIO_LIMIT and silent_text:
        reasons.append("silent_upload")
    if abs(float(metrics.get("dc_offset") or 0.0)) > DC_OFFSET_LIMIT:
        reasons.append("dc_offset")
    return reasons
