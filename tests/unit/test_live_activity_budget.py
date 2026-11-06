"""APNS-N1: Live Activity pushes share the relay's budget with the user's
alerts (one Computer window of 30/min and 2000/UTC day). On 3 Nov they used it
up by midday and every alert after that was refused until UTC midnight.

Live Activity now gets a bounded share, one Host-wide budget counted in sends
(one per activity token per state): a burst of 10, and 300 a day refilled
evenly (one send every 288 s), leaving the alerts at least 20/min and
1700/day. A 429 backs Live Activity off for max(retryAfter, a floor
from 60 s doubling to 30 min); an alert's 429 does too. A held update is not
lost: the latest state goes out when the allowance returns. Alerts are never
held by Live Activity state."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import apns, config, live_activity
from tests.unit.test_live_activity_push import FakeClock

TOKENS = ("ab" * 32, "cd" * 32)


class Transport:
    def __init__(self, status=200, reason="", on_send=None):
        self.sent = []
        self.status, self.reason, self.on_send = status, reason, on_send

    def send(self, token, environment, payload, *, push_type="alert", priority="10",
             collapse_id=None):
        self.sent.append((token, payload, push_type, priority))
        if self.on_send:
            self.on_send()
        return self.status, self.reason, ""

    def reset(self):
        pass


@pytest.fixture
def world(monkeypatch):
    clock = FakeClock()
    gate_cls = getattr(apns, "_LiveActivityGate", None)
    if gate_cls is not None:
        monkeypatch.setattr(apns, "_LA_GATE", gate_cls(clock=clock, schedule=clock.schedule))
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(
        apns_enabled=lambda: True, apns_environment="production"))
    transport = Transport()
    monkeypatch.setattr(apns, "_transport", lambda cfg: transport)
    return clock, transport


def _register(*tokens):
    for index, token in enumerate(tokens):
        live_activity.register(token=token, activity_id=f"act-{index}")


def test_a_burst_sends_at_most_the_allowance_and_the_latest_state_follows(world):
    clock, transport = world
    _register(TOKENS[0])
    for n in range(100):
        apns.send_live_activity({"n": n}, "5")
    assert len(transport.sent) == 10
    clock.advance(288)                          # one allowance refilled
    assert len(transport.sent) == 11
    assert transport.sent[-1][1] == {"n": 99}   # coalesced: the latest state


def test_the_host_wide_cap_holds_across_two_tokens(world):
    clock, transport = world
    _register(*TOKENS)
    for n in range(100):
        apns.send_live_activity({"n": n}, "5")
    assert len(transport.sent) == 10            # sends across both tokens, not 10 each
    clock.advance(288)
    assert len(transport.sent) == 11            # one refilled send in total, not one per token
    clock.advance(288)
    assert len(transport.sent) == 12
    assert {token for token, *_ in transport.sent[-2:]} == set(TOKENS)   # both catch up
    for n in range(100, 104):                   # states keep changing at the budget's pace
        apns.send_live_activity({"n": n}, "5")
        clock.advance(288)
    assert {token for token, *_ in transport.sent[-4:]} == set(TOKENS)   # neither starves


def test_a_rate_limit_backs_off_for_retry_after_and_a_200_resets_the_floor(world):
    clock, transport = world
    _register(TOKENS[0])
    transport.status, transport.reason = 429, "push_rate_limited"
    transport.on_send = lambda: apns._LA_GATE.rate_limited(120)   # the relay transport's hook
    apns.send_live_activity({"n": 1}, "5")
    assert len(transport.sent) == 1
    transport.status, transport.reason, transport.on_send = 200, "", None
    clock.advance(60)
    apns.send_live_activity({"n": 2}, "5")
    assert len(transport.sent) == 1             # still inside retryAfter
    clock.advance(61)
    assert len(transport.sent) == 2 and transport.sent[-1][1] == {"n": 2}
    assert apns._LA_GATE.floor == 0             # a 200 resets the escalation


def test_consecutive_rate_limits_double_the_floor(world):
    gate = apns._LA_GATE
    gate.rate_limited(0)
    assert gate.floor == 60
    gate.rate_limited(0)
    gate.rate_limited(0)
    assert gate.floor == 240
    for _ in range(10):
        gate.rate_limited(0)
    assert gate.floor == 1800


class _Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_an_alert_refused_for_the_shared_budget_pauses_live_activity(world, monkeypatch):
    clock, transport = world
    calls = []

    class Relay:
        def request(self, method, url, headers=None, json=None):
            calls.append(url)
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            return _Response(429, {"error": {"code": "push_rate_limited"}, "retryAfter": 300})
    monkeypatch.setattr(apns, "_relay_client", lambda: Relay())
    apns._reset_relay_state()
    relay = apns._RelayTransport(SimpleNamespace(
        audio_central_url="https://central.example", audio_central_credential="cred"))
    assert relay.send(TOKENS[0], "production", {"aps": {}})[0] == 429
    _register(TOKENS[1])
    apns.send_live_activity({"n": 1}, "5")
    assert transport.sent == []                 # Live Activity waits out the alert's window
    clock.advance(301)
    assert len(transport.sent) == 1


def test_alerts_are_never_held_by_live_activity_state(world, monkeypatch):
    clock, _transport = world
    apns._LA_GATE.rate_limited(3600)            # Live Activity backed off
    sent = []

    class Relay:
        def request(self, method, url, headers=None, json=None):
            sent.append(url)
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            return _Response(200, {"results": [{"status": 200, "apnsId": "x"}]})
    monkeypatch.setattr(apns, "_relay_client", lambda: Relay())
    apns._reset_relay_state()
    relay = apns._RelayTransport(SimpleNamespace(
        audio_central_url="https://central.example", audio_central_credential="cred"))
    assert relay.send(TOKENS[0], "production", {"aps": {}})[0] == 200
    assert any(url.endswith("/v1/push/send") for url in sent)


def test_a_structural_change_waits_ten_seconds_after_the_last_push():
    from lib import agents as agents_db
    from tests.unit.test_live_activity_push import _status
    rachel = agents_db.create_agent(persona="Rachel", voice_id="v", cwd="/tmp", session="rachel")
    mike = agents_db.create_agent(persona="Mike", voice_id="v", cwd="/tmp", session="mike")
    clock, sent = FakeClock(), []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append(priority), clock=clock, schedule=clock.schedule)
    pusher.observe(_status(rachel, "rachel", "thinking", turn_started=1))
    assert len(sent) == 1
    clock.advance(3)
    pusher.observe(_status(mike, "mike", "thinking", turn_started=2))   # working count changes
    assert len(sent) == 1                       # not after 2 s any more
    clock.advance(8)
    assert len(sent) == 2


# Reviewer C's controls on 942113ac.
THIRD = "ef" * 32


def test_a_single_rejected_state_retries_without_another_state_update(world):
    clock, transport = world
    _register(TOKENS[0])
    transport.status, transport.reason = 429, "push_rate_limited"
    transport.on_send = lambda: apns._LA_GATE.rate_limited(120)
    apns.send_live_activity({"n": 1}, "5")
    assert len(transport.sent) == 1
    transport.status, transport.reason, transport.on_send = 200, "", None
    clock.advance(121)                          # no new state: the held one still goes
    assert len(transport.sent) == 2 and transport.sent[-1][1] == {"n": 1}


def test_three_tokens_all_catch_up_when_states_keep_changing(world):
    clock, transport = world
    _register(*TOKENS, THIRD)
    for n in range(100):
        apns.send_live_activity({"n": n}, "5")
    start = len(transport.sent)
    assert start == 10
    for n in range(6):
        apns.send_live_activity({"n": 100 + n}, "5")
        clock.advance(288)
    assert len(transport.sent) == start + 6
    assert {token for token, *_ in transport.sent[start:]} == {*TOKENS, THIRD}


def test_a_relay_429_stops_the_rest_of_the_same_batch(world):
    clock, transport = world
    _register(*TOKENS, THIRD)
    transport.status, transport.reason = 429, "push_rate_limited"
    transport.on_send = lambda: apns._LA_GATE.rate_limited(120)
    apns.send_live_activity({"n": 1}, "5")
    assert len(transport.sent) == 1             # the rest wait out the pause
    transport.status, transport.reason, transport.on_send = 200, "", None
    clock.advance(121)
    assert len(transport.sent) == 4             # ...and then all three go


def test_a_timer_retry_never_finishes_an_old_state_after_a_newer_one(world, monkeypatch):
    """Reviewer C's control on 46438d88: one ordered sender, so a held state
    retried by the timer cannot land after a newer state sent meanwhile."""
    import threading
    clock, _ = world
    _register(TOKENS[0])
    entered, release, new_finished = (threading.Event() for _ in range(3))
    delivered = []

    class Sender:
        reject = True

        def send(self, token, environment, payload, **kwargs):
            if self.reject:
                self.reject = False
                apns._LA_GATE.rate_limited(120)
                return 429, "push_rate_limited", ""
            if payload["n"] == 1:
                entered.set()
                assert release.wait(5)
            delivered.append(payload["n"])
            return 200, "", ""

        def reset(self):
            pass
    sender = Sender()
    monkeypatch.setattr(apns, "_transport", lambda cfg: sender)
    apns.send_live_activity({"n": 1}, "5")
    retry = threading.Thread(target=lambda: clock.advance(121), daemon=True)

    def new_state():
        try:
            apns.send_live_activity({"n": 2}, "5")
        finally:
            new_finished.set()
    newest = threading.Thread(target=new_state, daemon=True)
    retry.start()
    try:
        assert entered.wait(3)
        newest.start()
        new_finished.wait(2)
    finally:
        release.set()
        retry.join(5)
        if newest.ident is not None:
            newest.join(5)
    assert not retry.is_alive() and not newest.is_alive()
    assert delivered[-1] == 2, delivered


# APNS-N3 step1: counted acceptance and correct relay backoff.
def test_each_accepted_la_send_has_a_content_free_counted_log(world, monkeypatch):
    _, transport = world
    records = []
    monkeypatch.setattr(apns, "log", lambda code, detail="": records.append((code, detail)))
    _register(*TOKENS)
    apns.send_live_activity({"aps": {"content-state": {"private": "DO_NOT_LOG"}}})
    accepted = [detail for code, detail in records if code == "apnsLiveActivityAccepted"]
    assert len(accepted) == len(transport.sent) == 2
    assert all("status=200" in detail for detail in accepted)
    assert all("DO_NOT_LOG" not in detail and all(token not in detail for token in TOKENS)
               for _, detail in records)


def test_alert_day_remainder_header_holds_la_until_reset_without_holding_alerts(world, monkeypatch):
    clock, transport = world
    calls = []
    class Relay:
        def request(self, method, url, headers=None, json=None):
            calls.append(url)
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            response = _Response(429, {"error": {"code": "push_rate_limited"}})
            response.headers = {"Retry-After": "43200"}
            return response
    monkeypatch.setattr(apns, "_relay_client", lambda: Relay())
    apns._reset_relay_state()
    relay = apns._RelayTransport(SimpleNamespace(
        audio_central_url="https://central.example", audio_central_credential="cred"))
    assert relay.send(TOKENS[0], "production", {"aps": {}})[0] == 429
    _register(TOKENS[1])
    apns.send_live_activity({"n": 1})
    clock.advance(43199)
    assert transport.sent == []
    # A second user alert still reaches the transport while LA is held.
    before = len(calls)
    assert relay.send(TOKENS[0], "production", {"aps": {}})[0] == 429
    assert len(calls) > before


@pytest.mark.parametrize("value", ["nan", "inf", "-10", float("nan"), None])
def test_invalid_retry_after_never_enters_a_nonfinite_or_negative_pause(value):
    response = _Response(429, {"retryAfter": value})
    response.headers = {}
    assert apns._retry_after(response) == 0
