"""Agent-live rings: the review's required fixes H-1, H-2, H-3, H-5 and M-1 (ROOT dispositions, 17 Oct).

H-1: provider audio reaches the phone only after the opening was admitted, never before it was sent and never
     after retirement. H-2: a close stops the session whatever its audit or save does, and a retry stops it
     again. H-3: fallback retires and stops Live before any classic call exists; a failure after the stop leaves
     Live retired and no classic call. H-5: the silence feed waits for both readies, under an absolute deadline
     that pings do not extend. M-1: the owner's repeat answer gets the call's actual state.
Ordering is asserted on what was actually written or called, not on flags or larger margins.
"""
from __future__ import annotations

import pytest

from lib import agent_ring_live as live
from lib import agent_ring_live_socket as sock
from lib import agent_rings as rings
from lib import group_calls
from tests.unit.test_agent_ring_live import PHONE, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_ring_live_socket import (AUDIO, Handler, _admitted, _join, _masked, _ready, _ring_of,
                                                     _serve, _types, _wait, call)  # noqa: F401 - call is a fixture
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture

DELTA = {"type": "session.output_audio.delta", "delta": AUDIO}


def _owner(grant, **kw):
    return {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"], "owner_nonce": grant["owner_nonce"],
            "request_id": "q1", **kw}


def _close(grant, **kw):
    return live.close(PHONE, _owner(grant, **{"call_epoch": 1, "reason": "user_hangup", **kw}))


def _fallback(grant, **kw):
    return live.fallback(PHONE, _owner(grant, **{"expected_epoch": 1, "audio_stopped": True, **kw}))


def _provider_ready(call):
    h = Handler()
    thread = _serve(h)
    h.send(_join(call.grant))
    _wait(lambda: "agent_live.provider_ready" in _types(h))
    return h, thread


def _session(call):
    _wait(lambda: live._session_for(call.grant["call_id"]) is not None)
    return live._session_for(call.grant["call_id"])


# ---- H-1 ------------------------------------------------------------------------------------

def test_provider_speech_before_the_opening_was_sent_never_reaches_the_phone_and_fails_the_call(call):
    h, thread = _provider_ready(call)
    call.up.push(DELTA)                                   # the phone never said client_ready: no opening yet
    thread.join(15)
    assert DELTA not in h.wfile.frames()
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert _ring_of(call.grant)["_live_phase"] == "retired"
    assert not call.up.of("session.instructions.append")


def test_provider_output_after_the_opening_was_sent_but_before_its_ack_is_terminal(call):
    call.up.take_opening = False
    h, thread = _provider_ready(call)
    h.send(_ready(call.grant))
    _wait(lambda: call.up.of("session.instructions.append"))
    call.up.push(DELTA)                                   # the provider speaks before it acknowledges the append
    call.up.push({"type": "session.instructions.appended"})
    thread.join(15)
    assert DELTA not in h.wfile.frames() and "agent_live.opening_admitted" not in _types(h)
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert _ring_of(call.grant)["_live_retired_reason"] == "provider_output_before_ack"


def test_output_in_the_window_between_a_saved_retirement_and_its_stop_never_reaches_the_phone(call, monkeypatch):
    h, thread = _admitted(call)
    session = _session(call)
    stop = live._stop_session
    monkeypatch.setattr(live, "_stop_session", lambda call_id, reason: None)     # the stop has not arrived yet
    _close(call.grant)
    assert _ring_of(call.grant)["_live_phase"] == "retired"
    before = len(h.wfile.frames())
    session.receive(DELTA)                     # whether or not tick's own currency check got there first
    assert DELTA not in h.wfile.frames()[before:]
    monkeypatch.setattr(live, "_stop_session", stop)
    live._stop_session(call.grant["call_id"], "user_hangup")
    thread.join(15)


def test_no_provider_audio_reaches_the_phone_after_retirement(call):
    h, thread = _admitted(call)
    session = _session(call)
    _close(call.grant)
    _wait(lambda: session.closed.is_set())
    session.receive(DELTA)                                # a late delta from the upstream thread
    thread.join(15)
    frames = h.wfile.frames()
    assert frames[-1]["type"] == "agent_live.closed" and DELTA not in frames


# ---- H-2 ------------------------------------------------------------------------------------

def test_a_close_whose_audit_fails_still_ends_the_session_and_a_retry_is_ok(call, monkeypatch):
    h, thread = _admitted(call)
    audit = rings._audit

    def broken(entry):
        if entry.get("outcome") == "live_closed":
            raise OSError("settings store down")
        return audit(entry)
    monkeypatch.setattr(rings, "_audit", broken)
    assert _close(call.grant)["state"] == "retired"
    thread.join(15)
    assert not thread.is_alive() and h.wfile.frames()[-1]["reason"] == "user_hangup" and call.up.closed
    assert _close(call.grant, request_id="q2")["ok"] is True


