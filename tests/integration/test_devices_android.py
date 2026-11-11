"""POST /devices for an Android phone (FCM), over real HTTP with a paired-device credential."""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlsplit

from tests.integration.test_server_di import fake_ctx, running_server  # noqa: F401 - fixtures

FCM_TOKEN = "dGVzdC1mY20tdG9rZW4:APA91bH" + "x" * 140
FCM_ROTATED = "cm90YXRlZC10b2tlbg:APA91bH" + "y" * 140


def _post(base, credential, body):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request("POST", "/devices", body=json.dumps(body),
                       headers={"Authorization": "Bearer " + credential,
                                "Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


def _phones(srv, ctx):
    from lib import device_pairing
    srv.ctx = ctx.with_(auth_token="local-administrator")
    android = device_pairing.exchange(device_pairing.issue()["code"])
    other = device_pairing.exchange(device_pairing.issue()["code"])
    return android, other


def _rows():
    from lib import db
    return {r["token"]: dict(r) for r in db.conn().execute(
        "SELECT token, platform, provider, principal, disabled_at FROM device_tokens")}


def test_an_android_phone_registers_rotates_and_owns_its_fcm_token(running_server):
    base, ctx, srv = running_server
    android, other = _phones(srv, ctx)
    body = {"token": FCM_TOKEN, "platform": "android", "provider": "fcm", "session": "koko",
            "base_url": "http://100.101.102.103:7682"}
    assert _post(base, android["token"], body) == (200, {"ok": True})
    assert _post(base, android["token"], body) == (200, {"ok": True})            # idempotent
    row = _rows()[FCM_TOKEN]
    assert (row["platform"], row["provider"], row["principal"], row["disabled_at"]) == (
        "android", "fcm", android["device_id"], None)

    assert _post(base, other["token"], body) == (
        409, {"ok": False, "error": "device_token_owned_elsewhere"})

    assert _post(base, android["token"], {**body, "token": FCM_ROTATED}) == (200, {"ok": True})
    rows = _rows()
    assert rows[FCM_TOKEN]["disabled_at"] is not None and rows[FCM_ROTATED]["disabled_at"] is None


def test_a_bad_android_registration_is_refused_and_ios_is_unchanged(running_server):
    base, ctx, srv = running_server
    android, _ = _phones(srv, ctx)
    for body in ({"token": "not an fcm token", "platform": "android"},
                 {"token": FCM_TOKEN, "platform": "android", "provider": "webpush"}):
        assert _post(base, android["token"], body) == (400, {"ok": False, "error": "device_token_invalid"})
    assert _rows() == {}
    # An iPhone's body, as it has always been sent: APNs, owned, answered as before.
    assert _post(base, android["token"], {"token": "a1" * 32, "session": "koko"}) == (200, {"ok": True})
    assert _rows()["a1" * 32]["provider"] == "apns"
