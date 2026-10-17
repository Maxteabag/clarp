"""Agent-live rings: the blockers of the focused review of f1666c4d (Solu/ROOT, 17 Oct).

A closure counts only when the provider session actually closed, within a bound measured from before the close
was invoked; a transport that connects after the call ended is closed and never used; a close replays only its
exact original request; a retirement and every effect of the session are ordered by one per-session fence; and
the socket's registry entry and voice claim are released whatever its cleanup throws.
"""
from __future__ import annotations

import threading
import time

import pytest

from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from lib import agent_rings as rings
from lib import group_calls, oracle_realtime
from tests.unit.test_agent_ring_live import PHONE, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_ring_live_hardening import DELTA, _close, _fallback, _provider_ready, _session
from tests.unit.test_agent_ring_live_socket import (Handler, Upstream, _admitted, _join, _ring_of, _serve, _wait,
                                                     call)  # noqa: F401 - call is a fixture
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture


def _refused(status, code, fn, *args, **kw):
    with pytest.raises(group_calls.CallError) as exc:
        fn(*args, **kw)
    assert (exc.value.status, exc.value.body()["error"]) == (status, code)


def _no_classic(call, monkeypatch):
    started = []
    start = group_calls.start
    monkeypatch.setattr(group_calls, "start", lambda *a, **kw: started.append(a) or start(*a, **kw))
    return started


# ---- bounded, truthful closure before any classic call ------------------------------------------

def test_a_hung_provider_close_is_bounded_and_starts_nothing_classic(call, monkeypatch):
    hang = threading.Event()
    real_close = call.up.close
    call.up.close = lambda: hang.wait(30) or real_close()          # the provider never finishes closing
    monkeypatch.setattr(live, "UPSTREAM_CLOSE_S", 0.5)
    started = _no_classic(call, monkeypatch)
    h, thread = _provider_ready(call)
    outcome = []

    def run():
        try:
            outcome.append(_fallback(call.grant))
        except group_calls.CallError as exc:
            outcome.append((exc.status, exc.body()["error"]))
    worker = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    worker.start()
    worker.join(5)
    try:
        assert not worker.is_alive(), "the fallback waited on a hung provider close"
        assert time.monotonic() - t0 < 3
        assert outcome == [(503, "live_unavailable")] and started == []
        assert _ring_of(call.grant)["_live_phase"] == "retired"
    finally:
        hang.set()
        worker.join(5)
        thread.join(15)


def test_a_provider_close_that_fails_is_not_a_closure_and_starts_nothing_classic(call, monkeypatch):
    def broken():
        raise OSError("close failed")
    call.up.close = broken
    monkeypatch.setattr(live, "UPSTREAM_CLOSE_S", 0.5)
    started = _no_classic(call, monkeypatch)
    h, thread = _provider_ready(call)
    _refused(503, "live_unavailable", _fallback, call.grant)
    session = _session(call) if live._session_for(call.grant["call_id"]) else None
    assert started == [] and _ring_of(call.grant)["_live_phase"] == "retired"
    if session is not None:
        assert session.upstream_close_done.is_set() and not session.upstream_closed.is_set()
    thread.join(15)


def test_a_transport_that_connects_after_the_call_ended_is_closed_and_never_used(call, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    late = Upstream()

    def slow_connect(key):
        entered.set()
        release.wait(10)
        return late
    monkeypatch.setattr(sock, "_connect", slow_connect)
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    assert entered.wait(10)
    _close(call.grant)                                   # the owner hangs up while the provider is connecting
    release.set()
    thread.join(15)
    _wait(lambda: late.closed, 5)
    assert late.closed and late.sent in ([], [{"type": "session.close"}])
    assert not late.of("session.start")


# ---- H4: a close replays only its exact original request ----------------------------------------

def test_a_close_replays_only_its_exact_request(call):
    first = _close(call.grant)
    assert _close(call.grant) == first                                     # the same request, retried
    _refused(400, "live_request_invalid", _close, call.grant, call_epoch=7)               # same id, changed body
    _refused(400, "live_request_invalid", _close, call.grant, reason="transport_lost")
    _refused(409, "live_call_stale", _close, call.grant, request_id="q2", call_epoch=7)   # another request
    _refused(409, "live_call_stale", _close, call.grant, request_id="q2")
    assert _ring_of(call.grant)["_live_close_receipt"] == {"request_id": "q1", "call_epoch": 1,
                                                         "reason": "user_hangup"}


def test_a_close_request_id_is_never_cut(call):
    _refused(400, "live_request_invalid", _close, call.grant, request_id="x" * 129)
    long_id = "y" * 128
    _close(call.grant, request_id=long_id)
    assert _ring_of(call.grant)["_live_close_receipt"]["request_id"] == long_id


def test_a_close_with_a_wrong_epoch_is_stale_whatever_the_phase(call):
    h, thread = _admitted(call)
    h.rfile.close()                                       # the socket retires the call on its own (transport lost)
    thread.join(15)
    assert _ring_of(call.grant)["_live_phase"] == "retired"
    _refused(409, "live_call_stale", _close, call.grant, call_epoch=7)


# ---- M1: an answer binds its exact intent -------------------------------------------------------

def test_a_live_answer_binds_its_exact_request_and_nonce(sander):
    from tests.unit.test_agent_ring_live import _live_ring
    ring = _live_ring()["ring"]
    answer = rings.answer
    _refused(400, "live_request_invalid", answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1")
    _refused(400, "live_request_invalid", answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             request_id="r" * 129)
    first = answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r" * 128,
                   client_nonce="n" * 128)
    stored = next(r for r in rings._load() if r["ring_id"] == ring["ring_id"])
    assert (stored["_live_request_id"], stored["_live_client_nonce"]) == ("r" * 128, "n" * 128)
    again = answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r" * 128,
                   client_nonce="n" * 128)
    assert again["changed"] is False and again["live"] == first["live"]
    _refused(400, "live_request_invalid", answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             request_id="r" * 128, client_nonce="other")
    _refused(409, "live_owner_conflict", answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             request_id="another-request", client_nonce="n" * 128)


