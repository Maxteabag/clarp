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


def test_provider_output_after_the_opening_was_sent_but_before_its_ack_is_held_until_the_ack(call):
    """2822 (18 Oct): the real provider can speak before it acknowledges the append. Held, then admitted, in order."""
    call.up.take_opening = False
    h, thread = _provider_ready(call)
    h.send(_ready(call.grant))
    _wait(lambda: call.up.of("session.instructions.append"))
    call.up.push(DELTA)                                   # the provider speaks before it acknowledges the append
    append = call.up.of("session.instructions.append")[-1]
    call.up.push({"type": "session.instructions.appended", "client_event_id": append["event_id"]})
    _wait(lambda: DELTA in h.wfile.frames())
    kinds = _types(h)
    assert kinds.index("agent_live.opening_admitted") < h.wfile.frames().index(DELTA)
    assert "agent_live.error" not in kinds


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
    assert _close(call.grant)["ok"] is True                   # the same close, retried


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
    with pytest.raises(group_calls.CallError) as exc:               # an old-epoch close after the fallback
        _close(call.grant, call_epoch=1, request_id="q9")
    assert (exc.value.status, exc.value.body()["error"]) == (409, "live_call_stale")
    closed = _close(call.grant, call_epoch=2, request_id="q9")
    assert (closed["call_epoch"], closed["state"]) == (2, "classic")


def test_a_close_whose_saves_both_fail_still_stops_the_session_and_says_so(call, monkeypatch):
    h, thread = _admitted(call)
    session = _session(call)
    import threading
    save, broke, closer = rings._save, [], threading.get_ident()

    def broken(records):
        # The store is down for everyone: the close's own two saves fail, and so does the session's record of
        # provider_closed after the asynchronous close, which would otherwise persist the retirement first.
        if threading.get_ident() != closer:
            raise OSError("disk full")
        if len(broke) < 2:
            broke.append(True)
            raise OSError("disk full")
        return save(records)
    monkeypatch.setattr(rings, "_save", broken)
    with pytest.raises(group_calls.CallError) as exc:
        _close(call.grant)
    assert exc.value.status == 503 and len(broke) == 2
    thread.join(15)
    assert session.closed.is_set() and not thread.is_alive() and call.up.closed


def test_a_session_retired_on_its_record_whose_stop_never_arrived_ends_itself(call):
    h, thread = _admitted(call)
    with live._SESSIONS_LOCK:                                       # the close's stop cannot find it
        session = live._SESSIONS.pop(call.grant["call_id"])
    with live._CLOSURES_LOCK:
        live._CLOSURES.pop(call.grant["call_id"])
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


def _classic_record_fails(monkeypatch, times):
    """group_calls.start succeeds, then the next ``times`` saves fail."""
    start, made, failures = group_calls.start, [], []
    save = rings._save

    def broken(records):
        if len(failures) < times:
            failures.append(True)
            raise OSError("disk full")
        return save(records)

    def recorded_start(*args, **kw):
        out = start(*args, **kw)
        made.append(out["call"]["call_id"])
        monkeypatch.setattr(rings, "_save", broken)
        return out
    monkeypatch.setattr(group_calls, "start", recorded_start)
    return made


def test_a_classic_call_whose_first_record_fails_is_reconciled_by_its_exact_id(call, monkeypatch):
    h, thread = _provider_ready(call)
    made = _classic_record_fails(monkeypatch, times=1)
    out = _fallback(call.grant)
    assert out["conversation_mode"] == "classic" and out["call"]["call_id"] == made[0]
    assert _ring_of(call.grant)["call_id"] == made[0] and _ring_of(call.grant)["_live_phase"] == "classic"
    thread.join(15)


def test_a_classic_call_that_cannot_be_recorded_is_reported_by_its_exact_id_not_rolled_back(call, monkeypatch):
    h, thread = _provider_ready(call)
    made = _classic_record_fails(monkeypatch, times=2)
    with pytest.raises(group_calls.CallError) as exc:
        _fallback(call.grant)
    assert exc.value.status == 503 and exc.value.body()["classic_call_id"] == made[0]
    assert group_calls.get_call(made[0])["state"] == "live"           # it exists; nothing pretends otherwise
    thread.join(15)


def test_a_close_during_the_fallbacks_voicing_cancels_the_classic_start(call, monkeypatch):
    h, thread = _provider_ready(call)
    started = []
    monkeypatch.setattr(group_calls, "start", lambda *a, **kw: started.append(a))
    voice = rings._voice

    def voicing_with_a_hangup(*args, **kw):                          # the barrier: close lands mid-TTS
        _close(call.grant, request_id="hang", call_epoch=2)
        return voice(*args, **kw)
    monkeypatch.setattr(rings, "_voice", voicing_with_a_hangup)
    with pytest.raises(group_calls.CallError) as exc:
        _fallback(call.grant)
    assert exc.value.status == 409 and not started
    assert _ring_of(call.grant)["_live_fallback_cancelled"] is True
    thread.join(15)


def test_fallback_waits_for_the_provider_closure_receipt_before_any_classic(call, monkeypatch):
    h, thread = _provider_ready(call)
    session = _session(call)
    seen = []
    start = group_calls.start

    def watched(*args, **kw):
        seen.append(session.upstream_closed.is_set() and call.up.closed)
        return start(*args, **kw)
    monkeypatch.setattr(group_calls, "start", watched)
    _fallback(call.grant)
    assert seen == [True] and call.up.of("session.close")
    thread.join(15)


