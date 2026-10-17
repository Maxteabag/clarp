"""Agent-live rings: WS GET /calls/ring/live (WIRE-v2 SCHEMA, M1 handshake).

The phone joins with the exact grant of its Live answer; every binding and the
one-use ticket are checked before the paid upstream opens. The opening is
admitted once (client ready, provider ready, call current), appended upstream
and acknowledged to the phone only after the provider took it. Until then the
Host feeds silence upstream and refuses phone audio; the first forwarded phone
audio closes the fallback door. Hangup, a provider close or a lost phone retire
the call's authority first. Another voice session is never superseded.
"""
from __future__ import annotations

import base64
import json
import queue
import struct
import threading
import time

import pytest
import websocket

from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from lib import agent_rings as rings
from lib import oracle_live_stable, oracle_realtime
from tests.unit.test_agent_ring_live import OPENING, OTHER_PHONE, PHONE, _live_ring, sander  # noqa: F401
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture

AUDIO = base64.b64encode(b"\x01\x00" * 480).decode()


def _masked(opcode, payload):
    payload = payload.encode() if isinstance(payload, str) else payload
    head = bytes([0x80 | opcode])
    n = len(payload)
    head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack("!H", n) if n < 65536 else \
        bytes([0x80 | 127]) + struct.pack("!Q", n)
    return head + b"\0\0\0\0" + payload


class Pipe:
    """The phone→Host byte stream: blocks like a socket until the test writes."""

    def __init__(self):
        self.buf, self.cond, self.eof = bytearray(), threading.Condition(), False

    def feed(self, data):
        with self.cond:
            self.buf += data
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def read(self, n):
        with self.cond:
            while len(self.buf) < n and not self.eof:
                if not self.cond.wait(sock.CLIENT_IDLE_S):      # as the real socket's idle limit
                    raise TimeoutError
            if len(self.buf) < n:
                return b""
            out, self.buf[:] = bytes(self.buf[:n]), self.buf[n:]
            return out


class Out:
    """The Host→phone byte stream, decoded into HTTP status and JSON frames."""

    def __init__(self):
        self.data, self.lock = bytearray(), threading.Lock()

    def write(self, data):
        with self.lock:
            self.data += data

    def flush(self):
        pass

    def frames(self):
        with self.lock:
            data = bytes(self.data)
        end = data.find(b"\r\n\r\n")
        body = data[end + 4:] if data.startswith(b"HTTP/1.1 101") else b""
        out, i = [], 0
        while i + 2 <= len(body):
            opcode, n, i = body[i] & 0x0F, body[i + 1] & 0x7F, i + 2
            if n == 126:
                n, i = struct.unpack("!H", body[i:i + 2])[0], i + 2
            elif n == 127:
                n, i = struct.unpack("!Q", body[i:i + 8])[0], i + 8
            if opcode == 0x1:                           # text frames carry the JSON; pongs are skipped
                out.append(json.loads(body[i:i + n]))
            i += n
        return out


class Connection:
    def __init__(self, pipe):
        self.pipe, self.timeouts = pipe, []

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, _how):
        self.pipe.close()


class Handler:
    def __init__(self, principal=PHONE, scope="full"):
        self.headers = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "dGhlIHNhbXBsZQ=="}
        self.rfile, self.wfile = Pipe(), Out()
        self.connection = Connection(self.rfile)
        self._request_auth_validated, self._request_device_scope = True, scope
        self._request_principal = principal
        self.status = None

    def send_response(self, code):
        self.status = code
        self.wfile.write(f"HTTP/1.1 {code} X\r\n".encode())

    def send_header(self, name, value):
        self.wfile.write(f"{name}: {value}\r\n".encode())

    def end_headers(self):
        self.wfile.write(b"\r\n")

    def send(self, event):
        self.rfile.feed(_masked(0x1, json.dumps(event)))

    def body(self):
        data = bytes(self.wfile.data)
        return json.loads(data[data.find(b"\r\n\r\n") + 4:])


