"""Agent-live rings: ROOT's follow-up dispositions after the installed d1229ddd (18 Oct).

B: a refusal before the join is accepted echoes only the well-typed identity this socket's own first join frame
claimed, as unverified correlation (never a lookup or a grant); a missing or malformed identity gets the
documented unbound refusal (empty ids, epoch 0) and never a fabricated match.
C: the same owner answering an answered Live ring with another request id is stale, not an owner conflict.
D: the join's protocol and call_epoch are strict integers (a bool is not 1); a wrong type is an owner conflict.
E: the fallback's request id is 1 to 128 UTF-8 bytes, like the close's and the answer's.
"""
from __future__ import annotations

import json

import pytest

from lib import agent_ring_live as live
from lib import agent_rings as rings
from lib import group_calls, oracle_realtime
from tests.unit.test_agent_ring_live import OTHER_PHONE, PHONE, _live_ring, sander  # noqa: F401 - fixture
from tests.unit.test_agent_ring_live_controls import Session, _fallback
from tests.unit.test_agent_ring_live_socket import Handler, _join, _masked, _ring_of, _serve, call  # noqa: F401
from tests.unit.test_agent_rings import env  # noqa: F401 - env is a fixture

UNBOUND = {"call_id": "", "call_epoch": 0, "attempt_id": ""}


def _refused(status, code, fn, *args, **kw):
    with pytest.raises(group_calls.CallError) as exc:
        fn(*args, **kw)
    assert (exc.value.status, exc.value.body()["error"]) == (status, code)


def _join_error(frame=None, handler=None, raw=None):
    """Serve one socket, send one first frame and return its only frame, the pre-join refusal."""
    h = handler or Handler()
    thread = _serve(h)
    if raw is not None:
        h.rfile.feed(raw)
    else:
        h.send(frame)
    thread.join(15)
    assert not thread.is_alive()
    [error] = h.wfile.frames()
    assert error["type"] == "agent_live.error" and error["retryable"] is False
    return error


def _ids(error):
    return {k: error[k] for k in ("call_id", "call_epoch", "attempt_id")}


# ---- B: the pre-join refusal echoes only this socket's own well-typed claim ----------------------------------

def test_a_wrong_owner_refusal_echoes_the_claimed_ids_and_nothing_else(call):
    frame = _join(call.grant)
    error = _join_error(frame, Handler(principal=OTHER_PHONE))
    assert error["code"] == "live_owner_conflict"
    assert _ids(error) == {"call_id": frame["call_id"], "call_epoch": frame["call_epoch"],
                           "attempt_id": frame["attempt_id"]}
    assert set(error) == {"type", "call_id", "call_epoch", "attempt_id", "code", "message", "retryable"}
    assert call.connects == [] and not _ring_of(call.grant).get("_live_join_consumed")


def test_an_expired_ticket_refusal_echoes_the_claimed_ids(call):
    call.env.clock.advance(rings.LIVE_JOIN_MS / 1000)
    error = _join_error(_join(call.grant))
    assert error["code"] == "live_join_expired"
    assert _ids(error) == {k: call.grant[k] for k in ("call_id", "call_epoch", "attempt_id")}


def test_the_echo_is_unverified_correlation_never_a_lookup(call):
    claimed = {"call_id": "alv_nobody", "call_epoch": 7, "attempt_id": "att_nobody"}
    error = _join_error(_join(call.grant, **claimed))
    assert error["code"] == "live_owner_conflict" and _ids(error) == claimed
    stored = _ring_of(call.grant)                       # the real call is untouched and not named
    assert not stored.get("_live_join_consumed") and stored["_live_phase"] == "answered_waiting_join"
    assert call.grant["call_id"] not in json.dumps(error)


@pytest.mark.parametrize("change", [
    {"call_id": 7}, {"call_id": ""}, {"call_id": "a" * 129}, {"call_id": "æ" * 65},
    {"attempt_id": None}, {"attempt_id": ""}, {"attempt_id": "æ" * 65},
    {"call_epoch": True}, {"call_epoch": "1"}, {"call_epoch": 1.0}, {"call_epoch": -1}, {"call_epoch": None},
])
def test_a_malformed_identity_gets_the_unbound_refusal_never_a_fabricated_match(call, change):
    error = _join_error(_join(call.grant, **change))
    assert _ids(error) == UNBOUND
    assert call.connects == [] and not _ring_of(call.grant).get("_live_join_consumed")


