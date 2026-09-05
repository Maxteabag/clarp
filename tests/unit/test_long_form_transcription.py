"""Long recordings escalate to a stronger transcription model.

The decision happens before inference, so it rests entirely on the probed
audio duration. The cases that matter are the ones where the probe cannot
tell: escalating on a guess would silently bill short clips to a paid
provider.
"""
from __future__ import annotations

import io
import wave

import pytest

from lib import audio_duration, stt_providers


def _wav(seconds: float, rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def test_wav_duration_is_read_from_the_header():
    assert audio_duration.seconds(_wav(2.0)) == pytest.approx(2.0, abs=0.01)
    assert audio_duration.seconds(_wav(0.25)) == pytest.approx(0.25, abs=0.01)


def test_undecodable_audio_reports_unknown_rather_than_raising():
    assert audio_duration.seconds(b"") == 0.0
    assert audio_duration.seconds(b"not audio at all") == 0.0
    # A RIFF header with a truncated body must not raise either.
    assert audio_duration.seconds(_wav(1.0)[:20]) == 0.0


def test_no_escalation_configured_means_no_escalation():
    stt_providers.update_settings({"long_form_model": ""})
    assert stt_providers.long_form_model_for(600.0) == ""


def test_clip_at_or_over_the_threshold_escalates():
    stt_providers.update_settings({
        "long_form_model": "deepgram:nova-3",
        "long_form_threshold_sec": 30,
    })
    assert stt_providers.long_form_model_for(30.0) == "deepgram:nova-3"
    assert stt_providers.long_form_model_for(120.0) == "deepgram:nova-3"


def test_short_clip_is_left_alone():
    stt_providers.update_settings({
        "long_form_model": "deepgram:nova-3",
        "long_form_threshold_sec": 30,
    })
    assert stt_providers.long_form_model_for(29.9) == ""


def test_unknown_duration_never_escalates():
    """0.0 means the probe failed, not "a zero-length clip"."""
    stt_providers.update_settings({
        "long_form_model": "deepgram:nova-3",
        "long_form_threshold_sec": 30,
    })
    assert stt_providers.long_form_model_for(0.0) == ""
    assert stt_providers.long_form_model_for(-1.0) == ""


def test_threshold_outside_the_supported_range_is_rejected():
    with pytest.raises(ValueError):
        stt_providers.update_settings({"long_form_threshold_sec": 0})
    with pytest.raises(ValueError):
        stt_providers.update_settings({"long_form_threshold_sec": 100_000})
    with pytest.raises(ValueError):
        stt_providers.update_settings({"long_form_threshold_sec": "30"})


def test_unknown_cloud_model_is_rejected_but_local_ids_pass():
    with pytest.raises(ValueError):
        stt_providers.update_settings({"long_form_model": "deepgram:not-a-model"})
    stt_providers.update_settings({"long_form_model": "faster-whisper:large-v3"})
    assert stt_providers.long_form_model() == "faster-whisper:large-v3"


def test_status_exposes_the_setting_for_the_client():
    stt_providers.update_settings({
        "long_form_model": "deepgram:nova-3",
        "long_form_threshold_sec": 45,
    })
    value = stt_providers.status()
    assert value["long_form_model"] == "deepgram:nova-3"
    assert value["long_form_threshold_sec"] == 45
    assert value["long_form_threshold_range"] == {
        "min": stt_providers.MIN_LONG_FORM_THRESHOLD_SEC,
        "max": stt_providers.MAX_LONG_FORM_THRESHOLD_SEC,
    }
