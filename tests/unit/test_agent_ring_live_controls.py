"""Agent-live rings: the owner's HTTP controls of a Live call (WIRE-v2 SCHEMA).

``POST /calls/ring/live/close`` retires the call's authority first and only
then stops its session; it is idempotent. ``POST /calls/ring/live/fallback``
is the one explicit switch to the classic call, allowed only before the Live
opening was admitted and before any user audio went upstream; it bumps the
epoch and starts the ordinary group call with its own opening, once.
"""
from __future__ import annotations

import pytest

from lib import agent_ring_live as live
from lib import agent_rings as rings
from lib import group_calls as calls
from tests.unit.test_agent_ring_live import OTHER_PHONE, PHONE, _live_ring, sander  # noqa: F401 - fixture
from tests.unit.test_agent_rings import _Handler, _server, env  # noqa: F401 - env is a fixture


@pytest.fixture
def grant(sander):
    live.reset_for_tests()
    ring = _live_ring()["ring"]
    return rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1")["live"]


def _body(grant, **kw):
    body = {"call_id": grant["call_id"], "attempt_id": grant["attempt_id"], "owner_nonce": grant["owner_nonce"],
            "request_id": "q1"}
    body.update(kw)
    return body


def _close(grant, who=PHONE, **kw):
    return live.close(who, _body(grant, **{"call_epoch": grant["call_epoch"], "reason": "user_hangup", **kw}))


def _fallback(grant, who=PHONE, **kw):
    return live.fallback(who, _body(grant, **{"expected_epoch": grant["call_epoch"], "audio_stopped": True, **kw}))


def _ring_of(grant):
    return next(r for r in rings._load() if r.get("_live_call_id") == grant["call_id"])


def _refused(status, code, fn, *args, **kw):
    with pytest.raises(calls.CallError) as exc:
        fn(*args, **kw)
    assert (exc.value.status, exc.value.body()) == (status, {"ok": False, "error": code})


class Session:
    def __init__(self, grant, order):
        self.order, self.call_id = order, grant["call_id"]
        live._SESSIONS[grant["call_id"]] = self

    def retire(self, reason):
        # Authority is already gone on the record when the session hears about it.
        self.order.append(("retire", reason, _ring_of({"call_id": self.call_id})["_live_phase"]))


# ---- close --------------------------------------------------------------------------

def test_close_retires_authority_first_then_the_session(grant):
    order = []
    Session(grant, order)
    out = _close(grant)
    assert out == {"ok": True, "call_id": grant["call_id"], "call_epoch": 1, "state": "retired"}
    assert order == [("retire", "user_hangup", "retired")]
    assert _ring_of(grant)["_live_retired_reason"] == "user_hangup"
    assert grant["owner_nonce"] not in str(out)


def test_close_is_idempotent_and_never_touches_the_session_twice(grant):
    order = []
    Session(grant, order)
    first = _close(grant)
    assert _close(grant) == first and _close(grant, request_id="q2", reason="transport_lost") == first
    assert len(order) == 1


@pytest.mark.parametrize("change", [{"owner_nonce": "f" * 64}, {"attempt_id": "att_other"}, {"call_id": "alv_nope"}])
def test_close_by_a_wrong_grant_is_an_owner_conflict(grant, change):
    _refused(409, "live_owner_conflict", _close, grant, **change)
    assert _ring_of(grant)["_live_phase"] == "answered_waiting_join"


def test_close_by_another_phone_is_an_owner_conflict(grant):
    _refused(409, "live_owner_conflict", _close, grant, who=OTHER_PHONE)


def test_close_with_a_stale_epoch_is_stale(grant):
    _refused(409, "live_call_stale", _close, grant, call_epoch=2)


@pytest.mark.parametrize("change", [{"reason": "bored"}, {"call_epoch": "1"}, {"call_epoch": 0}, {"request_id": 5}])
def test_close_with_a_bad_body_is_invalid(grant, change):
    body = _body(grant, call_epoch=1, reason="user_hangup")
    body.update(change)
    _refused(400, "live_request_invalid", live.close, PHONE, body)


# ---- fallback -----------------------------------------------------------------------

