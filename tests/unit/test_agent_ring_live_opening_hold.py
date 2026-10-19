"""Agent-live opening ACK vs provider output (2822, 18 Oct 15:57): the real provider can start speaking after the opening
append was sent but before ``session.instructions.appended`` arrives (observed GPT-Live ACK latency ~1.1 s).

Output that arrives after the opening append was actually handed to the upstream socket and before its ACK is HELD on the
Host (bounded by bytes and by the unchanged OPENING_ACK_S), never forwarded before ``agent_live.opening_admitted``; on the
matching ACK the phone gets ``opening_admitted`` first and then the held audio in order, once. No ACK, overflow, an invalid
frame, a retirement or a stale/foreign ACK stay terminal or without effect. Output before the opening send stays fatal.
Ordering is asserted on what was actually written, with a fake upstream (no provider).
"""
from __future__ import annotations

import base64

from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from tests.unit.test_agent_ring_live import PHONE, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_ring_live_socket import (AUDIO, Handler, _join, _ready, _ring_of, _serve, _types, _wait,
                                                     call)  # noqa: F401 - call is a fixture
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture


def _delta(audio=AUDIO):
    return {"type": "session.output_audio.delta", "delta": audio}


def _opening_sent(call):
    """Join, provider ready, client ready; the fake upstream does NOT acknowledge the opening by itself."""
    call.up.take_opening = False
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    _wait(lambda: call.up.of("session.instructions.append"))
    return h, thread


def _ack(call, **extra):
    append = call.up.of("session.instructions.append")[-1]
    return {"type": "session.instructions.appended", "client_event_id": append.get("event_id"), **extra}


def _audio_after(frames, kind="agent_live.opening_admitted"):
    kinds = [f["type"] for f in frames]
    if kind not in kinds:
        return []
    i = kinds.index(kind)
    return [f for f in frames[i + 1:] if f["type"] == "session.output_audio.delta"]


def test_the_opening_append_carries_its_own_event_id(call):
    h, thread = _opening_sent(call)
    append = call.up.of("session.instructions.append")[-1]
    assert isinstance(append.get("event_id"), str) and len(append["event_id"]) >= 16
    call.up.push(_ack(call))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))


def test_output_before_the_ack_is_held_then_follows_the_admission_in_order(call):
    h, thread = _opening_sent(call)
    first = base64.b64encode(b"\x01\x00" * 480).decode()
    second = base64.b64encode(b"\x02\x00" * 480).decode()
    call.up.push(_delta(first))                           # the 15:57 order: speech before the ACK
    call.up.push(_delta(second))
    call.up.push(_ack(call))
    _wait(lambda: len(_audio_after(h.wfile.frames())) == 2)
    frames = h.wfile.frames()
    kinds = [f["type"] for f in frames]
    assert kinds.count("agent_live.opening_admitted") == 1
    assert "agent_live.error" not in kinds
    i = kinds.index("agent_live.opening_admitted")
    assert all(f["type"] != "session.output_audio.delta" for f in frames[:i])      # nothing before the admission
    assert [f["delta"] for f in _audio_after(frames)] == [first, second]           # held audio, in order, once
    call.up.push(_delta())                                                          # live output after the drain
    _wait(lambda: len(_audio_after(h.wfile.frames())) == 3)


def test_a_foreign_ack_does_not_admit_and_the_matching_one_does(call):
    h, thread = _opening_sent(call)
    call.up.push(_delta())
    call.up.push(_ack(call, client_event_id="someone-elses-append"))
    call.up.push({"type": "session.thinking.appended", "client_event_id": "x"})   # another channel's receipt
    call.up.push(_ack(call))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    assert _types(h).count("agent_live.opening_admitted") == 1


