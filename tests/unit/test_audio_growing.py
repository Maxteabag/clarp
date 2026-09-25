"""Characterization tests for lib.audio_growing (chunked serve of a growing mp3).

Pins the sidecar-based in-progress signal and that the chunked response
carries exactly the bytes the file ends up with, including bytes written
after the reader first hit EOF, terminated once the sidecar is finalized.
"""
from __future__ import annotations

import io
import threading

import pytest

from lib import audio_growing as ag, clips


class FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass


def _dechunk(raw: bytes) -> bytes:
    out, rfile = b"", io.BytesIO(raw)
    while True:
        size = int(rfile.readline().strip(), 16)
        if size == 0:
            assert rfile.read() == b"\r\n"
            return out
        out += rfile.read(size)
        assert rfile.read(2) == b"\r\n"


@pytest.mark.parametrize("sidecar,in_progress,finalized", [
    (None, False, True),                                   # no sidecar: legacy static clip
    ({"streamable": True}, True, False),
    ({"streamable": True, "bytes": None}, True, False),    # write_sidecar drops None values
    ({"streamable": True, "bytes": 1234}, False, True),
    # A non-streamable sidecar without `bytes` is neither: is_in_progress
    # needs the streamable flag, _is_finalized only looks for `bytes`.
    ({"streamable": False}, False, False),
    ({"clip_id": 1}, False, False),
])
def test_is_in_progress_and_finalized(tmp_path, sidecar, in_progress, finalized):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"x")
    if sidecar is not None:
        clips.write_sidecar(mp3, bytes_=sidecar.get("bytes"),
                            extra={k: v for k, v in sidecar.items() if k != "bytes"})
    assert ag.is_in_progress(mp3) is in_progress
    assert ag._is_finalized(mp3) is finalized


def test_corrupt_sidecar_counts_as_not_in_progress(tmp_path):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"x")
    clips.sidecar_path(mp3).write_text("{not json")
    assert ag.is_in_progress(mp3) is False
    assert ag._is_finalized(mp3) is True


def test_finalized_file_is_served_whole_with_chunked_headers(tmp_path):
    mp3 = tmp_path / "c.mp3"
    data = bytes(range(256)) * 40          # 10240 bytes: several 4 KiB chunks
    mp3.write_bytes(data)
    clips.write_sidecar(mp3, bytes_=len(data), extra={"streamable": True})
    handler = FakeHandler()
    ag.serve_growing(handler, mp3)
    assert handler.status == 200
    assert handler.sent_headers == {"Content-Type": "audio/mpeg", "Transfer-Encoding": "chunked",
                                    "Cache-Control": "no-store", "Connection": "close"}
    assert _dechunk(handler.wfile.getvalue()) == data


def test_legacy_file_without_sidecar_terminates_immediately(tmp_path):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"abc")
    handler = FakeHandler()
    ag.serve_growing(handler, mp3)
    assert _dechunk(handler.wfile.getvalue()) == b"abc"


def test_growing_file_streams_bytes_written_after_first_eof(tmp_path):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"head")
    clips.write_sidecar(mp3, extra={"streamable": True})

    def producer():
        with mp3.open("ab") as f:
            f.write(b"-more")
        # finalize: rewrite sidecar with the byte count
        clips.write_sidecar(mp3, bytes_=mp3.stat().st_size, extra={"streamable": True})

    threading.Timer(0.12, producer).start()
    handler = FakeHandler()
    ag.serve_growing(handler, mp3)
    assert _dechunk(handler.wfile.getvalue()) == b"head-more"


def test_finalize_race_tail_is_swept(tmp_path, monkeypatch):
    # The sidecar becomes final between our EOF read and the finalize check;
    # the trailing bytes must still be sent before the terminator.
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"head")
    clips.write_sidecar(mp3, extra={"streamable": True})
    real = ag._is_finalized

    def finalize_then_report(path):
        with path.open("ab") as f:
            f.write(b"TAIL")
        clips.write_sidecar(path, bytes_=path.stat().st_size, extra={"streamable": True})
        return real(path)

    monkeypatch.setattr(ag, "_is_finalized", finalize_then_report)
    handler = FakeHandler()
    ag.serve_growing(handler, mp3)
    assert _dechunk(handler.wfile.getvalue()) == b"headTAIL"


def test_stalled_growth_gives_up_after_max_wait(tmp_path, monkeypatch):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"partial")
    clips.write_sidecar(mp3, extra={"streamable": True})
    monkeypatch.setattr(ag, "MAX_GROW_WAIT_SEC", 0.1)
    monkeypatch.setattr(ag, "GROW_POLL_SEC", 0.01)
    handler = FakeHandler()
    ag.serve_growing(handler, mp3)
    assert _dechunk(handler.wfile.getvalue()) == b"partial"


def test_header_write_failure_is_logged_and_aborts(tmp_path, monkeypatch):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"x")
    logged = []
    monkeypatch.setattr(ag, "log_exception", lambda event, exc, detail="": logged.append((event, detail)))

    class Broken(FakeHandler):
        def send_response(self, code):
            raise OSError("gone")

    handler = Broken()
    ag.serve_growing(handler, mp3)
    assert logged == [("audioGrowingHeaderFail", "c.mp3")]
    assert handler.wfile.getvalue() == b""


def test_client_disconnect_mid_stream_is_silent(tmp_path, monkeypatch):
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(b"abc")
    logged = []
    monkeypatch.setattr(ag, "log_exception", lambda *a, **k: logged.append(a))

    class Dropping(FakeHandler):
        def __init__(self):
            super().__init__()
            self.wfile = type("W", (), {"write": lambda s, b: (_ for _ in ()).throw(BrokenPipeError()),
                                        "flush": lambda s: None})()

    ag.serve_growing(Dropping(), mp3)
    assert logged == []
