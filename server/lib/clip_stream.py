"""Clip-id keyed live audio stream broker.

The TTS worker owns producer state now: it creates the clips row before the
first ElevenLabs byte, publishes the SSE event directly, and appends chunks to
this broker as they arrive. Files remain a replay/cache artifact, not the
event source.

The HTTP handler in this module is the iOS-fallback path. iOS Safari's
`<audio>` element does NOT support MSE for audio/mpeg, so it always reaches
`/clips/<id>/stream` over plain HTTP. Two things iOS does that we have to
handle (the trace of cur=0.000001 was the symptom of getting these wrong):

  1. Range probing: iOS sends `GET /clips/N/stream` with `Range: bytes=0-1`
     to discover the file size BEFORE the real fetch. If we answer that
     with chunked Transfer-Encoding and no Content-Length, iOS parks the
     `<audio>` element in a "loaded but won't decode" state, currentTime
     never advances, and the 30-second safety cap fires.

  2. Sometimes a second GET with `Range: bytes=N-` for the actual fetch
     (5-10ms after the probe). It expects 206 Partial Content with
     Content-Range and Content-Length.

So the dispatcher below picks:
  * complete clip + a file on disk → serve as a regular static file with
    full Range support (whether or not the client sent a Range header)
  * still streaming + Range header → 206 + Content-Range with unknown
    total ("bytes N-M/*"), buffer bytes from the broker until we have
    the requested slice
  * still streaming + no Range → original chunked TE live stream
"""
from __future__ import annotations

import pathlib
import threading
import time
from dataclasses import dataclass, field

from .log import log_exception


CHUNK_WAIT_SEC = 30.0


@dataclass
class _ClipStream:
    chunks: list[bytes] = field(default_factory=list)
    finished: bool = False
    failed: bool = False
    error: str = ""
    cond: threading.Condition = field(default_factory=threading.Condition)
    created_at: float = field(default_factory=time.time)


class ClipStreamBroker:
    """In-memory fan-out for currently-streaming clips.

    Late clients receive all buffered chunks from the start, then block for
    more. Once a stream is finished, it stays briefly in memory; older clips
    fall back to the saved file path from SQLite.
    """

    RETAIN_SEC = 300.0

    def __init__(self):
        self._streams: dict[int, _ClipStream] = {}
        self._lock = threading.Lock()

    def open(self, clip_id: int) -> None:
        with self._lock:
            self._prune_locked()
            self._streams.setdefault(int(clip_id), _ClipStream())

    def append(self, clip_id: int, chunk: bytes) -> None:
        if not chunk:
            return
        s = self._get_or_open(clip_id)
        with s.cond:
            if not s.finished and not s.failed:
                s.chunks.append(bytes(chunk))
                s.cond.notify_all()

    def finish(self, clip_id: int) -> None:
        s = self._get_or_open(clip_id)
        with s.cond:
            s.finished = True
            s.cond.notify_all()

    def fail(self, clip_id: int, error: str = "") -> None:
        s = self._get_or_open(clip_id)
        with s.cond:
            s.failed = True
            s.error = error
            s.cond.notify_all()

    def get(self, clip_id: int) -> _ClipStream | None:
        with self._lock:
            self._prune_locked()
            return self._streams.get(int(clip_id))

    def _get_or_open(self, clip_id: int) -> _ClipStream:
        with self._lock:
            self._prune_locked()
            return self._streams.setdefault(int(clip_id), _ClipStream())

    def _prune_locked(self) -> None:
        cutoff = time.time() - self.RETAIN_SEC
        dead = [
            cid for cid, stream in self._streams.items()
            if (stream.finished or stream.failed) and stream.created_at < cutoff
        ]
        for cid in dead:
            self._streams.pop(cid, None)


# ---- HTTP serving ---------------------------------------------------------


