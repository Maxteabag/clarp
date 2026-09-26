"""Short sound cues for Oracle v2 calls, synthesised on the Host.

Each cue is 24 kHz mono PCM16, the format GPT-Live streams and the phone
already plays from ``session.output_audio.delta``, so no client change is
needed to hear them. They are sine tones with a soft attack and release,
120-400 ms long, peaking near -18 dBFS (the tick and the bell sit lower).
Nothing is stored on disk; each cue is generated once and cached.

Cues:
  switch_started   rising two-note chime: the Host is switching the call
  connected        bright chime, pitched per agent: an agent is on the line
  back_to_oracle   falling two-note chime: the user is back with Oracle
  switch_failed    low double tone: the switch did not happen
  handed_off       very soft tick: work was handed to an agent
  result           soft bell: an agent's result is about to be read out
"""
from __future__ import annotations

import array
import functools
import hashlib
import math

RATE = 24000
PEAK = 10 ** (-18 / 20)          # -18 dBFS
FADE_SECONDS = 0.012
CUES = ("switch_started", "connected", "back_to_oracle", "switch_failed", "handed_off", "result")
# Major pentatonic from C5: an agent's "connected" chime picks one as its root.
_TINTS = (523.25, 587.33, 659.25, 783.99, 880.00)


def _tone(freq, seconds, *, level=1.0, decay=None, partials=((1, 1.0),)):
    """Samples in -1..1: a tone with a raised-cosine fade in and out, or an
    exponential ``decay`` (time constant, seconds) after the attack."""
    count = int(RATE * seconds)
    fade = max(1, int(RATE * FADE_SECONDS))
    norm = sum(weight for _, weight in partials)
    out = []
    for i in range(count):
        t = i / RATE
        value = sum(weight * math.sin(2 * math.pi * freq * mult * t) for mult, weight in partials) / norm
        envelope = 1.0
        if i < fade:
            envelope = 0.5 - 0.5 * math.cos(math.pi * i / fade)
        if count - i <= fade:
            envelope *= 0.5 - 0.5 * math.cos(math.pi * (count - i) / fade)
        if decay:
            envelope *= math.exp(-t / decay)
        out.append(value * envelope * level)
    return out


def _silence(seconds):
    return [0.0] * int(RATE * seconds)


def _pcm(samples):
    scale = 32767 * PEAK
    return array.array("h", (int(round(max(-1.0, min(1.0, s)) * scale)) for s in samples)).tobytes()


_SOFT = ((1, 1.0), (2, 0.18))
_BELL = ((1, 1.0), (2.76, 0.35), (5.4, 0.12))


def tint(persona):
    """The root frequency of an agent's "connected" chime."""
    key = str(persona or "").strip().casefold()
    return _TINTS[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(_TINTS)] if key else _TINTS[2]


@functools.lru_cache(maxsize=64)
def pcm(name, persona=None):
    """PCM16 bytes for cue ``name``; ``persona`` tints the "connected" chime."""
    if name == "switch_started":
        samples = (_tone(659.25, 0.12, partials=_SOFT) + _silence(0.03)
                   + _tone(880.00, 0.15, partials=_SOFT))
    elif name == "connected":
        root = tint(persona)
        samples = (_tone(root, 0.09, partials=_SOFT) + _tone(root * 1.5, 0.22, decay=0.12, partials=_BELL))
    elif name == "back_to_oracle":
        samples = (_tone(880.00, 0.12, partials=_SOFT) + _silence(0.03)
                   + _tone(659.25, 0.15, partials=_SOFT))
    elif name == "switch_failed":
        samples = (_tone(220.00, 0.12, partials=((1, 1.0), (3, 0.25))) + _silence(0.06)
                   + _tone(196.00, 0.14, partials=((1, 1.0), (3, 0.25))))
    elif name == "handed_off":
        samples = _tone(1318.5, 0.12, level=0.35, decay=0.025)
    elif name == "result":
        samples = _tone(1046.5, 0.40, level=0.7, decay=0.14, partials=_BELL)
    else:
        raise ValueError("unknown Oracle cue: " + str(name))
    return _pcm(samples)
