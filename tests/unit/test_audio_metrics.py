"""Level and integrity metrics on synthetic captures."""
from __future__ import annotations

import io
import math
import pathlib
import struct
import sys
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib import audio_metrics  # noqa: E402

RATE = 16_000


def _wav(samples: list[float], rate: int = RATE, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for s in samples:
            v = max(-1.0, min(1.0, s))
            frames += struct.pack("<h", int(v * 32767)) * channels
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _sine(seconds: float, amplitude: float, hz: float = 220.0) -> list[float]:
    n = int(seconds * RATE)
    return [amplitude * math.sin(2 * math.pi * hz * i / RATE) for i in range(n)]


def test_sine_reports_expected_rms_and_peak():
    m = audio_metrics.analyze(_wav(_sine(1.0, 0.5)), "audio/wav")
    assert "decode_error" not in m
    assert m["sample_rate"] == RATE and m["samples"] == RATE
    assert m["duration_ms"] == 1000.0
    assert abs(m["peak_db"] - (-6.0)) < 0.2
    assert abs(m["rms_db"] - (-9.0)) < 0.3  # sine RMS = peak - 3 dB
    assert m["clip_ratio"] == 0.0
    assert m["silence_ratio"] == 0.0
    assert m["leading_silence_ms"] == 0 and m["trailing_silence_ms"] == 0
    assert audio_metrics.corruption_reasons(m, transcript="hi") == []


def test_silence_around_speech_is_measured():
    samples = [0.0] * int(0.3 * RATE) + _sine(0.5, 0.4) + [0.0] * int(0.2 * RATE)
    m = audio_metrics.analyze(_wav(samples), "audio/wav")
    assert abs(m["leading_silence_ms"] - 300) <= 20
    assert abs(m["trailing_silence_ms"] - 200) <= 20
    assert 0.45 <= m["silence_ratio"] <= 0.55


def test_digital_silence_with_empty_transcript_is_a_silent_upload():
    m = audio_metrics.analyze(_wav([0.0] * RATE), "audio/wav")
    assert m["peak_db"] == -120.0 and m["silence_ratio"] == 1.0
    reasons = audio_metrics.corruption_reasons(m, transcript="")
    assert "silent_upload" in reasons and "too_quiet" in reasons


def test_clipped_square_wave_is_flagged():
    samples = [1.0 if (i // 40) % 2 == 0 else -1.0 for i in range(RATE)]
    m = audio_metrics.analyze(_wav(samples), "audio/wav")
    assert m["clip_ratio"] > 0.9
    assert "clipping" in audio_metrics.corruption_reasons(m, transcript="loud")


def test_too_short_and_dc_offset_are_flagged():
    m = audio_metrics.analyze(_wav([0.5] * int(0.05 * RATE)), "audio/wav")
    reasons = audio_metrics.corruption_reasons(m, transcript="x")
    assert "too_short" in reasons and "dc_offset" in reasons


def test_stereo_wav_is_folded_to_mono():
    m = audio_metrics.analyze(_wav(_sine(0.5, 0.5), channels=2), "audio/wav")
    assert abs(m["peak_db"] - (-6.0)) < 0.2


def test_garbage_bytes_are_a_decode_error_not_an_exception():
    m = audio_metrics.analyze(b"\xff\xfb" * 300, "audio/webm")
    assert m["bytes"] == 600
    assert m["decode_error"]
    assert audio_metrics.corruption_reasons(m) == ["decode_error"]


def test_empty_body_is_a_decode_error():
    m = audio_metrics.analyze(b"", "audio/wav")
    assert m["decode_error"]
