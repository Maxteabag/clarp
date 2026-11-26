"""Streaming audio corruption diagnostics for the TTS delivery path.

`PcmStreamAnalyzer` sits on the producer side of a clip (the delivery
session's `feed`) and inspects every chunk as it arrives, so when a user
reports "the audio was garbled" we already have, per clip:

* signal level: peak, RMS, DC offset, clipped and non-finite sample counts
* continuity: the largest sample-to-sample jump (also across chunk borders),
  silence gaps in the middle of speech, a trailing partial frame
* latency: time to first chunk, the largest gap between chunks, how far the
  producer ran behind real time (a slow producer starves the client)

Anomalies are reported through `on_anomaly` as they happen (rate-limited per
clip so a broken stream can't flood the eventlog) and everything is rolled up
by `summary()` at finalize. Raw PCM streams get the full treatment; encoded
streams (mp3/aac) get the timing half only.

Pure Python on purpose: the worker thread already decodes base64 per chunk,
and one pass over 44.1k samples/s costs a few tens of ms per second of audio.
"""
from __future__ import annotations

import math
import sys
import time
from array import array
from typing import Callable

RAW_ENCODINGS = {"pcm_f32le": ("f", 4, 1.0), "pcm_s16le": ("h", 2, 32768.0)}

# Detection thresholds (full-scale float units / milliseconds).
CLIP_LEVEL = 0.999
DISCONTINUITY_JUMP = 0.5
SILENCE_LEVEL = 1e-4
SILENCE_GAP_MS = 200
CHUNK_GAP_MS = 250
SLOW_PRODUCER_DEFICIT_MS = 500
DC_OFFSET_LEVEL = 0.1
DEFAULT_MAX_ANOMALY_EVENTS = 12


