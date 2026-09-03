"""Producer-side chunk pacing: gaps the user would hear become events."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import clip_pacing  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _setup(threshold_ms=700):
    clock = Clock()
    fed, events = [], []

    def feed(i, c):
        fed.append((i, c))

    def emit(source, event, **kw):
        events.append((source, event, kw))

    wrapped, finish = clip_pacing.track(
        feed, clip_id=9, delivery="chunked-file", emit=emit, now=clock,
        threshold_ms=threshold_ms, trace_id="t1")
    return clock, wrapped, finish, fed, events


def test_steady_chunks_produce_only_a_summary():
    clock, wrapped, finish, fed, events = _setup()
    for i in range(4):
        clock.t += 0.2
        wrapped(i, b"x" * 100)
    clock.t += 0.1
    summary = finish("complete")
    assert [f[0] for f in fed] == [0, 1, 2, 3]
    assert [e[1] for e in events] == [clip_pacing.SUMMARY_EVENT]
    assert summary == {
        "clip_id": 9, "delivery": "chunked-file", "outcome": "complete",
        "chunks": 4, "bytes": 400, "total_ms": 900, "first_chunk_ms": 200,
        "max_gap_ms": 200, "mean_gap_ms": 200, "gaps_over_threshold": 0,
        "threshold_ms": 700,
    }
    assert events[0][2]["trace_id"] == "t1"
    assert events[0][2]["clip_id"] == 9
    assert events[0][2]["duration_ms"] == 900


def test_a_gap_over_the_threshold_is_reported_where_it_happened():
    clock, wrapped, finish, fed, events = _setup()
    clock.t += 0.3
    wrapped(0, b"a" * 10)
    clock.t += 0.2
    wrapped(1, b"b" * 10)
    clock.t += 1.5
    wrapped(2, b"c" * 10)
    gaps = [e for e in events if e[1] == clip_pacing.GAP_EVENT]
    assert len(gaps) == 1
    detail = gaps[0][2]["detail"]
    assert detail == {"clip_id": 9, "delivery": "chunked-file", "gap_ms": 1500,
                      "chunk_idx": 2, "bytes_so_far": 20, "since_start_ms": 2000}
    assert gaps[0][2]["duration_ms"] == 1500
    summary = finish("complete")
    assert summary["max_gap_ms"] == 1500
    assert summary["gaps_over_threshold"] == 1


def test_bookkeeping_failure_still_forwards_the_chunk():
    fed = []

    def feed(i, c):
        fed.append(i)

    def emit(*a, **kw):
        raise RuntimeError("log down")

    clock = Clock()
    wrapped, finish = clip_pacing.track(
        feed, clip_id=1, delivery="hls", emit=emit, now=clock, threshold_ms=1)
    wrapped(0, b"x")
    clock.t += 5
    wrapped(1, b"y")   # gap event raises inside emit
    assert fed == [0, 1]
    assert finish("failed")["outcome"] == "failed"


def test_no_chunks_summarizes_honestly():
    clock, wrapped, finish, fed, events = _setup()
    clock.t += 2
    summary = finish("failed")
    assert summary["chunks"] == 0
    assert summary["first_chunk_ms"] is None
    assert summary["mean_gap_ms"] is None
    assert summary["total_ms"] == 2000
