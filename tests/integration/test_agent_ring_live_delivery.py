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
    return client, response


def _until(response, ring_id):
    """Every agent-ring event this client gets, up to and including the one for ring_id.

    Deduplicated by event_id as a real client does: an event broadcast while the stream connects
    arrives both in the replay page and on the live queue."""
    seen = []
    ids = getattr(response, "_seen_event_ids", set())
    response._seen_event_ids = ids
    while True:
        line = response.fp.readline()
        assert line, "stream ended"
        if not line.startswith(b"data: "):
            continue
        event = json.loads(line[6:])
        if event.get("event_id") is not None:
            if event["event_id"] in ids:
                continue
            ids.add(event["event_id"])
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


# ---- GET /calls/ring over real HTTP ---------------------------------------------------

def _store(ring_id, *, live, state="ringing", answered_by=None):
    record = {"ring_id": ring_id, "session": "rachel", "agent_id": "a", "persona": "Rachel", "reason": "r",
              "opening_line": "o", "opening_audio_url": "", "state": state, "created_at": 1,
              "expires_at": agent_rings.now_ms() + 600_000, "ended_at": None, "answered_by": answered_by or "",
              "call_id": ""}
    if live:
        record["conversation_mode"] = "agent_live_v1"
    with agent_rings._lock:
        records = agent_rings._load()
        records.append(record)
        agent_rings._save(records)


def _get(base, token, ring_id=None):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        client.request("GET", "/calls/ring" + (f"?ring_id={ring_id}" if ring_id else ""),
                       headers={"Authorization": "Bearer " + token})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


def _devices(srv, ctx):
    from lib import device_pairing
    srv.ctx = ctx.with_(auth_token="local-administrator")
    old = device_pairing.exchange(device_pairing.issue()["code"])        # a published classic phone (2818)
    new = device_pairing.exchange(device_pairing.issue()["code"])        # registered for agent_live_v1
    unknown = device_pairing.exchange(device_pairing.issue()["code"])    # paired, never registered VoIP
    agent_rings.register_voip_token(old["device_id"], "ab" * 32)
    agent_rings.register_voip_token(new["device_id"], "cd" * 32,
                                    supported_conversation_modes=["classic", "agent_live_v1"])
    return old, new, unknown


def test_get_calls_ring_shows_a_live_ring_only_to_the_live_capable_phone(running_server):
    base, ctx, srv = running_server
    old, new, unknown = _devices(srv, ctx)
    _store("rng_live", live=True)
    status, body = _get(base, new["token"])
    assert status == 200 and body["ring"]["ring_id"] == "rng_live"
    assert body["ring"]["conversation_mode"] == "agent_live_v1"
    assert _get(base, new["token"], "rng_live")[1]["ring"]["ring_id"] == "rng_live"
    for device in (old, unknown):
        status, body = _get(base, device["token"])
        assert status == 200 and body["ring"] is None
        status, body = _get(base, device["token"], "rng_live")      # by id: as unknown as any other id
        assert (status, body.get("error")) == (404, "unknown call")


def test_get_calls_ring_shows_a_classic_ring_to_every_phone(running_server):
    base, ctx, srv = running_server
    old, new, unknown = _devices(srv, ctx)
    _store("rng_classic", live=False)
    for device in (old, new, unknown):
        assert _get(base, device["token"])[1]["ring"]["ring_id"] == "rng_classic"
        assert _get(base, device["token"], "rng_classic")[1]["ring"]["ring_id"] == "rng_classic"


def test_the_phone_that_answered_a_live_ring_still_reads_it_after_dropping_live(running_server):
    base, ctx, srv = running_server
    old, new, _ = _devices(srv, ctx)
    _store("rng_answered", live=True, state="answered", answered_by=new["device_id"])
    agent_rings.register_voip_token(new["device_id"], "cd" * 32)        # re-registers classic only
    status, body = _get(base, new["token"], "rng_answered")
    assert status == 200 and body["ring"]["ring_id"] == "rng_answered"
    assert _get(base, old["token"], "rng_answered")[0] == 404           # the exception is the answerer's only


# ---- capability is read per event, not cached per connection ---------------------------

def test_a_phone_that_drops_live_on_an_open_stream_stops_getting_live_events(running_server):
    base, ctx, srv = running_server
    _, new, _ = _devices(srv, ctx)
    client, response = _stream(base, new["token"])
    try:
        events.broadcast(srv.ctx.stream, _ring("rng_live_1", conversation_mode="agent_live_v1"))
        assert [e["ring_id"] for e in _until(response, "rng_live_1")] == ["rng_live_1"]
        agent_rings.register_voip_token(new["device_id"], "cd" * 32)    # same connection, Live dropped
        events.broadcast(srv.ctx.stream, _ring("rng_live_2", conversation_mode="agent_live_v1"))
        events.broadcast(srv.ctx.stream, _ring("rng_classic_2"))
        assert [e["ring_id"] for e in _until(response, "rng_classic_2")] == ["rng_classic_2"]
    finally:
        client.close()


# ---- explicit limit: a stale registration is trusted until the phone registers again ----

def test_a_stale_live_registration_after_a_downgrade_is_still_trusted_an_explicit_limit(running_server):
    """NOT a no-ring guarantee. The Host cannot know that a phone which registered agent_live_v1 was
    downgraded: until it registers again it still gets the Live ring. Only a fresh registration without
    agent_live_v1 (or a forgotten token) stops it. This test pins the limit so it is never mistaken
    for isolation."""
    base, ctx, srv = running_server
    _, new, _ = _devices(srv, ctx)
    _store("rng_stale", live=True)
    # "Downgraded" app that has not registered again: nothing on the Host changed.
    assert _get(base, new["token"])[1]["ring"]["ring_id"] == "rng_stale"
    assert agent_rings.live_capable(new["device_id"]) is True
    # The only removal is its own fresh registration (or a forgotten token).
    agent_rings.register_voip_token(new["device_id"], "")
    assert agent_rings.live_capable(new["device_id"]) is False
    assert _get(base, new["token"])[1]["ring"] is None
