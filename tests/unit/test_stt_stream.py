"""Provider-owned turn taking relay: Flux events in, Clarp turns out."""
from __future__ import annotations

import io
import json
import struct
import threading
import wave
from urllib.parse import parse_qs, urlparse

from lib import stt_stream, ws
from lib.stt_stream import TurnEvent, TurnLedger, flux_url, normalize_flux_message


def test_flux_turn_info_normalises_and_other_messages_are_ignored():
    ev = normalize_flux_message({
        "type": "TurnInfo", "event": "EagerEndOfTurn", "turn_index": 3,
        "transcript": "ship Clarp", "end_of_turn_confidence": 0.61,
        "words": [{"word": "ship", "confidence": 0.9}]})
    assert ev == TurnEvent(kind="eager", turn=3, text="ship Clarp", confidence=0.61,
                           words=({"word": "ship", "confidence": 0.9},))
    for kind, event in (("start", "StartOfTurn"), ("update", "Update"),
                        ("resumed", "TurnResumed"), ("end", "EndOfTurn")):
        assert normalize_flux_message({"type": "TurnInfo", "event": event}).kind == kind
    assert normalize_flux_message({"type": "Connected"}) is None
    assert normalize_flux_message({"type": "TurnInfo", "event": "Mystery"}) is None
    assert normalize_flux_message("nonsense") is None


def test_flux_url_clamps_tuning_and_repeats_keyterms():
    url = flux_url(eot_threshold=0.2, eager_eot_threshold=0.95, eot_timeout_ms=10,
                   keyterms=["Clarp", "Knut Thomas"])
    q = parse_qs(urlparse(url).query)
    assert url.startswith("wss://api.deepgram.com/v2/listen?")
    assert q["model"] == ["flux-general-en"]
    assert q["encoding"] == ["linear16"] and q["sample_rate"] == ["16000"]
    assert q["eot_threshold"] == ["0.50"]
    assert q["eager_eot_threshold"] == ["0.90"]
    assert q["eot_timeout_ms"] == ["500"]
    assert parse_qs(urlparse(flux_url()).query)["eot_timeout_ms"] == ["60000"]
    assert q["keyterm"] == ["Clarp", "Knut Thomas"]
    assert "eager_eot_threshold" not in parse_qs(
        urlparse(flux_url(eager_eot_threshold=None)).query)


def _ledger(records, retained, clock):
    traces = iter(["t1", "t2", "t3"])
    return TurnLedger(
        session="rachel", provider="deepgram", model="flux-general-en",
        keyterms=("Clarp",), capacity=50, new_trace=lambda: next(traces),
        record_turn=lambda **kw: records.append(kw) or 41 + len(records),
        retain=lambda trace, wav: retained.append((trace, wav)),
        now=lambda: clock[0])


def test_ledger_opens_a_turn_on_first_audio_and_closes_it_on_end():
    records, retained, clock = [], [], [0.0]
    ledger = _ledger(records, retained, clock)
    ledger.heard(b"\x01\x00" * 160)
    clock[0] = 0.3
    start = ledger.apply(TurnEvent(kind="start", turn=1))
    assert start["trace_id"] == "t1" and "vocab_run_id" not in start
    eager = ledger.apply(TurnEvent(kind="eager", turn=1, text="hello", confidence=0.6))
    assert eager == {"type": "turn", "event": "eager", "turn": 1, "text": "hello",
                     "trace_id": "t1", "confidence": 0.6}
    clock[0] = 1.5
    end = ledger.apply(TurnEvent(kind="end", turn=1, text="hello Clarp"))
    assert end["vocab_run_id"] == 42 and end["turn_ms"] == 1500 and end["trace_id"] == "t1"
    assert records[0]["transcript"] == "hello Clarp"
    assert records[0]["keyterms"] == ("Clarp",) and records[0]["latency_ms"] == 1500
    trace, wav_bytes = retained[0]
    assert trace == "t1"
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 160)
    # The next turn gets a fresh trace and starts with no audio.
    assert ledger.apply(TurnEvent(kind="start", turn=2))["trace_id"] == "t2"
    assert ledger.turns_ended == 1


def test_ledger_survives_a_failing_recorder_and_bounds_its_buffer():
    retained, clock = [], [0.0]
    ledger = TurnLedger(
        session="s", provider="deepgram", model="m", keyterms=(), capacity=50,
        new_trace=lambda: "t", record_turn=lambda **kw: 1 / 0,
        retain=lambda t, w: retained.append(t), now=lambda: clock[0])
    ledger.max_buffer_bytes = 100
    ledger.heard(b"a" * 150)
    assert len(ledger.audio) == 100
    end = ledger.apply(TurnEvent(kind="end", turn=1, text="x"))
    assert end["vocab_run_id"] is None and retained == ["t"]