def test_a_missing_identity_or_an_unreadable_first_frame_gets_the_unbound_refusal(call):
    frame = _join(call.grant)
    del frame["attempt_id"]
    assert _ids(_join_error(frame)) == UNBOUND
    assert _ids(_join_error(["not", "an", "object"])) == UNBOUND
    assert _ids(_join_error(raw=_masked(0x1, "{not json"))) == UNBOUND
    assert _ids(_join_error(raw=_masked(0x2, "binary"))) == UNBOUND


def test_one_socket_never_sees_another_sockets_claim(call):
    first = {"call_id": "alv_first", "call_epoch": 3, "attempt_id": "att_first"}
    second = {"call_id": "alv_second", "call_epoch": 4, "attempt_id": "att_second"}
    a, b = Handler(), Handler()
    ta, tb = _serve(a), _serve(b)
    a.send(_join(call.grant, **first))
    b.send(_join(call.grant, **second))
    ta.join(15)
    tb.join(15)
    [ea], [eb] = a.wfile.frames(), b.wfile.frames()
    assert _ids(ea) == first and _ids(eb) == second
    third = _join(call.grant)
    del third["call_id"]
    assert _ids(_join_error(third)) == UNBOUND           # nothing is remembered from the earlier sockets


# ---- C: the same owner with another answer request id is stale -------------------------------------------------

def test_the_same_owner_answering_again_with_another_request_is_stale(sander):
    ring = _live_ring()["ring"]
    rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="n1")
    _refused(409, "live_call_stale", rings.answer, PHONE, ring["ring_id"], conversation_mode="agent_live_v1",
             request_id="r2", client_nonce="n1")
    _refused(409, "live_owner_conflict", rings.answer, OTHER_PHONE, ring["ring_id"],
             conversation_mode="agent_live_v1", request_id="r1", client_nonce="n1")
    stored = next(r for r in rings._load() if r["ring_id"] == ring["ring_id"])
    assert stored["_live_request_id"] == "r1" and stored["answered_by"] == PHONE   # no new attempt minted


# ---- D: strict integers in the join -----------------------------------------------------------------------------

@pytest.mark.parametrize("change", [{"protocol": True}, {"call_epoch": True}, {"protocol": "1"},
                                    {"call_epoch": "1"}, {"protocol": 1.0}])
def test_a_join_with_a_non_integer_protocol_or_epoch_is_an_owner_conflict_and_consumes_nothing(call, change):
    error = _join_error(_join(call.grant, **change))
    assert error["code"] == "live_owner_conflict"
    assert call.connects == [] and not _ring_of(call.grant).get("_live_join_consumed")
    assert oracle_realtime._ACTIVE_PRINCIPALS == {}


# ---- E: the fallback's request id ------------------------------------------------------------------------------

@pytest.fixture
def grant(sander):
    live.reset_for_tests()
    ring = _live_ring()["ring"]
    return rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1",
                        client_nonce="n-r1")["live"]


@pytest.mark.parametrize("request_id", ["q" * 129, "æ" * 65, "", 7, None])
def test_a_fallback_request_id_is_one_to_128_utf8_bytes(grant, request_id):
    _refused(400, "live_request_invalid", _fallback, grant, request_id=request_id)
    stored = next(r for r in rings._load() if r.get("_live_call_id") == grant["call_id"])
    assert stored["_live_phase"] == "answered_waiting_join" and not stored.get("_live_fallback_request_id")


def test_a_fallback_request_id_of_exactly_128_bytes_is_kept(grant):
    Session(grant, [])
    out = _fallback(grant, request_id="æ" * 64)
    assert out["conversation_mode"] == "classic"
    stored = next(r for r in rings._load() if r.get("_live_call_id") == grant["call_id"])
    assert stored["_live_fallback_request_id"] == "æ" * 64
