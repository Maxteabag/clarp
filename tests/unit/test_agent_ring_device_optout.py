"""A phone can switch off agent rings for itself ("Ring denne telefonen" off).

Peter, 18 Oct: the phone that films the demo must not ring while the other one does. A phone that
switched rings off gets no agent ring of any kind, classic or Live: no VoIP, no ring alert push, no
agent-ring SSE event and nothing from GET /calls/ring. Every other phone is unchanged, and with no
phone switched off delivery is exactly as before.
"""
from __future__ import annotations

import pytest

from lib import agent_ring_live as live
from lib import agent_rings as rings
from lib import apns
from lib.group_calls import CallError
from tests.unit.test_agent_ring_live import OTHER_PHONE, _live_ring, _pair, sander  # noqa: F401 - sander is a fixture
from tests.unit.test_agent_rings import PHONE, _ring, env  # noqa: F401 - env is a fixture

# The env fixture replaces both senders; these tests need the real token selection under them.
_REAL_PUSH, _REAL_PUSH_VOIP = rings._push, rings._push_voip

ALERT_PHONE, ALERT_OTHER = "a1" * 32, "b2" * 32


@pytest.fixture
def senders(sander, monkeypatch):
    """Both phones registered for VoIP (Live-capable); capture what each sender is asked to reach."""
    rings.register_voip_token(OTHER_PHONE, "cd" * 32, supported_conversation_modes=["classic", "agent_live_v1"])
    sent = {"voip": [], "alert": []}

    def voip(ring, tokens):
        sent["voip"].append(sorted(t["principal"] for t in tokens))
        return {"sent": len(tokens)}

    def alert(ring, kind, exclude_tokens=None):
        sent["alert"].append((kind, sorted(exclude_tokens or ())))
        return {"sent": 1}
    monkeypatch.setattr(rings, "_push", _REAL_PUSH)
    monkeypatch.setattr(rings, "_push_voip", _REAL_PUSH_VOIP)
    monkeypatch.setattr(apns, "send_agent_ring_voip", voip)
    monkeypatch.setattr(apns, "send_agent_ring", alert)
    return sent


def _event(**extra):
    return {"type": "agent-ring", "ring_id": "rng_x", "state": "ringing", **extra}


def test_every_phone_receives_rings_by_default(sander):
    assert rings.receives_rings(PHONE) and rings.receives_rings(OTHER_PHONE)
    assert rings.device_state(PHONE) == {"ok": True, "receive_rings": True, "push_token_bound": False}


def test_a_phone_that_switched_rings_off_gets_no_voip_for_a_classic_or_a_live_ring(senders):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    classic = _ring()["ring"]
    rings.decline(OTHER_PHONE, classic["ring_id"])          # one ring at a time
    _live_ring()
    assert senders["voip"] == [[OTHER_PHONE], [OTHER_PHONE]]


def test_with_no_phone_switched_off_voip_reaches_every_registered_phone(senders):
    _ring()
    assert senders["voip"] == [sorted([PHONE, OTHER_PHONE])]


def test_a_phone_that_switched_rings_off_with_its_push_token_gets_no_ring_alert_of_any_kind(senders, sander):
    rings.set_receive_rings(PHONE, {"receive_rings": False, "push_token": ALERT_PHONE.upper()})
    classic = _ring()["ring"]
    rings.decline(OTHER_PHONE, classic["ring_id"])
    assert senders["alert"] and all(excluded == [ALERT_PHONE] for _, excluded in senders["alert"])


def test_the_alert_sender_skips_exactly_the_excluded_tokens(monkeypatch):
    rows = [{"token": ALERT_PHONE}, {"token": ALERT_OTHER}]
    monkeypatch.setattr(apns, "active_tokens", lambda: rows)
    assert apns.ring_alert_tokens({ALERT_PHONE}) == [{"token": ALERT_OTHER}]
    assert apns.ring_alert_tokens(None) == rows


def test_a_phone_that_switched_rings_off_sees_no_ring_event_and_no_ring_in_the_poll(sander):
    classic = _ring()["ring"]
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert not rings.event_visible(_event(), PHONE)
    assert not rings.event_visible(_event(conversation_mode="agent_live_v1"), PHONE)
    assert not rings.event_visible(classic, PHONE)               # a public record (GET ?ring_id) too
    assert rings.current(PHONE) is None
    assert rings.event_visible(_event(), OTHER_PHONE) and rings.current(OTHER_PHONE)["ring_id"] == classic["ring_id"]
    assert rings.event_visible({"type": "transcript-updated"}, PHONE)   # only agent rings are held back


