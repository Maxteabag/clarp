import math
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import audio_metrics  # noqa: E402
from lib.audio_metrics import PcmStreamAnalyzer  # noqa: E402

SR = 44100


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def f32(samples):
    return struct.pack(f"<{len(samples)}f", *samples)


def s16(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def tone(seconds, amp=0.5, hz=440.0, sr=SR):
    n = int(seconds * sr)
    return [amp * math.sin(2 * math.pi * hz * i / sr) for i in range(n)]


def test_clean_tone_has_no_anomalies_and_sane_levels():
    clock = Clock()
    events = []
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR, clock=clock,
                          on_anomaly=events.append)
    clock.advance(0.120)   # ttfb
    for _ in range(10):
        assert a.feed(f32(tone(0.1))) == []
        clock.advance(0.05)   # producer runs 2x real time
    assert a.finish() == []
    s = a.summary()
    assert events == []
    assert s["anomalies"] == {}
    assert s["ttfb_ms"] == 120
    assert s["audio_ms"] == 1000
    assert s["chunks"] == 10
    assert s["bytes"] == 10 * int(0.1 * SR) * 4
    assert 0.49 < s["peak"] <= 0.5
    assert abs(s["rms"] - 0.5 / math.sqrt(2)) < 0.01
    assert abs(s["dc_offset"]) < 0.01
    assert s["clipped_samples"] == 0 and s["non_finite_samples"] == 0
    assert s["realtime_factor"] > 1.5
    assert s["max_producer_deficit_ms"] <= 0
    assert s["trailing_bytes"] == 0


def test_int16_wire_is_scaled_to_full_scale():
    a = PcmStreamAnalyzer(encoding="pcm_s16le", sample_rate=24000)
    a.feed(s16([0, 16384, -16384, 0]))
    assert a.summary()["peak"] == 0.5
    assert a.summary()["audio_ms"] == 0   # 4 samples < 1ms


def test_chunk_boundary_split_frames_are_reassembled():
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR)
    raw = f32(tone(0.05))
    a.feed(raw[:7])
    a.feed(raw[7:])
    assert a.finish() == []
    assert a.sample_count == int(0.05 * SR)


def test_partial_trailing_frame_is_reported_at_finish():
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR)
    a.feed(f32(tone(0.05)) + b"\x00\x01\x02")
    kinds = [e["kind"] for e in a.finish()]
    assert kinds == ["partial_frame"]
    assert a.summary()["trailing_bytes"] == 3


def test_clipping_and_non_finite_samples():
    events = []
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR, on_anomaly=events.append)
    a.feed(f32([0.0, 1.0, -1.0, 0.9999, float("nan"), float("inf"), 0.1]))
    kinds = sorted(e["kind"] for e in events)
    assert "clipping" in kinds and "non_finite" in kinds
    s = a.summary()
    assert s["clipped_samples"] == 3
    assert s["non_finite_samples"] == 2
    assert s["anomalies"] == {"clipping": 1, "non_finite": 1, "discontinuity": 1}


def test_discontinuity_across_chunk_border():
    events = []
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR, on_anomaly=events.append)
    a.feed(f32([0.4] * 100))
    a.feed(f32([-0.4] * 100))          # 0.8 jump exactly at the border
    assert [e["kind"] for e in events] == ["discontinuity"]
    assert events[0]["jump"] == 0.8
    assert events[0]["at_ms"] == int(100 * 1000 / SR)
    assert a.summary()["max_adjacent_jump"] == 0.8


def test_silence_gap_mid_speech_but_not_leading_silence():
    events = []
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR, on_anomaly=events.append)
    a.feed(f32([0.0] * int(0.5 * SR)))          # leading silence: normal
    a.feed(f32(tone(0.2)))
    a.feed(f32([0.0] * int(0.3 * SR)))          # 300ms hole in the middle
    a.feed(f32(tone(0.2)))
    a.feed(f32([0.0] * int(0.4 * SR)))          # trailing silence: normal
    a.finish()
    assert [e["kind"] for e in events] == ["silence_gap"]
    assert 290 <= events[0]["gap_ms"] <= 310
    assert 690 <= events[0]["at_ms"] <= 1010
    s = a.summary()
    assert s["silence_gaps"] == 1
    assert 290 <= s["longest_silence_ms"] <= 310


def test_all_silent_and_dc_offset_at_finish():
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR)
    a.feed(f32([0.0] * SR))
    assert [e["kind"] for e in a.finish()] == ["all_silent"]
    b = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR)
    b.feed(f32([0.3 + x for x in tone(0.2, amp=0.1)]))
    assert [e["kind"] for e in b.finish()] == ["dc_offset"]


def test_chunk_gap_and_slow_producer_timing():
    clock = Clock()
    events = []
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR, clock=clock,
                          on_anomaly=events.append)
    a.feed(f32(tone(0.1)))
    clock.advance(0.9)                       # 900ms with no bytes
    a.feed(f32(tone(0.1)))                   # 200ms of audio in 900ms wall
    kinds = [e["kind"] for e in events]
    assert kinds == ["chunk_gap", "slow_producer"]
    assert events[0]["gap_ms"] == 900
    assert events[1]["deficit_ms"] == 700
    s = a.summary()
    assert s["max_chunk_gap_ms"] == 900
    assert s["chunk_gaps_over"] == 1
    assert s["max_producer_deficit_ms"] == 700
    assert s["realtime_factor"] < 1
    # slow_producer is reported once per clip, not per chunk.
    clock.advance(0.9)
    a.feed(f32(tone(0.1)))
    assert [e["kind"] for e in events].count("slow_producer") == 1


def test_anomaly_events_are_rate_limited_but_still_counted():
    events = []
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR,
                          on_anomaly=events.append, max_anomaly_events=3)
    for _ in range(8):
        a.feed(f32([1.0, 1.0]))              # clipping every chunk
    assert len(events) == 3
    s = a.summary()
    assert s["anomalies"]["clipping"] == 8
    assert s["anomaly_events_suppressed"] == 5


def test_anomaly_callback_errors_never_escape():
    def boom(_):
        raise RuntimeError("logger down")
    a = PcmStreamAnalyzer(encoding="pcm_f32le", sample_rate=SR, on_anomaly=boom)
    a.feed(f32([1.0]))
    assert a.summary()["anomalies"] == {"clipping": 1}


def test_encoded_streams_get_timing_only():
    clock = Clock()
    events = []
    a = PcmStreamAnalyzer(encoding="mp3", sample_rate=SR, clock=clock,
                          on_anomaly=events.append)
    assert a.raw is False
    a.feed(b"\xff\xfb" * 500)
    clock.advance(0.4)
    a.feed(b"\xff\xfb" * 500)
    assert [e["kind"] for e in events] == ["chunk_gap"]
    s = a.summary()
    assert s["chunks"] == 2 and s["bytes"] == 2000
    assert "peak" not in s and "audio_ms" not in s
    assert a.finish() == []


def test_thresholds_are_exported_for_the_client_to_mirror():
    assert audio_metrics.SILENCE_GAP_MS == 200
    assert audio_metrics.DISCONTINUITY_JUMP == 0.5
