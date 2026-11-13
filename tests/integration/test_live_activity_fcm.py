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
                                                          day_limit=day_limit)
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
    assert end["android"]["priority"] == "HIGH"
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
    live_activity_fcm._day_reserve(first["device_id"], 2, force=False)   # the first has one send spent today
    fleet = Fleet(day_limit=2)
    fleet.status(fleet.rachel, "thinking", turn_started=1_000)
    fleet.clock.advance(11)
    fleet.status(fleet.mike, "thinking", turn_started=2_000)
    assert len(_messages(google, ANDROID)) == 1                          # its budget ran out
    assert len(_messages(google, ANDROID_2)) == 2                        # the other device's did not
    assert live_activity_fcm.day_counts() == {first["device_id"]: 2, second["device_id"]: 2}
    assert len(fleet.apns) == 2                                          # the iPhone sink got both
    assert settings_store.get(apns._LA_DAY_KEY) is None                  # and its budget is untouched


def test_a_burst_is_held_per_device_and_the_latest_state_goes_out_when_it_refills(host, google):
    base = host
    phone = _pair()
    assert _push_row(base, phone)[0] == 200
    assert _call(base, phone["token"], "POST", "/devices/live-activity", FCM_BODY)[0] == 200
    clock = FakeClock()
    sender = live_activity_fcm.FcmActivitySender(clock=clock, schedule=clock.schedule)
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
    sender = live_activity_fcm.FcmActivitySender(clock=clock, schedule=clock.schedule)
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
    sender = live_activity_fcm.FcmActivitySender()
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
