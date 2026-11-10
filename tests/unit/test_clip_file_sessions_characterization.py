"""S7 step 1c, G0: what the two file-backed clip sessions do today, pinned
before RawPcmSession and ChunkedFileSession fold into one FileBrokerSession.
Written against the old code; every row of the table must hold after the fold."""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import clips as clips_lib
from lib.clip_delivery.chunked_file import ChunkedFileDelivery, ChunkedFileSession
from lib.clip_delivery.raw_pcm import RawPcmDelivery, RawPcmSession
from lib.db import conn


class Broker:
    def __init__(self, *, raises=False):
        self.calls = []
        self.raises = raises

    def _record(self, *call):
        self.calls.append(call)
        if self.raises:
            raise RuntimeError("broker down")

    def open(self, clip_id):
        self._record("open", clip_id)

    def append(self, clip_id, chunk):
        self._record("append", clip_id, chunk)

    def finish(self, clip_id):
        self._record("finish", clip_id)

    def fail(self, clip_id, error):
        self._record("fail", clip_id, error)


DELIVERIES = {
    "chunked": lambda broker: ChunkedFileDelivery(broker=broker),
    "raw-default": lambda broker: RawPcmDelivery(broker=broker),
    "raw-tuned": lambda broker: RawPcmDelivery(broker=broker, encoding="pcm_s16le", sample_rate=24000),
}


@pytest.fixture
def agent(tmp_path):
    agent_id = agents_db.create_agent(persona="Ada", voice_id="v-ada", cwd=str(tmp_path), session="ada")
    return agents_db.get_by_agent_id(agent_id)


def _begin(kind, broker, agent, tmp_path):
    return DELIVERIES[kind](broker).begin(
        audio_dir=tmp_path / "audio", agent=agent, voice_id="v-ada", session="ada",
        source="test", text_len=11, trace_id="trace-1")


def _row(clip_id):
    return dict(conn().execute(
        "SELECT path, producer_status, bytes, error, trace_id, voice_id FROM clips WHERE clip_id=?",
        (clip_id,)).fetchone())


def _sidecar(session):
    data = json.loads(clips_lib.sidecar_path(session.target_path).read_text())
    data.pop("created_at", None)
    data.pop("ts", None)
    return data


def _format(kind):
    return {"chunked": None,
            "raw-default": {"container": "raw", "encoding": "pcm_f32le", "sample_rate": 44100, "channels": 1},
            "raw-tuned": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000, "channels": 1}}[kind]


def _expected_fields(kind, clip_id):
    stream = f"/clips/{clip_id}/stream"
    if kind == "chunked":
        return {"streamable": True, "stream_url": stream, "complete_url": f"/clips/{clip_id}/complete.mp3"}
    return {"delivery": "raw-pcm", "streamable": True, "stream_url": stream, "url": stream,
            "audio_format": _format(kind)}


@pytest.mark.parametrize("kind", sorted(DELIVERIES))
def test_begin_records_a_streaming_clip_opens_the_broker_and_writes_the_first_sidecar(kind, agent, tmp_path):
    broker = Broker()
    session = _begin(kind, broker, agent, tmp_path)
    assert isinstance(session, ChunkedFileSession if kind == "chunked" else RawPcmSession)
    suffix = ".mp3" if kind == "chunked" else ".pcm"
    assert session.target_path.suffix == suffix
    assert session.target_path.name.endswith(f"__ada{suffix}")
    assert session.target_path.parent == tmp_path / "audio"
    assert session.publish_after_finalize is False
    row = _row(session.clip_id)
    assert row == {"path": str(session.target_path), "producer_status": "streaming", "bytes": None,
                   "error": None, "trace_id": "trace-1", "voice_id": "v-ada"}
    assert broker.calls == [("open", session.clip_id)]
    first = _sidecar(session)
    assert "bytes" not in first
    for key, value in _expected_fields(kind, session.clip_id).items():
        assert first[key] == value
    assert first["clip_id"] == session.clip_id
    assert session.sse_fields == _expected_fields(kind, session.clip_id)


