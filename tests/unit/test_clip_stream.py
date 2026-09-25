"""Characterization tests for lib.clip_stream (clip broker + iOS Range serving).

Pins the Range header parser, the in-memory broker lifecycle and pruning,
and the dispatcher's four response shapes: static-with-Range for complete
files, 206 `bytes N-M/*` for live streams, chunked TE without Range, and
404. The HTTP handler is a fake that records status and headers.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from lib import clip_stream as cs, db


# ---- _parse_range_header -----------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("bytes=0-1", (0, 1)),
    ("bytes=42-", (42, None)),
    ("BYTES=5-9", (5, 9)),
    ("  bytes=0-0 ", (0, 0)),
    ("bytes= 3 - 7 ", (3, 7)),
    ("", None), (None, None),
    ("items=0-1", None),
    ("bytes=0-1,5-9", None),     # multi-range unsupported
    ("bytes=-500", None),        # suffix range unsupported
    ("bytes=5", None),
    ("bytes=a-b", None),
    ("bytes=9-5", None),
    ("bytes=-1-5", None),
])
def test_parse_range_header(value, expected):
    assert cs._parse_range_header(value) == expected


# ---- broker -----------------------------------------------------------------


def test_broker_open_append_finish_lifecycle():
    broker = cs.ClipStreamBroker()
    assert broker.get(1) is None
    broker.open(1)
    stream = broker.get(1)
    assert stream is not None and stream.chunks == [] and not stream.finished
    broker.append(1, b"")             # ignored
    broker.append(1, bytearray(b"ab"))
    broker.append(1, b"cd")
    assert stream.chunks == [b"ab", b"cd"]
    assert isinstance(stream.chunks[0], bytes)
    broker.finish(1)
    broker.append(1, b"late")
    assert stream.finished is True and stream.chunks == [b"ab", b"cd"]


def test_broker_append_without_open_creates_stream_and_fail_records_error():
    broker = cs.ClipStreamBroker()
    broker.append(7, b"x")
    broker.fail(7, "provider 500")
    stream = broker.get(7)
    assert stream.failed is True and stream.error == "provider 500"
    broker.append(7, b"more")
    assert stream.chunks == [b"x"]


def test_broker_open_is_idempotent_and_keys_are_ints():
    broker = cs.ClipStreamBroker()
    broker.open("3")
    broker.append(3, b"a")
    broker.open(3)
    assert broker.get("3").chunks == [b"a"]


def test_broker_prunes_only_finished_or_failed_streams_older_than_retain():
    broker = cs.ClipStreamBroker()
    broker.open(1); broker.finish(1)
    broker.open(2); broker.fail(2)
    broker.open(3)                       # still live
    broker.open(5); broker.finish(5)     # finished but recent
    # `created_at` is stamped by a dataclass default_factory bound to the real
    # clock, so age the streams directly instead of patching time.
    old = cs.time.time() - broker.RETAIN_SEC - 1
    for cid in (1, 2, 3):
        broker.get(cid).created_at = old
    broker.open(4)                       # any access prunes
    assert broker.get(1) is None and broker.get(2) is None
    assert broker.get(3) is not None     # live streams are never pruned
    assert broker.get(4) is not None and broker.get(5) is not None


# ---- HTTP dispatcher ---------------------------------------------------------


class FakeHandler:
    def __init__(self, range_header: str | None = None):
        self.headers = {} if range_header is None else {"Range": range_header}
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers: dict[str, str] = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass


def _insert_clip(path: str, producer_status: str | None) -> int:
    cur = db.conn().execute(
        "INSERT INTO clips (agent_id, path, created_at, producer_status) VALUES (?, ?, ?, ?)",
        ("agent-1", path, 1, producer_status))
    return int(cur.lastrowid)


@pytest.fixture
def audio_dir(tmp_path):
    d = tmp_path / "audio"
    d.mkdir()
    return d


def _complete_clip(audio_dir, data=b"0123456789", suffix=".mp3", status="complete"):
    path = audio_dir / f"clip{suffix}"
    path.write_bytes(data)
    return _insert_clip(f"/audio/clip{suffix}", status)


def test_no_row_is_404(audio_dir):
    handler = FakeHandler()
    cs.serve_clip_stream(handler, cs.ClipStreamBroker(), 999, audio_dir)
    assert handler.status == 404 and handler.wfile.getvalue() == b"no such clip"


def test_complete_file_without_range_is_plain_200(audio_dir):
    clip_id = _complete_clip(audio_dir)
    handler = FakeHandler()
    cs.serve_clip_stream(handler, cs.ClipStreamBroker(), clip_id, audio_dir)
    assert handler.status == 200
    assert handler.sent_headers == {"Content-Type": "audio/mpeg", "Content-Length": "10",
                                    "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    assert handler.wfile.getvalue() == b"0123456789"


@pytest.mark.parametrize("range_header,status,body,content_range", [
    ("bytes=0-1", 206, b"01", "bytes 0-1/10"),
    ("bytes=4-", 206, b"456789", "bytes 4-9/10"),
    ("bytes=8-50", 206, b"89", "bytes 8-9/10"),
    ("bytes=10-", 416, b"", "bytes */10"),
])
def test_complete_file_range_serving(audio_dir, range_header, status, body, content_range):
    clip_id = _complete_clip(audio_dir)
    handler = FakeHandler(range_header)
    cs.serve_clip_stream(handler, cs.ClipStreamBroker(), clip_id, audio_dir)
    assert handler.status == status
    assert handler.sent_headers["Content-Range"] == content_range
    assert handler.sent_headers["Content-Length"] == str(len(body))
    assert handler.wfile.getvalue() == body


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_producer_status_complete_or_failed_uses_static_path_even_if_stream_in_memory(audio_dir, status):
    clip_id = _complete_clip(audio_dir, status=status)
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"partial")
    handler = FakeHandler()
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.status == 200 and handler.wfile.getvalue() == b"0123456789"


def test_pcm_suffix_is_octet_stream(audio_dir):
    clip_id = _complete_clip(audio_dir, suffix=".pcm")
    handler = FakeHandler()
    cs.serve_clip_stream(handler, cs.ClipStreamBroker(), clip_id, audio_dir)
    assert handler.sent_headers["Content-Type"] == "application/octet-stream"


def test_absolute_path_rows_are_served_directly(tmp_path, audio_dir):
    path = tmp_path / "elsewhere.mp3"
    path.write_bytes(b"abc")
    clip_id = _insert_clip(str(path), "complete")
    handler = FakeHandler()
    cs.serve_clip_stream(handler, cs.ClipStreamBroker(), clip_id, audio_dir)
    assert handler.status == 200 and handler.wfile.getvalue() == b"abc"


def test_live_stream_with_range_probe_answers_206_with_unknown_total(audio_dir):
    clip_id = _insert_clip("/audio/live.mp3", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"ID3\x04")
    handler = FakeHandler("bytes=0-1")
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.status == 206
    assert handler.sent_headers["Content-Range"] == "bytes 0-1/*"
    assert handler.sent_headers["Content-Length"] == "2"
    assert handler.sent_headers["Content-Type"] == "audio/mpeg"
    assert handler.wfile.getvalue() == b"ID"


def test_live_stream_open_ended_range_waits_for_finish_then_reports_total(audio_dir):
    clip_id = _insert_clip("/audio/live.mp3", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"abcd")

    def producer():
        broker.append(clip_id, b"efgh")
        broker.finish(clip_id)

    threading.Timer(0.05, producer).start()
    handler = FakeHandler("bytes=2-")
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.status == 206
    assert handler.sent_headers["Content-Range"] == "bytes 2-7/8"
    assert handler.wfile.getvalue() == b"cdefgh"


def test_live_stream_range_past_available_after_finish_is_416(audio_dir):
    clip_id = _insert_clip("/audio/live.mp3", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"abcd")
    broker.finish(clip_id)
    handler = FakeHandler("bytes=10-")
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.status == 416
    assert handler.sent_headers["Content-Range"] == "bytes */4"
    assert handler.sent_headers["Content-Length"] == "0"


def test_live_stream_range_times_out_and_reports_unknown_total(audio_dir, monkeypatch):
    monkeypatch.setattr(cs, "CHUNK_WAIT_SEC", 0.05)
    clip_id = _insert_clip("/audio/live.mp3", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    handler = FakeHandler("bytes=0-1")
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.status == 416
    assert handler.sent_headers["Content-Range"] == "bytes */*"


def _dechunk(raw: bytes) -> list[bytes]:
    chunks, rfile = [], io.BytesIO(raw)
    while True:
        size = int(rfile.readline().strip() or b"0", 16)
        if size == 0:
            assert rfile.read() == b"\r\n"
            return chunks
        chunks.append(rfile.read(size))
        assert rfile.read(2) == b"\r\n"


def test_live_stream_without_range_is_chunked_until_finish(audio_dir):
    clip_id = _insert_clip("/audio/live.mp3", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"one")

    def producer():
        broker.append(clip_id, b"two")
        broker.finish(clip_id)

    threading.Timer(0.05, producer).start()
    handler = FakeHandler()
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.status == 200
    assert handler.sent_headers == {"Content-Type": "audio/mpeg", "Transfer-Encoding": "chunked",
                                    "Cache-Control": "no-store", "Connection": "close"}
    assert _dechunk(handler.wfile.getvalue()) == [b"one", b"two"]


def test_live_chunked_wait_timeout_marks_stream_failed(audio_dir, monkeypatch):
    monkeypatch.setattr(cs, "CHUNK_WAIT_SEC", 0.05)
    clip_id = _insert_clip("/audio/live.mp3", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"only")
    handler = FakeHandler()
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert _dechunk(handler.wfile.getvalue()) == [b"only"]
    stream = broker.get(clip_id)
    assert stream.failed is True and stream.error == "stream wait timeout"


def test_finished_stream_gone_from_memory_falls_back_to_file_even_if_status_unknown(audio_dir):
    clip_id = _complete_clip(audio_dir, status=None)
    handler = FakeHandler("bytes=0-3")
    cs.serve_clip_stream(handler, cs.ClipStreamBroker(), clip_id, audio_dir)
    assert handler.status == 206 and handler.wfile.getvalue() == b"0123"


def test_live_content_type_follows_row_suffix(audio_dir):
    clip_id = _insert_clip("/audio/live.pcm", "streaming")
    broker = cs.ClipStreamBroker()
    broker.open(clip_id)
    broker.append(clip_id, b"\x00\x00")
    broker.finish(clip_id)
    handler = FakeHandler()
    cs.serve_clip_stream(handler, broker, clip_id, audio_dir)
    assert handler.sent_headers["Content-Type"] == "application/octet-stream"


def test_producer_complete_helper_tolerates_missing_row():
    assert cs._is_producer_complete(12345) is False
    assert cs._path_for_clip(12345, Path("/x")) is None
