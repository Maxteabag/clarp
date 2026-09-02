"""Lightweight VAD test harness — core library.

Everything runs at 16 kHz mono. Noise is synthesized (no multi-GB datasets);
speech clips are supplied as WAVs (e.g. ElevenLabs TTS). Because we place a
known clean clip at a known offset in a known noise bed at a controlled SNR,
the speech region is ground-truth-by-construction — no forced alignment.
"""
from __future__ import annotations

import numpy as np
import soundfile as sf

SR = 16000


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------
def load_mono16k(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:
        # simple linear resample — fine for VAD-grade audio
        n = int(round(len(audio) * SR / sr))
        audio = np.interp(np.linspace(0, len(audio), n, endpoint=False),
                          np.arange(len(audio)), audio).astype(np.float32)
    return audio


def write_wav(path: str, audio: np.ndarray) -> None:
    sf.write(path, np.clip(audio, -1.0, 1.0), SR, subtype="PCM_16")


# --------------------------------------------------------------------------
# Synthetic noise beds (a few KB each, generated on demand)
# --------------------------------------------------------------------------
def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _normalize(x: np.ndarray, rms: float = 0.1) -> np.ndarray:
    cur = np.sqrt(np.mean(x ** 2)) or 1.0
    return (x * (rms / cur)).astype(np.float32)


def synth_noise(kind: str, dur_s: float, seed: int = 0) -> np.ndarray:
    n = int(dur_s * SR)
    r = _rng(seed)
    if kind == "white":
        x = r.standard_normal(n)
    elif kind in ("pink", "brown"):
        # shape white noise spectrum by 1/f (pink) or 1/f^2 (brown)
        white = r.standard_normal(n)
        spec = np.fft.rfft(white)
        f = np.fft.rfftfreq(n, 1 / SR)
        f[0] = f[1] if len(f) > 1 else 1.0
        exp = 1.0 if kind == "pink" else 2.0
        spec = spec / (f ** (exp / 2.0))
        x = np.fft.irfft(spec, n)
    elif kind == "hum":
        t = np.arange(n) / SR
        x = (np.sin(2 * np.pi * 50 * t) + 0.5 * np.sin(2 * np.pi * 150 * t)
             + 0.3 * np.sin(2 * np.pi * 250 * t))
        x += 0.05 * r.standard_normal(n)            # a little hiss
    elif kind == "hvac":
        # low rumble (brown-ish) + steady broadband hiss
        white = r.standard_normal(n)
        spec = np.fft.rfft(white)
        f = np.fft.rfftfreq(n, 1 / SR); f[0] = 1.0
        rumble = np.fft.irfft(spec / (f ** 1.0), n)
        x = 0.7 * rumble + 0.3 * r.standard_normal(n)
    elif kind == "babble":
        # crude multi-talker babble: sum of band-limited, amplitude-modulated
        # noise streams — broadband with speech-like temporal structure, the
        # classic VAD false-trigger trap.
        x = np.zeros(n)
        for k in range(6):
            stream = r.standard_normal(n)
            spec = np.fft.rfft(stream)
            f = np.fft.rfftfreq(n, 1 / SR)
            band = (f > 200) & (f < 3500)
            spec[~band] *= 0.05
            stream = np.fft.irfft(spec, n)
            t = np.arange(n) / SR
            mod = 0.5 + 0.5 * np.sin(2 * np.pi * r.uniform(2, 6) * t + r.uniform(0, 6))
            x += stream * mod
    else:
        raise ValueError(f"unknown noise kind: {kind}")
    return _normalize(x)


# --------------------------------------------------------------------------
# SNR mixing — ground truth by construction
# --------------------------------------------------------------------------
def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)) + 1e-12)


def speech_extent(clean: np.ndarray, frame_ms: int = 20, rel_db: float = -30.0):
    """Find the actual [onset,offset] seconds of speech in a clean clip — TTS
    pads/trims silence inconsistently, so file length is not the truth."""
    hop = int(frame_ms / 1000 * SR)
    peak = rms(clean)
    thr = peak * (10 ** (rel_db / 20.0))
    # i is a sample index (range steps by hop), so seconds = i / SR.
    voiced = [i for i in range(0, len(clean) - hop, hop)
              if rms(clean[i:i + hop]) > thr]
    if not voiced:
        return (0.0, len(clean) / SR)
    return (voiced[0] / SR, (voiced[-1] + hop) / SR)


def place_in_bed(speech: np.ndarray, noise: np.ndarray, *, onset_s: float,
                 total_s: float, snr_db: float, gain_db: float = 0.0):
    """Place `speech` at `onset_s` inside a `total_s` noise bed at `snr_db`.
    Returns (mix, ref_onset_s, ref_offset_s). Ground truth uses the clean
    clip's true speech extent, not its padded file length."""
    total = int(total_s * SR)
    bed = np.resize(noise, total).astype(np.float32).copy()
    onset = int(onset_s * SR)
    seg = speech[: max(0, total - onset)]

    # scale noise to hit target SNR relative to the speech
    s_rms, n_rms = rms(seg), rms(bed)
    bed *= (s_rms / (10 ** (snr_db / 20.0))) / n_rms

    mix = bed
    mix[onset:onset + len(seg)] += seg
    mix *= 10 ** (gain_db / 20.0)                 # absolute-volume stress
    peak = np.max(np.abs(mix))
    if peak > 1.0:
        mix /= peak                               # avoid clip
    e_on, e_off = speech_extent(seg)
    return mix.astype(np.float32), onset_s + e_on, onset_s + e_off


