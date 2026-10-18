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


def test_a_phone_switches_its_own_rings_off_and_on_over_http(running_server):
    base, ctx, srv = running_server
    camera, other = _phones(srv, ctx)
    assert _call(base, camera["token"], "GET") == (200, {"ok": True, "receive_rings": True, "push_token_bound": False})
    assert _call(base, camera["token"], "POST", {"receive_rings": False, "push_token": "a1" * 32}) == \
        (200, {"ok": True, "receive_rings": False, "push_token_bound": True})
    assert _call(base, other["token"], "GET")[1]["receive_rings"] is True
    assert _call(base, camera["token"], "POST", {"receive_rings": True})[1]["receive_rings"] is True


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
