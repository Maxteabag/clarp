"""Producer-side pacing of one clip's audio chunks.

A stall the client hears can start on the server: the TTS provider paused
between chunks, so the stream the browser was playing ran dry. The client's
fault record alone cannot tell that apart from a bad network. This tracks the
arrival rhythm of chunks during one synthesis and reports any gap long enough
to be audible, plus a per-clip summary the client's `audioClipSummary` can be
laid next to.

Pure: the caller supplies the clock and the sink, so the worker's tests never
need real time or a real event log.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

# Below this the browser's own buffer absorbs the pause; above it the user
# hears the stream hesitate on a live (chunked) delivery.
DEFAULT_GAP_THRESHOLD_MS = 700

GAP_EVENT = "synthChunkGap"
SUMMARY_EVENT = "synthPacing"


@dataclass
class Pacing:
    clip_id: int | None
    delivery: str
    threshold_ms: int = DEFAULT_GAP_THRESHOLD_MS
    started_at: float = 0.0
    last_at: float = 0.0
    chunks: int = 0
    bytes: int = 0
    max_gap_ms: int = 0
    gaps_over: int = 0
    first_chunk_ms: int | None = None
    gap_ms_list: list[int] = field(default_factory=list, repr=False)


def track(feed: Callable[[int, bytes], None], *,
          clip_id: int | None,
          delivery: str,
          emit: Callable[..., None],
          now: Callable[[], float] = time.monotonic,
          threshold_ms: int = DEFAULT_GAP_THRESHOLD_MS,
          **event_kw) -> tuple[Callable[[int, bytes], None], Callable[[str], dict]]:
    """Wrap a delivery session's `feed`.

    Returns `(feed, finish)`. `feed` has the same signature as the original
    and always forwards to it, even if bookkeeping fails. `finish(outcome)`
    emits the summary and returns it; call it once after finalize or fail.
    """
    pacing = Pacing(clip_id=clip_id, delivery=delivery, threshold_ms=threshold_ms)
    pacing.started_at = now()

    def wrapped(chunk_idx: int, chunk: bytes) -> None:
        try:
            _note(pacing, chunk, now(), emit, event_kw)
        except Exception:  # noqa: BLE001 - never let diagnostics break audio
            pass
        feed(chunk_idx, chunk)

    def finish(outcome: str) -> dict:
        summary = summarize(pacing, now(), outcome)
        try:
            emit("tts_worker", SUMMARY_EVENT, clip_id=clip_id,
                 duration_ms=summary["total_ms"], detail=summary, **event_kw)
        except Exception:  # noqa: BLE001
            pass
        return summary

    return wrapped, finish


def _note(p: Pacing, chunk: bytes, t: float, emit, event_kw) -> None:
    if p.chunks == 0:
        p.first_chunk_ms = int((t - p.started_at) * 1000)
    else:
        gap_ms = int((t - p.last_at) * 1000)
        p.gap_ms_list.append(gap_ms)
        if gap_ms > p.max_gap_ms:
            p.max_gap_ms = gap_ms
        if gap_ms >= p.threshold_ms:
            p.gaps_over += 1
            emit("tts_worker", GAP_EVENT, clip_id=p.clip_id,
                 duration_ms=gap_ms,
                 detail={"clip_id": p.clip_id, "delivery": p.delivery,
                         "gap_ms": gap_ms, "chunk_idx": p.chunks,
                         "bytes_so_far": p.bytes,
                         "since_start_ms": int((t - p.started_at) * 1000)},
                 **event_kw)
    p.chunks += 1
    p.bytes += len(chunk)
    p.last_at = t


def summarize(p: Pacing, t: float, outcome: str) -> dict:
    gaps = p.gap_ms_list
    return {
        "clip_id": p.clip_id,
        "delivery": p.delivery,
        "outcome": outcome,
        "chunks": p.chunks,
        "bytes": p.bytes,
        "total_ms": int((t - p.started_at) * 1000),
        "first_chunk_ms": p.first_chunk_ms,
        "max_gap_ms": p.max_gap_ms,
        "mean_gap_ms": int(sum(gaps) / len(gaps)) if gaps else None,
        "gaps_over_threshold": p.gaps_over,
        "threshold_ms": p.threshold_ms,
    }