# --------------------------------------------------------------------------
# Endpointing state machine (shared) — segments from a per-hop speech flag
# --------------------------------------------------------------------------
def endpoint(flags: list[bool], hop_s: float, *, start_ms=250, end_ms=600,
             pad_ms=200):
    """Turn per-hop speech booleans into [start,end] segments with start
    debounce + silence hangover + onset pre-roll padding."""
    segs = []
    in_speech = False
    run = 0
    start_i = 0
    need_start = max(1, int(start_ms / 1000 / hop_s))
    need_end = max(1, int(end_ms / 1000 / hop_s))
    for i, f in enumerate(flags):
        if not in_speech:
            if f:
                run += 1
                if run >= need_start:
                    in_speech = True
                    start_i = i - run + 1
                    run = 0
            else:
                run = 0
        else:
            if not f:
                run += 1
                if run >= need_end:
                    segs.append((start_i, i - run + 1))
                    in_speech = False
                    run = 0
            else:
                run = 0
    if in_speech:
        segs.append((start_i, len(flags)))
    pad = pad_ms / 1000
    return [(max(0.0, s * hop_s - pad), e * hop_s) for s, e in segs]


# --------------------------------------------------------------------------
# Detectors → list of (start_s, end_s)
# --------------------------------------------------------------------------
class Silero:
    """Canonical Silero VAD via the `silero-vad` package. `get_speech_timestamps`
    handles the v5 state/context internally; its params ARE the endpointing knobs
    we'd port to the on-device FluidAudio/CoreML config:
      threshold, min_speech_duration_ms, min_silence_duration_ms, speech_pad_ms.
    """
    def __init__(self, model_path: str | None = None, *, threshold: float = 0.5,
                 min_speech_ms: int = 250, min_silence_ms: int = 600,
                 speech_pad_ms: int = 200):
        from silero_vad import load_silero_vad, get_speech_timestamps
        self.model = load_silero_vad(onnx=False)
        self._gst = get_speech_timestamps
        self.kw = dict(threshold=threshold, min_speech_duration_ms=min_speech_ms,
                       min_silence_duration_ms=min_silence_ms,
                       speech_pad_ms=speech_pad_ms, sampling_rate=SR,
                       return_seconds=True)

    def segments(self, audio: np.ndarray):
        ts = self._gst(audio, self.model, **self.kw)
        return [(float(t["start"]), float(t["end"])) for t in ts]


class WebRTC:
    def __init__(self, mode: int = 2):
        import webrtcvad
        self.vad = webrtcvad.Vad(mode)

    def segments(self, audio: np.ndarray):
        frame_ms = 30
        win = int(frame_ms / 1000 * SR)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        flags = []
        for i in range(0, len(pcm) - win + 1, win):
            flags.append(self.vad.is_speech(pcm[i:i + win].tobytes(), SR))
        return endpoint(flags, win / SR)


class EnergyBaseline:
    """Approximates our current iOS detector: 300-3400 Hz band energy with a
    flat-spectrum (noise) gate, fixed-ish thresholds. Included to show how the
    hand-rolled approach compares."""
    def __init__(self, on_db: float = -45.0, off_db: float = -52.0):
        self.on_db, self.off_db = on_db, off_db

    def _energy(self, frame: np.ndarray) -> float:
        w = np.hanning(len(frame))
        mag = np.abs(np.fft.rfft(frame * w))
        f = np.fft.rfftfreq(len(frame), 1 / SR)
        band = (f >= 300) & (f <= 3400)
        b = mag[band]
        if b.size == 0 or b.mean() <= 0:
            return -120.0
        if b.max() / (b.mean() + 1e-9) < 1.25:        # flat → noise
            return -120.0
        return 20 * np.log10(b.mean() / len(frame) + 1e-9)

    def segments(self, audio: np.ndarray):
        win = 512
        flags = [self._energy(audio[i:i + win]) >= self.on_db
                 for i in range(0, len(audio) - win + 1, win)]
        return endpoint(flags, win / SR)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _mask(segs, dur_s, frame_s=0.01):
    n = int(dur_s / frame_s)
    m = np.zeros(n, dtype=bool)
    for s, e in segs:
        m[max(0, int(s / frame_s)):min(n, int(e / frame_s))] = True
    return m


def score(ref_on, ref_off, hyp_segs, dur_s):
    ref = _mask([(ref_on, ref_off)], dur_s)
    hyp = _mask(hyp_segs, dur_s)
    tp = int(np.sum(ref & hyp)); fp = int(np.sum(~ref & hyp))
    fn = int(np.sum(ref & ~hyp)); tn = int(np.sum(~ref & ~hyp))
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    fa_rate = fp / (fp + tn) if fp + tn else 0.0
    # endpointing latency (s): + = late, - = early/clipped
    if hyp_segs:
        onset_lat = hyp_segs[0][0] - ref_on
        offset_lat = hyp_segs[-1][1] - ref_off
    else:
        onset_lat = offset_lat = float("nan")
    return dict(precision=prec, recall=rec, f1=f1, false_alarm=fa_rate,
                n_segments=len(hyp_segs), onset_lat=onset_lat,
                offset_lat=offset_lat, detected=bool(hyp_segs))