class Upstream:
    """GPT-Live, faked: started on start, appended on an append, closed on close."""

    def __init__(self, take_opening=True):
        self.sent, self.inbox, self.closed, self.take_opening = [], queue.Queue(), False, take_opening

    def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.start":
            self.inbox.put({"type": "session.started"})
        elif event["type"] == "session.instructions.append" and self.take_opening:
            self.inbox.put({"type": "session.instructions.appended"})
        elif event["type"] == "session.close":
            self.inbox.put({"type": "session.closed", "usage": {"seconds": 1}})

    def push(self, event):
        self.inbox.put(event)

    def recv(self):
        try:
            return json.dumps(self.inbox.get(timeout=0.05))
        except queue.Empty:
            raise websocket.WebSocketTimeoutException() from None

    def close(self):
        self.closed = True

    def of(self, kind):
        return [e for e in self.sent if e["type"] == kind]


def _wait(cond, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met")


@pytest.fixture
def call(sander, monkeypatch):
    """A Live-answered ring, a fake upstream and clean voice-ownership claims."""
    def reset():
        with oracle_realtime._ACTIVE_LOCK:
            oracle_realtime._ACTIVE_PRINCIPALS.clear()
        with oracle_live_stable._CLOSING_CONDITION:
            oracle_live_stable._CLOSING.clear()
            oracle_live_stable._STOP_HOOKS.clear()
        live.reset_for_tests()
    reset()
    ring = _live_ring()["ring"]
    grant = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")["live"]
    up = Upstream()
    connects = []
    monkeypatch.setattr(sock, "_connect", lambda key: connects.append(key) or up)
    monkeypatch.setattr(live, "device_ok", lambda principal: True)
    from lib import config

    class Cfg:
        def openai_key(self):
            return "sk-test"
    monkeypatch.setattr(config, "load", lambda: Cfg())
    state = type("Call", (), {"grant": grant, "up": up, "connects": connects, "env": sander})()
    yield state
    reset()


def _join(grant, **change):
    frame = {"type": "agent_live.join", **{k: grant[k] for k in sock.JOIN_KEYS}}
    frame.update(change)
    return frame


def _serve(handler):
    thread = threading.Thread(target=sock.serve, args=(handler,), daemon=True)
    thread.start()
    return thread


def _types(handler):
    return [f["type"] for f in handler.wfile.frames()]


def _ring_of(grant):
    return next(r for r in rings._load() if r.get("_live_call_id") == grant["call_id"])


def _ready(grant):
    return {"type": "agent_live.client_ready", "call_id": grant["call_id"], "call_epoch": grant["call_epoch"],
            "attempt_id": grant["attempt_id"], "audio": True, "capture": True}


def _admitted(call):
    """Join, provider ready, client ready, opening admitted: the live part of a call."""
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    return h, thread


# ---- the whole call ---------------------------------------------------------------------

def test_a_live_call_joins_opens_once_talks_and_hangs_up(call):
    h, thread = _admitted(call)
    frames = h.wfile.frames()
    session = next(f for f in frames if f["type"] == "agent_live.session")
    g = call.grant
    assert session == {"type": "agent_live.session", "protocol": 1, "host_id": g["host_id"], "ring_id": g["ring_id"],
                       "agent_id": "a-sander", "call_id": g["call_id"], "call_epoch": 1, "attempt_id": g["attempt_id"],
                       "context_digest": g["context_digest"], "provider": "openai", "model": "gpt-live-1",
                       "voice": "marin"}
    admitted = next(f for f in frames if f["type"] == "agent_live.opening_admitted")
    assert admitted == {"type": "agent_live.opening_admitted", "call_id": g["call_id"], "call_epoch": 1,
                        "attempt_id": g["attempt_id"], "context_digest": g["context_digest"]}
    assert _types(h)[:3] == ["agent_live.session", "agent_live.provider_ready", "agent_live.opening_admitted"]
    # The upstream session is the fixed one, built from the snapshot only.
    [start] = call.up.of("session.start")
    assert start["session"]["model"] == "gpt-live-1" and start["session"]["audio"]["output"]["voice"] == "marin"
    assert start["session"]["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert "Vann renner fra Farris-flaska" in start["session"]["instructions"]
    assert call.up.of("session.instructions.append") == [
        {"type": "session.instructions.append", "content": "OPENING: say exactly this, once, now: " + OPENING,
         "delegation_id": None}]
    assert _ring_of(g)["_live_opening_consumed"] is True and _ring_of(g)["_live_phase"] == "active"
    # Silence went upstream until the phone's first audio; after it, only the phone's audio.
    _wait(lambda: call.up.of("session.input_audio.append"))
    assert all(e["audio"] == sock.SILENCE for e in call.up.of("session.input_audio.append"))
    h.send({"type": "session.input_audio.append", "audio": AUDIO})
    _wait(lambda: any(e["audio"] == AUDIO for e in call.up.of("session.input_audio.append")))
    assert _ring_of(g)["_live_audio_forwarded"] is True
    time.sleep(0.3)
    after = call.up.sent[[e.get("audio") for e in call.up.sent].index(AUDIO) + 1:]
    assert not any(e.get("audio") == sock.SILENCE for e in after)
    call.up.push({"type": "session.output_audio.delta", "delta": AUDIO})
    _wait(lambda: {"type": "session.output_audio.delta", "delta": AUDIO} in h.wfile.frames())
    h.send({"type": "session.close"})
    thread.join(15)
    assert not thread.is_alive()
    closed = h.wfile.frames()[-1]
    assert closed == {"type": "agent_live.closed", "call_id": g["call_id"], "call_epoch": 1,
                      "attempt_id": g["attempt_id"], "reason": "user_hangup"}
    assert _ring_of(g)["_live_phase"] == "retired" and call.up.of("session.close") and call.up.closed
    assert oracle_realtime._ACTIVE_PRINCIPALS == {} and live._SESSIONS == {}
    assert g["owner_nonce"] not in bytes(h.wfile.data).decode(errors="replace")
    assert g["join_ticket"] not in bytes(h.wfile.data).decode(errors="replace")


def test_the_opening_is_admitted_once_however_often_the_phone_says_ready(call):
    h, thread = _admitted(call)
    h.send(_ready(call.grant))
    h.send(_ready(call.grant))
    call.up.push({"type": "session.instructions.appended"})
    time.sleep(0.3)
    assert len(call.up.of("session.instructions.append")) == 1
    assert _types(h).count("agent_live.opening_admitted") == 1
    h.send({"type": "session.close"})
    thread.join(15)


def test_ready_before_the_provider_still_opens_once_after_it(call, monkeypatch):
    h = Handler()
    slow = Upstream()
    slow.send_start = slow.send

    def send(raw):
        event = json.loads(raw)
        if event["type"] == "session.start":
            slow.sent.append(event)          # started arrives only when the test says so
            return
        slow.send_start(raw)
    slow.send = send
    monkeypatch.setattr(sock, "_connect", lambda key: slow)
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.session" in _types(h))
    h.send(_ready(call.grant))
    time.sleep(0.2)
    assert slow.of("session.instructions.append") == []
    slow.push({"type": "session.started"})
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    assert len(slow.of("session.instructions.append")) == 1
    h.send({"type": "session.close"})
    thread.join(15)


def test_client_ready_without_audio_and_capture_is_not_ready(call):
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send({**_ready(call.grant), "capture": False})
    time.sleep(0.3)
    assert call.up.of("session.instructions.append") == [] and not _ring_of(call.grant).get("_live_opening_consumed")
    h.send({"type": "session.close"})
    thread.join(15)


# ---- refusals ---------------------------------------------------------------------------

def _refused_join(call, frame, code, handler=None):
    h = handler or Handler()
    thread = _serve(h)
    h.send(frame)
    thread.join(15)
    assert not thread.is_alive()
    [error] = h.wfile.frames()
    assert error == {"type": "agent_live.error", "call_id": "", "call_epoch": 0, "attempt_id": "", "code": code,
                     "message": code, "retryable": False}
    return h


@pytest.mark.parametrize("change,code", [
    ({"owner_nonce": "0" * 64}, "live_owner_conflict"), ({"join_ticket": "0" * 64}, "live_owner_conflict"),
    ({"attempt_id": "att_x"}, "live_owner_conflict"), ({"call_id": "alv_x"}, "live_owner_conflict"),
    ({"call_epoch": 2}, "live_call_stale"), ({"ring_id": "rng_x"}, "live_call_stale"),
    ({"context_digest": "0" * 64}, "live_call_stale"), ({"host_id": "host_other"}, "live_call_stale"),
    ({"protocol": 2}, "live_call_stale"), ({"type": "agent_live.hello"}, "live_owner_conflict"),
])
def test_a_join_that_does_not_match_its_grant_is_refused_before_anything_paid(call, change, code):
    _refused_join(call, _join(call.grant, **change), code)
    assert call.connects == [] and not _ring_of(call.grant).get("_live_join_consumed")
    assert oracle_realtime._ACTIVE_PRINCIPALS == {}


def test_a_join_from_another_phone_is_an_owner_conflict(call):
    _refused_join(call, _join(call.grant), "live_owner_conflict", Handler(principal=OTHER_PHONE))
    assert call.connects == []


def test_the_join_ticket_is_single_use(call):
    h, thread = _admitted(call)
    busy = Handler()
    sock.serve(busy)                                                    # the owner's voice slot is busy…
    assert busy.status == 409 and busy.body() == {"error": "live_owner_conflict"}
    h.send({"type": "session.close"})
    thread.join(15)
    _refused_join(call, _join(call.grant), "live_join_consumed")        # …and once free, the ticket is spent
    assert len(call.connects) == 1


def test_a_join_after_the_ticket_expired_is_refused(call):
    call.env.clock.advance(rings.LIVE_JOIN_MS / 1000)
    _refused_join(call, _join(call.grant), "live_join_expired")
    assert call.connects == []


def test_a_join_after_close_is_stale(call):
    live.close(PHONE, {"call_id": call.grant["call_id"], "call_epoch": 1, "attempt_id": call.grant["attempt_id"],
                       "owner_nonce": call.grant["owner_nonce"], "request_id": "q", "reason": "user_hangup"})
    _refused_join(call, _join(call.grant), "live_call_stale")


def test_a_busy_voice_owner_is_refused_before_the_upgrade_and_never_superseded(call):
    token = oracle_realtime.claim_session(PHONE)
    stopped = []
    oracle_live_stable.register_stop(PHONE, token, lambda: stopped.append(True))
    h = Handler()
    sock.serve(h)
    assert h.status == 409 and h.body() == {"error": "live_owner_conflict"}
    assert stopped == [] and oracle_realtime._ACTIVE_PRINCIPALS[PHONE] == token and call.connects == []


def test_a_caller_without_validated_full_scope_gets_the_unchanged_calls_auth_body(call):
    # The shared layer already stops limited devices (403) and bad tokens (401); this route
    # guard only meets a local caller let through unvalidated, and answers exactly like the
    # other /calls routes (no live_auth rewrite).
    h = Handler(scope="limited")
    sock.serve(h)
    assert h.status == 401 and h.body() == {"error": "Group calls require full-device authentication"}


def test_phone_audio_before_the_opening_was_admitted_is_refused_not_dropped(call):
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send({"type": "session.input_audio.append", "audio": AUDIO})
    thread.join(15)
    error = next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")
    assert error["code"] == "audio_not_ready" and error["retryable"] is False
    assert not any(e.get("audio") == AUDIO for e in call.up.sent)
    assert _ring_of(call.grant)["_live_phase"] == "retired" and not _ring_of(call.grant).get("_live_audio_forwarded")


def test_an_opening_the_provider_never_takes_fails_visibly(call, monkeypatch):
    monkeypatch.setattr(sock, "OPENING_ACK_S", 0.3)
    call.up.take_opening = False
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert "agent_live.opening_admitted" not in _types(h)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert _ring_of(call.grant)["_live_phase"] == "retired"


def test_a_provider_that_cannot_be_reached_tombstones_the_call(call, monkeypatch):
    def down(key):
        call.connects.append(key)
        raise OSError("refused")
    monkeypatch.setattr(sock, "_connect", down)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    thread.join(15)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert _ring_of(call.grant)["_live_phase"] == "retired"
    _refused_join(call, _join(call.grant), "live_join_consumed")        # never retried
    assert len(call.connects) == 1


# ---- retirement from outside ---------------------------------------------------------------

def test_an_http_close_retires_authority_then_ends_the_socket(call):
    h, thread = _admitted(call)
    out = live.close(PHONE, {"call_id": call.grant["call_id"], "call_epoch": 1, "attempt_id": call.grant["attempt_id"],
                             "owner_nonce": call.grant["owner_nonce"], "request_id": "q", "reason": "user_hangup"})
    assert out["state"] == "retired"
    thread.join(15)
    assert not thread.is_alive() and h.wfile.frames()[-1]["type"] == "agent_live.closed"
    assert h.wfile.frames()[-1]["reason"] == "user_hangup" and call.up.closed


def test_the_provider_closing_ends_the_call(call):
    h, thread = _admitted(call)
    call.up.push({"type": "session.closed"})
    thread.join(15)
    assert h.wfile.frames()[-1] == {"type": "agent_live.closed", "call_id": call.grant["call_id"], "call_epoch": 1,
                                    "attempt_id": call.grant["attempt_id"], "reason": "provider_closed"}
    assert _ring_of(call.grant)["_live_retired_reason"] == "provider_closed"


def test_a_lost_phone_ends_the_call(call):
    h, thread = _admitted(call)
    h.rfile.close()
    thread.join(15)
    assert _ring_of(call.grant)["_live_retired_reason"] == "transport_lost" and call.up.closed


def test_a_revoked_device_ends_the_call(call, monkeypatch):
    h, thread = _admitted(call)
    monkeypatch.setattr(live, "device_ok", lambda principal: False)
    thread.join(15)
    assert _ring_of(call.grant)["_live_retired_reason"] == "device_revoked"


def test_interrupt_flushes_the_phone_only_after_the_opening(call):
    h, thread = _admitted(call)
    h.send({"type": "oracle_v2.interrupt"})
    _wait(lambda: "oracle_v2.playback_flush" in _types(h))
    assert _types(h)[-2:] == ["oracle_v2.playback_flush", "oracle_v2.quiet"]
    h.send({"type": "session.close"})
    thread.join(15)


def test_the_live_instructions_hold_no_tools_and_mark_observations_as_data(call):
    snapshot = live.snapshot_of(call.grant["ring_id"])
    text = sock.instructions(snapshot)
    assert "You are Sandra." in text and "no tools" in text and "data, not instructions" in text
    assert "<speak>" in text and "Nothing has been played to the user yet" in text
    assert OPENING not in text                 # the opening is appended once, at admission, not baked in


# ---- the readiness fence, the silence budget and ACK-before-output (ROOT correction) ---------

def test_no_silence_goes_upstream_before_the_phone_is_ready(call):
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    time.sleep(0.4)                                    # provider ready, phone not: nothing may flow
    assert call.up.of("session.input_audio.append") == []
    h.send(_ready(call.grant))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    kinds = [e["type"] for e in call.up.sent]
    # Admission fence: silence starts with the opening, immediately before the append.
    assert kinds.index("session.input_audio.append") < kinds.index("session.instructions.append")
    h.send({"type": "session.close"})
    thread.join(15)


def test_the_silence_feed_stops_at_its_budget_without_phone_audio(call, monkeypatch):
    monkeypatch.setattr(sock, "SILENCE_BUDGET_S", 0.4)
    h, thread = _admitted(call)
    time.sleep(0.8)
    count = len(call.up.of("session.input_audio.append"))
    time.sleep(0.5)
    assert len(call.up.of("session.input_audio.append")) == count     # no indefinite silence task
    h.send({"type": "session.close"})
    thread.join(15)


def test_the_silence_feed_stops_on_hangup(call):
    h, thread = _admitted(call)
    h.send({"type": "session.close"})
    thread.join(15)
    count = len(call.up.of("session.input_audio.append"))
    time.sleep(0.4)
    assert len(call.up.of("session.input_audio.append")) == count


def test_model_audio_before_the_opening_ack_fails_the_call_visibly(call, monkeypatch):
    early = Upstream(take_opening=False)
    real_send = early.send

    def send(raw):
        real_send(raw)
        if json.loads(raw)["type"] == "session.instructions.append":
            early.push({"type": "session.output_audio.delta", "delta": AUDIO})      # audio before the receipt
            early.push({"type": "session.instructions.appended"})
    early.send = send
    monkeypatch.setattr(sock, "_connect", lambda key: early)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert "session.output_audio.delta" not in _types(h) and "agent_live.opening_admitted" not in _types(h)
    error = next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")
    assert error["code"] == "live_unavailable" and error["retryable"] is False
    assert _ring_of(call.grant)["_live_phase"] == "retired" and _ring_of(call.grant)["_live_opening_consumed"]


def test_output_before_the_ack_beyond_its_bound_fails_visibly(call, monkeypatch):
    noisy = Upstream(take_opening=False)
    real_send = noisy.send

    def send(raw):
        real_send(raw)
        if json.loads(raw)["type"] == "session.instructions.append":
            for _ in range(4):
                noisy.push({"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(48000)).decode()})
    noisy.send = send
    monkeypatch.setattr(sock, "_connect", lambda key: noisy)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert "session.output_audio.delta" not in _types(h) and "agent_live.opening_admitted" not in _types(h)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"


@pytest.mark.parametrize("failure", ["no_receipt", "provider_error"])
def test_an_uncertain_admission_keeps_the_opening_consumed_and_refuses_fallback(call, monkeypatch, failure):
    monkeypatch.setattr(sock, "OPENING_ACK_S", 0.3)
    call.up.take_opening = False
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    _wait(lambda: call.up.of("session.instructions.append"))
    if failure == "provider_error":
        call.up.push({"type": "error", "error": {"code": "context_injection_incomplete"}})
    thread.join(15)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    ring = _ring_of(call.grant)
    assert ring["_live_opening_consumed"] is True and ring["_live_phase"] == "retired"
    from lib import group_calls
    with pytest.raises(group_calls.CallError) as exc:
        live.fallback(PHONE, {"call_id": call.grant["call_id"], "expected_epoch": 1, "attempt_id": call.grant["attempt_id"],
                              "owner_nonce": call.grant["owner_nonce"], "request_id": "fb", "audio_stopped": True})
    assert (exc.value.status, exc.value.error) == (409, "live_fallback_not_allowed")
    assert _ring_of(call.grant)["_live_opening_consumed"] is True


@pytest.mark.parametrize("drop", ["Upgrade", "Sec-WebSocket-Key"])
def test_a_request_that_is_not_a_websocket_upgrade_gets_one_fixed_refusal(call, drop):
    h = Handler()
    del h.headers[drop]
    sock.serve(h)
    assert h.status == 426 and h.body() == {"error": "live_websocket_required"}
    assert call.connects == [] and oracle_realtime._ACTIVE_PRINCIPALS == {}


# ---- the setup deadline: pings do not keep an unready call (and its paid upstream) alive ------

def test_a_phone_that_never_gets_ready_is_failed_at_the_setup_deadline(call, monkeypatch):
    monkeypatch.setattr(sock, "SETUP_S", 0.4)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    for _ in range(6):                                  # pings alone never count as readiness
        h.rfile.feed(_masked(0x9, b""))
        time.sleep(0.1)
    thread.join(15)
    assert not thread.is_alive()
    error = next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")
    assert error["code"] == "audio_not_ready" and error["call_id"] == call.grant["call_id"]
    assert call.up.of("session.instructions.append") == [] and call.up.of("session.input_audio.append") == []
    assert _ring_of(call.grant)["_live_phase"] == "retired" and call.up.closed
    assert not _ring_of(call.grant).get("_live_opening_consumed")


def test_a_provider_that_never_starts_is_failed_at_the_setup_deadline(call, monkeypatch):
    monkeypatch.setattr(sock, "SETUP_S", 0.4)
    mute = Upstream()
    mute.send = lambda raw: mute.sent.append(json.loads(raw))     # never answers session.start
    monkeypatch.setattr(sock, "_connect", lambda key: mute)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.session" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert "agent_live.provider_ready" not in _types(h) and _ring_of(call.grant)["_live_phase"] == "retired"


def test_no_provider_frame_reaches_the_phone_after_retirement(call):
    h, thread = _admitted(call)
    live.close(PHONE, {"call_id": call.grant["call_id"], "call_epoch": 1, "attempt_id": call.grant["attempt_id"],
                       "owner_nonce": call.grant["owner_nonce"], "request_id": "q", "reason": "user_hangup"})
    thread.join(15)
    before = len(h.wfile.frames())
    session_frames = [f["type"] for f in h.wfile.frames()]
    assert session_frames[-1] == "agent_live.closed"
    call.up.push({"type": "session.output_audio.delta", "delta": AUDIO})
    call.up.push({"type": "session.instructions.appended"})
    time.sleep(0.3)
    assert len(h.wfile.frames()) == before


def test_a_client_ready_after_retirement_starts_nothing(call):
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    live.close(PHONE, {"call_id": call.grant["call_id"], "call_epoch": 1, "attempt_id": call.grant["attempt_id"],
                       "owner_nonce": call.grant["owner_nonce"], "request_id": "q", "reason": "user_hangup"})
    thread.join(15)
    h.send(_ready(call.grant))                          # too late: the call's authority is gone
    time.sleep(0.3)
    assert call.up.of("session.instructions.append") == [] and call.up.of("session.input_audio.append") == []
    assert not _ring_of(call.grant).get("_live_opening_consumed")
    assert "agent_live.opening_admitted" not in _types(h)


# ---- drain-review re-review findings (H-1 residue, H-2 a/c, L-2) -------------------------------

def test_model_audio_before_the_opening_was_even_sent_fails_the_call_visibly(call, monkeypatch):
    eager = Upstream()
    real_send = eager.send

    def send(raw):
        real_send(raw)
        if json.loads(raw)["type"] == "session.start":
            eager.push({"type": "session.output_audio.delta", "delta": AUDIO})     # an unadmitted opening
    eager.send = send
    monkeypatch.setattr(sock, "_connect", lambda key: eager)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    thread.join(15)
    assert "session.output_audio.delta" not in _types(h)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert not _ring_of(call.grant).get("_live_opening_consumed")       # nothing consumed: fallback stays open


def test_the_instructions_tell_the_model_to_stay_silent_until_the_opening():
    from tests.unit.test_agent_ring_live import _context
    snapshot = {"profile": {"display_name": "Sandra"}, "texts": {}, "event_context": _context()}
    text = sock.instructions(snapshot)
    assert "Say nothing until you are given the opening line" in text


def test_a_session_whose_record_was_retired_elsewhere_stops_itself(call):
    h, thread = _admitted(call)
    with rings._lock:                                  # authority retired without telling the session
        records = rings._load()
        ring = next(r for r in records if r.get("_live_call_id") == call.grant["call_id"])
        live.retire(ring, "elsewhere")
        rings._save(records)
    thread.join(15)
    assert not thread.is_alive() and h.wfile.frames()[-1]["type"] == "agent_live.closed"
    assert call.up.closed


@pytest.mark.parametrize("change,code", [
    ({"protocol": True}, "live_owner_conflict"), ({"call_epoch": True}, "live_owner_conflict"),
    ({"call_epoch": 1.0}, "live_owner_conflict"), ({"protocol": "1"}, "live_owner_conflict"),
    ({"protocol": 2}, "live_call_stale"), ({"call_epoch": 3}, "live_call_stale"),
])
def test_a_join_with_a_malformed_number_is_an_owner_conflict_and_a_wrong_value_is_stale(call, change, code):
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant, **change))
    thread.join(15)
    assert [f["code"] for f in h.wfile.frames()] == [code]
    assert call.connects == []


def test_a_hangup_during_the_paid_connect_never_leaves_the_session_running(call, monkeypatch):
    entered, release, up = threading.Event(), threading.Event(), Upstream()

    def slow_connect(key):
        entered.set()
        assert release.wait(5)
        return up
    monkeypatch.setattr(sock, "_connect", slow_connect)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    assert entered.wait(5)
    live.close(PHONE, {"call_id": call.grant["call_id"], "call_epoch": 1, "attempt_id": call.grant["attempt_id"],
                       "owner_nonce": call.grant["owner_nonce"], "request_id": "q", "reason": "user_hangup"})
    release.set()                                         # the connector returns after the hangup
    thread.join(15)
    assert not thread.is_alive() and up.closed and up.of("session.start") == []


def test_a_non_device_principal_is_refused_403_live_auth_before_the_upgrade(call):
    h = Handler(principal="administrator")
    sock.serve(h)                                       # refused at the route: no upgrade, no frame
    assert h.status == 403 and h.body() == {"error": "live_auth"}
    assert call.connects == [] and oracle_realtime._ACTIVE_PRINCIPALS == {}


def test_a_recovery_answer_after_the_join_reports_the_grant_consumed(call):
    h, thread = _admitted(call)
    again = rings.answer(PHONE, call.grant["ring_id"], conversation_mode="agent_live_v1", request_id="r1",
                         client_nonce="c1")
    assert again["live"]["join_consumed"] is True and again["live"]["join_ticket"] == call.grant["join_ticket"]
    assert again["live"]["attempt_id"] == call.grant["attempt_id"] and again["changed"] is False
    h.send({"type": "session.close"})
    thread.join(15)