def test_a_close_whose_save_fails_still_stops_the_session_and_says_so(call, monkeypatch):
    h, thread = _admitted(call)
    session = _session(call)
    save, broke = rings._save, []

    def broken(records):
        if not broke:
            broke.append(True)
            raise OSError("disk full")
        return save(records)
    monkeypatch.setattr(rings, "_save", broken)
    assert _close(call.grant)["state"] == "retired"                # the second save, after the stop, held
    thread.join(15)
    assert broke and session.closed.is_set() and not thread.is_alive() and call.up.closed
    assert _ring_of(call.grant)["_live_phase"] == "retired"


def test_a_close_whose_save_and_logging_both_fail_still_stops_the_session(call, monkeypatch):
    from lib import log as logmod
    h, thread = _admitted(call)
    session = _session(call)
    save, broke = rings._save, []

    def broken(records):
        if not broke:
            broke.append(True)
            raise OSError("disk full")
        return save(records)

    def no_logging(*args, **kw):
        raise RuntimeError("log sink down")
    monkeypatch.setattr(rings, "_save", broken)
    monkeypatch.setattr(logmod, "log_exception", no_logging)
    _close(call.grant)
    thread.join(15)
    assert session.closed.is_set() and not thread.is_alive() and call.up.closed


def test_close_answers_the_rings_own_epoch_and_state(call):
    out = _fallback(call.grant)
    assert out["conversation_mode"] == "classic"
    closed = _close(call.grant, call_epoch=1, request_id="q9")      # an old-epoch close after the fallback
    assert (closed["call_epoch"], closed["state"]) == (2, "classic")


def test_a_close_whose_saves_both_fail_still_stops_the_session_and_says_so(call, monkeypatch):
    h, thread = _admitted(call)
    session = _session(call)
    save, broke = rings._save, []

    def broken(records):
        if len(broke) < 2:
            broke.append(True)
            raise OSError("disk full")
        return save(records)
    monkeypatch.setattr(rings, "_save", broken)
    with pytest.raises(group_calls.CallError) as exc:
        _close(call.grant)
    assert exc.value.status == 503
    thread.join(15)
    assert session.closed.is_set() and not thread.is_alive() and call.up.closed


def test_a_session_retired_on_its_record_whose_stop_never_arrived_ends_itself(call):
    h, thread = _admitted(call)
    with live._SESSIONS_LOCK:                                       # the close's stop cannot find it
        session = live._SESSIONS.pop(call.grant["call_id"])
    _close(call.grant)
    thread.join(15)
    assert session.closed.is_set() and not thread.is_alive()
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_call_stale"


# ---- H-3 ------------------------------------------------------------------------------------

def test_fallback_stops_live_before_the_classic_call_starts(call, monkeypatch):
    h, thread = _provider_ready(call)
    session = _session(call)
    start, seen = group_calls.start, []

    def watched(*args, **kw):
        seen.append((session.closed.is_set(), _ring_of(call.grant)["_live_phase"]))
        return start(*args, **kw)
    monkeypatch.setattr(group_calls, "start", watched)
    out = _fallback(call.grant)
    assert seen == [(True, "retired")]                             # Live already stopped and retired
    assert out["conversation_mode"] == "classic" and _ring_of(call.grant)["_live_phase"] == "classic"
    thread.join(15)
    assert h.wfile.frames()[-1]["reason"] == "fallback" and call.up.closed


def test_a_fallback_whose_classic_start_fails_leaves_live_retired_and_no_classic_call(call, monkeypatch):
    h, thread = _provider_ready(call)
    session = _session(call)

    def down(*args, **kw):
        raise OSError("tts down")
    monkeypatch.setattr(group_calls, "start", down)
    with pytest.raises(group_calls.CallError) as exc:
        _fallback(call.grant)
    assert exc.value.status == 503
    stored = _ring_of(call.grant)
    assert stored["_live_phase"] == "retired" and stored["_live_call_epoch"] == 2 and not stored.get("call_id")
    thread.join(15)
    assert session.closed.is_set() and call.up.closed
    with pytest.raises(group_calls.CallError) as again:              # never retried into two audio paths
        _fallback(call.grant, expected_epoch=2, request_id="q2")
    assert again.value.status == 409


def test_a_fallback_whose_claim_save_fails_still_stops_live_and_starts_nothing_classic(call, monkeypatch):
    h, thread = _provider_ready(call)
    session = _session(call)
    started = []
    monkeypatch.setattr(group_calls, "start", lambda *a, **kw: started.append(a))
    save, broke = rings._save, []

    def broken(records):
        if not broke:
            broke.append(True)
            raise OSError("disk full")
        return save(records)
    monkeypatch.setattr(rings, "_save", broken)
    with pytest.raises(group_calls.CallError) as exc:
        _fallback(call.grant)
    assert exc.value.status == 503 and not started
    thread.join(15)
    assert session.closed.is_set() and call.up.closed
    assert _ring_of(call.grant)["_live_phase"] == "retired"


