"""Live calls and Live rings for an Android phone (feature agent_live_fcm, contract 94), over real HTTP.

An Android phone declares ``supported_conversation_modes`` with its FCM registration (POST /devices);
the declaration lives on that push row and follows its ownership and revocation rules. A Live ring
reaches such a phone as an FCM data message naming ``conversation_mode``. An iPhone, through either
route, behaves as before. FCM itself is a fake Google (tests/unit/test_fcm.py): nothing here proves
delivery to a real phone.
"""
from __future__ import annotations

import hashlib
import http.client
import json
from urllib.parse import urlsplit

import pytest

from lib import agent_ring_live as live
from lib import agent_rings, apns, db
from tests.integration.test_server_di import fake_ctx, running_server  # noqa: F401 - fixtures
from tests.unit.test_agent_ring_live import FILES, OPENING, PROFILE, _context
from tests.unit.test_agent_rings import _agent
from tests.unit.test_fcm import (  # noqa: F401 - fixtures
    SEND, _config, _fresh_fcm_state, _service_account, google, rsa_key)

ADMIN = "local-administrator"
ANDROID = "dGVzdC1mY20tdG9rZW4:APA91bH" + "x" * 140
ANDROID_2 = "cm90YXRlZC10b2tlbg:APA91bH" + "y" * 140
IPHONE = "a1" * 32
LIVE = ["agent_live_v1"]


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


def _android(base, phone, token=ANDROID, **extra):
    return _call(base, phone["token"], "POST", "/devices",
                 {"token": token, "platform": "android", "provider": "fcm", "session": "koko", **extra})


