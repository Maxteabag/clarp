"""A phone that switched agent rings off, over real HTTP: the route, its refusals, /events and GET /calls/ring."""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlsplit

from lib import agent_rings, events
from tests.integration.test_agent_ring_live_delivery import _ring, _store, _stream, _until
from tests.integration.test_server_di import fake_ctx, running_server  # noqa: F401 - fixtures


def _call(base, token, method, body=None):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request(method, "/calls/ring/device", body=None if body is None else json.dumps(body),
                       headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


def _get_ring(base, token):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request("GET", "/calls/ring", headers={"Authorization": "Bearer " + token})
        response = client.getresponse()
        return json.loads(response.read() or b"{}")
    finally:
        client.close()


def _phones(srv, ctx):
    from lib import device_pairing
    srv.ctx = ctx.with_(auth_token="local-administrator")
    camera = device_pairing.exchange(device_pairing.issue()["code"])     # films the demo: rings off
    other = device_pairing.exchange(device_pairing.issue()["code"])      # the phone that should ring
    return camera, other


def _register(base, token, apns_token):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request("POST", "/devices", body=json.dumps({"token": apns_token, "session": "koko"}),
                       headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


def test_a_phone_switches_its_own_rings_off_and_on_over_http(running_server):
    base, ctx, srv = running_server
    camera, other = _phones(srv, ctx)
    assert _call(base, camera["token"], "GET") == \
        (200, {"ok": True, "receive_rings": True, "alerts_suppressed": False, "state": "on"})
    assert _register(base, camera["token"], "a1" * 32)[0] == 200          # POST /devices binds it to the camera
    assert _call(base, camera["token"], "POST", {"receive_rings": False}) == \
        (200, {"ok": True, "receive_rings": False, "alerts_suppressed": True, "state": "off"})
    assert _call(base, other["token"], "GET")[1]["receive_rings"] is True
    assert _call(base, camera["token"], "POST", {"receive_rings": True})[1]["state"] == "on"


def test_post_devices_binds_to_the_authenticated_phone_and_refuses_another_phone_or_the_administrator(
        running_server):
    from lib import apns, db
    base, ctx, srv = running_server
    camera, other = _phones(srv, ctx)
    assert _register(base, camera["token"], "a1" * 32)[0] == 200
    assert _register(base, other["token"], "b2" * 32)[0] == 200           # same client session "koko"
    owners = {r["token"]: (r["owner"], r["disabled_at"]) for r in
              db.conn().execute("SELECT token, principal AS owner, disabled_at FROM device_tokens")}
    assert owners["a1" * 32] == (camera["device_id"], None) and owners["b2" * 32] == (other["device_id"], None)
    for token in (other["token"], "local-administrator"):
        assert _register(base, token, "a1" * 32) == (409, {"ok": False, "error": "device_token_owned_elsewhere"})
    assert apns.owned_active_tokens() and owners == {
        r["token"]: (r["owner"], r["disabled_at"]) for r in
        db.conn().execute("SELECT token, principal AS owner, disabled_at FROM device_tokens")}


def test_the_administrator_cannot_switch_rings_and_a_malformed_body_is_refused(running_server):
    base, ctx, srv = running_server
    camera, _ = _phones(srv, ctx)
    status, body = _call(base, "local-administrator", "POST", {"receive_rings": False})
    assert (status, body.get("error")) == (403, "ring_device_required")
    status, body = _call(base, camera["token"], "POST", {"receive_rings": "no"})
    assert (status, body.get("error")) == (400, "ring_device_invalid")
    assert agent_rings.receives_rings(camera["device_id"])


def test_a_phone_with_rings_off_gets_no_ring_event_and_no_ring_in_the_poll(running_server):
    base, ctx, srv = running_server
    camera, other = _phones(srv, ctx)
    assert _call(base, camera["token"], "POST", {"receive_rings": False})[0] == 200
    _store("rng_classic", live=False)
    assert _get_ring(base, camera["token"])["ring"] is None
    assert _get_ring(base, other["token"])["ring"]["ring_id"] == "rng_classic"
    streams = {name: _stream(base, phone["token"]) for name, phone in (("camera", camera), ("other", other))}
    try:
        events.broadcast(srv.ctx.stream, _ring("rng_a"))
        assert [e["ring_id"] for e in _until(streams["other"][1], "rng_a")] == ["rng_a"]
        # Turning rings back on: the next ring reaches the camera phone again, the earlier one never did.
        assert _call(base, camera["token"], "POST", {"receive_rings": True})[0] == 200
        events.broadcast(srv.ctx.stream, _ring("rng_b"))
        assert [e["ring_id"] for e in _until(streams["camera"][1], "rng_b")] == ["rng_b"]
    finally:
        for client, _ in streams.values():
            client.close()


def test_a_phone_with_rings_off_still_gets_the_lifecycle_of_the_ring_it_answered(running_server):
    base, ctx, srv = running_server
    camera, other = _phones(srv, ctx)
    assert _call(base, camera["token"], "POST", {"receive_rings": False})[0] == 200
    client, response = _stream(base, camera["token"])
    try:
        events.broadcast(srv.ctx.stream, _ring("rng_new"))                                   # a new ring: held back
        events.broadcast(srv.ctx.stream, _owned("rng_mine", camera["device_id"]))     # the ring it answered
        assert [e["ring_id"] for e in _until(response, "rng_mine")] == ["rng_mine"]
    finally:
        client.close()


def _owned(ring_id, owner):
    return events.agent_ring(ring_id=ring_id, session="rachel", agent_id="a", persona="Rachel", reason="r",
                             opening_line="o", opening_audio_url=None, state="answered", created_at=1,
                             expires_at=2, ended_at=None, answered_by=owner, call_id="c1", server_now=1)
