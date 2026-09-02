"""VAD edge-case regression tests (host, needs the harness venv: `pytest`).

Guards the failure modes that bit us on-device:
  * silence / breaths / steady noise must NOT trigger speech (false triggers
    were pausing the agent's playback);
  * a too-short blip must not start an utterance;
  * real speech must still be detected (no over-correction).

Run from vad-harness/ with the venv active:  python -m pytest test_vad_edge.py -q
"""
import glob
import os

import numpy as np
import pytest

import vadlib as V

MODEL = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
SPEECH = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "corpus", "speech", "*.wav")))


def _vad():
    return V.Silero(MODEL)


@pytest.mark.parametrize("kind", ["white", "pink", "hvac", "babble", "hum"])
def test_noise_only_does_not_trigger(kind):
    # 6s of pure noise (no speech) must produce no speech segments.
    noise = V.synth_noise(kind, 6.0, seed=11)
    segs = _vad().segments(noise)
    assert segs == [], f"{kind} noise falsely triggered VAD: {segs}"


def test_silence_does_not_trigger():
    silence = np.zeros(int(6.0 * V.SR), dtype=np.float32)
    assert _vad().segments(silence) == [], "pure silence triggered VAD"


def test_short_blip_does_not_trigger():
    # a 60ms tone burst inside silence — below min-speech, must not start.
    audio = np.zeros(int(3.0 * V.SR), dtype=np.float32)
    t = np.arange(int(0.06 * V.SR)) / V.SR
    blip = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    audio[V.SR : V.SR + len(blip)] += blip
    assert _vad().segments(audio) == [], "a 60ms blip falsely started an utterance"


@pytest.mark.skipif(not SPEECH, reason="no speech clips in corpus/")
def test_real_speech_is_detected():
    # sanity: don't over-correct — a clean speech clip must still be detected.
    speech = V.load_mono16k(SPEECH[0])
    assert _vad().segments(speech), f"real speech not detected in {SPEECH[0]}"


@pytest.mark.skipif(not SPEECH, reason="no speech clips in corpus/")
def test_speech_in_moderate_noise_is_detected():
    speech = V.load_mono16k(SPEECH[0])
    mix, ron, roff = V.place_in_bed(
        speech, V.synth_noise("babble", 6.0, seed=5),
        onset_s=1.5, total_s=6.0, snr_db=10)
    segs = _vad().segments(mix)
    assert any(e > ron and s < roff for s, e in segs), \
        f"speech at {ron:.1f}-{roff:.1f}s missed in 10dB noise: {segs}"
