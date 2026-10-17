"""H6: a Live ring reaches only a phone whose registration advertises agent_live_v1, over the real /events stream.

An older phone (2818) reports any agent-ring event to CallKit as an incoming call, so a Live ring's SSE event must
never reach it, while every classic ring still does. Ordering proof: the Live event is broadcast BEFORE a classic
one on the same stream, so a client that has received the classic event would already have received the Live one.
"""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlsplit

from lib import agent_rings, events
from tests.integration.test_server_di import fake_ctx, running_server  # noqa: F401 - fixtures


def _stream(base, token):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    client.request("GET", "/events", headers={"Authorization": "Bearer " + token, "X-Clarp-Transport": "relay"})
    response = client.getresponse()
    assert response.status == 200
    # Barrier: the server writes ": connected" only after it subscribed this client to the stream.
    while response.fp.readline().strip() != b": connected":
        pass
    return client, response


def _until(response, ring_id):
    """Every agent-ring event this client gets, up to and including the one for ring_id."""
    seen = []
    while True:
        line = response.fp.readline()
        assert line, "stream ended"
        if not line.startswith(b"data: "):
            continue
        event = json.loads(line[6:])
        if event.get("type") == "agent-ring":
            seen.append(event)
            if event.get("ring_id") == ring_id:
                return seen


def _ring(ring_id, **extra):
    return events.agent_ring(ring_id=ring_id, session="rachel", agent_id="a", persona="Rachel", reason="r",
                             opening_line="o", opening_audio_url=None, state="ringing", created_at=1,
                             expires_at=2, ended_at=None, answered_by=None, call_id=None, server_now=1, **extra)


def test_a_live_ring_event_reaches_only_the_live_capable_phone(running_server):
    from lib import device_pairing
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token="local-administrator")
    old = device_pairing.exchange(device_pairing.issue()["code"])
    new = device_pairing.exchange(device_pairing.issue()["code"])
    agent_rings.register_voip_token(old["device_id"], "ab" * 32)
    agent_rings.register_voip_token(new["device_id"], "cd" * 32,
                                    supported_conversation_modes=["classic", "agent_live_v1"])
    streams = {name: _stream(base, device["token"]) for name, device in (("old", old), ("new", new))}
    try:
        events.broadcast(srv.ctx.stream, _ring("rng_live", conversation_mode="agent_live_v1"))
        events.broadcast(srv.ctx.stream, _ring("rng_classic"))
        old_seen = [e["ring_id"] for e in _until(streams["old"][1], "rng_classic")]
        new_seen = [e["ring_id"] for e in _until(streams["new"][1], "rng_classic")]
        assert old_seen == ["rng_classic"]
        assert new_seen == ["rng_live", "rng_classic"]
        # Replay after reconnect is filtered the same way.
        client, response = _stream(base, old["token"])
        try:
            replay = [e["ring_id"] for e in _until(response, "rng_classic")]
        finally:
            client.close()
        assert "rng_live" not in replay
    finally:
        for client, _ in streams.values():
            client.close()
