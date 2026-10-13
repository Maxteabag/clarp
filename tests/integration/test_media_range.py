"""Byte ranges of published media are streamed from the file, never read whole.

AVPlayer probes a video with ``bytes=0-1``, fetches small ranges and opens
``bytes=N-`` ranges it cancels once it has enough (2026-10-13 stalls on a 34 MB
video). Each answer must cost a bounded amount of memory and time whatever
the file size, keep making progress for a slow reader, stop when the client
goes, and be framed exactly (RFC 9110 §14).
"""
from __future__ import annotations

import json
import pathlib
import socket
import threading
import time
import tracemalloc
import urllib.request

import pytest

from .test_media_endpoint import AUTH, server_module, running_server  # noqa: F401

SIZE = 34_322_583          # the stalled asset's size


def _video_bytes(size: int) -> bytes:
    head = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    pattern = bytes(range(256)) * 4096
    return head + (pattern * (size // len(pattern) + 1))[: size - len(head)]


class Served:
    """What the server did for one request: memory, error, its phases."""

    def __init__(self, monkeypatch):
        self.done = threading.Event()
        self.record = {}
        original = server_module.Handler._send_file
        served = self

        def wrapped(handler, *args, **kwargs):
            error = None
            try:
                return original(handler, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - recorded, re-raised
                error = exc
                raise
            finally:
                served.record = {
                    "peak": tracemalloc.get_traced_memory()[1],
                    "error": error,
                    "phases": dict(getattr(handler, "_response_phases", None) or {}),
                }
                served.done.set()

        monkeypatch.setattr(server_module.Handler, "_send_file", wrapped)

    def arm(self):
        self.done.clear()
        tracemalloc.reset_peak()

    def wait(self, timeout=30):
        assert self.done.wait(timeout), "the handler did not finish"
        return self.record


@pytest.fixture
def video(running_server, monkeypatch):
    base, _media = running_server
    blob = _video_bytes(SIZE)
    req = urllib.request.Request(base + "/media", data=blob, method="POST", headers={
        **AUTH, "Content-Type": "video/mp4", "X-Session": "lena-7b4b",
        "X-File-Name": "clip.mp4"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        asset = json.loads(resp.read())["asset"]
    tracemalloc.start()
    served = Served(monkeypatch)
    yield int(base.rsplit(":", 1)[1]), asset, served, blob
    tracemalloc.stop()


def _get(port, url, rng=None, *, read=None, slow=None, rcvbuf=None, extra=()):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.connect(("127.0.0.1", port))
    lines = [f"GET {url} HTTP/1.1", "Host: 127.0.0.1",
             f"Authorization: {AUTH['Authorization']}", "Connection: close", *extra]
    if rng is not None:
        lines.append(f"Range: {rng}")
    # Header values are latin-1 on the wire, as http.server decodes them.
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    if not head:
        return None, {}, b""                         # no response at all
    status = int(head.split(b" ")[1])
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    want = int(headers.get("content-length", "0"))
    limit = want if read is None else min(read, want)
    body = bytearray(body)
    try:
        while len(body) < limit:
            chunk = s.recv(slow[0] if slow else 262144)
            if not chunk:
                break
            body += chunk
            if slow:
                time.sleep(slow[1])
    except (ConnectionResetError, socket.timeout):
        pass
    s.close()
    return status, headers, bytes(body)


# --- memory and first byte do not depend on the file size ---------------------

@pytest.mark.parametrize("rng", ["bytes=0-1", f"bytes={SIZE // 2}-{SIZE // 2 + 114 * 1024 - 1}"])
def test_a_small_range_costs_memory_bounded_by_the_range_not_the_file(video, rng):
    port, asset, served, blob = video
    served.arm()
    status, headers, body = _get(port, asset["url"], rng)
    record = served.wait()
    lo, hi = (int(x) for x in rng.split("=")[1].split("-"))
    assert status == 206 and body == blob[lo:hi + 1]
    assert record["peak"] < 2 * 1024 * 1024          # was ~33 MB: the whole file
    assert record["phases"]["file_bytes_read"] == hi - lo + 1


def test_an_open_ended_range_cancelled_by_the_client_stops_reading(video):
    port, asset, served, blob = video
    served.arm()
    status, headers, body = _get(port, asset["url"], "bytes=1000000-", read=1_000_000)
    record = served.wait()
    assert status == 206 and body == blob[1_000_000:1_000_000 + len(body)]
    assert isinstance(record["error"], (BrokenPipeError, ConnectionResetError))
    phases = record["phases"]
    assert phases["file_bytes_read"] < 16 * 1024 * 1024   # not the 33 MB remainder
    assert phases["file_bytes_read"] - phases["response_bytes"] <= 256 * 1024
    # tracemalloc is process-wide: this test's own client holds its 1 MB read.
    assert record["peak"] < 4 * 1024 * 1024          # was ~65 MB: file plus slice


# --- a slow but progressing reader is not cut; a stalled one is reaped --------

def test_a_slow_reader_that_keeps_progressing_gets_the_whole_range(video, monkeypatch):
    port, asset, served, blob = video
    monkeypatch.setattr(server_module.Handler, "timeout", 1)
    served.arm()
    # 16 MiB at ~3 MB/s: far longer than one timeout, more than socket buffers hold.
    length = 16 * 1024 * 1024
    rng = f"bytes=1000000-{1000000 + length - 1}"
    started = time.monotonic()
    status, headers, body = _get(port, asset["url"], rng, slow=(65536, 0.02), rcvbuf=65536)
    record = served.wait()
    assert time.monotonic() - started > 2.0
    assert record["error"] is None
    assert len(body) == length and body == blob[1000000:1000000 + length]


def test_a_stalled_reader_is_still_reaped(video, monkeypatch):
    port, asset, served, blob = video
    monkeypatch.setattr(server_module.Handler, "timeout", 1)
    served.arm()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    s.connect(("127.0.0.1", port))
    s.sendall((f"GET {asset['url']} HTTP/1.1\r\nHost: x\r\nAuthorization: "
               f"{AUTH['Authorization']}\r\nRange: bytes=0-\r\n\r\n").encode())
    record = served.wait(timeout=20)                 # never reads a byte
    s.close()
    assert isinstance(record["error"], TimeoutError)
    assert record["phases"]["file_bytes_read"] < 16 * 1024 * 1024


# --- framing -------------------------------------------------------------------

@pytest.mark.parametrize("rng,start,end", [
    ("bytes=0-1", 0, 1),
    ("bytes=0-0", 0, 0),
    ("bytes=100-199", 100, 199),
    ("bytes=-500", SIZE - 500, SIZE - 1),
    ("bytes=-1", SIZE - 1, SIZE - 1),
    (f"bytes={SIZE - 1}-{SIZE - 1}", SIZE - 1, SIZE - 1),
    (f"bytes={SIZE - 10}-", SIZE - 10, SIZE - 1),
    (f"bytes=100-{SIZE * 10}", 100, SIZE - 1),      # end beyond EOF is clamped
    ("bytes=0-1,5-9", 0, 1),                         # multi-range: the first, as today
])
def test_a_satisfiable_range_is_framed_exactly(video, rng, start, end):
    port, asset, served, blob = video
    status, headers, body = _get(port, asset["url"], rng)
    assert status == 206
    assert headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert int(headers["content-length"]) == end - start + 1 == len(body)
    assert body == blob[start:end + 1]
    assert headers.get("transfer-encoding") is None


@pytest.mark.parametrize("rng", [f"bytes={SIZE}-", f"bytes={SIZE + 5}-{SIZE + 10}", "bytes=-0"])
def test_an_unsatisfiable_range_is_416(video, rng):
    port, asset, served, blob = video
    status, headers, body = _get(port, asset["url"], rng)
    assert status == 416
    assert headers["content-range"] == f"bytes */{SIZE}"
    assert body == b""


@pytest.mark.parametrize("rng", ["bytes=5-1", "bytes=abc", "bytes=-1-5", "items=0-1"])
def test_an_invalid_range_is_ignored_with_the_full_body(video, rng):
    port, asset, served, blob = video
    status, headers, body = _get(port, asset["url"], rng)
    assert status == 200
    assert "content-range" not in headers
    assert int(headers["content-length"]) == SIZE and body == blob


def test_headers_and_bytes_are_unchanged(video):
    port, asset, served, blob = video
    for rng, code in ((None, 200), ("bytes=0-1", 206)):
        status, headers, body = _get(port, asset["url"], rng)
        assert status == code
        assert headers["content-type"] == "video/mp4"
        assert headers["accept-ranges"] == "bytes"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert headers["cache-control"] == "no-store"
    assert body == blob[:2]
    status, headers, body = _get(port, asset["url"])
    assert body == blob


def test_auth_is_still_required(video):
    port, asset, served, blob = video
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"GET {asset['url']} HTTP/1.1\r\nHost: x\r\nRange: bytes=0-1\r\n"
              "Connection: close\r\n\r\n".encode())
    s.settimeout(5)
    status = int(s.recv(4096).split(b" ")[1])
    s.close()
    assert status == 401


def test_an_empty_file_has_no_satisfiable_range(video):
    port, asset, served, blob = video
    from lib import media_store
    pathlib.Path(media_store.get(asset["asset_id"])["storage_path"]).write_bytes(b"")
    status, headers, body = _get(port, asset["url"])
    assert (status, headers["content-length"], body) == (200, "0", b"")
    status, headers, body = _get(port, asset["url"], "bytes=0-1")
    assert status == 416 and headers["content-range"] == "bytes */0"



# --- F1: only ASCII digits are a range; nothing in parsing may escape ---------

@pytest.mark.parametrize("rng", [
    "bytes=\xb2-",                 # superscript two (latin-1 0xB2)
    "bytes=0-\xb9",                # superscript one
    "bytes=\xb3-\xb3",
    "bytes=0-1\xb2",
    "bytes=\u0661-\u0662",        # Arabic-Indic digits (not latin-1: sent as '?')
    "bytes=0-" + "9" * 5000,       # past int()'s 4300-digit conversion limit
    "bytes=" + "9" * 5000 + "-",
    "bytes=\xa00-1",               # no-break space
    "bytes=0\t-\x0b1",            # a vertical tab is not range whitespace
    "bytes=1_0-2_0",               # int() would take underscores
    "bytes=+1-2",
], ids=["sup2-open", "sup1-end", "sup3-both", "sup2-suffix-digit", "arabic-indic",
        "huge-end", "huge-start", "nbsp", "vtab", "underscores", "plus"])
def test_a_non_ascii_or_oversized_range_is_ignored_not_a_crash(video, rng):
    port, asset, served, blob = video
    rng = rng.encode("latin-1", "replace").decode("latin-1")
    served.arm()
    status, headers, body = _get(port, asset["url"], rng)
    record = served.wait()
    assert record["error"] is None
    assert status == 200 and "content-range" not in headers
    assert int(headers["content-length"]) == SIZE and body == blob


@pytest.mark.parametrize("rng,start,end", [
    ("bytes= 0 - 1 ", 0, 1),                     # ASCII spaces, as int() always took
    ("bytes=\t100-199", 100, 199),
])
def test_ascii_whitespace_inside_a_range_is_still_tolerated(video, rng, start, end):
    port, asset, served, blob = video
    status, headers, body = _get(port, asset["url"], rng)
    assert status == 206 and headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert body == blob[start:end + 1]


def test_a_huge_but_valid_start_is_unsatisfiable(video):
    port, asset, served, blob = video
    status, headers, body = _get(port, asset["url"], "bytes=99999999999999999999-")
    assert status == 416 and headers["content-range"] == f"bytes */{SIZE}"


# --- F2: a file that shrinks mid-stream is logged, then the connection closes --

def test_a_file_that_shrinks_mid_stream_is_logged_and_closed(video, monkeypatch):
    port, asset, served, blob = video
    from lib import media_store
    path = pathlib.Path(media_store.get(asset["asset_id"])["storage_path"])
    lines = []
    monkeypatch.setattr(server_module, "log",
                        lambda tag, msg="", *a, **k: lines.append((tag, msg)))
    original = server_module.Handler.end_headers

    def shrink_after_headers(handler):
        original(handler)
        if handler.path.startswith("/media/"):
            with open(path, "r+b") as f:             # truncated after framing
                f.truncate(1000)

    monkeypatch.setattr(server_module.Handler, "end_headers", shrink_after_headers)
    served.arm()
    status, headers, body = _get(port, asset["url"], "bytes=0-99999")
    served.wait()
    assert status == 206 and headers["content-length"] == "100000"
    assert body == blob[:1000]                       # short, then the server closed
    [short] = [m for t, m in lines if t == "fileShortRead"]
    assert str(path) in short and "expected=100000" in short and "sent=1000" in short


# --- F3: the shared path behaves the same for a non-media file ------------------

def test_a_static_file_range_uses_the_same_framing(running_server):
    base, _media = running_server
    port = int(base.rsplit(":", 1)[1])
    icon = (pathlib.Path(__file__).resolve().parents[2] / "static" / "icon.png").read_bytes()
    status, headers, body = _get(port, "/static/icon.png", "bytes=0-9")
    assert status == 206 and headers["content-range"] == f"bytes 0-9/{len(icon)}"
    assert body == icon[:10] and headers["content-type"] == "image/png"
    assert "x-content-type-options" not in headers   # only media is sandboxed
    status, headers, body = _get(port, "/static/icon.png", f"bytes={len(icon)}-")
    assert status == 416 and headers["content-range"] == f"bytes */{len(icon)}"
    status, headers, body = _get(port, "/static/icon.png", "bytes=\xb2-")
    assert status == 200 and body == icon


def test_gzipped_app_text_survives_a_malformed_range(running_server):
    """The in-memory gzip path parses ranges with the same function."""
    base, _media = running_server
    port = int(base.rsplit(":", 1)[1])
    status, headers, body = _get(port, "/static/styles.css", "bytes=\xb2-",
                                 extra=("Accept-Encoding: gzip",))
    assert status == 200 and headers.get("content-encoding") == "gzip"