# ---- one fence orders a retirement and every effect ---------------------------------------------

def test_a_retirement_waits_for_an_effect_that_already_passed_its_check_and_stops_the_next(call):
    h, thread = _admitted(call)
    session = _session(call)
    order, in_write, release = [], threading.Event(), threading.Event()
    downstream = session.downstream

    def slow_downstream(event):
        delta = event.get("type") == "session.output_audio.delta"
        if delta and not release.is_set():
            in_write.set()
            release.wait(5)               # the delta passed its revocation check and has not been written yet
        downstream(event)
        if delta:
            order.append("frame")
    session.downstream = slow_downstream
    effect = threading.Thread(target=session.receive, args=(DELTA,), daemon=True)
    effect.start()
    assert in_write.wait(5)
    closer = threading.Thread(target=lambda: (_close(call.grant), order.append("retired")), daemon=True)
    closer.start()
    time.sleep(0.3)
    release.set()
    effect.join(5)
    closer.join(5)
    assert order == ["frame", "retired"], "an effect that passed its check landed after the retirement"
    session.receive(DELTA)                               # after the retirement: nothing
    assert order == ["frame", "retired"]
    thread.join(15)


# ---- serve releases its registry entry and claim whatever cleanup throws ------------------------

def test_serve_releases_its_registry_and_claim_when_cleanup_throws(call, monkeypatch):
    def broken(self, *a):
        raise RuntimeError("cleanup failed")
    h, thread = _admitted(call)
    monkeypatch.setattr(sock.LiveSocket, "_end_locally", broken)
    monkeypatch.setattr(sock.LiveSocket, "close_upstream", broken)
    h.rfile.close()
    thread.join(15)
    assert not thread.is_alive()
    assert live._SESSIONS == {} and PHONE not in oracle_realtime._ACTIVE_PRINCIPALS


# ---- review of cedd3038 ------------------------------------------------------------------------

def test_a_hung_polite_session_close_send_never_keeps_the_provider_transport_open(call, monkeypatch):
    hang = threading.Event()
    send = call.up.send

    def stuck_send(raw):
        if '"session.close"' in raw:
            hang.wait(30)                                 # the polite goodbye never goes out
        return send(raw)
    call.up.send = stuck_send
    monkeypatch.setattr(live, "UPSTREAM_CLOSE_S", 3.0)
    h, thread = _provider_ready(call)
    t0 = time.monotonic()
    try:
        out = _fallback(call.grant)                       # the transport is force-closed: a real closure
        assert time.monotonic() - t0 < 3 and call.up.closed and out["conversation_mode"] == "classic"
    finally:
        hang.set()
        thread.join(15)


def test_a_retirement_that_cannot_pass_a_stuck_effect_fails_closed(call, monkeypatch):
    monkeypatch.setattr(live, "FENCE_WAIT_S", 0.3)
    h, thread = _admitted(call)
    session = _session(call)
    in_write, release, order = threading.Event(), threading.Event(), []
    downstream = session.downstream

    def stuck_downstream(event):
        delta = event.get("type") == "session.output_audio.delta"
        if delta and not release.is_set():
            in_write.set()
            release.wait(10)                              # still writing long after FENCE_WAIT_S
        downstream(event)
        if delta:
            order.append("frame")
    session.downstream = stuck_downstream
    effect = threading.Thread(target=session.receive, args=(DELTA,), daemon=True)
    effect.start()
    assert in_write.wait(5)
    try:
        assert live._stop_session(call.grant["call_id"], "fallback", wait=0.6) is False   # never a clean stop
        assert call.up.closed and session.stop.is_set()                                   # transport forced shut
        assert session.fence_stuck.is_set()
    finally:
        release.set()
        effect.join(5)
    _wait(lambda: not session.fence_stuck.is_set(), 5)
    assert live._stop_session(call.grant["call_id"], "fallback", wait=1.0) is True        # settled now
    thread.join(15)