class PcmStreamAnalyzer:
    def __init__(self, *, encoding: str, sample_rate: int, channels: int = 1,
                 clock: Callable[[], float] = time.monotonic,
                 on_anomaly: Callable[[dict], None] | None = None,
                 max_anomaly_events: int = DEFAULT_MAX_ANOMALY_EVENTS):
        self.encoding = str(encoding or "")
        self.sample_rate = max(1, int(sample_rate or 0))
        self.channels = max(1, int(channels or 1))
        self._clock = clock
        self._on_anomaly = on_anomaly
        self._max_events = max(0, int(max_anomaly_events))
        spec = RAW_ENCODINGS.get(self.encoding)
        self._typecode, self._bytes_per_sample, self._scale = spec or ("", 0, 1.0)
        self.raw = spec is not None

        self.created_at = self._clock()
        self.first_chunk_at: float | None = None
        self.last_chunk_at: float | None = None
        self.chunk_count = 0
        self.byte_count = 0
        self.max_chunk_gap_ms = 0
        self.chunk_gaps_over = 0
        self.max_deficit_ms = 0

        self._carry = b""
        self.sample_count = 0
        self.peak = 0.0
        self._sumsq = 0.0
        self._sum = 0.0
        self.clipped = 0
        self.non_finite = 0
        self.max_jump = 0.0
        self._last_sample: float | None = None
        self._heard_signal = False
        self._silent_run = 0
        self._silence_reported = False
        self.silence_gaps = 0
        self.longest_silence_ms = 0

        self.anomaly_counts: dict[str, int] = {}
        self._emitted = 0
        self.suppressed = 0

    # ---- feeding -----------------------------------------------------------

    def feed(self, chunk: bytes) -> list[dict]:
        now = self._clock()
        found: list[dict] = []
        if self.first_chunk_at is None:
            self.first_chunk_at = now
        elif self.last_chunk_at is not None:
            gap_ms = int((now - self.last_chunk_at) * 1000)
            if gap_ms > self.max_chunk_gap_ms:
                self.max_chunk_gap_ms = gap_ms
            if gap_ms > CHUNK_GAP_MS:
                self.chunk_gaps_over += 1
                found.append(self._anomaly("chunk_gap", gap_ms=gap_ms))
        self.last_chunk_at = now
        self.chunk_count += 1
        self.byte_count += len(chunk)

        if self.raw and chunk:
            found.extend(self._analyze_samples(chunk))

        # Producer pace: audio delivered so far vs wall clock since the first
        # chunk. A negative margin means the client (which plays at real time
        # after a ~250ms pre-buffer) will run dry.
        if self.raw and self.first_chunk_at is not None:
            wall_ms = int((now - self.first_chunk_at) * 1000)
            deficit = wall_ms - self.audio_ms
            if deficit > self.max_deficit_ms:
                self.max_deficit_ms = deficit
            if (deficit > SLOW_PRODUCER_DEFICIT_MS
                    and self.anomaly_counts.get("slow_producer", 0) == 0):
                found.append(self._anomaly(
                    "slow_producer", deficit_ms=deficit,
                    audio_ms=self.audio_ms, wall_ms=wall_ms))
        return found

    def _analyze_samples(self, chunk: bytes) -> list[dict]:
        found: list[dict] = []
        data = self._carry + chunk if self._carry else chunk
        usable = len(data) - (len(data) % self._bytes_per_sample)
        self._carry = data[usable:]
        if usable <= 0:
            return found
        samples = array(self._typecode)
        samples.frombytes(data[:usable])
        if sys.byteorder != "little":
            samples.byteswap()

        scale = self._scale
        last = self._last_sample
        peak = self.peak
        sumsq = 0.0
        total = 0.0
        clipped = 0
        non_finite = 0
        max_jump = self.max_jump
        silent_run = self._silent_run
        heard = self._heard_signal
        rate_ms = self.sample_rate * self.channels / 1000.0
        chunk_start_sample = self.sample_count
        first_jump_at: int | None = None
        for index, raw_value in enumerate(samples):
            value = raw_value / scale
            if not math.isfinite(value):
                non_finite += 1
                continue
            mag = abs(value)
            if mag > peak:
                peak = mag
            if mag >= CLIP_LEVEL:
                clipped += 1
            sumsq += value * value
            total += value
            if last is not None:
                jump = abs(value - last)
                if jump > max_jump:
                    max_jump = jump
                if jump > DISCONTINUITY_JUMP and first_jump_at is None:
                    first_jump_at = chunk_start_sample + index
            last = value
            if mag < SILENCE_LEVEL:
                silent_run += 1
            else:
                if heard and silent_run:
                    run_ms = int(silent_run / rate_ms)
                    if run_ms > self.longest_silence_ms:
                        self.longest_silence_ms = run_ms
                    if run_ms >= SILENCE_GAP_MS:
                        self.silence_gaps += 1
                        found.append(self._anomaly(
                            "silence_gap", gap_ms=run_ms,
                            at_ms=int((chunk_start_sample + index) / rate_ms)))
                silent_run = 0
                heard = True

        self.sample_count += len(samples)
        self.peak = peak
        self._sumsq += sumsq
        self._sum += total
        self.max_jump = max_jump
        self._last_sample = last
        self._silent_run = silent_run
        self._heard_signal = heard
        if clipped:
            self.clipped += clipped
            found.append(self._anomaly("clipping", samples=clipped,
                                       at_ms=int(chunk_start_sample / rate_ms)))
        if non_finite:
            self.non_finite += non_finite
            found.append(self._anomaly("non_finite", samples=non_finite,
                                       at_ms=int(chunk_start_sample / rate_ms)))
        if first_jump_at is not None:
            found.append(self._anomaly("discontinuity", jump=round(max_jump, 4),
                                       at_ms=int(first_jump_at / rate_ms)))
        return found

    def _anomaly(self, kind: str, **fields) -> dict:
        self.anomaly_counts[kind] = self.anomaly_counts.get(kind, 0) + 1
        event = {"kind": kind, "chunk": self.chunk_count,
                 "bytes": self.byte_count, "audio_ms": self.audio_ms, **fields}
        if self._on_anomaly is not None:
            if self._emitted < self._max_events:
                self._emitted += 1
                try:
                    self._on_anomaly(event)
                except Exception:  # noqa: BLE001 — diagnostics never break delivery
                    pass
            else:
                self.suppressed += 1
        return event

    # ---- rollup ------------------------------------------------------------

    @property
    def audio_ms(self) -> int:
        if not self.raw:
            return 0
        return int(self.sample_count * 1000 / (self.sample_rate * self.channels))

    @property
    def rms(self) -> float:
        finite = self.sample_count - self.non_finite
        return math.sqrt(self._sumsq / finite) if finite > 0 else 0.0

    @property
    def dc_offset(self) -> float:
        finite = self.sample_count - self.non_finite
        return self._sum / finite if finite > 0 else 0.0

    def finish(self) -> list[dict]:
        """Call once at EOS: end-of-stream checks that can't fire mid-stream."""
        found: list[dict] = []
        if self.raw and self._carry:
            found.append(self._anomaly("partial_frame", trailing_bytes=len(self._carry)))
        if self.raw and self.sample_count and abs(self.dc_offset) > DC_OFFSET_LEVEL:
            found.append(self._anomaly("dc_offset", offset=round(self.dc_offset, 4)))
        if self.raw and self.sample_count and not self._heard_signal:
            found.append(self._anomaly("all_silent", audio_ms=self.audio_ms))
        return found

    def summary(self) -> dict:
        now = self._clock()
        ttfb_ms = (int((self.first_chunk_at - self.created_at) * 1000)
                   if self.first_chunk_at is not None else None)
        wall_ms = (int((self.last_chunk_at - self.first_chunk_at) * 1000)
                   if self.first_chunk_at is not None and self.last_chunk_at is not None
                   else None)
        out = {
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "chunks": self.chunk_count,
            "bytes": self.byte_count,
            "ttfb_ms": ttfb_ms,
            "produce_wall_ms": wall_ms,
            "max_chunk_gap_ms": self.max_chunk_gap_ms,
            "chunk_gaps_over_ms": CHUNK_GAP_MS,
            "chunk_gaps_over": self.chunk_gaps_over,
            "elapsed_ms": int((now - self.created_at) * 1000),
            "anomalies": dict(self.anomaly_counts),
            "anomaly_events_suppressed": self.suppressed,
        }
        if self.raw:
            audio_ms = self.audio_ms
            out.update({
                "audio_ms": audio_ms,
                "realtime_factor": (round(audio_ms / wall_ms, 3)
                                    if wall_ms and audio_ms else None),
                "max_producer_deficit_ms": self.max_deficit_ms,
                "peak": round(self.peak, 6),
                "rms": round(self.rms, 6),
                "dc_offset": round(self.dc_offset, 6),
                "clipped_samples": self.clipped,
                "non_finite_samples": self.non_finite,
                "max_adjacent_jump": round(self.max_jump, 6),
                "silence_gaps": self.silence_gaps,
                "longest_silence_ms": self.longest_silence_ms,
                "trailing_bytes": len(self._carry),
            })
        return out