@pytest.mark.parametrize("kind", sorted(DELIVERIES))
def test_feed_and_finalize(kind, agent, tmp_path):
    broker = Broker()
    session = _begin(kind, broker, agent, tmp_path)
    session.feed(0, b"abc")
    session.feed(1, b"defg")
    session.target_path.write_bytes(b"abcdefg")
    result = session.finalize(total_bytes=999)
    clip_id = session.clip_id
    assert broker.calls == [("open", clip_id), ("append", clip_id, b"abc"), ("append", clip_id, b"defg"),
                            ("finish", clip_id)]
    assert (result.path, result.clip_id, result.ok) == (session.target_path, clip_id, True)
    assert result.sse_url == (f"/audio/{session.target_path.name}" if kind == "chunked"
                              else f"/clips/{clip_id}/stream")
    assert _row(clip_id)["producer_status"] == "complete"
    assert _row(clip_id)["bytes"] == 7, "the file's size wins over total_bytes"
    final = _sidecar(session)
    assert final["bytes"] == 7
    for key, value in _expected_fields(kind, clip_id).items():
        assert final[key] == value


@pytest.mark.parametrize("kind", sorted(DELIVERIES))
def test_finalize_without_a_file_counts_total_bytes(kind, agent, tmp_path):
    session = _begin(kind, Broker(), agent, tmp_path)
    session.finalize(total_bytes=42)
    assert _row(session.clip_id)["bytes"] == 42


@pytest.mark.parametrize("kind", sorted(DELIVERIES))
def test_fail_removes_the_partial_clip_and_marks_it_failed(kind, agent, tmp_path):
    broker = Broker()
    session = _begin(kind, broker, agent, tmp_path)
    session.target_path.write_bytes(b"partial")
    session.fail("provider exploded")
    assert not session.target_path.exists()
    assert not clips_lib.sidecar_path(session.target_path).exists()
    row = _row(session.clip_id)
    assert (row["producer_status"], row["error"]) == ("failed", "provider exploded")
    assert broker.calls[-1] == ("fail", session.clip_id, "provider exploded")
    session.fail("again")    # nothing left to remove: no raise
    assert _row(session.clip_id)["error"] == "again"


@pytest.mark.parametrize("kind", sorted(DELIVERIES))
def test_a_broken_broker_never_stops_the_clip(kind, agent, tmp_path):
    broker = Broker(raises=True)
    session = _begin(kind, broker, agent, tmp_path)
    session.feed(0, b"x")
    session.target_path.write_bytes(b"x")
    assert session.finalize(total_bytes=1).ok is True
    assert _row(session.clip_id)["producer_status"] == "complete"
    assert [c[0] for c in broker.calls] == ["open", "append", "finish"]
    session.fail("late")
    assert [c[0] for c in broker.calls][-1] == "fail"


@pytest.mark.parametrize("kind", sorted(DELIVERIES))
def test_no_broker_streams_to_the_file_only(kind, agent, tmp_path):
    session = _begin(kind, None, agent, tmp_path)
    session.feed(0, b"abc")
    session.target_path.write_bytes(b"abc")
    assert session.finalize(total_bytes=3).ok is True
    session2 = _begin(kind, None, agent, tmp_path)
    session2.fail("no broker")
    assert _row(session2.clip_id)["producer_status"] == "failed"


def test_raw_session_defaults_its_format_when_none_is_given(tmp_path):
    session = RawPcmSession(broker=None, target=tmp_path / "x.pcm", clip_id=7, agent={"agent_id": "a"},
                            voice_id="v", session="s", source="t", text_len=1, trace_id=None)
    assert session.sse_fields["audio_format"] == {"container": "raw", "encoding": "pcm_f32le",
                                                  "sample_rate": 44100, "channels": 1}