def test_no_receipt_in_time_starts_nothing_classic(call, monkeypatch):
    h, thread = _provider_ready(call)
    session = _session(call)
    started = []
    monkeypatch.setattr(group_calls, "start", lambda *a, **kw: started.append(a))
    monkeypatch.setattr(live, "UPSTREAM_CLOSE_S", 0.2)
    monkeypatch.setattr(session, "close_upstream", lambda: None)     # the provider never confirms
    with pytest.raises(group_calls.CallError) as exc:
        _fallback(call.grant)
    assert exc.value.status == 503 and not started
    session.upstream_closed.set()
    thread.join(15)


def test_revocation_stops_upstream_effects_at_once(call):
    h, thread = _admitted(call)
    session = _session(call)
    with rings._lock:                                                 # retired under the lock, not yet stopped
        records = rings._load()
        live.retire(next(r for r in records if r.get("_live_call_id") == call.grant["call_id"]), "user_hangup")
        rings._save(records)
    before = len(call.up.sent)
    session.upstream_send({"type": "session.input_audio.append", "audio": AUDIO})
    assert len(call.up.sent) == before
    live._stop_session(call.grant["call_id"], "user_hangup")
    thread.join(15)


def test_a_socket_whose_retirement_save_fails_still_stops(call, monkeypatch):
    h, thread = _admitted(call)
    session = _session(call)

    def broken(records):
        raise OSError("disk full")
    monkeypatch.setattr(rings, "_save", broken)
    h.send({"type": "session.close"})
    thread.join(15)
    assert session.closed.is_set() and not thread.is_alive() and call.up.closed


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
    return rings.answer(PHONE, grant["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="n-r1")  # the same intent


def test_a_repeat_answer_before_the_join_gets_the_same_grant(call):
    again = _repeat(call.grant)
    assert again["changed"] is False and again["live"]["join_ticket"] == call.grant["join_ticket"]


def test_a_repeat_answer_after_the_join_is_the_same_grant_marked_join_consumed(call):
    h, thread = _admitted(call)
    again = _repeat(call.grant)["live"]
    assert again["call_id"] == call.grant["call_id"] and again["join_consumed"] is True
    assert call.grant["join_consumed"] is False
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


# ---- H6: delivery of a Live ring --------------------------------------------------------------

def test_a_live_ring_never_falls_back_to_the_global_alert_while_classic_still_does(sander):
    from tests.unit.test_agent_ring_live import _live_ring as live_ring
    from tests.unit.test_agent_rings import _ring as classic_ring
    sander.pushes.clear()                                            # the env's VoIP stub sends nothing
    ring = live_ring()["ring"]
    assert not [kind for kind, pushed in sander.pushes if pushed["ring_id"] == ring["ring_id"]]
    rings.decline(PHONE, ring["ring_id"])
    classic = classic_ring()["ring"]
    assert ("ring", classic["ring_id"]) in [(kind, pushed["ring_id"]) for kind, pushed in sander.pushes]


def test_the_ring_poll_shows_a_live_ring_only_to_a_live_capable_phone(sander):
    from tests.unit.test_agent_ring_live import OTHER_PHONE
    from tests.unit.test_agent_ring_live import _live_ring as live_ring
    rings.register_voip_token(OTHER_PHONE, "cd" * 32)                # an older phone: classic only
    ring = live_ring()["ring"]
    assert rings.current(PHONE)["ring_id"] == ring["ring_id"]
    assert rings.current(OTHER_PHONE) is None and rings.current("administrator") is None
    assert not rings.event_visible(ring, OTHER_PHONE) and rings.event_visible(ring, PHONE)


def test_a_classic_ring_keeps_voip_or_alert_exactly_as_before(sander, monkeypatch):
    from tests.unit.test_agent_rings import _ring as classic_ring
    sander.pushes.clear()
    voip = []
    monkeypatch.setattr(rings, "_push_voip", lambda ring: voip.append(ring["ring_id"]) or {"sent": 1})
    sent = classic_ring()["ring"]                                    # VoIP delivered: no duplicate alert
    assert voip == [sent["ring_id"]] and not [p for p in sander.pushes if p[1]["ring_id"] == sent["ring_id"]]
    rings.decline(PHONE, sent["ring_id"])
    sander.clock.advance(rings.AGENT_INTERVAL_MS / 1000 + 1)
    monkeypatch.setattr(rings, "_push_voip", lambda ring: {"sent": 0})
    unsent = classic_ring()["ring"]                                  # VoIP not delivered: the alert, as before
    assert ("ring", unsent["ring_id"]) in [(kind, p["ring_id"]) for kind, p in sander.pushes]


def test_a_repeat_close_replays_the_first_closes_receipt_only_for_its_exact_request(call):
    # Replaces the f1666c4d test that let any epoch replay the receipt (forbidden by the review).
    first = _close(call.grant)
    assert _close(call.grant) == first and first["call_epoch"] == 1 and first["state"] == "retired"
    with pytest.raises(group_calls.CallError) as exc:
        _close(call.grant, call_epoch=7, request_id="q7", reason="transport_lost")
    assert (exc.value.status, exc.value.body()["error"]) == (409, "live_call_stale")
    assert _ring_of(call.grant)["_live_close_receipt"] == {"call_epoch": 1, "reason": "user_hangup",
                                                         "request_id": "q1"}