def test_switching_rings_on_again_restores_every_path(senders):
    rings.set_receive_rings(PHONE, {"receive_rings": False, "push_token": ALERT_PHONE})
    assert rings.set_receive_rings(PHONE, {"receive_rings": True}) == \
        {"ok": True, "receive_rings": True, "push_token_bound": False}
    _ring()
    assert senders["voip"] == [sorted([PHONE, OTHER_PHONE])]
    assert senders["alert"] == [] or all(excluded == [] for _, excluded in senders["alert"])
    assert rings.event_visible(_event(), PHONE)


def test_only_a_paired_full_scope_device_switches_its_own_rings(sander, monkeypatch):
    for principal in ("administrator", "", "device_unpaired"):
        with pytest.raises(CallError) as exc:
            rings.set_receive_rings(principal, {"receive_rings": False})
        assert (exc.value.status, exc.value.body()["error"]) == (403, "ring_device_required")
    monkeypatch.setattr(live, "device_ok", lambda principal: False)      # revoked or limited
    with pytest.raises(CallError) as exc:
        rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert exc.value.status == 403
    assert rings.receives_rings(PHONE)


@pytest.mark.parametrize("body", [
    {}, {"receive_rings": 0}, {"receive_rings": "false"}, {"receive_rings": None},
    {"receive_rings": False, "push_token": "not-hex"}, {"receive_rings": False, "push_token": 7},
    {"receive_rings": False, "principal": OTHER_PHONE},          # no field names another phone
])
def test_a_malformed_request_changes_nothing(sander, body):
    with pytest.raises(CallError) as exc:
        rings.set_receive_rings(PHONE, body)
    assert (exc.value.status, exc.value.body()["error"]) == (400, "ring_device_invalid")
    assert rings.receives_rings(PHONE) and rings.receives_rings(OTHER_PHONE)


def test_one_phone_switching_off_never_touches_another(sander):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert not rings.receives_rings(PHONE) and rings.receives_rings(OTHER_PHONE)
    assert rings.device_state(OTHER_PHONE)["receive_rings"] is True


# ---- review findings on 18880840 ---------------------------------------------------------

def _bind_alert(token, session="s"):
    apns.register_token(token, session=session)


def test_switching_off_again_without_a_token_keeps_the_earlier_binding(senders, sander):
    _bind_alert(ALERT_PHONE)
    rings.set_receive_rings(PHONE, {"receive_rings": False, "push_token": ALERT_PHONE})
    assert rings.set_receive_rings(PHONE, {"receive_rings": False})["push_token_bound"] is True
    classic = _ring()["ring"]
    rings.decline(OTHER_PHONE, classic["ring_id"])
    assert senders["alert"] and all(excluded == [ALERT_PHONE] for _, excluded in senders["alert"])


def test_a_rotated_alert_token_is_reported_unbound_never_as_complete(sander):
    _bind_alert(ALERT_PHONE, session="phone")
    rings.set_receive_rings(PHONE, {"receive_rings": False, "push_token": ALERT_PHONE})
    assert rings.device_state(PHONE)["push_token_bound"] is True
    _bind_alert(ALERT_OTHER, session="phone")          # iOS rotated the token: the old row is disabled
    assert rings.device_state(PHONE) == {"ok": True, "receive_rings": False, "push_token_bound": False}


def test_a_token_already_bound_to_another_phone_is_refused_never_taken(sander):
    _bind_alert(ALERT_PHONE)
    rings.set_receive_rings(PHONE, {"receive_rings": False, "push_token": ALERT_PHONE})
    with pytest.raises(CallError) as exc:
        rings.set_receive_rings(OTHER_PHONE, {"receive_rings": False, "push_token": ALERT_PHONE})
    assert (exc.value.status, exc.value.body()["error"]) == (409, "ring_device_token_conflict")
    assert rings.device_state(PHONE)["push_token_bound"] is True and rings.receives_rings(OTHER_PHONE)


def test_a_live_ring_is_refused_when_every_capable_phone_switched_rings_off(senders):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    rings.set_receive_rings(OTHER_PHONE, {"receive_rings": False})
    before = len(rings._load())
    with pytest.raises(CallError) as exc:
        _live_ring()
    assert (exc.value.status, exc.value.body()["error"]) == (409, "live_client_required")
    assert len(rings._load()) == before and senders["voip"] == []


def test_the_ring_a_phone_answered_stays_visible_to_it_after_it_switched_rings_off(sander):
    rings.set_receive_rings(PHONE, {"receive_rings": False})
    assert rings.event_visible(_event(state="answered", answered_by=PHONE), PHONE)
    assert rings.event_visible(_event(state="ended", answered_by=PHONE, conversation_mode="agent_live_v1"), PHONE)
    assert not rings.event_visible(_event(state="answered", answered_by=OTHER_PHONE), PHONE)
    assert not rings.event_visible(_event(state="ringing"), PHONE)