def test_a_held_opening_without_any_ack_fails_and_its_audio_never_reaches_the_phone(call, monkeypatch):
    monkeypatch.setattr(sock, "OPENING_ACK_S", 0.4)
    h, thread = _opening_sent(call)
    call.up.push(_delta())
    thread.join(15)
    frames = h.wfile.frames()
    assert all(f["type"] != "session.output_audio.delta" for f in frames)
    assert "agent_live.opening_admitted" not in [f["type"] for f in frames]
    assert next(f for f in frames if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_not_taken"


def test_held_output_beyond_its_byte_cap_is_terminal_and_never_forwarded(call, monkeypatch):
    monkeypatch.setattr(sock, "OPENING_HOLD_BYTES", 1920)
    h, thread = _opening_sent(call)
    chunk = base64.b64encode(bytes(1000)).decode()
    call.up.push(_delta(chunk))
    call.up.push(_delta(chunk))                           # 2000 > 1920: overflow, nothing truncated
    thread.join(15)
    frames = h.wfile.frames()
    assert all(f["type"] != "session.output_audio.delta" for f in frames)
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_output_overflow"


def test_an_invalid_held_frame_is_terminal(call):
    h, thread = _opening_sent(call)
    call.up.push(_delta("%%%not-base64%%%"))
    thread.join(15)
    assert _ring_of(call.grant)["_live_retired_reason"] == "provider_output_invalid"
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())


def test_a_retirement_during_the_hold_discards_it(call):
    h, thread = _opening_sent(call)
    call.up.push(_delta())
    _wait(lambda: live._session_for(call.grant["call_id"]) is not None)
    session = live._session_for(call.grant["call_id"])
    _wait(lambda: session._held_bytes > 0)
    live.close(PHONE, {"call_id": call.grant["call_id"], "attempt_id": call.grant["attempt_id"],
                       "owner_nonce": call.grant["owner_nonce"], "request_id": "q1", "call_epoch": 1,
                       "reason": "user_hangup"})
    _wait(lambda: session.closed.is_set())
    session.receive(_ack(call))                           # a late ACK after the retirement
    thread.join(15)
    frames = h.wfile.frames()
    assert all(f["type"] != "session.output_audio.delta" for f in frames)
    assert "agent_live.opening_admitted" not in [f["type"] for f in frames]
    assert session._held == []


def test_a_duplicate_ack_admits_once_and_never_replays_the_held_audio(call):
    h, thread = _opening_sent(call)
    call.up.push(_delta())
    call.up.push(_ack(call))
    call.up.push(_ack(call))
    _wait(lambda: len(_audio_after(h.wfile.frames())) == 1)
    call.up.push(_delta(base64.b64encode(b"\x03\x00" * 10).decode()))
    _wait(lambda: len(_audio_after(h.wfile.frames())) == 2)
    assert _types(h).count("agent_live.opening_admitted") == 1
    assert [f["delta"] for f in _audio_after(h.wfile.frames())][0] == AUDIO      # held audio exactly once


def test_output_before_the_opening_was_sent_stays_fatal(call):
    call.up.take_opening = False
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    call.up.push(_delta())                                # no client_ready: no opening yet
    thread.join(15)
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())
    assert _ring_of(call.grant)["_live_retired_reason"] == "provider_output_before_ack"
    assert not call.up.of("session.instructions.append")


def test_the_upstream_order_log_is_metadata_only_and_never_breaks_the_call(call, monkeypatch):
    seen = []
    monkeypatch.setattr(sock, "_quiet_note", lambda name, detail: seen.append((name, detail)))
    h, thread = _opening_sent(call)
    call.up.push(_delta())
    call.up.push(_ack(call))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    session = live._session_for(call.grant["call_id"])
    session.retire("user_hangup")
    thread.join(15)
    order = [d for n, d in seen if n == "agentLiveUpstreamOrder"]
    assert order and "session.output_audio.delta" in order[0] and "session.instructions.appended" in order[0]
    assert AUDIO not in order[0] and "OPENING" not in order[0]                       # no audio, no content


# ---- ROOT rev 2: the uncorrelated-ACK fallback, the send stages, a reentrant fast ACK, a send failure ----

def _session_of(call):
    _wait(lambda: live._session_for(call.grant["call_id"]) is not None)
    return live._session_for(call.grant["call_id"])


