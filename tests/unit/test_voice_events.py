"""The permanent voice timeline: clock correction, storage, and the
per-utterance rollup that turns rows into latencies and flags."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib import db, voice_events  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.reset_for_tests(tmp_path / "state.sqlite")
    yield
    db.close_local()


def test_record_stamps_receipt_time_when_no_client_clock():
    before = voice_events.now_ms()
    eid = voice_events.record("speech_start", source="ios", session="rachel",
                              utterance_id="u1")
    row = voice_events.query(utterance_id="u1")[0]
    assert row["event_id"] == eid
    assert before <= row["ts"] <= voice_events.now_ms()
    assert row["received_at"] == row["ts"]
    assert row["client_ts"] is None
    assert row["detail"] == {}


def test_batch_corrects_client_clock_from_sent_at():
    # Phone clock runs 90 s ahead of the server.
    server_now = 1_700_000_000_000
    phone_now = server_now + 90_000
    result = voice_events.record_batch(
        source="ios", client_id="phone-1", sent_at=phone_now,
        received_at=server_now, default_session="rachel",
        events=[
            {"event": "speech_start", "ts": phone_now - 2_000, "mono_ms": 10,
             "utterance_id": "u1", "detail": {"silence_ms": 800}},
            {"event": "speech_end", "ts": phone_now - 500, "utterance_id": "u1",
             "duration_ms": 1500, "level_db": -18.5, "peak_db": -3.2},
        ])
    assert result == {"stored": 2, "clock_offset_ms": -90_000}
    rows = voice_events.query(session="rachel")
    assert [r["event"] for r in rows] == ["speech_start", "speech_end"]
    assert rows[0]["ts"] == server_now - 2_000
    assert rows[0]["client_ts"] == phone_now - 2_000
    assert rows[0]["clock_offset_ms"] == -90_000
    assert rows[0]["mono_ms"] == 10
    assert rows[0]["source"] == "ios" and rows[0]["client_id"] == "phone-1"
    assert rows[0]["detail"] == {"silence_ms": 800}
    assert rows[1]["duration_ms"] == 1500 and rows[1]["level_db"] == -18.5


def test_batch_without_sent_at_uses_receipt_time_and_keeps_raw_client_ts():
    result = voice_events.record_batch(
        source="pwa", received_at=5_000,
        events=[{"event": "barge_in", "ts": 123, "session": "mike"}])
    assert result["clock_offset_ms"] is None
    row = voice_events.query(session="mike")[0]
    assert row["ts"] == 5_000 and row["client_ts"] == 123


def test_implausible_offset_is_ignored():
    day = 24 * 60 * 60 * 1000
    result = voice_events.record_batch(
        source="ios", sent_at=1_000, received_at=1_000 + 2 * day,
        events=[{"event": "listen_start", "ts": 1_000}])
    assert result["clock_offset_ms"] is None
    assert voice_events.query()[0]["ts"] == 1_000 + 2 * day


def test_batch_skips_junk_items_and_caps_size():
    result = voice_events.record_batch(
        source="ios", received_at=1,
        events=["nope", {"detail": {}}, {"event": "listen_start"}])
    assert result["stored"] == 1
    too_many = [{"event": "level"}] * (voice_events.MAX_BATCH + 5)
    assert voice_events.record_batch(source="ios", received_at=1,
                                     events=too_many)["stored"] == voice_events.MAX_BATCH


def test_unknown_source_is_kept_as_other_and_text_is_capped():
    voice_events.record("transcript", source="martian", text="x" * 10_000)
    row = voice_events.query()[0]
    assert row["source"] == "other"
    assert len(row["text"]) == voice_events.MAX_TEXT


def test_query_filters_and_orders_oldest_first_within_limit():
    for i, name in enumerate(["speech_start", "speech_end", "transcript", "send"]):
        voice_events.record(name, ts=1000 + i, session="rachel", utterance_id="u1",
                            trace_id="t1" if i >= 2 else None)
    voice_events.record("speech_start", ts=1010, session="mike", utterance_id="u2")
    assert [r["event"] for r in voice_events.query(session="rachel")] == [
        "speech_start", "speech_end", "transcript", "send"]
    assert [r["event"] for r in voice_events.query(trace_id="t1")] == ["transcript", "send"]
    assert [r["ts"] for r in voice_events.query(since=1002, until=1003)] == [1002, 1003]
    # limit keeps the newest rows, still returned oldest first.
    assert [r["ts"] for r in voice_events.query(limit=2)] == [1003, 1010]
    assert [r["event"] for r in voice_events.query(event="speech_start")] == [
        "speech_start", "speech_start"]


def _utterance(uid: str, t0: int, *, trace: str, text: str = "hello there",
               corrupt: list[str] | None = None, barge: bool = False):
    voice_events.record("speech_start", ts=t0, source="ios", session="rachel",
                        utterance_id=uid, detail={"silence_ms": 640})
    voice_events.record("speech_end", ts=t0 + 1_800, source="ios", session="rachel",
                        utterance_id=uid, duration_ms=1_800, level_db=-20.0, peak_db=-4.0)
    voice_events.record("upload_start", ts=t0 + 1_850, source="ios", session="rachel",
                        utterance_id=uid)
    voice_events.record("upload_end", ts=t0 + 2_050, source="ios", session="rachel",
                        utterance_id=uid, duration_ms=200, detail={"bytes": 48_000})
    voice_events.record("transcript", ts=t0 + 2_600, source="server", session="rachel",
                        utterance_id=uid, trace_id=trace, duration_ms=540, text=text)
    voice_events.record("level", ts=t0 + 2_650, source="server", session="rachel",
                        utterance_id=uid, trace_id=trace, level_db=-21.0, peak_db=-5.0)
    voice_events.record("send", ts=t0 + 2_700, source="server", session="rachel",
                        utterance_id=uid, trace_id=trace, text=text)
    if corrupt:
        voice_events.record("corrupt", ts=t0 + 2_660, source="server", session="rachel",
                            utterance_id=uid, trace_id=trace, detail={"reasons": corrupt})
    if barge:
        voice_events.record("barge_in", ts=t0 + 9_000, source="ios", session="rachel",
                            utterance_id=uid)
    # Playback rows only know the trace, like /clips/ack does.
    voice_events.record("play_start", ts=t0 + 5_900, source="server", session="rachel",
                        trace_id=trace, detail={"clip_id": 7})
    voice_events.record("play_end", ts=t0 + 8_400, source="server", session="rachel",
                        trace_id=trace, detail={"clip_id": 7})


def test_utterance_rollup_folds_the_timeline_into_latencies():
    _utterance("u1", 100_000, trace="t1")
    _utterance("u2", 200_000, trace="t2", text="", corrupt=["silent_upload"], barge=True)
    rows = voice_events.utterances(session="rachel")
    assert [r["utterance_id"] for r in rows] == ["u2", "u1"]  # newest first
    u1 = rows[1]
    assert u1["trace_id"] == "t1"
    assert u1["started_at"] == 100_000 and u1["ended_at"] == 101_800
    assert u1["speech_ms"] == 1_800
    assert u1["silence_before_ms"] == 640
    assert u1["level_db"] == -20.0 and u1["peak_db"] == -4.0  # client measured
    assert u1["upload_ms"] == 200
    assert u1["stt_ms"] == 540
    assert u1["transcript"] == "hello there"
    assert u1["speech_to_text_ms"] == 800
    assert u1["text_to_send_ms"] == 100
    assert u1["send_to_play_ms"] == 3_200
    assert u1["speech_to_play_ms"] == 4_100
    assert u1["play_ms"] == 2_500
    assert u1["corrupt"] == [] and u1["barge_in"] is False
    assert u1["events"] == 9
    u2 = rows[0]
    assert u2["corrupt"] == ["silent_upload"] and u2["barge_in"] is True
    assert u2["transcript"] == ""


def test_utterance_rollup_tolerates_server_only_rows():
    # A client that sends nothing still yields a row from the server's side.
    voice_events.record("transcript", ts=1_000, session="rachel", utterance_id="job-9",
                        trace_id="t9", duration_ms=300, text="only server")
    voice_events.record("level", ts=1_050, session="rachel", utterance_id="job-9",
                        trace_id="t9", level_db=-30.0, peak_db=-9.0)
    voice_events.record("play_start", ts=4_000, session="rachel", trace_id="t9")
    (row,) = voice_events.utterances(session="rachel")
    assert row["started_at"] == 1_000 and row["ended_at"] is None
    assert row["speech_ms"] is None and row["speech_to_play_ms"] is None
    assert row["level_db"] == -30.0  # falls back to the server's decode
    assert row["send_to_play_ms"] is None
    assert row["transcript"] == "only server"
