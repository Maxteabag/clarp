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


@pytest.mark.parametrize("kind", ["relay", "grant"])
def test_header_only_day_refusal_from_any_transport_holds_la_to_utc_reset(world, monkeypatch, kind):
    clock, transport = world
    start = clock.now
    monkeypatch.setattr(apns.time, "time", lambda: 1793923200 + 43200 + clock.now - start)
    class Relay:
        def request(self, method, url, **kwargs):
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            response = _Response(429, {"error": {"code": "push_rate_limited"}})
            # 759: no JSON retryAfter and no day/minute discriminator. >1h
            # conservatively holds until midnight even if this header is shorter.
            response.headers = {"Retry-After": "3601"}
            return response
    monkeypatch.setattr(apns, "_relay_client", lambda: Relay())
    apns._reset_relay_state()
    cfg = SimpleNamespace(audio_central_url="https://central.example", audio_central_credential="cred")
    relay = apns._RelayTransport(cfg) if kind == "relay" else apns._GrantTransport(cfg)
    send = (lambda: relay.send(TOKENS[0], "production", {"aps": {}})) if kind == "relay" else (lambda: relay.send("synthetic-grant", {"aps": {}}))
    assert send()[0] == 429
    _register(TOKENS[1])
    apns.send_live_activity({"n": 1})
    clock.advance(3602)
    assert transport.sent == []
    apns.send_live_activity({"n": 2})
    clock.advance(43200 - 3602 - 1)
    assert transport.sent == []
    clock.advance(1)
    assert len(transport.sent) == 1 and transport.sent[0][1] == {"n": 2}


@pytest.mark.parametrize("seconds", [60, 3600])
def test_retry_at_or_below_one_hour_remains_a_normal_pause(world, monkeypatch, seconds):
    clock, transport = world
    start = clock.now
    monkeypatch.setattr(apns.time, "time", lambda: 1793923200 + 43200 + clock.now - start)
    _register(TOKENS[0])
    apns._LA_GATE.rate_limited(seconds)
    apns.send_live_activity({"n": 1})
    clock.advance(seconds - 1)
    assert transport.sent == []
    clock.advance(1)
    assert len(transport.sent) == 1


@pytest.mark.parametrize("header,body,expected", [
    ("43200", {}, 43200), ("60", {"retryAfter": 120}, 120),
    ("bad", {"retryAfter": 120}, 120), ("-1", {}, 0),
    ("nan", {}, 0), ("inf", {}, 0), ("", {}, 0),
])
def test_retry_after_keeps_the_longest_valid_advertised_wait(header, body, expected):
    response = _Response(429, body)
    response.headers = {"Retry-After": header}
    assert apns._retry_after(response) == expected


def _wire_refusal(monkeypatch, *, kind="relay", outer=429, header="3601", body=None):
    import httpx
    class Relay:
        def request(self, method, url, **kwargs):
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            response = _Response(outer, body if body is not None else {"error": {"code": "push_rate_limited"}})
            response.headers = httpx.Headers({"rEtRy-AfTeR": header})
            return response
    monkeypatch.setattr(apns, "_relay_client", lambda: Relay())
    apns._reset_relay_state()
    cfg = SimpleNamespace(audio_central_url="https://central.example", audio_central_credential="cred")
    if kind == "grant":
        return apns._GrantTransport(cfg).send("synthetic-grant", {"aps": {}})
    return apns._RelayTransport(cfg).send(TOKENS[0], "production", {"aps": {}})


@pytest.mark.parametrize("kind", ["relay", "grant"])
def test_per_recipient_429_pauses_la_but_is_not_invented_as_day_refusal(world, monkeypatch, kind):
    clock, transport = world
    result = _wire_refusal(monkeypatch, kind=kind, outer=200, header="60",
                           body={"results": [{"status": 429, "reason": "TooManyRequests", "apnsId": "synthetic"}]})
    assert result == (429, "TooManyRequests", "synthetic")
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    clock.advance(59)
    assert transport.sent == []
    clock.advance(1)
    assert len(transport.sent) == 1


@pytest.mark.parametrize("wall,header,wait", [
    (86399.75, "3601", 3601), (.25, "3601", 86399.75), (43200, "50000", 50000),
])
def test_day_pause_preserves_fractional_midnight_and_longer_header(world, monkeypatch, wall, header, wait):
    import os
    import time
    clock, transport = world
    start = clock.now
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Oslo")  # including the DST change day
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        monkeypatch.setattr(apns.time, "time", lambda: 1774742400 + wall + clock.now - start)
        assert _wire_refusal(monkeypatch, header=header)[0] == 429
        _register(TOKENS[0])
        apns.send_live_activity({"n": 1})
        clock.advance(wait - .125)
        apns._LA_GATE.delivered()  # a concurrent successful send cannot clear this hold
        assert transport.sent == []
        clock.advance(.125)
        assert len(transport.sent) == 1
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        if hasattr(time, "tzset"):
            time.tzset()