def test_an_ack_without_client_event_id_is_accepted_only_as_the_single_issued_append_and_marked(call, monkeypatch):
    seen = []
    monkeypatch.setattr(sock, "_quiet_note", lambda name, detail: seen.append((name, detail)))
    h, thread = _opening_sent(call)
    session = _session_of(call)
    assert session.opening_issued and session.instructions_appends_issued == 1
    call.up.push({"type": "session.instructions.appended"})          # no client_event_id
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    assert session._ack_kind == "uncorrelated_single_outstanding"
    session.retire("user_hangup")
    thread.join(15)
    assert any("ack=uncorrelated_single_outstanding" in d for n, d in seen if n == "agentLiveUpstreamOrder")


def test_an_ack_without_client_event_id_is_rejected_when_more_than_one_append_was_issued(call):
    h, thread = _opening_sent(call)
    session = _session_of(call)
    with session._state:
        session.instructions_appends_issued = 2                         # ambiguous: another append outstanding
    call.up.push(_delta())
    call.up.push({"type": "session.instructions.appended"})
    call.up.push({"type": "session.output_audio.delta", "delta": AUDIO})
    import time
    time.sleep(0.3)
    assert "agent_live.opening_admitted" not in _types(h)
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())
    call.up.push(_ack(call))                                              # the correlated receipt still admits
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    assert session._ack_kind == "correlated"


def test_a_fast_uncorrelated_ack_during_the_send_is_decided_after_the_append_was_issued(call):
    call.up.take_opening = False
    original = call.up.send

    def send(raw):
        original(raw)
        import json
        if json.loads(raw)["type"] == "session.instructions.append":
            session = live._session_for(call.grant["call_id"])
            assert session.opening_send_started and not session.opening_issued     # inside the send
            session.receive({"type": "session.instructions.appended"})            # reentrant, before issued
            assert not session.admitted and session._pending_ack is not None
    call.up.send = send
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    session = _session_of(call)
    assert session._ack_kind == "uncorrelated_single_outstanding" and session._pending_ack is None
    assert _types(h).count("agent_live.opening_admitted") == 1


def test_a_failed_opening_send_ends_the_call_and_nothing_is_held_or_forwarded(call):
    call.up.take_opening = False
    original = call.up.send

    def send(raw):
        import json
        if json.loads(raw)["type"] == "session.instructions.append":
            raise OSError("upstream write failed")
        original(raw)
    call.up.send = send
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_send_failed"
    assert "agent_live.opening_admitted" not in _types(h)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"


def test_output_during_the_drain_window_follows_the_held_audio_never_precedes_admission(call):
    h, thread = _opening_sent(call)
    session = _session_of(call)
    first = base64.b64encode(b"\x05\x00" * 100).decode()
    call.up.push(_delta(first))
    _wait(lambda: session._held_bytes > 0)
    original_record = session._record

    def slow_record(change):                      # the ring write (outside the emit lock) takes a while
        import time
        ok = original_record(change)
        session._output(base64.b64encode(b"\x06\x00" * 100).decode())   # live output racing the admission
        time.sleep(0.05)
        return ok
    session._record = slow_record
    call.up.push(_ack(call))
    _wait(lambda: len(_audio_after(h.wfile.frames())) == 2)
    frames = h.wfile.frames()
    kinds = [f["type"] for f in frames]
    i = kinds.index("agent_live.opening_admitted")
    assert all(f["type"] != "session.output_audio.delta" for f in frames[:i])
    assert [f["delta"] for f in _audio_after(frames)] == [first, base64.b64encode(b"\x06\x00" * 100).decode()]


def test_output_between_the_reservation_and_the_actual_append_send_stays_fatal(call):
    """opening_sent is reserved before SILENCE and before the append; output in that window is not attributable."""
    import time
    call.up.take_opening = False
    original = call.up.send

    def send(raw):
        import json
        event = json.loads(raw)
        if event["type"] == "session.input_audio.append" and event["audio"] == sock.SILENCE \
                and not call.up.of("session.instructions.append"):
            session = live._session_for(call.grant["call_id"])
            if session is not None and session.opening_sent and not session.opening_send_started:
                call.up.push(_delta())            # provider output while only the reservation and SILENCE exist
                time.sleep(0.3)                   # the upstream thread handles it before the append can go out
        original(raw)
    call.up.send = send
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert _ring_of(call.grant)["_live_retired_reason"] == "provider_output_before_ack"
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())
    assert "agent_live.opening_admitted" not in _types(h)


