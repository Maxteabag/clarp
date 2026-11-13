"""The agents-working summary for an Android phone (feature live_activity_fcm, contract 95), over real HTTP.

The phone asks with POST /devices/live-activity {"platform": "fcm", "kind": "agents-working"} and stops
with DELETE on the same path. The fleet's content state reaches it as a data-only FCM message at the
moments the Live Activity pusher decides, within a budget per device counted apart from the iPhone's.
FCM is a fake Google (tests/unit/test_fcm.py): nothing here proves delivery to a real phone.
"""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlsplit

import pytest

from lib import agents as agents_db
from lib import apns, live_activity, live_activity_fcm, server_identity
from tests.integration.test_server_di import fake_ctx, running_server  # noqa: F401 - fixtures
from tests.unit.test_fcm import (  # noqa: F401 - fixtures
    _Resp, _config, _fcm_error, _fresh_fcm_state, _service_account, google, rsa_key, sleeps)
from tests.unit.test_live_activity_push import FakeClock, _status

ADMIN = "local-administrator"
ANDROID = "dGVzdC1mY20tdG9rZW4:APA91bH" + "x" * 140
ANDROID_2 = "cm90YXRlZC10b2tlbg:APA91bH" + "y" * 140
FCM_BODY = {"platform": "fcm", "kind": "agents-working"}


def _call(base, credential, method, path, body=None):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
    try:
        client.request(method, path, body=None if body is None else json.dumps(body),
                       headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


def _pair(scope="full"):
    from lib import device_pairing
    return device_pairing.exchange(device_pairing.issue(scope=scope)["code"])


def _push_row(base, phone, token=ANDROID):
    return _call(base, phone["token"], "POST", "/devices",
                 {"token": token, "platform": "android", "provider": "fcm", "session": "rachel"})


@pytest.fixture
def host(running_server, tmp_path, rsa_key):
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token=ADMIN)
    _config(tmp_path, _service_account(tmp_path, rsa_key[1]))
    return base


class Fleet:
    """A Live Activity pusher whose iPhone sink records and whose Android sink is the real sender."""

    def __init__(self, day_limit=None):
        self.clock = FakeClock()
        self.apns: list[tuple[dict, str]] = []
        self.sender = live_activity_fcm.FcmActivitySender(clock=self.clock, schedule=self.clock.schedule,
                                                          day_limit=day_limit, inline=True)
        self.pusher = live_activity.LiveActivityPusher(
            lambda payload, priority: self.apns.append((payload, priority)),
            clock=self.clock, schedule=self.clock.schedule, send_fcm=self.sender.send)
        rows = {a["session"]: a["agent_id"] for a in agents_db.list_agents()}
        self.rachel, self.mike = rows["rachel"], rows["claude"]

    def status(self, agent, state, **kw):
        session = "rachel" if agent == self.rachel else "claude"
        self.pusher.observe(_status(agent, session, state, **kw))


def _messages(google, token=None):
    calls = [c["json"]["message"] for c in google.send_calls]
    return [m for m in calls if token is None or m["token"] == token]


# ---- 2. the route --------------------------------------------------------------------------------

def test_a_paired_android_phone_asks_for_the_summary_and_stops_it(host):
    base = host
    phone = _pair()
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY) == (
        409, {"ok": False, "error": "fcm_push_token_required"})          # no FCM push row yet
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY) == (
        200, {"ok": True, "platform": "fcm", "kind": "agents-working"})
    [row] = live_activity_fcm.registrations()
    assert (row["principal"], row["platform"], row["kind"]) == (phone["device_id"], "fcm", "agents-working")
    assert live_activity.tokens() == []                                  # no iPhone row appears
    assert _call(base, phone["token"], "POST", "/devices/live-activity", {**FCM_BODY, "kind": "other"}) == (
        400, {"ok": False, "error": "kind must be agents-working"})
    assert _call(base, phone["token"], "DELETE", "/devices/live-activity", {"platform": "fcm"}) == (
        200, {"ok": True, "removed": 1})
    assert live_activity_fcm.registrations() == []
    assert _call(base, phone["token"], "DELETE", "/devices/live-activity", {"platform": "fcm"}) == (
        200, {"ok": True, "removed": 0})
    # "ended": true on POST stops it too, as on the iPhone route.
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", {**FCM_BODY, "ended": True}) == (
        200, {"ok": True, "removed": 1})