def test_a_classic_call_whose_id_cannot_be_recorded_is_ended_by_that_exact_id(call, monkeypatch):
    h, thread = _provider_ready(call)
    start, end = group_calls.start, group_calls.end
    made, ended = [], []

    def recorded_start(*args, **kw):
        out = start(*args, **kw)
        made.append(out["call"]["call_id"])
        monkeypatch.setattr(rings, "_save", broken)                  # the save that records it fails
        return out

    def broken(records):
        raise OSError("disk full")

    def watched_end(caller, **kw):
        ended.append(kw.get("call_id"))
        return end(caller, **kw)
    monkeypatch.setattr(group_calls, "start", recorded_start)
    monkeypatch.setattr(group_calls, "end", watched_end)
    with pytest.raises(group_calls.CallError) as exc:
        _fallback(call.grant)
    assert exc.value.status == 503 and made and ended == made
    assert group_calls.get_call(made[0])["state"] != "live"
    thread.join(15)


def test_a_fallback_whose_audit_fails_still_has_stopped_live_and_started_classic(call, monkeypatch):
    h, thread = _provider_ready(call)
    audit = rings._audit

    def broken(entry):
        if entry.get("outcome") == "live_fallback":
            raise OSError("settings store down")
        return audit(entry)
    monkeypatch.setattr(rings, "_audit", broken)
    out = _fallback(call.grant)
    assert out["conversation_mode"] == "classic"
    thread.join(15)
    assert call.up.closed and _ring_of(call.grant)["_live_phase"] == "classic"


# ---- H-5 ------------------------------------------------------------------------------------

def test_without_client_ready_no_silence_goes_upstream_and_the_readiness_deadline_retires_the_call(call, monkeypatch):
    monkeypatch.setattr(sock, "READY_S", 0.5)
    h, thread = _provider_ready(call)
    thread.join(15)
    assert not call.up.of("session.input_audio.append")
    assert next(f for f in h.wfile.frames() if f["type"] == "agent_live.error")["code"] == "live_unavailable"
    assert _ring_of(call.grant)["_live_retired_reason"] == "not_ready"


def test_pings_do_not_extend_the_readiness_deadline(call, monkeypatch):
    monkeypatch.setattr(sock, "READY_S", 0.5)
    h, thread = _provider_ready(call)
    session = _session(call)
    while not session.closed.is_set() and thread.is_alive():
        h.rfile.feed(_masked(0x9, b"p"))                            # ping traffic only
        thread.join(0.05)
    thread.join(15)
    assert _ring_of(call.grant)["_live_retired_reason"] == "not_ready"
    assert not call.up.of("session.input_audio.append")


def test_a_client_ready_after_retirement_starts_no_feed_no_opening_and_changes_nothing(call):
    h, thread = _provider_ready(call)
    session = _session(call)
    _close(call.grant)
    _wait(lambda: session.closed.is_set())
    before = dict(_ring_of(call.grant))
    session.client(_ready(call.grant))
    thread.join(15)
    assert not call.up.of("session.instructions.append") and not call.up.of("session.input_audio.append")
    assert _ring_of(call.grant) == before


# ---- M-1 ------------------------------------------------------------------------------------

def _repeat(grant):
    return rings.answer(PHONE, grant["ring_id"], conversation_mode="agent_live_v1", request_id="r2")


def test_a_repeat_answer_before_the_join_gets_the_same_grant(call):
    again = _repeat(call.grant)
    assert again["changed"] is False and again["live"]["join_ticket"] == call.grant["join_ticket"]


def test_a_repeat_answer_after_the_join_never_hands_back_the_used_ticket(call):
    h, thread = _admitted(call)
    with pytest.raises(group_calls.CallError) as exc:
        _repeat(call.grant)
    assert (exc.value.status, exc.value.body()["error"]) == (409, "live_join_consumed")
    h.send({"type": "session.close"})
    thread.join(15)


def test_a_repeat_answer_after_close_is_stale(call):
    _close(call.grant)
    with pytest.raises(group_calls.CallError) as exc:
        _repeat(call.grant)
    assert (exc.value.status, exc.value.body()["error"]) == (409, "live_call_stale")


def test_a_repeat_answer_after_fallback_is_the_classic_call(call):
    out = _fallback(call.grant)
    again = _repeat(call.grant)
    assert again["conversation_mode"] == "classic" and again["changed"] is False and "live" not in again
    assert again["call"]["call_id"] == out["call"]["call_id"]