# ---- ROOT focused review: reservation/SILENCE ACK, send failure with a fast ACK, encoded bound, throwing logger ----

def test_an_ack_during_the_reservation_or_silence_never_admits_then_the_sole_issued_append_admits_once(call):
    import time
    call.up.take_opening = False
    original = call.up.send

    def send(raw):
        import json
        event = json.loads(raw)
        if event["type"] == "session.input_audio.append" and event["audio"] == sock.SILENCE \
                and not call.up.of("session.instructions.append"):
            session = live._session_for(call.grant["call_id"])
            if session is not None and session.opening_sent and not session.opening_send_started:
                session.receive({"type": "session.instructions.appended"})       # no id, before the append exists
                assert not session.admitted and session._pending_ack is None
        original(raw)
    call.up.send = send
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    _wait(lambda: call.up.of("session.instructions.append"))
    time.sleep(0.2)
    assert "agent_live.opening_admitted" not in _types(h)
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())
    call.up.push({"type": "session.instructions.appended"})                      # the sole issued append, no id
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    assert _types(h).count("agent_live.opening_admitted") == 1
    assert _session_of(call)._ack_kind == "uncorrelated_single_outstanding"


def test_a_send_failure_with_a_fast_matched_ack_and_output_leaks_nothing_and_does_not_deadlock(call):
    call.up.take_opening = False
    original = call.up.send

    def send(raw):
        import json
        event = json.loads(raw)
        if event["type"] == "session.instructions.append":
            session = live._session_for(call.grant["call_id"])
            session.receive(_delta())                                               # held after the send mark
            session.receive({"type": "session.instructions.appended", "client_event_id": event["event_id"]})
            raise OSError("upstream write failed after the provider saw it")
        original(raw)
    call.up.send = send
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert not thread.is_alive()                                                    # no reentrant deadlock
    frames = h.wfile.frames()
    assert "agent_live.opening_admitted" not in [f["type"] for f in frames]
    assert all(f["type"] != "session.output_audio.delta" for f in frames)
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_send_failed"


def test_an_oversized_encoded_delta_is_overflow_before_any_decode(call, monkeypatch):
    h, thread = _opening_sent(call)
    decoded = []
    real = sock.base64.b64decode
    monkeypatch.setattr(sock.base64, "b64decode", lambda *a, **k: decoded.append(1) or real(*a, **k))
    big = base64.b64encode(bytes(sock.OPENING_HOLD_BYTES + 3)).decode()           # 192004 chars > 192000
    assert len(big) > sock.OPENING_HOLD_FRAME_B64
    call.up.push(_delta(big))
    thread.join(15)
    assert not decoded                                                               # rejected before decoding
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_output_overflow"
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())