def serve_clip_stream(handler, broker: ClipStreamBroker,
                      clip_id: int, audio_dir: pathlib.Path) -> None:
    """Dispatch `/clips/<clip_id>/stream` to the right response shape.

    See module docstring for the iOS Safari motivation. The branching is:
      - complete clip + file on disk → static-with-Range (the common case
        once synthesis has finished)
      - live stream + Range header   → live-Range (iOS probing or seeking
        mid-synthesis)
      - live stream + no Range       → chunked TE (back-compat for clients
        that don't send Range, like the desktop streaming-player)
    """
    range_spec = _parse_range_header(handler.headers.get("Range", ""))
    stream = broker.get(clip_id)
    file_path = _path_for_clip(clip_id, audio_dir)

    # COMPLETE-FILE PATH. Producer finished and we have an mp3 on disk —
    # this is the most common case by the time iOS hits us, because
    # synthesis is typically over before the user's `<audio>` element gets
    # around to fetching the URL. Static serve with proper Range gives iOS
    # the Content-Length + 206 it expects.
    if file_path and file_path.is_file() and _is_producer_complete(clip_id):
        return _serve_static_with_range(handler, file_path, range_spec)

    # LIVE-RANGE PATH. The producer is still streaming AND the client sent
    # Range. iOS sends Range: bytes=0-1 to probe even on a fresh fetch, so
    # this path triggers a lot. Serve 206 + Content-Range: bytes N-M/*
    # (total unknown) — iOS understands this shape.
    if stream is not None and range_spec is not None:
        return _serve_live_range(handler, stream, range_spec, clip_id)

    # LIVE-CHUNKED PATH. Original behavior for clients that don't send
    # Range (the in-house WS streaming-player on desktop falls here).
    if stream is not None:
        return _serve_live_chunked(handler, stream, clip_id)

    # FALLBACK. Producer is gone from memory AND the file isn't on disk.
    if file_path and file_path.is_file():
        return _serve_static_with_range(handler, file_path, range_spec)
    return _send_http_error(handler, 404, "no such clip")


# ---- response variants ----------------------------------------------------


def _serve_static_with_range(handler, path: pathlib.Path,
                             range_spec: "tuple[int, int | None] | None") -> None:
    """Static-file serve with optional Range. 200 if no Range; 206 + slice
    if Range was provided. Pinned by the sim-e2e iOS probe test."""
    try:
        total = path.stat().st_size
    except OSError as e:
        log_exception("clipStreamStatFail", e, detail=str(path))
        return _send_http_error(handler, 500, "stat failed")

    if range_spec is None:
        # No Range — plain 200 with Content-Length.
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", _content_type_for_clip(path))
            handler.send_header("Content-Length", str(total))
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            with path.open("rb") as f:
                _copy_fd(f, handler.wfile)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as e:
            log_exception("clipStreamStaticFail", e, detail=str(path))
        return

    # Range serve. Clamp to file size, return 416 if the spec is past EOF.
    start, end = range_spec
    if end is None or end >= total:
        end = total - 1
    if start >= total or start < 0 or start > end:
        try:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{total}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        except OSError:
            pass
        return

    length = end - start + 1
    try:
        handler.send_response(206)
        handler.send_header("Content-Type", _content_type_for_clip(path))
        handler.send_header("Content-Length", str(length))
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                buf = f.read(min(64 * 1024, remaining))
                if not buf:
                    break
                handler.wfile.write(buf)
                remaining -= len(buf)
    except (BrokenPipeError, ConnectionResetError):
        return
    except OSError as e:
        log_exception("clipStreamRangeFail", e, detail=str(path))


def _serve_live_range(handler, stream: _ClipStream,
                      range_spec: "tuple[int, int | None]",
                      clip_id: int) -> None:
    """Range request against an in-progress stream.

    Block until the requested byte range is available in the broker, then
    serve 206 with `Content-Range: bytes N-M/*` (total unknown because
    the producer hasn't finished). The "*/" form is HTTP/1.1-legal and is
    what we need for "I'm giving you a slice of an in-progress resource"
    — see RFC 7233 §4.2.

    iOS Safari's first probe is typically `Range: bytes=0-1`. We can
    answer that almost instantly because the broker has the first chunk
    very quickly; the second iOS fetch is usually `Range: bytes=N-` (open
    ended) which we satisfy by waiting for `finished` then computing the
    real total."""
    start, end = range_spec

    def _ready_for(target_end: int | None) -> bool:
        with stream.cond:
            available = sum(len(c) for c in stream.chunks)
            if stream.finished or stream.failed:
                return True
            return target_end is not None and available > target_end

    # Bounded wait for the requested slice.
    deadline = time.time() + CHUNK_WAIT_SEC
    while not _ready_for(end):
        with stream.cond:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            stream.cond.wait(timeout=min(0.5, remaining))

    with stream.cond:
        available_bytes = b"".join(stream.chunks)
        is_finished = stream.finished or stream.failed

    total = len(available_bytes) if is_finished else None

    if start >= len(available_bytes):
        # Asked past what we have — 416 with what we know.
        try:
            handler.send_response(416)
            handler.send_header("Content-Range",
                                f"bytes */{total if total is not None else '*'}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        except OSError:
            pass
        return

    # Effective end of the slice.
    if end is None or end >= len(available_bytes):
        end = len(available_bytes) - 1

    slice_bytes = available_bytes[start:end + 1]
    length = len(slice_bytes)
    total_str = str(total) if total is not None else "*"

    try:
        handler.send_response(206)
        handler.send_header("Content-Type", _content_type_for_live_clip(clip_id))
        handler.send_header("Content-Length", str(length))
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total_str}")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(slice_bytes)
    except (BrokenPipeError, ConnectionResetError):
        return
    except OSError as e:
        log_exception("clipStreamLiveRangeFail", e, detail=str(clip_id))


