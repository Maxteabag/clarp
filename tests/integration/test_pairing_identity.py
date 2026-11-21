"""R172: an app confirms the Host before it spends a pairing code, and a limited
device may end its own pairing when it finds it paired the wrong Host."""
from __future__ import annotations

import json
import threading
import urllib.error

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _free_port, _get, _post, _post_with_headers, build_server, fake_ctx,
)

ADMIN = "administrator-token"


@pytest.fixture
def token_host(fake_ctx):
    ctx = fake_ctx.with_(auth_token=ADMIN)
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", srv
    finally:
        srv.shutdown()
        srv.server_close()


def _pair(base, scope):
    from lib import device_pairing
    issued = device_pairing.issue(scope=scope, ttl_seconds=600)
    status, body = _post(base + "/pairing/exchange", {"code": issued["code"], "device_name": scope})
    assert status == 201
    device = json.loads(body)["device"]
    return device["device_id"], {"Authorization": f"Bearer {device['token']}"}


def _status(call):
    try:
        return call()[0]
    except urllib.error.HTTPError as exc:
        return exc.code


def test_identity_answers_without_a_token_and_says_nothing_else(token_host):
    from lib import server_identity
    base, _srv = token_host
    assert _status(lambda: _get(base + "/server-info")) == 401   # the Host does want a token
    status, body = _get(base + "/pairing/identity")
    assert status == 200
    answer = json.loads(body)
    assert answer == {"server_id": server_identity.server_id(),
                      "name": answer["name"],
                      "contract": {"host": server_identity.HOST_CONTRACT}}
    assert isinstance(answer["name"], str) and answer["name"]
    assert "pairing_identity" in server_identity.FEATURES


def test_identity_reads_have_their_own_budget(token_host):
    base, srv = token_host
    limit = srv.identity_reads.limit
    for _ in range(limit):
        assert _status(lambda: _get(base + "/pairing/identity")) == 200
    with pytest.raises(urllib.error.HTTPError) as limited:
        _get(base + "/pairing/identity")
    assert limited.value.code == 429 and int(limited.value.headers["Retry-After"]) >= 1
    # Reading the identity spent nothing of the exchange's failure budget.
    assert srv.auth_failures._sources == {}


def test_a_limited_device_revokes_itself_and_its_token_stops_at_once(token_host):
    base, _srv = token_host
    device_id, headers = _pair(base, "limited")
    assert _status(lambda: _get(base + "/agents/snapshot", headers=headers)) == 200
    status, body = _post_with_headers(base + "/paired-devices/revoke", {"device_id": device_id}, headers)
    assert (status, json.loads(body)) == (200, {"ok": True})
    assert _status(lambda: _get(base + "/agents/snapshot", headers=headers)) == 401


def test_a_limited_device_cannot_revoke_another(token_host):
    base, _srv = token_host
    _mine, headers = _pair(base, "limited")
    other, other_headers = _pair(base, "full")
    with pytest.raises(urllib.error.HTTPError) as refused:
        _post_with_headers(base + "/paired-devices/revoke", {"device_id": other}, headers)
    assert refused.value.code == 403
    assert json.loads(refused.value.read()) == {"error": "full device access required"}
    assert _status(lambda: _get(base + "/agents/snapshot", headers=other_headers)) == 200
    # Still no other paired-devices route for a limited device.
    assert _status(lambda: _get(base + "/paired-devices", headers=headers)) == 403


def test_a_full_device_revokes_any_device_as_before(token_host):
    base, _srv = token_host
    limited, limited_headers = _pair(base, "limited")
    _full, full_headers = _pair(base, "full")
    status, _body = _post_with_headers(base + "/paired-devices/revoke", {"device_id": limited}, full_headers)
    assert status == 200
    assert _status(lambda: _get(base + "/agents/snapshot", headers=limited_headers)) == 401