def test_only_a_paired_full_scope_device_may_ask(host):
    base = host
    limited = _pair(scope="limited")
    assert _call(base, ADMIN, "POST", "/devices/live-activity", FCM_BODY)[0] == 403
    assert _call(base, limited["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 403
    assert _call(base, limited["token"], "DELETE", "/devices/live-activity", {"platform": "fcm"})[0] == 403
    assert live_activity_fcm.registrations() == []


# ---- 4, 5, 6, 7. the message, its pace, priority, budget and the quiet end ----------------------

def test_the_summary_is_a_data_only_message_with_the_apns_content_state(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    fleet = Fleet()
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    [message] = _messages(google)
    assert set(message) == {"token", "data", "android"}                 # no notification block
    assert message["token"] == ANDROID
    assert message["android"] == {"priority": "HIGH", "collapse_key": "agents-working"}
    data = message["data"]
    assert set(data) == {"payload_version", "kind", "event", "state", "server_instance_id", "collapse_id",
                         "sent_at"}
    assert (data["kind"], data["event"], data["collapse_id"]) == ("agents-working", "update", "agents-working")
    [(apns_payload, _)] = fleet.apns
    assert json.loads(data["state"]) == apns_payload["aps"]["content-state"]
    assert json.loads(data["state"])["working"] == 1


def test_high_priority_only_for_structural_changes_and_a_rising_needs_you(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    fleet = Fleet()
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)            # a turn starts: structural
    fleet.clock.advance(1)
    fleet.status(fleet.rachel, "tool", tool="exec", started=1_100, turn_started=1_000)   # routine: waits
    assert len(_messages(google)) == 1
    fleet.clock.advance(15)                                               # the 15 s window: routine, NORMAL
    fleet.status(fleet.mike, "waiting")                                   # needs you rises: at once, HIGH
    fleet.clock.advance(11)
    fleet.status(fleet.mike, "thinking", turn_started=2_000)              # mike starts work: structural
    priorities = [m["android"]["priority"] for m in _messages(google)]
    assert priorities == ["HIGH", "NORMAL", "HIGH", "HIGH"]
    # The iPhone's priorities are what they were: 10 only for a rising needs_you.
    assert [priority for _, priority in fleet.apns] == ["5", "5", "10", "5"]


def test_the_quiet_state_goes_out_once_as_the_end_even_with_the_budget_spent(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    fleet = Fleet(day_limit=1)
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    fleet.clock.advance(11)
    fleet.status(fleet.mike, "thinking", turn_started=2_000)              # over the day limit: held
    assert len(_messages(google)) == 1
    fleet.clock.advance(11)
    fleet.status(fleet.rachel, "idle")
    fleet.status(fleet.mike, "idle")
    fleet.clock.advance(11)                                               # the structural floor passes
    messages = _messages(google)
    assert len(messages) == 2
    end = messages[-1]
    state = json.loads(end["data"]["state"])
    assert end["data"]["event"] == "end" and (state["working"], state["needs_you"]) == (0, 0)
    assert end["android"]["priority"] == "NORMAL"                       # it shows the user nothing
    fleet.clock.advance(60)
    fleet.status(fleet.rachel, "idle")                                    # nothing changes: nothing more
    assert len(_messages(google)) == 2


def test_the_budget_is_per_device_and_apart_from_the_iphones(host, google):
    from lib import settings_store
    base = host
    first, second = _pair(), _pair()
    for phone, token in ((first, ANDROID), (second, ANDROID_2)):
        assert _push_row(base, phone, token)[0] == 200
        assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    fleet = Fleet(day_limit=2)
    sender = live_activity_fcm.FcmActivitySender
    live_activity_fcm.reserve(first["device_id"], fleet.clock(), update=True, day_limit=2, burst=sender.BURST,
                              refill=sender.REFILL_SEC)                   # the first has one update spent today
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    fleet.clock.advance(11)
    fleet.status(fleet.mike, "thinking", turn_started=2_000)
    assert len(_messages(google, ANDROID)) == 1                          # its budget ran out
    assert len(_messages(google, ANDROID_2)) == 2                        # the other device's did not
    for phone in (first, second):
        assert live_activity_fcm.budget(phone["device_id"], fleet.clock(), burst=10, refill=288)[0] == 2
    assert len(fleet.apns) == 2                                          # the iPhone sink got both
    assert settings_store.get(apns._LA_DAY_KEY) is None                  # and its budget is untouched


def test_a_burst_is_held_per_device_and_the_latest_state_goes_out_when_it_refills(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    clock = FakeClock()
    sender = live_activity_fcm.FcmActivitySender(clock=clock, schedule=clock.schedule, inline=True)
    for n in range(12):
        sender.send({"working": 1, "needs_you": 0, "agents": [], "lead": None, "n": n}, True)
    assert len(_messages(google)) == 10                                   # the burst
    clock.advance(sender.REFILL_SEC + 1)                                  # one refill: the latest of the rest
    states = [json.loads(m["data"]["state"])["n"] for m in _messages(google)]
    assert states == list(range(10)) + [11]



def test_a_429_backs_the_device_off_and_the_held_state_goes_out_after(host, google, sleeps):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    google.sends = [_fcm_error(429, "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED")] * 3 + [_Resp(200, {"name": "m"})]
    clock = FakeClock()
    sender = live_activity_fcm.FcmActivitySender(clock=clock, schedule=clock.schedule, inline=True)
    assert sender.send({"working": 1, "needs_you": 0, "n": 1}, True) == {"sent": 0, "held": 1, "failed": 0}
    sender.send({"working": 2, "needs_you": 0, "n": 2}, True)            # newer while backed off: it wins
    clock.advance(sender.FLOOR_MIN_SEC - 1)
    assert len(google.send_calls) == 3                                   # still backing off
    clock.advance(2)
    last = google.send_calls[-1]["json"]["message"]["data"]
    assert len(google.send_calls) == 4 and json.loads(last["state"])["n"] == 2


def test_a_token_fcm_says_is_gone_is_disabled(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    google.sends = [_fcm_error(404, "UNREGISTERED", "NOT_FOUND")]
    sender = live_activity_fcm.FcmActivitySender(inline=True)
    assert sender.send({"working": 1, "needs_you": 0}, True)["failed"] == 1
    assert apns.live_fcm_rows(phone["device_id"]) == []

# ---- 3. the row follows its device and the push row ----------------------------------------------

def test_a_revoked_device_gets_nothing(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    assert _call(base, ADMIN, "POST", "/paired-devices/revoke", {"device_id": phone["device_id"]})[0] == 200
    Fleet().status(agents_db.list_agents()[0]["agent_id"], "thinking", turn_started=1_000)
    assert _messages(google) == []


def test_a_device_whose_row_outlived_its_revocation_or_lost_full_scope_gets_nothing(host, google):
    from lib import db
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    db.conn().execute("UPDATE paired_devices SET scope = 'limited' WHERE device_id = ?", (phone["device_id"],))
    assert apns.live_fcm_rows(phone["device_id"])                         # the push row is still live
    Fleet().status(agents_db.list_agents()[0]["agent_id"], "thinking", turn_started=1_000)
    assert _messages(google) == []


def test_unregistering_the_push_row_stops_the_summary_and_a_rotation_follows_it(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    assert _push_row(base, phone, ANDROID_2)[0] == 200                    # FCM rotated the token
    fleet = Fleet()
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    assert [m["token"] for m in _messages(google)] == [ANDROID_2]
    assert _call(base, phone["token"], "DELETE", "/devices", {"token": ANDROID_2})[0] == 200
    fleet.clock.advance(11)
    fleet.status(fleet.mike, "thinking", turn_started=2_000)
    assert len(_messages(google)) == 1


def test_after_a_take_over_only_the_new_pairing_that_asked_gets_it(host, google):
    base = host
    old = _pair()
    assert _push_row(base, old)[0] == 200
    assert _call(base, old["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    assert _call(base, ADMIN, "POST", "/paired-devices/revoke", {"device_id": old["device_id"]})[0] == 200
    again = _pair()                                                       # the same phone, paired again
    assert _push_row(base, again)[0] == 200                               # takes the token over
    fleet = Fleet()
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    assert _messages(google) == []                                        # the old pairing's ask is void
    assert _call(base, again["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    fleet.clock.advance(11)
    fleet.status(fleet.mike, "thinking", turn_started=2_000)
    assert [m["token"] for m in _messages(google)] == [ANDROID]


def test_without_an_fcm_key_nothing_is_sent(host, google, tmp_path):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    _config(tmp_path)                                                     # the service account is gone
    Fleet().status(agents_db.list_agents()[0]["agent_id"], "thinking", turn_started=1_000)
    assert _messages(google) == []


# ---- 8. the feature ------------------------------------------------------------------------------

@pytest.mark.parametrize("fcm_on,live_on", [(True, True), (False, True), (True, False)])
def test_the_feature_is_advertised_only_with_a_usable_fcm_key_and_live_events(monkeypatch, fcm_on, live_on):
    monkeypatch.setattr(server_identity, "_fcm_probe", lambda: fcm_on)
    monkeypatch.setattr(server_identity, "_live_probe", lambda: live_on)
    info = server_identity.get_server_info()
    advertised = "live_activity_fcm" in info["capabilities"]["features"]
    assert advertised == (fcm_on and live_on)
    if advertised:
        assert info["contract"]["features"]["live_activity_fcm"] == 95 <= info["contract"]["host"]


# ---- the iPhone route is unchanged -----------------------------------------------------------------

def test_the_iphone_route_is_unchanged(host, google):
    base = host
    phone = _pair()
    assert _call(base, phone["token"], "POST", "/devices/live-activity",
                 {"token": "c4" * 32, "activity_id": "act-1", "kind": "agents-working"}) == (
        200, {"ok": True, "activity_id": "act-1"})
    assert [(r["activity_id"], r["principal"]) for r in live_activity.tokens()] == [("act-1", phone["device_id"])]
    assert live_activity_fcm.registrations() == []
    assert _call(base, phone["token"], "DELETE", "/devices/live-activity", {"activity_id": "act-1"}) == (
        200, {"ok": True, "removed": 1})
    fleet = Fleet()
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    [(payload, priority)] = fleet.apns
    assert set(payload) == {"aps"} and payload["aps"]["event"] == "update" and priority == "5"
    assert _messages(google) == []


def test_the_started_server_feeds_the_android_sender_from_the_pusher(running_server):
    import time
    _base, _ctx, srv = running_server
    for _ in range(100):
        if getattr(srv, "live_activity_pusher", None) is not None:
            break
        time.sleep(0.05)
    assert srv.live_activity_pusher._send_fcm is not None
    assert isinstance(srv.live_activity_fcm, live_activity_fcm.FcmActivitySender)


# ---- Arnold's review of 87135240 -------------------------------------------------------------------

class HeapClock:
    """A fake wall clock with an ordered timer heap, for a day of fleet changes."""

    def __init__(self, start=1000.0):
        import heapq
        self._heapq, self.now, self._heap, self._n = heapq, start, [], 0

    def __call__(self):
        return self.now

    def schedule(self, delay, fn):
        cancelled = [False]
        self._heapq.heappush(self._heap, (self.now + max(0.0, delay), self._n, fn, cancelled))
        self._n += 1

        class _Handle:
            def cancel(self_inner):
                cancelled[0] = True
        return _Handle()

    def run_until(self, t):
        while self._heap and self._heap[0][0] <= t:
            due, _n, fn, cancelled = self._heapq.heappop(self._heap)
            if not cancelled[0]:
                self.now = max(self.now, due)
                fn()
        self.now = max(self.now, t)


class Recorder:
    """A transport that delivers, or answers what ``answer`` says, and records what was sent."""

    def __init__(self, clock):
        self.clock, self.sent, self.answer = clock, [], (200, "", "m")

    def send(self, token, payload, *, push_type, priority, collapse_id):
        answer = self.answer() if callable(self.answer) else self.answer
        if answer[0] == 200:
            self.sent.append({"t": self.clock(), "event": payload["event"], "priority": priority,
                              "state": payload["state"], "push_type": push_type, "collapse": collapse_id})
        return answer

    def reset(self):
        pass


def _android_device(base):
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    return phone


@pytest.fixture
def fcm_only(tmp_path, rsa_key):
    """No server (a day of fleet changes takes seconds; the server's own workers would wake meanwhile):
    an FCM key and one Android phone registered the way POST /devices and the route register it."""
    _config(tmp_path, _service_account(tmp_path, rsa_key[1]))
    phone = _pair()
    apns.register_token(ANDROID, session="rachel", platform="android", owner=phone["device_id"])
    live_activity_fcm.register(phone["device_id"])
    return phone


def _sim(events, *, seconds=86_400):
    """The real pusher and sender (inline) on a heap clock; ``events`` is [(t, agent_id, state, turn_ms)]."""
    clock = HeapClock()
    recorder = Recorder(clock)
    sender = live_activity_fcm.FcmActivitySender(clock=clock, schedule=clock.schedule, transport=recorder,
                                                 inline=True)
    pusher = live_activity.LiveActivityPusher(lambda payload, priority: None, clock=clock,
                                              schedule=clock.schedule, send_fcm=sender.offer)
    start = clock.now
    for t, agent, state, turn in events:
        clock.run_until(start + t)
        tool = "exec" if state == "tool" else None
        pusher.observe(_status(agent, agent, state, tool=tool, started=turn, turn_started=turn))
    clock.run_until(start + seconds)
    return recorder.sent


def _turns(period, length, *, seconds=86_400):
    events = []
    for k in range(int(seconds // period)):
        t = k * period
        events += [(t, "agent-one", "thinking", int(t * 1000) + 1), (t + length, "agent-one", "idle", None)]
    return events


FLEETS = {
    # 200 agents at work, one of them starting a new step every 5 s (it becomes the lead), never quiet.
    "200 agents never quiet": [(0, f"agent-{i}", "tool", 1 + i) for i in range(200)] + [
        (5 * k, f"agent-{k % 200}", "thinking" if k % 2 else "tool", 5_000 * k) for k in range(1, 17_280)],
    "a 2-minute turn every 15 minutes": _turns(900, 120),
    "a 1-minute turn every 5 minutes": _turns(300, 60),
    "a 40 s turn every 2 minutes": _turns(120, 40),
    "starting and stopping every 10 s": _turns(20, 10),
}
# (updates, ends) over 24 hours for one phone. The reviewer's counts at 87135240, for comparison:
# 300/0, 96/96, 150/288, 6/720, 5/4320.
EXPECTED = {
    "200 agents never quiet": (309, 0),
    "a 2-minute turn every 15 minutes": (96, 96),
    "a 1-minute turn every 5 minutes": (153, 153),
    "a 40 s turn every 2 minutes": (158, 158),
    "starting and stopping every 10 s": (163, 163),
}


@pytest.mark.parametrize("fleet", list(FLEETS))
def test_a_day_of_each_fleet_stays_within_twice_the_budget_and_ends_follow_updates(fcm_only, fleet):
    """Finding 1: an end only after an update since the last end, none from the burst, NORMAL priority."""
    sent = _sim(FLEETS[fleet])
    updates = [m for m in sent if m["event"] == "update"]
    ends = [m for m in sent if m["event"] == "end"]
    assert len(updates) <= 10 + 86_400 / 288 + 1                       # the burst plus a day's refill
    assert len(ends) <= len(updates) and len(sent) <= 2 * 311
    shown = False
    for m in sent:                                                       # never two ends without an update
        assert m["event"] == "update" or shown
        shown = m["event"] == "update"
    assert all(m["priority"] == "5" for m in ends)
    assert all(m["collapse"] == "agents-working" and m["push_type"] == "liveactivity" for m in sent)
    assert (len(updates), len(ends)) == EXPECTED[fleet]


WORK = {"working": 1, "needs_you": 0, "decisions": 0, "lead": None, "agents": [], "updated_at_ms": 1}
QUIET = {"working": 0, "needs_you": 0, "decisions": 0, "lead": None, "agents": [], "updated_at_ms": 2}


def _sender(clock, recorder, **kw):
    return live_activity_fcm.FcmActivitySender(clock=clock, schedule=clock.schedule, transport=recorder,
                                               inline=True, **kw)


@pytest.mark.parametrize("failure", ["503", "network"])
def test_an_end_that_fails_stays_owed_and_is_retried_with_a_back_off(fcm_only, failure):
    """Finding 2: Google answering 503 or a network error when the fleet goes quiet."""
    clock = HeapClock()
    recorder = Recorder(clock)
    sender = _sender(clock, recorder)
    sender.offer(WORK, True)

    def broken():
        if failure == "network":
            raise OSError("connection reset")
        return (503, "UNAVAILABLE", "")
    recorder.answer = broken
    start = clock.now
    sender.offer(QUIET, True)
    clock.run_until(start + 59)
    clock.run_until(start + 61)                                         # the first retry, 60 s on: fails
    assert [m["event"] for m in recorder.sent] == ["update"]
    recorder.answer = (200, "", "m")                                     # Google is well again
    clock.run_until(start + 60 + 119)
    assert [m["event"] for m in recorder.sent] == ["update"]            # the back-off doubled to 120 s
    clock.run_until(start + 60 + 121)
    assert [m["event"] for m in recorder.sent] == ["update", "end"]
    clock.run_until(start + 7200)                                        # delivered once, then nothing
    assert len(recorder.sent) == 2


def test_an_owed_end_gives_way_when_the_fleet_is_busy_again(fcm_only):
    clock = HeapClock()
    recorder = Recorder(clock)
    sender = _sender(clock, recorder)
    sender.offer(WORK, True)
    recorder.answer = (503, "UNAVAILABLE", "")
    sender.offer(QUIET, True)
    recorder.answer = (200, "", "m")
    sender.offer({**WORK, "updated_at_ms": 3}, True)                    # busy again before the retry
    clock.run_until(clock.now + 3600)
    assert [(m["event"], m["state"]["updated_at_ms"]) for m in recorder.sent] == [("update", 1), ("update", 3)]


def test_after_a_hung_google_only_the_newest_state_goes_out(fcm_only):
    """Finding 3: latest wins in front of the sender too; one state waits per device, never a backlog."""
    import threading
    import time
    release, entered = threading.Event(), threading.Event()
    sent = []

    class Hanging:
        def send(self, token, payload, **kw):
            if not sent:
                entered.set()
                release.wait(10)
            sent.append(payload["state"]["updated_at_ms"])
            return 200, "", "m"

        def reset(self):
            pass
    sender = live_activity_fcm.FcmActivitySender(transport=Hanging())          # its own worker thread
    sender.offer({**WORK, "updated_at_ms": 0}, True)
    assert entered.wait(5)                                               # the first state is with Google
    deadline = time.monotonic() + 5
    for n in range(1, 6):
        sender.offer({**WORK, "working": 1 + n, "updated_at_ms": n}, n % 2 == 0)
    sender.offer({**QUIET, "updated_at_ms": 6}, False)
    release.set()
    while sender._working and time.monotonic() < deadline + 5:
        time.sleep(0.01)
    assert sent == [0, 6]                                                # the first, then the newest (the end)
    assert live_activity_fcm.budget(fcm_only["device_id"], time.time(), burst=10, refill=288)[0] == 2


def test_a_restart_does_not_refill_the_burst(fcm_only):
    """Finding 4: the burst is kept in the database with the day count."""
    clock = HeapClock()
    recorder = Recorder(clock)
    first = _sender(clock, recorder)
    for n in range(12):
        first.offer({**WORK, "updated_at_ms": n}, True)
    assert len(recorder.sent) == 10
    first.close()
    again = _sender(clock, recorder)                                     # the Host started again
    for n in range(12, 24):
        again.offer({**WORK, "updated_at_ms": n}, True)
    clock.run_until(clock.now + 150)
    assert len(recorder.sent) == 10                                      # not 12 in the first 2.5 minutes
    clock.run_until(clock.now + 300)
    assert len(recorder.sent) == 11 and recorder.sent[-1]["state"]["updated_at_ms"] == 23


@pytest.mark.parametrize("last", ["update", "end"])
def test_after_a_restart_a_device_last_shown_work_gets_the_present_state(fcm_only, last):
    """Finding 4: the agent's turn ended while the Host was down; no event will come."""
    clock = HeapClock()
    recorder = Recorder(clock)
    before = _sender(clock, recorder)
    before.offer(WORK, True)
    if last == "end":
        before.offer(QUIET, True)
    before.close()
    sent_before = len(recorder.sent)
    after = _sender(clock, recorder)
    pusher = live_activity.LiveActivityPusher(lambda *_: None, clock=clock, schedule=clock.schedule,
                                              send_fcm=after.offer)
    after.after_start(pusher.present_state)
    clock.run_until(clock.now + live_activity_fcm.START_GRACE_SEC + 1)
    new = recorder.sent[sent_before:]
    if last == "update":
        assert [m["event"] for m in new] == ["end"]
        assert (new[0]["state"]["working"], new[0]["state"]["needs_you"]) == (0, 0)
    else:
        assert new == []                                                 # nothing shown: nothing owed


def test_an_offer_before_the_grace_makes_the_start_check_a_no_op(fcm_only):
    clock = HeapClock()
    recorder = Recorder(clock)
    _sender(clock, recorder).offer(WORK, True)
    after = _sender(clock, recorder)
    after.after_start(lambda: QUIET)
    after.offer({**WORK, "updated_at_ms": 5}, True)                     # the live hub spoke first
    clock.run_until(clock.now + live_activity_fcm.START_GRACE_SEC + 1)
    assert [m["state"]["updated_at_ms"] for m in recorder.sent] == [1, 5]


def test_revoking_the_device_removes_its_request_and_its_budget(host):
    """Finding 5."""
    from lib import settings_store
    base = host
    phone = _android_device(base)
    live_activity_fcm.reserve(phone["device_id"], 1000.0, update=True, day_limit=300, burst=10, refill=288)
    assert _call(base, ADMIN, "POST", "/paired-devices/revoke", {"device_id": phone["device_id"]})[0] == 200
    assert live_activity_fcm.registrations() == []
    assert phone["device_id"] not in (settings_store.get(live_activity_fcm._BUDGET_KEY) or "")


def test_an_end_takes_nothing_from_the_burst(fcm_only):
    clock = HeapClock()
    recorder = Recorder(clock)
    sender = _sender(clock, recorder)
    for n in range(9):
        sender.offer({**WORK, "updated_at_ms": n}, True)
    sender.offer(QUIET, True)                                            # the end, with one update left
    sender.offer({**WORK, "updated_at_ms": 100}, True)                   # busy again: the tenth update goes
    assert [m["event"] for m in recorder.sent] == ["update"] * 9 + ["end", "update"]
    assert recorder.sent[-2]["priority"] == "5"



def test_a_closed_summary_sender_takes_no_state(fcm_only):
    clock = HeapClock()
    recorder = Recorder(clock)
    sender = _sender(clock, recorder)
    sender.offer(WORK, True)
    sender.close()
    assert sender.offer({**WORK, "updated_at_ms": 9}, True) is None
    clock.run_until(clock.now + 3600)
    assert [m["state"]["updated_at_ms"] for m in recorder.sent] == [1]


def test_without_a_usable_key_the_sender_sends_nothing_even_with_a_transport(fcm_only, tmp_path):
    """The early return: the transport given here would deliver, so only the key check stops it."""
    clock = HeapClock()
    recorder = Recorder(clock)
    sender = _sender(clock, recorder)
    _config(tmp_path)                                                    # the service account is gone
    assert sender.offer(WORK, True) == {"sent": 0, "held": 0, "failed": 0}
    assert recorder.sent == [] and sender._latest is None
