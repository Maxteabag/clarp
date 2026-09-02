"""HTTP endpoint for serving a possibly-growing audio clip.

The /audio/<file> route normally serves a static mp3, but during Phase B
the worker writes bytes to disk *while* an HTTP client may already be
requesting them — the iOS Safari fallback path opens the file via plain
HTTP because MSE doesn't support audio/mpeg. A static-file response with
Content-Length pointing at the partial size truncates playback.

This module streams a growing file over HTTP/1.1 and emits
Transfer-Encoding: chunked frames instead of WebSocket binary frames. The
detection signal is the sidecar: during synthesis it lacks the `bytes`
field; only the worker's post-EOS finalize call writes `bytes=<size>`.

Pinned by tests/integration/test_audio_pipeline_seams.py::
test_audio_endpoint_returns_full_bytes_for_in_progress_clip.
"""
from __future__ import annotations

import pathlib
import time

from . import clips as _clips
from .log import log_exception


CHUNK_SIZE = 4 * 1024
GROW_POLL_SEC = 0.05
MAX_GROW_WAIT_SEC = 30.0


def is_in_progress(mp3_path: pathlib.Path) -> bool:
    """A clip is mid-synthesis iff its sidecar exists, marks streamable,
    and has no `bytes` field yet. The worker writes the pre-synth sidecar
    BEFORE opening the mp3, then re-writes it with `bytes=<final_size>`
    after EOS — so the absence of `bytes` is the in-progress signal."""
    meta = _clips.read_sidecar(mp3_path)
    if not meta:
        return False
    if not meta.get("streamable"):
        return False
    return "bytes" not in meta


def serve_growing(handler, path: pathlib.Path) -> None:
    """Serve `path` with Transfer-Encoding: chunked, polling for new bytes
    as they're written. Closes the response when the sidecar finalizes
    (carries `bytes`) and we've drained the file, or after
    MAX_GROW_WAIT_SEC of no progress at EOF."""
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
    except OSError as e:
        log_exception("audioGrowingHeaderFail", e, detail=path.name)
        return

    try:
        _stream_chunked(handler, path)
    except (BrokenPipeError, ConnectionResetError):
        return
    except OSError as e:
        log_exception("audioGrowingStreamFail", e, detail=path.name)


def _stream_chunked(handler, path: pathlib.Path) -> None:
    """Read `path` to EOF, emit HTTP chunks; wait at EOF for the sidecar
    to finalize before sending the terminator. Yields the same byte
    sequence the file ultimately contains."""
    with path.open("rb") as f:
        eof_started_at: float | None = None
        while True:
            chunk = f.read(CHUNK_SIZE)
            if chunk:
                eof_started_at = None
                _write_chunk(handler, chunk)
                continue
            # EOF — decide whether more bytes are coming.
            if _is_finalized(path):
                # One last sweep in case the finalize raced with our read.
                tail = f.read()
                if tail:
                    _write_chunk(handler, tail)
                break
            if eof_started_at is None:
                eof_started_at = time.time()
            elif time.time() - eof_started_at > MAX_GROW_WAIT_SEC:
                break
            time.sleep(GROW_POLL_SEC)
    # Terminator: zero-length chunk + trailing CRLF.
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


def _is_finalized(path: pathlib.Path) -> bool:
    """Inverse of is_in_progress, plus the no-sidecar case (legacy static
    clips should be treated as already finalized so the loop terminates).
    """
    meta = _clips.read_sidecar(path)
    if not meta:
        return True
    return "bytes" in meta
