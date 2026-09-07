"""HTTP half of the voice timeline: /voice-events in and out, plus the rows
the server files on its own from /transcribe, /send, and /clips/ack."""
from __future__ import annotations

import io
import json
import math
import pathlib
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

from lib import agents as agents_db  # noqa: E402
from lib import db, voice_events  # noqa: E402
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("claude_pwa_server_voice", REPO / "server" / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
build_server = server_module.build_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wav_sine(seconds=0.6, amplitude=0.4, rate=16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(int(seconds * rate))))
    return buf.getvalue()


@pytest.fixture
def running(tmp_path):
    db.reset_for_tests(tmp_path / "state.sqlite")
    static = REPO / "static"
    audio = tmp_path / "audio"
    audio.mkdir()
    agents_db.create_agent(persona="Rachel", voice_id="V_RACHEL",
                           cwd=str(tmp_path), session="rachel")
    ctx = ServerContext(
        root=tmp_path, static=static, audio_dir=audio,
        agents_path=tmp_path / "agents.json", default_session="rachel",
        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
        stt=StubSTT(text="turn left ahead", ends_terminal=True),
        roster_names=("Rachel",),
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    try:
        yield {"base": base, "ctx": ctx, "audio": audio}
    finally:
        srv.shutdown()
        srv.server_close()
        db.close_local()


def _post(base, path, body, headers=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"_raw": raw.decode("utf-8", "replace")}


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read())


def _wait_for_events(min_count, **filters):
    for _ in range(100):
        rows = voice_events.query(**filters)
        if len(rows) >= min_count:
            return rows
        time.sleep(0.02)
    return voice_events.query(**filters)


def test_batch_is_stored_on_the_corrected_clock(running):
    base = running["base"]
    phone_now = voice_events.now_ms() + 45_000  # phone runs 45 s fast
    status, body = _post(base, "/voice-events", {
        "source": "ios", "client_id": "iphone-1", "sent_at": phone_now,
        "session": "rachel",
        "events": [
            {"event": "speech_start", "ts": phone_now - 3_000, "mono_ms": 100,
             "utterance_id": "job-1", "detail": {"silence_ms": 1200}},
            {"event": "speech_end", "ts": phone_now - 1_000, "mono_ms": 2100,
             "utterance_id": "job-1", "duration_ms": 2000,
             "level_db": -19.2, "peak_db": -2.8},
        ]})
    assert status == 200, body
    assert body["ok"] is True and body["stored"] == 2
    assert -46_000 <= body["clock_offset_ms"] <= -44_000
    assert body["server_now"] > 0

    status, out = _get(base, "/voice-events?session=rachel&utterance_id=job-1")
    assert status == 200
    events = out["events"]
    assert [e["event"] for e in events] == ["speech_start", "speech_end"]
    # Corrected onto the server clock: about 3 s before "now", not 42 s after.
    assert abs(events[0]["ts"] - (voice_events.now_ms() - 3_000)) < 1_500
    assert events[0]["client_ts"] == phone_now - 3_000
    assert events[0]["detail"] == {"silence_ms": 1200}
    assert events[1]["level_db"] == -19.2


def test_bad_batches_are_rejected(running):
    base = running["base"]
    assert _post(base, "/voice-events", {"events": "nope"})[0] == 400
    assert _post(base, "/voice-events", b"[1,2]")[0] == 400
    too_many = {"events": [{"event": "level"}] * (voice_events.MAX_BATCH + 1)}
    assert _post(base, "/voice-events", too_many)[0] == 413