def test_a_throwing_logger_cannot_skip_the_overflow_fail_or_the_retire(call, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("stderr gone")
    monkeypatch.setattr(sock, "OPENING_HOLD_BYTES", 1920)
    h, thread = _opening_sent(call)
    monkeypatch.setattr(sock, "log", boom)                                          # every log() call now raises
    chunk = base64.b64encode(bytes(1000)).decode()
    call.up.push(_delta(chunk))
    call.up.push(_delta(chunk))
    thread.join(15)
    assert not thread.is_alive()
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_output_overflow"
    assert all(f["type"] != "session.output_audio.delta" for f in h.wfile.frames())


def test_a_matched_ack_after_the_send_threw_but_before_the_durable_fail_admits_nothing(call, monkeypatch):
    call.up.take_opening = False
    original = call.up.send
    injected = []

    def send(raw):
        import json
        if json.loads(raw)["type"] == "session.instructions.append":
            raise OSError("upstream write failed")
        original(raw)
    call.up.send = send
    real_fail = sock.LiveSocket.fail

    def fail(self, code, reason):
        if reason == "opening_send_failed" and not injected:     # the window: thrown, terminal marked, fail not yet run
            injected.append(1)
            append_id = self.opening_event_id
            self.receive({"type": "session.output_audio.delta", "delta": AUDIO})
            self.receive({"type": "session.instructions.appended", "client_event_id": append_id})
            assert not self.admitted and self._held == []
        return real_fail(self, code, reason)
    monkeypatch.setattr(sock.LiveSocket, "fail", fail)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    thread.join(15)
    assert injected
    frames = h.wfile.frames()
    assert "agent_live.opening_admitted" not in [f["type"] for f in frames]
    assert all(f["type"] != "session.output_audio.delta" for f in frames)
    assert _ring_of(call.grant)["_live_retired_reason"] == "opening_send_failed"


def test_a_fast_correlated_ack_during_a_successful_send_admits_exactly_once_after_it(call):
    call.up.take_opening = False
    original = call.up.send

    def send(raw):
        import json
        event = json.loads(raw)
        original(raw)
        if event["type"] == "session.instructions.append":
            session = live._session_for(call.grant["call_id"])
            session.receive({"type": "session.instructions.appended", "client_event_id": event["event_id"]})
            assert not session.admitted and session._pending_ack is not None     # deferred until the send returned
    call.up.send = send
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    h.send(_ready(call.grant))
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    session = _session_of(call)
    assert session._ack_kind == "correlated" and session._pending_ack is None
    assert _types(h).count("agent_live.opening_admitted") == 1


# ---- ROOT rev 5: present-but-malformed id, and the terminal-verdict gap ----

import pytest  # noqa: E402


@pytest.mark.parametrize("malformed", [None, "", 123, ["x"], "someone-else"])
def test_only_a_truly_absent_client_event_id_keeps_the_sole_append_fallback(call, malformed):
    h, thread = _opening_sent(call)
    session = _session_of(call)
    session.receive({"type": "session.instructions.appended", "client_event_id": malformed})     # present, malformed
    assert not session.admitted and "agent_live.opening_admitted" not in _types(h)
    session.receive({"type": "session.instructions.appended"})                                  # key truly absent
    _wait(lambda: "agent_live.opening_admitted" in _types(h))
    assert session._ack_kind == "uncorrelated_single_outstanding"
    assert _types(h).count("agent_live.opening_admitted") == 1


@pytest.mark.parametrize("bad, reason", [("overflow", "opening_output_overflow"), ("invalid", "provider_output_invalid")])
def test_a_racing_ack_after_a_terminal_hold_verdict_but_before_the_durable_fail_admits_nothing(call, monkeypatch, bad,
                                                                                                reason):
    if bad == "overflow":
        monkeypatch.setattr(sock, "OPENING_HOLD_BYTES", 1920)
    h, thread = _opening_sent(call)
    session = _session_of(call)
    session.receive(_delta(base64.b64encode(bytes(1000)).decode()))                  # held, valid
    real_fail = sock.LiveSocket.fail
    raced = []

    def fail(self, code, why):
        if why == reason and not raced:                                              # verdict reached, fail not yet run
            raced.append(1)
            self._admit({"type": "session.instructions.appended", "client_event_id": self.opening_event_id})
            assert not self.admitted
        return real_fail(self, code, why)
    monkeypatch.setattr(sock.LiveSocket, "fail", fail)
    session.receive(_delta(base64.b64encode(bytes(1000)).decode() if bad == "overflow" else "%%%bad%%%"))
    thread.join(15)
    assert raced
    frames = h.wfile.frames()
    assert "agent_live.opening_admitted" not in [f["type"] for f in frames]
    assert all(f["type"] != "session.output_audio.delta" for f in frames)
    assert next(f for f in frames if f["type"] == "agent_live.error")["code"] == "live_unavailable"   # receipt kept
    assert _ring_of(call.grant)["_live_retired_reason"] == reason