# --- the relay loop with a fake phone and a fake provider ------------------

def _client_frame(opcode: int, payload: bytes) -> bytes:
    mask = b"\x11\x22\x33\x44"
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    else:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    return head + mask + bytes(payload[i] ^ mask[i & 3] for i in range(n))


def _server_frames(data: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(data):
        opcode = data[i] & 0x0F
        n = data[i + 1] & 0x7F
        i += 2
        if n == 126:
            n = struct.unpack("!H", data[i:i + 2])[0]; i += 2
        elif n == 127:
            n = struct.unpack("!Q", data[i:i + 8])[0]; i += 8
        out.append((opcode, data[i:i + n])); i += n
    return out


class FakeUpstream:
    def __init__(self, scripted: list[str]):
        self.scripted = list(scripted)
        self.sent_binary: list[bytes] = []
        self.sent_text: list[str] = []
        self.closed = False
        self.gate = threading.Event()

    def recv(self):
        # Hold the provider's events until the phone has sent its audio, so
        # the test is deterministic about ordering.
        self.gate.wait(timeout=2)
        if self.scripted:
            return self.scripted.pop(0)
        raise ConnectionError("closed")

    def send_binary(self, data: bytes):
        self.sent_binary.append(data)
        self.gate.set()

    def send(self, text: str):
        self.sent_text.append(text)

    def close(self):
        self.closed = True


class FakeHandler:
    def __init__(self, frames: list[bytes]):
        self.rfile = io.BytesIO(b"".join(frames))
        self.wfile = io.BytesIO()


def test_relay_forwards_audio_up_and_turn_events_down_then_closes():
    upstream = FakeUpstream([
        json.dumps({"type": "Connected"}),
        json.dumps({"type": "TurnInfo", "event": "StartOfTurn", "turn_index": 0}),
        json.dumps({"type": "TurnInfo", "event": "EndOfTurn", "turn_index": 0,
                    "transcript": "hello Clarp", "end_of_turn_confidence": 0.9}),
        json.dumps({"type": "Error", "description": "quota"}),
    ])
    handler = FakeHandler([
        _client_frame(ws.OP_BINARY, b"\x00\x01" * 400),
        _client_frame(ws.OP_PING, b"hi"),
        _client_frame(ws.OP_TEXT, json.dumps({"type": "close"}).encode()),
    ])
    records = []
    ledger = TurnLedger(
        session="rachel", provider="deepgram", model="flux-general-en",
        keyterms=("Clarp", "ECIT"), capacity=50, new_trace=lambda: "trace1",
        record_turn=lambda **kw: records.append(kw) or 7, retain=lambda t, w: None)
    stt_stream.run_relay(handler, upstream, ledger, session="rachel", agent_id="a1",
                         engine="deepgram:flux-general-en", stream_trace="s1")

    assert upstream.sent_binary == [b"\x00\x01" * 400]
    assert json.loads(upstream.sent_text[-1]) == {"type": "CloseStream"}
    assert upstream.closed
    frames = _server_frames(handler.wfile.getvalue())
    texts = [json.loads(p) for op, p in frames if op == ws.OP_TEXT]
    assert texts[0] == {"type": "ready", "engine": "deepgram:flux-general-en",
                        "turn_detection": "provider", "keyterms": 2, "trace_id": "s1"}
    kinds = [(t["type"], t.get("event")) for t in texts[1:]]
    assert ("turn", "start") in kinds and ("turn", "end") in kinds
    end = next(t for t in texts if t.get("event") == "end")
    assert end["text"] == "hello Clarp" and end["vocab_run_id"] == 7 and end["trace_id"] == "trace1"
    assert any(t["type"] == "error" and "quota" in t["message"] for t in texts)
    assert texts[-1] == {"type": "closed", "turns": 1}
    assert any(op == ws.OP_PONG for op, _ in frames)
    assert frames[-1][0] == ws.OP_CLOSE
    assert records[0]["transcript"] == "hello Clarp"


def test_record_turn_run_writes_a_vocab_run_row():
    from lib import vocab_store
    run_id = stt_stream.record_turn_run(
        session="rachel", trace_id="abc", provider="deepgram", model="flux-general-en",
        keyterms=("Clarp", "ECIT"), capacity=50, transcript="hi", latency_ms=900)
    run = vocab_store.run_for_trace("abc")
    assert run["run_id"] == run_id and run["transcript"] == "hi"
    assert run["payload"] == "Clarp, ECIT" and run["used"] == 2 and run["capacity"] == 50
    assert [t["text"] for t in run["included"]] == ["Clarp", "ECIT"]