def _serve_live_chunked(handler, stream: _ClipStream, clip_id: int,
                        content_type: str | None = None) -> None:
    """Original live-stream behavior: chunked Transfer-Encoding."""
    try:
        handler.send_response(200)
        handler.send_header(
            "Content-Type",
            content_type or _content_type_for_live_clip(clip_id),
        )
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
    except OSError as e:
        log_exception("clipStreamHeaderFail", e, detail=str(clip_id))
        return

    try:
        _write_live_chunks(handler, stream)
    except (BrokenPipeError, ConnectionResetError):
        return
    except OSError as e:
        log_exception("clipStreamWriteFail", e, detail=str(clip_id))


def _write_live_chunks(handler, stream: _ClipStream) -> None:
    idx = 0
    wait_started: float | None = None
    while True:
        with stream.cond:
            while idx >= len(stream.chunks) and not stream.finished and not stream.failed:
                if wait_started is None:
                    wait_started = time.time()
                remaining = CHUNK_WAIT_SEC - (time.time() - wait_started)
                if remaining <= 0:
                    stream.failed = True
                    stream.error = "stream wait timeout"
                    break
                stream.cond.wait(timeout=min(0.5, remaining))
            if idx < len(stream.chunks):
                chunk = stream.chunks[idx]
                idx += 1
                wait_started = None
            elif stream.finished or stream.failed:
                break
            else:
                continue
        _write_chunk(handler, chunk)
    try:
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except OSError:
        pass


def _write_chunk(handler, data: bytes) -> None:
    handler.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
    handler.wfile.write(data)
    handler.wfile.write(b"\r\n")
    handler.wfile.flush()


def _copy_fd(src, dst, chunk_size: int = 64 * 1024) -> None:
    while True:
        buf = src.read(chunk_size)
        if not buf:
            break
        dst.write(buf)


# ---- helpers --------------------------------------------------------------


def _content_type_for_clip(path: pathlib.Path) -> str:
    if path.suffix.lower() == ".pcm":
        return "application/octet-stream"
    return "audio/mpeg"


def _content_type_for_live_clip(clip_id: int) -> str:
    path = _path_for_clip(clip_id, pathlib.Path("/"))
    if path is not None:
        return _content_type_for_clip(path)
    return "audio/mpeg"


def _parse_range_header(value: str) -> "tuple[int, int | None] | None":
    """Parse a single-byte-range request like `bytes=0-1` or `bytes=42-`.

    Returns (start, end) where end is inclusive, or None if the header is
    absent / multi-range / malformed. We only support a single range —
    HTML5 `<audio>` never sends multi-range requests, and supporting them
    is a multipart/byteranges minefield we don't need to walk into.
    """
    value = (value or "").strip()
    if not value or not value.lower().startswith("bytes="):
        return None
    spec = value[6:].strip()
    if "," in spec:
        return None        # multi-range — not supported
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    start_s = start_s.strip()
    end_s = end_s.strip()
    try:
        if not start_s:
            # `bytes=-N` (last N bytes) — rare for audio, treat as not-
            # supported so we don't have to wait for total to be known.
            return None
        start = int(start_s)
        end: int | None = int(end_s) if end_s else None
    except ValueError:
        return None
    if start < 0 or (end is not None and end < start):
        return None
    return (start, end)


def _is_producer_complete(clip_id: int) -> bool:
    """SQLite check: has the worker marked this clip's producer_status
    COMPLETE? Used to decide between static-file Range serving (safe,
    total known) and live-Range serving (Content-Range with /*).
    """
    try:
        from . import db
        row = db.conn().execute(
            "SELECT producer_status FROM clips WHERE clip_id = ?",
            (int(clip_id),),
        ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    ps = row["producer_status"] if "producer_status" in row.keys() else None
    return ps in ("complete", "failed")


def _path_for_clip(clip_id: int, audio_dir: pathlib.Path) -> pathlib.Path | None:
    try:
        from . import db
        row = db.conn().execute(
            "SELECT path FROM clips WHERE clip_id = ?",
            (clip_id,),
        ).fetchone()
    except Exception:
        return None
    if not row or not row["path"]:
        return None
    raw = str(row["path"])
    if raw.startswith("/audio/"):
        return (audio_dir / raw.rsplit("/", 1)[-1]).resolve()
    return pathlib.Path(raw).resolve()


def _send_http_error(handler, code: int, message: str) -> None:
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(message)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(message.encode("utf-8"))
    except OSError as e:
        log_exception("clipStreamErrorReplyFail", e, detail=str(code))