@pytest.mark.parametrize("header", ["", "-1", "0", "1.5", "nan", "inf", "Fri, 06 Nov 2026 20:00:00 GMT", "9" * 400])
def test_unusable_wait_cannot_shorten_active_day_pause_or_overflow_timers(world, monkeypatch, header):
    import threading
    clock, transport = world
    start = clock.now
    monkeypatch.setattr(apns.time, "time", lambda: 1793923200 + 43200 + clock.now - start)
    original_schedule = clock.schedule
    def bounded_schedule(delay, callback):
        assert 0 < delay <= threading.TIMEOUT_MAX
        return original_schedule(delay, callback)
    apns._LA_GATE._schedule = bounded_schedule
    assert _wire_refusal(monkeypatch, header="43200")[0] == 429
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    _wire_refusal(monkeypatch, header=header)
    apns._LA_GATE.delivered()
    clock.advance(43199)
    assert transport.sent == []
    clock.advance(1)
    assert len(transport.sent) == 1


def test_huge_finite_header_arms_a_bounded_wakeup_without_shortening_pause(world, monkeypatch):
    import threading
    clock, transport = world
    original_schedule = clock.schedule
    delays = []
    def bounded_schedule(delay, callback):
        assert 0 < delay <= threading.TIMEOUT_MAX
        delays.append(delay)
        return original_schedule(delay, callback)
    apns._LA_GATE._schedule = bounded_schedule
    assert _wire_refusal(monkeypatch, header="9" * 30)[0] == 429
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    assert len(delays) == 1
    assert not transport.sent


def test_legacy_json_wait_is_respected_but_not_a_day_discriminator(world, monkeypatch):
    clock, transport = world
    start = clock.now
    monkeypatch.setattr(apns.time, "time", lambda: 1793923200 + 43200 + clock.now - start)
    _wire_refusal(monkeypatch, header="", body={"error": {"code": "push_rate_limited"}, "retryAfter": 3601})
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    clock.advance(3601)
    assert len(transport.sent) == 1


def test_alert_completes_while_la_network_send_is_held(world, monkeypatch):
    import threading
    _, _ = world
    entered, release, alert_done = (threading.Event() for _ in range(3))
    outcomes = []
    class Held(Transport):
        def send(self, *args, **kwargs):
            entered.set()
            assert release.wait(5), "LA test barrier not released"
            return super().send(*args, **kwargs)
    sender = Held()
    monkeypatch.setattr(apns, "_transport", lambda cfg: sender)
    _register(TOKENS[0])
    worker = threading.Thread(target=lambda: apns.send_live_activity({"n": 1}))
    def alert():
        try:
            outcomes.append(_wire_refusal(monkeypatch, header="43200"))
        finally:
            alert_done.set()
    second = threading.Thread(target=alert)
    worker.start()
    try:
        assert entered.wait(3), "LA send did not enter barrier"
        second.start()
        assert alert_done.wait(3), "alert waited on LA's network/send lock"
        assert outcomes[0][0] == 429
    finally:
        release.set()
        worker.join(5)
        if second.ident is not None:
            second.join(5)
    assert not worker.is_alive() and not second.is_alive()
    assert apns._LA_GATE.paused()  # the in-flight LA200 must not erase the alert's day hold


def test_day_refusal_with_existing_timer_requeues_only_latest_multitoken_state(world, monkeypatch):
    clock, sender = world
    start = clock.now
    monkeypatch.setattr(apns.time, "time", lambda: 1793923200 + 43200 + clock.now - start)
    _register(*TOKENS, THIRD)
    for n in range(4):
        apns.send_live_activity({"n": n})
    assert len(sender.sent) == 10  # a timer already owns the two remaining recipients
    sender.status, sender.reason = 429, "push_rate_limited"
    sender.on_send = lambda: _wire_refusal(monkeypatch, header="3601")
    clock.advance(288)
    assert len(sender.sent) == 11
    sender.status, sender.reason, sender.on_send = 200, "", None
    apns.send_live_activity({"n": 99})
    clock.advance(43200 - 288 - 1)
    assert len(sender.sent) == 11
    assert len([timer for timer in clock.timers if not timer[2]]) == 1
    clock.advance(1)
    assert len(sender.sent) == 14
    assert all(item[1] == {"n": 99} for item in sender.sent[-3:])


