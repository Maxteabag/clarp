"""Agent-live rings: a Live call id is never a group-call id (WIRE-v2 SCHEMA).

Live calls keep their own ``alv_`` ids and no group-call record. Every classic
group-call operation handed one refuses it explicitly with its existing status
and error, even while a classic group call is live for the same phone; none
succeeds silently. These strings are the ones the compatibility row records.
"""
from __future__ import annotations

import pytest

from lib import agent_rings as rings
from lib import group_calls as calls
from tests.unit.test_agent_ring_live import PHONE, _live_ring, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_rings import _ring, env  # noqa: F401 - env is a fixture


@pytest.fixture
def live_id(sander):
    ring = _live_ring()["ring"]
    return rings.answer(PHONE, ring["ring_id"], conversation_mode="agent_live_v1", request_id="r1", client_nonce="c1")["live"]["call_id"]


@pytest.fixture(params=[False, True], ids=["no-group-call", "group-call-live"])
def with_group_call(request, live_id, sander):
    if request.param:
        sander.clock.advance(rings.AGENT_INTERVAL_MS)
        rings.answer(PHONE, _ring()["ring"]["ring_id"])
    return request.param


def _refused(fn, *args, **kw):
    with pytest.raises(calls.CallError) as exc:
        fn(*args, **kw)
    return exc.value.status, exc.value.body()


def test_send_with_a_live_call_id_is_refused(live_id, with_group_call):
    assert _refused(calls.route, PHONE, live_id, "") == (409, {"ok": False, "error": "call ended", "call_id": live_id})


@pytest.mark.parametrize("action", ["add", "remove", "hold", "resume", "switch", "transfer"])
def test_call_changes_with_a_live_call_id_are_refused(live_id, with_group_call, action):
    assert _refused(calls.change, PHONE, action, "Theo", call_id=live_id) == (404, {"ok": False, "error": "no live call"})


def test_ending_a_live_call_id_as_a_group_call_is_refused(live_id, with_group_call):
    assert _refused(calls.end, PHONE, call_id=live_id) == (404, {"ok": False, "error": "no live call"})


def test_a_live_call_is_never_listed_or_found_as_a_group_call(live_id, with_group_call):
    assert calls.get_call(live_id) is None
    live = calls.snapshot(PHONE)["call"]
    assert (live is None) is (not with_group_call)
    assert live is None or live["call_id"] != live_id


def test_declining_or_cancelling_an_answered_live_ring_changes_nothing(live_id, sander):
    ring_id = next(r["ring_id"] for r in rings._load() if r.get("_live_call_id") == live_id)
    status, body = _refused(rings.decline, PHONE, ring_id)
    assert (status, body["error"]) == (409, "the call is over")
    out = rings.cancel("a-sander", ring_id)                 # the existing no-op on a finished ring
    assert out["changed"] is False and rings.get(ring_id)["state"] == "answered"