def test_fallback_before_the_opening_starts_the_classic_call_once(grant, sander):
    order = []
    Session(grant, order)
    out = _fallback(grant)
    assert out["conversation_mode"] == "classic" and out["changed"] is True
    assert out["call"]["call_id"] and not out["call"]["call_id"].startswith("alv_")
    assert out["opening"]["text"] == _ring_of(grant)["opening_line"] and out["opening"]["audio_url"]
    assert out["ring"]["ring_id"] == grant["ring_id"]
    stored = _ring_of(grant)
    assert stored["_live_phase"] == "classic" and stored["_live_call_epoch"] == 2
    assert order == [("retire", "fallback", "classic")]          # authority had already moved on
    again = _fallback(grant)                                      # the same request: the same call, no second
    assert again["changed"] is False and again["call"]["call_id"] == out["call"]["call_id"]
    _refused(409, "live_fallback_not_allowed", _fallback, grant, request_id="q2")


def test_fallback_after_a_close_without_opening_is_allowed(grant):
    _close(grant, reason="audio_not_ready")
    assert _fallback(grant)["conversation_mode"] == "classic"


@pytest.mark.parametrize("boundary", ["_live_opening_consumed", "_live_audio_forwarded"])
def test_fallback_after_the_opening_or_user_audio_is_refused(grant, boundary):
    with rings._lock:
        records = rings._load()
        next(r for r in records if r.get("_live_call_id") == grant["call_id"])[boundary] = True
        rings._save(records)
    _refused(409, "live_fallback_not_allowed", _fallback, grant)
    assert _ring_of(grant)["_live_phase"] == "answered_waiting_join" and not _ring_of(grant)["call_id"]


def test_fallback_needs_the_phones_audio_stopped_word_and_a_current_epoch(grant):
    _refused(400, "live_request_invalid", _fallback, grant, audio_stopped=False)
    _refused(400, "live_request_invalid", _fallback, grant, audio_stopped="true")
    _refused(409, "live_call_stale", _fallback, grant, expected_epoch=2)
    _refused(409, "live_owner_conflict", _fallback, grant, who=OTHER_PHONE)


# ---- the HTTP routes ------------------------------------------------------------------

def test_the_live_control_routes_answer_with_the_fixed_strings(grant, sander):
    server = _server()
    bad = _Handler(_body(grant, call_epoch=1, reason="user_hangup", owner_nonce="0" * 64), sander)
    server.Handler._handle_calls_ring_live_close(bad)
    assert bad.sent == [(409, {"ok": False, "error": "live_owner_conflict"})]
    limited = _Handler(_body(grant, call_epoch=1, reason="user_hangup"), sander, scope="limited")
    server.Handler._handle_calls_ring_live_close(limited)
    assert limited.sent[0][0] == 401
    fb = _Handler(_body(grant, expected_epoch=1, audio_stopped=True), sander)
    server.Handler._handle_calls_ring_live_fallback(fb)
    assert fb.sent[0][0] == 200 and fb.sent[0][1]["conversation_mode"] == "classic"
    close = _Handler(_body(grant, call_epoch=1, reason="user_hangup"), sander)
    server.Handler._handle_calls_ring_live_close(close)
    assert close.sent == [(200, {"ok": True, "call_id": grant["call_id"], "call_epoch": 1, "state": "retired"})]


def test_the_ring_and_answer_routes_carry_the_live_fields(sander):
    from tests.unit.test_agent_ring_live import OPENING, _context
    server = _server()
    made = _Handler({"reason": "r", "opening_line": OPENING, "conversation_mode": "agent_live_v1",
                     "event_context": _context()}, sander, turn_agent="a-sander")
    server.Handler._handle_calls_ring(made)
    assert made.sent[0][0] == 200 and made.sent[0][1]["ring"]["conversation_mode"] == "agent_live_v1"
    rid = made.sent[0][1]["ring"]["ring_id"]
    old = _Handler({"ring_id": rid}, sander)
    server.Handler._handle_calls_ring_answer(old)
    assert old.sent == [(409, {"ok": False, "error": "live_client_required"})]
    answer = _Handler({"ring_id": rid, "conversation_mode": "agent_live_v1", "request_id": "r1",
                       "client_nonce": "c1"}, sander)
    server.Handler._handle_calls_ring_answer(answer)
    assert answer.sent[0][0] == 200 and answer.sent[0][1]["live"]["call_id"].startswith("alv_")
    reg = _Handler({"token": "ef" * 32, "supported_conversation_modes": ["agent_live_v1"]}, sander,
                   principal=OTHER_PHONE)
    server.Handler._handle_calls_ring_voip_token(reg)
    assert reg.sent[0][0] == 200
    assert {r["principal"]: r for r in rings.voip_tokens()}[OTHER_PHONE]["supported_conversation_modes"] == \
        ["agent_live_v1"]
