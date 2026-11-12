"""Push tokens through the Host over real HTTP (feature push_unregister, contract 93):
DELETE /devices, GET /devices/push-tokens, and what revoking a pairing does to its tokens."""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlsplit

from tests.integration.test_server_di import fake_ctx, running_server  # noqa: F401 - fixtures

FCM_TOKEN = "dGVzdC1mY20tdG9rZW4:APA91bH" + "x" * 140
IPHONE = "a1" * 32
ADMIN = "local-administrator"


def _call(base, credential, method, path, body=None):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request(method, path, body=None if body is None else json.dumps(body),
                       headers={"Authorization": "Bearer " + credential,
                                "Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


def _pair(srv, ctx, scope="full"):
    from lib import device_pairing
    srv.ctx = ctx.with_(auth_token=ADMIN)
    return device_pairing.exchange(device_pairing.issue(scope=scope)["code"])


def _rows():
    from lib import db
    return {r["token"]: dict(r) for r in db.conn().execute(
        "SELECT token, provider, principal, disabled_at FROM device_tokens")}


def _register(base, device, token, **extra):
    body = {"token": token, "session": "koko", **extra}
    return _call(base, device["token"], "POST", "/devices", body)


def test_a_device_unregisters_only_its_own_token(running_server):
    base, ctx, srv = running_server
    phone, other = _pair(srv, ctx), _pair(srv, ctx)
    assert _register(base, phone, FCM_TOKEN, platform="android")[0] == 200
    assert _register(base, other, IPHONE)[0] == 200
    # Another device's token: not found for this caller, nothing changes.
    assert _call(base, phone["token"], "DELETE", "/devices", {"token": IPHONE}) == (
        404, {"ok": False, "error": "device_token_not_found"})
    assert _call(base, phone["token"], "DELETE", "/devices", {"device_id": other["device_id"]}) == (
        400, {"ok": False, "error": "token_required"})
    assert _call(base, phone["token"], "DELETE", "/devices", {"token": FCM_TOKEN}) == (
        200, {"ok": True, "disabled": 1})
    rows = _rows()
    assert rows[FCM_TOKEN]["disabled_at"] is not None and rows[IPHONE]["disabled_at"] is None
    # Unregistering twice finds nothing live; registering again brings it back.
    assert _call(base, phone["token"], "DELETE", "/devices", {"token": FCM_TOKEN})[0] == 404
    assert _register(base, phone, FCM_TOKEN, platform="android")[0] == 200
    assert _rows()[FCM_TOKEN]["disabled_at"] is None


def test_the_administrator_disables_by_prefix_or_device_and_lists_without_full_tokens(running_server):
    base, ctx, srv = running_server
    phone = _pair(srv, ctx)
    assert _register(base, phone, FCM_TOKEN, platform="android")[0] == 200
    assert _register(base, phone, IPHONE)[0] == 200
    status, listing = _call(base, ADMIN, "GET", "/devices/push-tokens")
    assert status == 200
    prefixes = {row["token_prefix"]: row for row in listing["tokens"]}
    assert set(prefixes) == {FCM_TOKEN[:12], IPHONE[:12]}
    assert all(len(p) == 12 for p in prefixes) and FCM_TOKEN not in json.dumps(listing)
    assert prefixes[FCM_TOKEN[:12]]["provider"] == "fcm"
    assert prefixes[FCM_TOKEN[:12]]["owner"] == phone["device_id"]
    assert _call(base, ADMIN, "DELETE", "/devices", {"token_prefix": "short"})[1]["error"] == \
        "token_prefix_too_short"
    assert _call(base, ADMIN, "DELETE", "/devices", {}) == (
        400, {"ok": False, "error": "one_of_token_token_prefix_device_id"})
    assert _call(base, ADMIN, "DELETE", "/devices", {"token_prefix": FCM_TOKEN[:12]}) == (
        200, {"ok": True, "disabled": 1})
    assert _call(base, ADMIN, "DELETE", "/devices", {"device_id": phone["device_id"]}) == (
        200, {"ok": True, "disabled": 1})
    assert all(row["disabled_at"] is not None for row in _rows().values())
    # A device sees only its own rows.
    other = _pair(srv, ctx)
    assert _call(base, other["token"], "GET", "/devices/push-tokens") == (200, {"ok": True, "tokens": []})


def test_a_limited_device_can_neither_list_nor_unregister(running_server):
    base, ctx, srv = running_server
    limited = _pair(srv, ctx, scope="limited")
    assert _call(base, limited["token"], "GET", "/devices/push-tokens")[0] == 403
    assert _call(base, limited["token"], "DELETE", "/devices", {"token": FCM_TOKEN})[0] == 403


def test_revoking_a_pairing_stops_its_pushes_and_the_phone_paired_again_takes_its_token(running_server):
    base, ctx, srv = running_server
    old, other = _pair(srv, ctx), _pair(srv, ctx)
    assert _register(base, old, FCM_TOKEN, platform="android")[0] == 200
    assert _register(base, old, IPHONE)[0] == 200
    assert _register(base, other, "b2" * 32)[0] == 200
    assert _call(base, ADMIN, "POST", "/paired-devices/revoke", {"device_id": old["device_id"]})[0] == 200
    rows = _rows()
    assert rows[FCM_TOKEN]["disabled_at"] is not None and rows[IPHONE]["disabled_at"] is not None
    assert rows[FCM_TOKEN]["principal"] == old["device_id"]           # provenance kept
    assert rows["b2" * 32]["disabled_at"] is None                    # another phone untouched
    # The same phone, paired again, registers its token: it takes it over.
    again = _pair(srv, ctx)
    assert _register(base, again, FCM_TOKEN, platform="android") == (200, {"ok": True})
    row = _rows()[FCM_TOKEN]
    assert (row["principal"], row["disabled_at"]) == (again["device_id"], None)
    # The administrator (an unbound registrant) never takes over a device's token.
    assert _call(base, ADMIN, "POST", "/devices", {"token": IPHONE})[0] == 409