def test_an_unconfirmed_closure_survives_the_registry_and_a_fallback_starts_nothing_classic(call, monkeypatch):
    def broken():
        raise OSError("close failed")
    call.up.close = broken
    monkeypatch.setattr(live, "UPSTREAM_CLOSE_S", 0.5)
    started = _no_classic(call, monkeypatch)
    h, thread = _provider_ready(call)
    h.rfile.close()                                       # the phone is gone: the socket retires and ends
    thread.join(15)
    assert live._session_for(call.grant["call_id"]) is None
    _refused(503, "live_unavailable", _fallback, call.grant)
    assert started == []


@pytest.mark.parametrize("field,value", [("request_id", "æ" * 65), ("client_nonce", "ø" * 65),
                                         ("client_nonce", None), ("request_id", "")])
def test_a_live_answer_intent_is_one_to_128_utf8_bytes_and_required(sander, field, value):
    from tests.unit.test_agent_ring_live import _live_ring
    ring = _live_ring()["ring"]
    intent = {"request_id": "r1", "client_nonce": "n1", field: value}
    _refused(400, "live_request_invalid", rings.answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             **intent)
    ok = rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="æ" * 64,
                      client_nonce="ø" * 64)                 # exactly 128 bytes each
    assert ok["changed"] is True


def test_a_close_request_id_is_at_most_128_utf8_bytes(call):
    _refused(400, "live_request_invalid", _close, call.grant, request_id="æ" * 65)
    _close(call.grant, request_id="æ" * 64)


# ---- review of e5a26be0 ------------------------------------------------------------------------

def _stuck_effect(call, session):
    in_write, release = threading.Event(), threading.Event()
    downstream = session.downstream

    def stuck(event):
        if event.get("type") == "session.output_audio.delta" and not release.is_set():
            in_write.set()
            release.wait(10)
        downstream(event)
    session.downstream = stuck
    effect = threading.Thread(target=session.receive, args=(DELTA,), daemon=True)
    effect.start()
    assert in_write.wait(5)
    return effect, release


def test_a_stuck_fence_with_a_throwing_log_still_shuts_both_transports_and_forbids_classic(call, monkeypatch):
    monkeypatch.setattr(live, "FENCE_WAIT_S", 0.3)
    h, thread = _admitted(call)
    session = _session(call)
    effect, release = _stuck_effect(call, session)

    def broken_log(*a, **kw):
        raise OSError("stderr gone")
    monkeypatch.setattr(sock, "log", broken_log)
    raised = []
    closer = threading.Thread(target=lambda: _call_quietly(raised, _close, call.grant), daemon=True)
    try:
        closer.start()
        closer.join(5)
        assert not closer.is_alive() and raised == []          # the close itself never fails on a log
        _wait(lambda: call.up.closed, 5)
        assert session.stop.is_set() and session.fence_stuck.is_set()
        assert live._stop_session(call.grant["call_id"], "fallback", wait=0.3) is False
    finally:
        release.set()
        effect.join(5)
        thread.join(15)


def _call_quietly(raised, fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001
        raised.append(exc)


class _CountingEvent(threading.Event):
    def __init__(self, source):
        super().__init__()
        self.waits = 0
        if source.is_set():
            self.set()

    def wait(self, timeout=None):
        self.waits += 1
        return super().wait(timeout)


def test_a_closed_upstream_with_a_stuck_effect_is_refused_at_once_without_spinning(call, monkeypatch):
    monkeypatch.setattr(live, "FENCE_WAIT_S", 0.3)
    h, thread = _admitted(call)
    session = _session(call)
    effect, release = _stuck_effect(call, session)
    try:
        live._mark_retired(call.grant["call_id"])         # fence times out: forced close, effect still stuck
        _wait(lambda: session.upstream_close_done.is_set(), 5)
        assert session.upstream_closed.is_set() and session.fence_stuck.is_set()
        session.upstream_close_done = _CountingEvent(session.upstream_close_done)
        t0 = time.monotonic()
        assert live._stop_session(call.grant["call_id"], "fallback", wait=2.0) is False
        assert time.monotonic() - t0 < 0.5                 # refused at once, not after spinning to the deadline
        assert session.upstream_close_done.waits <= 2
    finally:
        release.set()
        effect.join(5)
        thread.join(15)


# ---- the socket route's one refusal for a request that is not a websocket ------------------------

@pytest.mark.parametrize("drop", ["Upgrade", "Sec-WebSocket-Key"])
def test_a_request_that_is_not_a_websocket_gets_one_426_live_websocket_required(call, drop):
    h = Handler()
    del h.headers[drop]
    sock.serve(h)
    assert (h.status, h.body()) == (426, {"error": "live_websocket_required"})
    assert call.connects == [] and live._SESSIONS == {}