def _row(token):
    row = db.conn().execute("SELECT principal, disabled_at, supported_conversation_modes FROM device_tokens "
                            "WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


@pytest.fixture
def host(running_server, tmp_path, rsa_key, monkeypatch):
    """The real server with an FCM service account (a fake Google), an agent with an approved Live profile
    (a ring story and a user-call story) and a usable Live provider."""
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token=ADMIN)
    _config(tmp_path, _service_account(tmp_path, rsa_key[1]))
    _agent("a-sander", "Sandra", "rk-sander-0001", voice="voice-s")
    folder = tmp_path / "agent-live" / "profiles" / PROFILE
    folder.mkdir(parents=True)
    pins = {}
    for key, (name, text) in {**FILES, "brief": ("USER-CALL.md", "Brukeren ringer selv.")}.items():
        (folder / name).write_text(text, encoding="utf-8")
        pins[key] = {"file": name, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    (folder / "profile.json").write_text(json.dumps({
        "profile_id": PROFILE, "version": "1", "agent_session": "rk-sander-0001", "display_name": "Sandra",
        "files": pins, "stories": {"bottle": ["agents", "persona", "bottle_rds"], "user_call": ["agents", "brief"]},
        "user_call_default": "live", "user_call_opening": "Hei, det er Sandra."}), encoding="utf-8")
    monkeypatch.setattr(live, "ROOT", tmp_path / "agent-live")
    monkeypatch.setattr(live, "provider_problem", lambda: None)
    monkeypatch.setattr(agent_rings, "_schedule_expiry", lambda ring: None)
    return base


def _live_ring(base):
    return _call(base, ADMIN, "POST", "/calls/ring", {
        "agent": "Sandra", "reason": "Vann ved vanntanken", "opening_line": OPENING,
        "conversation_mode": "agent_live_v1", "event_context": _context()})


def _store_live_ring(ring_id="rng_probe"):
    """A ringing Live ring in the store: GET /calls/ring shows it only to a Live-capable phone (H6)."""
    with agent_rings._lock:
        records = agent_rings._load()
        records.append({"ring_id": ring_id, "session": "rk-sander-0001", "agent_id": "a-sander",
                        "persona": "Sandra", "reason": "r", "opening_line": "o", "opening_audio_url": "",
                        "state": "ringing", "created_at": 1, "expires_at": agent_rings.now_ms() + 600_000,
                        "ended_at": None, "answered_by": "", "call_id": "", "conversation_mode": "agent_live_v1"})
        agent_rings._save(records)


def _sees_live(base, phone):
    status, body = _call(base, phone["token"], "GET", "/calls/ring")
    assert status == 200
    capable = agent_rings.live_capable(phone["device_id"])
    assert capable == ((body.get("ring") or {}).get("ring_id") == "rng_probe")
    return capable


def _data(send):
    return send["json"]["message"]["data"]


# ---- 1. the declaration on POST /devices -------------------------------------------------------

def test_an_android_phone_declares_live_on_its_push_row_and_starts_a_live_call(host):
    base = host
    phone = _pair()
    assert _android(base, phone)[0] == 200
    call = {"agent": "a-sander", "request_id": "req-1", "client_nonce": "n-1"}
    assert _call(base, phone["token"], "POST", "/calls/live", call) == (
        409, {"ok": False, "error": "live_client_required"})
    assert _android(base, phone, supported_conversation_modes=["agent_live_v1", "agent_live_v1"]) == (
        200, {"ok": True})
    assert _row(ANDROID)["supported_conversation_modes"] == '["agent_live_v1"]'
    status, body = _call(base, phone["token"], "POST", "/calls/live", call)
    assert status == 200 and body["conversation_mode"] == "agent_live_v1"
    assert body["ring"]["answered_by"] == phone["device_id"] and body["ring"]["origin"] == "user_call"


@pytest.mark.parametrize("modes", ["agent_live_v1", ["agent_live_v2"], [1], ["classic", "agent_live_v1", "classic"], {}])
def test_a_bad_declaration_is_refused_and_writes_nothing(host, modes):
    base = host
    phone = _pair()
    assert _android(base, phone, supported_conversation_modes=modes) == (
        400, {"ok": False, "error": "live_mode_invalid"})
    assert _row(ANDROID) is None


def test_the_feature_and_contract_94_are_advertised(host):
    status, info = _call(host, ADMIN, "GET", "/server-info")
    assert status == 200 and info["contract"]["host"] >= 94
    assert info["contract"]["features"]["agent_live_fcm"] == 94
    assert "agent_live_fcm" in info["capabilities"]["features"]


# ---- 2 and 4. live_capable reads the device row; the Live ring goes out by FCM -------------------

def test_a_live_ring_reaches_only_the_declared_android_phone_by_fcm_naming_conversation_mode(host, google):
    base = host
    declared, classic, iphone = _pair(), _pair(), _pair()
    assert _android(base, declared, supported_conversation_modes=LIVE)[0] == 200
    assert _android(base, classic, token=ANDROID_2)[0] == 200
    assert _call(base, iphone["token"], "POST", "/devices", {"token": IPHONE, "session": "koko"})[0] == 200
    status, body = _live_ring(base)
    assert status == 200, body
    ring = body["ring"]
    assert ring["conversation_mode"] == "agent_live_v1"
    assert body["delivery"]["fcm_live"] == {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}
    [send] = google.send_calls
    message = send["json"]["message"]
    assert message["token"] == ANDROID and message["android"]["priority"] == "HIGH"
    data = message["data"]
    assert (data["kind"], data["ring_id"], data["ring_state"], data["conversation_mode"]) == (
        "agent-ring", ring["ring_id"], "ringing", "agent_live_v1")
    assert data["collapse_id"] == data["notification_id"] == "ring-" + ring["ring_id"]
    assert data["persona"] == "Sandra" and data["session"] == "rk-sander-0001" and "expires_at" in data
    # GET /calls/ring: the declared phone sees it, the classic Android phone and the iPhone do not (H6).
    assert _call(base, declared["token"], "GET", "/calls/ring")[1]["ring"]["ring_id"] == ring["ring_id"]
    assert _call(base, classic["token"], "GET", "/calls/ring")[1]["ring"] is None
    assert _call(base, iphone["token"], "GET", "/calls/ring")[1]["ring"] is None
    # The phone answers Live; the answered state goes to it under the same collapse id.
    status, answer = _call(base, declared["token"], "POST", "/calls/ring/answer", {
        "ring_id": ring["ring_id"], "conversation_mode": "agent_live_v1", "request_id": "r-1",
        "client_nonce": "n-1"})
    assert status == 200 and answer["conversation_mode"] == "agent_live_v1" and answer["live"]
    assert [(_data(s)["ring_state"], _data(s)["conversation_mode"], s["json"]["message"]["token"])
            for s in google.send_calls] == [("ringing", "agent_live_v1", ANDROID),
                                            ("answered", "agent_live_v1", ANDROID)]


def test_every_later_state_of_a_live_ring_reaches_the_android_phone(host, google):
    base = host
    phone = _pair()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    ring = _live_ring(base)[1]["ring"]
    assert _call(base, phone["token"], "POST", "/calls/ring/decline", {"ring_id": ring["ring_id"]})[0] == 200
    assert [_data(s)["ring_state"] for s in google.send_calls] == ["ringing", "declined"]
    assert {_data(s)["conversation_mode"] for s in google.send_calls} == {"agent_live_v1"}


def test_a_live_ring_with_no_capable_phone_is_refused_and_sends_nothing(host, google):
    base = host
    phone = _pair()
    assert _android(base, phone)[0] == 200                             # FCM row, nothing declared
    assert _live_ring(base) == (409, {"ok": False, "error": "live_client_required"})
    # A declared phone that switched agent rings off for itself is not a recipient either.
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    assert _call(base, phone["token"], "POST", "/calls/ring/device", {"receive_rings": False})[0] == 200
    assert _live_ring(base) == (409, {"ok": False, "error": "live_client_required"})
    assert google.send_calls == []


def test_a_classic_ring_to_a_declared_android_phone_is_unchanged(host, google):
    base = host
    phone = _pair()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    status, body = _call(base, ADMIN, "POST", "/calls/ring", {
        "agent": "Sandra", "reason": "Vann", "opening_line": "Hei."})
    assert status == 200 and "fcm_live" not in body["delivery"]
    [send] = google.send_calls
    assert _data(send)["kind"] == "agent-ring" and "conversation_mode" not in _data(send)


def test_the_live_agent_tools_count_a_declared_android_phone_as_a_recipient(host):
    from lib.agent_live_tools import Effects
    base = host
    phone = _pair()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    tools = Effects.__new__(Effects)
    assert tools.recipient_ok(phone["device_id"]) is True
    assert tools.recipient_ok(_pair()["device_id"]) is False


# ---- 3. the push row's ownership and revocation rules -------------------------------------------

def test_unregistering_the_row_ends_live_capability(host):
    base = host
    phone = _pair()
    _store_live_ring()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    assert _sees_live(base, phone)
    assert _call(base, phone["token"], "DELETE", "/devices", {"token": ANDROID}) == (200, {"ok": True, "disabled": 1})
    assert not _sees_live(base, phone)


def test_a_disabled_row_ends_live_capability(host):
    base = host
    phone = _pair()
    _store_live_ring()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    apns.disable_token(ANDROID, "Unregistered")                       # FCM said the token is gone
    assert not _sees_live(base, phone)


def test_re_registering_replaces_the_declaration_and_without_the_field_clears_it(host):
    base = host
    phone = _pair()
    _store_live_ring()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    assert _sees_live(base, phone)
    assert _android(base, phone) == (200, {"ok": True})               # no field: cleared
    assert _row(ANDROID)["supported_conversation_modes"] == "" and not _sees_live(base, phone)
    assert _android(base, phone, supported_conversation_modes=["classic"])[0] == 200
    assert not _sees_live(base, phone)
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    assert _sees_live(base, phone)
    # A rotated token replaces the old row, and only the new registration's declaration counts.
    assert _android(base, phone, token=ANDROID_2)[0] == 200
    assert _row(ANDROID)["disabled_at"] is not None and not _sees_live(base, phone)


def test_revoking_the_pairing_ends_live_capability(host):
    base = host
    phone, other = _pair(), _pair()
    _store_live_ring()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    assert _android(base, other, token=ANDROID_2, supported_conversation_modes=LIVE)[0] == 200
    assert _call(base, ADMIN, "POST", "/paired-devices/revoke", {"device_id": phone["device_id"]})[0] == 200
    assert not agent_rings.live_capable(phone["device_id"])
    assert _sees_live(base, other)                                    # another phone untouched


def test_a_revoked_owner_is_not_capable_even_if_its_row_outlived_the_revocation(host):
    base = host
    phone = _pair()
    assert _android(base, phone, supported_conversation_modes=LIVE)[0] == 200
    db.conn().execute("UPDATE paired_devices SET revoked_at = ? WHERE device_id = ?",
                      (db.now_ms(), phone["device_id"]))                # revoked, row still live
    assert _row(ANDROID)["disabled_at"] is None
    assert not agent_rings.live_capable(phone["device_id"])


def test_a_row_taken_over_by_another_pairing_carries_only_the_new_declaration(host):
    base = host
    old = _pair()
    _store_live_ring()
    assert _android(base, old, supported_conversation_modes=LIVE)[0] == 200
    assert _call(base, ADMIN, "POST", "/paired-devices/revoke", {"device_id": old["device_id"]})[0] == 200
    again = _pair()                                                    # the same phone, paired again
    assert _android(base, again) == (200, {"ok": True})
    assert _row(ANDROID)["principal"] == again["device_id"]
    assert not agent_rings.live_capable(old["device_id"]) and not _sees_live(base, again)
    assert _android(base, again, supported_conversation_modes=LIVE)[0] == 200
    assert _sees_live(base, again) and not agent_rings.live_capable(old["device_id"])


def test_an_unbound_or_limited_registration_makes_nobody_capable(host):
    base = host
    limited = _pair(scope="limited")
    assert _call(base, ADMIN, "POST", "/devices", {"token": ANDROID, "platform": "android",
                                                   "supported_conversation_modes": LIVE})[0] == 200
    assert _row(ANDROID)["principal"] == ""
    _android(base, limited, token=ANDROID_2, supported_conversation_modes=LIVE)
    assert (_row(ANDROID_2) or {}).get("principal", "") == ""
    assert not agent_rings.live_capable(limited["device_id"]) and not agent_rings._eligible_live_principals()


# ---- 5. an iPhone through either route behaves as before ----------------------------------------

def test_an_iphone_alert_registration_ignores_the_field_as_before(host):
    base = host
    phone = _pair()
    for modes in (None, LIVE, "garbage", [1]):
        body = {"token": IPHONE, "session": "koko"}
        if modes is not None:
            body["supported_conversation_modes"] = modes
        assert _call(base, phone["token"], "POST", "/devices", body) == (200, {"ok": True})
        assert _row(IPHONE) == {"principal": phone["device_id"], "disabled_at": None,
                                "supported_conversation_modes": ""}
    assert not agent_rings.live_capable(phone["device_id"])


def test_an_iphone_voip_registration_and_its_live_ring_are_unchanged(host, google, monkeypatch):
    base = host
    phone = _pair()
    voip = []
    monkeypatch.setattr(apns, "send_agent_ring_voip",
                        lambda ring, tokens: voip.append([t["principal"] for t in tokens])
                        or {"enabled": True, "sent": len(tokens), "failed": 0})
    assert _call(base, phone["token"], "POST", "/calls/ring/voip-token",
                 {"token": "cd" * 32, "supported_conversation_modes": ["x"]}) == (
        400, {"ok": False, "error": "live_mode_invalid"})
    assert _call(base, phone["token"], "POST", "/calls/ring/voip-token",
                 {"token": "cd" * 32, "supported_conversation_modes": ["classic", "agent_live_v1"]}) == (
        200, {"ok": True, "registered": True})
    assert [{k: v for k, v in row.items() if k != "updated_at"} for row in agent_rings.voip_tokens()] == [
        {"principal": phone["device_id"], "token": "cd" * 32, "environment": "production",
         "supported_conversation_modes": ["classic", "agent_live_v1"]}]
    assert _call(base, phone["token"], "POST", "/devices", {"token": IPHONE, "session": "koko"})[0] == 200
    status, body = _live_ring(base)
    assert status == 200
    assert body["delivery"] == {"voip": {"enabled": True, "sent": 1, "failed": 0}}   # no fcm_live key
    assert voip == [[phone["device_id"]]] and google.send_calls == [] and google.apns_calls == []


def test_an_iphone_and_an_android_phone_each_get_their_own_live_push(host, google, monkeypatch):
    base = host
    iphone, android = _pair(), _pair()
    voip = []
    monkeypatch.setattr(apns, "send_agent_ring_voip",
                        lambda ring, tokens: voip.append([t["principal"] for t in tokens])
                        or {"enabled": True, "sent": len(tokens), "failed": 0})
    agent_rings.register_voip_token(iphone["device_id"], "cd" * 32, supported_conversation_modes=LIVE)
    assert _android(base, android, supported_conversation_modes=LIVE)[0] == 200
    status, body = _live_ring(base)
    assert status == 200 and body["delivery"]["voip"]["sent"] == 1 and body["delivery"]["fcm_live"]["sent"] == 1
    assert voip == [[iphone["device_id"]]]
    assert [s["json"]["message"]["token"] for s in google.send_calls] == [ANDROID]