def test_transcribe_files_transcript_level_and_links_send(running):
    base = running["base"]
    audio = _wav_sine()
    req = urllib.request.Request(
        base + "/transcribe", data=audio, method="POST",
        headers={"Content-Type": "audio/wav",
                 "X-Transcription-ID": "job-7",
                 "X-Client-Ts": str(voice_events.now_ms() - 400)})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read())
    assert body["text"] == "turn left ahead"
    trace_id = body["trace_id"]

    rows = _wait_for_events(2, utterance_id="job-7")
    by_event = {r["event"]: r for r in rows}
    assert {"transcript", "level"} <= set(by_event)
    transcript = by_event["transcript"]
    assert transcript["source"] == "server"
    assert transcript["trace_id"] == trace_id
    assert transcript["text"] == "turn left ahead"
    assert transcript["client_ts"] is not None
    assert transcript["detail"]["bytes"] == len(audio)
    assert transcript["detail"]["content_type"] == "audio/wav"
    level = by_event["level"]
    assert abs(level["peak_db"] - (-8.0)) < 0.5   # 0.4 amplitude sine
    assert abs(level["duration_ms"] - 600) < 5
    assert level["detail"]["clip_ratio"] == 0.0
    assert "corrupt" not in by_event

    # A retry of the same job is a `retry` row, not a second transcript.
    with urllib.request.urlopen(req, timeout=15) as r:
        assert json.loads(r.read())["cached"] is True
    rows = _wait_for_events(3, utterance_id="job-7")
    assert [r["event"] for r in rows if r["event"] == "retry"] == ["retry"]
    assert [r["event"] for r in rows].count("transcript") == 1

    # The send that follows carries the same utterance and trace.
    from lib.turn_dispatch import DispatchResult, TurnDispatchService
    real = TurnDispatchService.dispatch

    def fake_dispatch(self, *, text, requested_session, trace_id, **kwargs):
        return DispatchResult(session=requested_session, backend="codex")

    TurnDispatchService.dispatch = fake_dispatch  # type: ignore[method-assign]
    try:
        status, out = _post(base, "/send", {
            "session": "rachel", "text": "turn left ahead",
            "client_msg_id": "c-1", "trace_id": trace_id,
            "transcription_id": "job-7-seg-0", "utterance_id": "job-7"})
    finally:
        TurnDispatchService.dispatch = real  # type: ignore[method-assign]
    assert status == 200, out
    send = voice_events.query(utterance_id="job-7", event="send")
    assert len(send) == 1
    assert send[0]["trace_id"] == trace_id and send[0]["session"] == "rachel"
    assert send[0]["detail"]["client_msg_id"] == "c-1"

    # The rollup sees one utterance with the server's decode as its level.
    status, out = _get(base, "/voice-events/utterances?session=rachel")
    (utt,) = out["utterances"]
    assert utt["utterance_id"] == "job-7" and utt["trace_id"] == trace_id
    assert utt["transcript"] == "turn left ahead"
    assert utt["text_to_send_ms"] is not None and utt["text_to_send_ms"] >= 0
    assert utt["corrupt"] == []


def test_silent_capture_is_marked_corrupt(running):
    base = running["base"]
    running["ctx"].stt.text = ""
    silence = io.BytesIO()
    with wave.open(silence, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * 16_000)
    req = urllib.request.Request(
        base + "/transcribe", data=silence.getvalue(), method="POST",
        headers={"Content-Type": "audio/wav", "X-Transcription-ID": "job-silent"})
    with urllib.request.urlopen(req, timeout=15) as r:
        assert json.loads(r.read())["text"] == ""
    rows = _wait_for_events(3, utterance_id="job-silent")
    corrupt = [r for r in rows if r["event"] == "corrupt"]
    assert len(corrupt) == 1
    assert "silent_upload" in corrupt[0]["detail"]["reasons"]
    assert corrupt[0]["detail"]["metrics"]["peak_db"] == -120.0


def test_truncated_upload_is_a_corrupt_row(running):
    base = running["base"]
    port = int(base.rsplit(":", 1)[1])
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(
            b"POST /transcribe HTTP/1.1\r\nHost: x\r\nContent-Type: audio/wav\r\n"
            b"X-Transcription-ID: job-cut\r\nContent-Length: 5000\r\n"
            b"Connection: close\r\n\r\n" + b"\x00" * 1200)
        s.shutdown(socket.SHUT_WR)
        raw = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            raw += chunk
    finally:
        s.close()
    assert b" 408 " in raw.split(b"\r\n", 1)[0]
    rows = _wait_for_events(1, utterance_id="job-cut")
    assert rows[0]["event"] == "corrupt"
    assert rows[0]["detail"]["reasons"] == ["incomplete_upload"]
    assert rows[0]["detail"] == {"reasons": ["incomplete_upload"],
                                 "expected": 5000, "received": 1200}


def test_clip_acks_become_playback_moments(running):
    base = running["base"]
    audio = running["audio"]
    clip_path = audio / "1700000000000__rachel.mp3"
    clip_path.write_bytes(b"\xff\xfb" * 100)
    agent = agents_db.get_by_session("rachel")
    agents_db.record_clip(agent_id=agent["agent_id"], path=f"/audio/{clip_path.name}",
                          voice_id="V_RACHEL", trace_id="trace-play",
                          byte_count=clip_path.stat().st_size)
    clip_id = int(db.conn().execute(
        "SELECT clip_id FROM clips WHERE path = ?", (f"/audio/{clip_path.name}",)
    ).fetchone()["clip_id"])
    for ack in ("queued", "play-start", "play-ok"):
        status, _ = _post(base, "/clips/ack", {"clip_id": clip_id, "status": ack,
                                               "url": f"/audio/{clip_path.name}"})
        assert status == 200
    rows = voice_events.query(trace_id="trace-play")
    assert [r["event"] for r in rows] == ["play_start", "play_end"]
    assert rows[0]["session"] == "rachel"  # looked up from the clip's agent
    assert rows[0]["detail"]["clip_id"] == clip_id