def test_mixed_la_results_count_only_successful_physical_sends(world, monkeypatch):
    _, _ = world
    _register(*TOKENS, THIRD)
    records, calls = [], []
    class Sender(Transport):
        def send(self, token, *args, **kwargs):
            calls.append(token)
            if len(calls) == 2:
                raise RuntimeError("synthetic transport failure")
            return (200, "", "synthetic") if len(calls) == 1 else (410, "Unregistered", "")
    monkeypatch.setattr(apns, "_transport", lambda cfg: Sender())
    monkeypatch.setattr(apns, "log", lambda code, detail="": records.append((code, detail)))
    apns.send_live_activity({"private": "DO_NOT_LOG"})
    assert len(calls) == 3
    accepted = [detail for code, detail in records if code == "apnsLiveActivityAccepted"]
    assert accepted == ["status=200 count=1"]
    assert all("DO_NOT_LOG" not in detail and all(token not in detail for token in (*TOKENS, THIRD))
               for _, detail in records)


@pytest.mark.parametrize("kind", ["relay", "direct"])
def test_actual_la_transport_success_counts_each_recipient_once(world, monkeypatch, kind):
    _, _ = world
    _register(*TOKENS)
    calls, logs = [], []
    cfg = SimpleNamespace(audio_central_url="https://central.example", audio_central_credential="cred", apns_bundle_id="test.bundle")
    class Client:
        def request(self, method, url, **kwargs):
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            calls.append(url)
            return _Response(200, {"results": [{"status": 200, "apnsId": "synthetic"}]})
        def post(self, url, **kwargs):
            calls.append(url)
            response = _Response(200, {})
            response.headers = {"apns-id": "synthetic"}
            return response
    client = Client()
    monkeypatch.setattr(apns, "_relay_client", lambda: client)
    monkeypatch.setattr(apns, "_pooled_client", lambda: client)
    monkeypatch.setattr(apns, "_auth_jwt", lambda cfg: "synthetic")
    apns._reset_relay_state()
    transport = apns._RelayTransport(cfg) if kind == "relay" else apns._DirectTransport(cfg)
    monkeypatch.setattr(apns, "_transport", lambda cfg: transport)
    monkeypatch.setattr(apns, "log", lambda code, detail="": logs.append((code, detail)))
    result = apns.send_live_activity({"n": 1})
    assert result["sent"] == len(calls) == 2
    assert [detail for code, detail in logs if code == "apnsLiveActivityAccepted"] == ["status=200 count=1"] * 2


def test_diagnostic_failure_does_not_resend_an_accepted_la(world, monkeypatch):
    clock, sender = world
    _register(*TOKENS, THIRD)
    def log_failure(*args):
        raise RuntimeError("synthetic diagnostic error")
    monkeypatch.setattr(apns, "log", log_failure)
    result = apns.send_live_activity({"n": 1})
    clock.advance(1000)
    assert result["sent"] == len(sender.sent) == 3


def test_malformed_json_retains_header_and_active_pause(world, monkeypatch):
    clock, sender = world
    response = _Response(429, {})
    response.headers = {"Retry-After": "3600"}
    def broken():
        raise ValueError("synthetic malformed JSON")
    response.json = broken
    assert apns._retry_after(response) == 3600
    apns._LA_GATE.rate_limited(3600)
    apns._LA_GATE.rate_limited(0)
    _register(TOKENS[0])
    apns.send_live_activity({"n": 1})
    clock.advance(3599)
    assert not sender.sent
    clock.advance(1)
    assert len(sender.sent) == 1



def test_per_recipient_la_refusal_applies_backoff_only_once(world, monkeypatch):
    clock, _ = world
    _register(TOKENS[0])
    sends = []
    class Relay:
        def request(self, method, url, **kwargs):
            if url.endswith("/v1/push/devices"):
                return _Response(200, {})
            sends.append(url)
            response = _Response(200, {"results": [{"status": 429 if len(sends) == 1 else 200,
                                                     "reason": "TooManyRequests" if len(sends) == 1 else ""}]})
            response.headers = {"Retry-After": "60"}
            return response
    monkeypatch.setattr(apns, "_relay_client", lambda: Relay())
    apns._reset_relay_state()
    relay = apns._RelayTransport(SimpleNamespace(audio_central_url="https://central.example", audio_central_credential="cred"))
    monkeypatch.setattr(apns, "_transport", lambda cfg: relay)
    assert apns.send_live_activity({"n": 1})["failed"] == 1
    assert apns._LA_GATE.floor == 60
    clock.advance(60)
    assert len(sends) == 2
    assert apns._LA_GATE.floor == 0
