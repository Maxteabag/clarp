"""The frozen agent-ring fixture (contract/fixtures/agent-ring.json) is what
the Host actually emits.

Theo rings, the user answers (a group call starts and the opening comes
back), then Theo rings again a minute later and the call is missed. Ids and
the clock are pinned, so the ``agent-ring`` events, the ring push payloads
and the answer response must equal the fixture exactly. The native clients
decode the same file. Regenerate with CLARP_UPDATE_FIXTURES=1 after an
intended, additive change, and tell the native clients.
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import uuid

from lib import agent_rings, apns, db, group_calls

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "contract/fixtures/agent-ring.json"
START = 1_791_160_000_000
PHONE = "device_fixture"


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        if event["type"] == "agent-ring":
            self.events.append(json.loads(json.dumps(event)))


class _TTS:
    def synthesize(self, text, voice_id, *, session=None):
        return pathlib.Path(f"/cache/1791160000000__{session}.mp3")


def _scenario(monkeypatch) -> dict:
    clock = {"now": START}
    monkeypatch.setattr(agent_rings, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(group_calls, "now_ms", lambda: clock["now"])
    ids = itertools.count(1)
    fixed = lambda: uuid.UUID(int=next(ids))  # noqa: E731
    monkeypatch.setattr(agent_rings.uuid, "uuid4", fixed)
    monkeypatch.setattr(group_calls.uuid, "uuid4", fixed)
    monkeypatch.setattr(group_calls, "_host_id", lambda: "host_fixture")
    monkeypatch.setattr(agent_rings, "_schedule_expiry", lambda ring: None)
    monkeypatch.setattr(agent_rings, "_push_voip", lambda ring: {"sent": 0})
    pushes = []
    monkeypatch.setattr(agent_rings, "_push", lambda ring, kind: pushes.append(
        apns.agent_ring_payload(ring, kind, server_instance_id="host_fixture")) or {"sent": 1})
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a-theo", "Theo", "v", "/tmp", "theo-97e5", START))
    stream = _Stream()
    ctx = type("Ctx", (), {"stream": stream, "tts": _TTS()})()
    agent_rings.bind(ctx)
    group_calls.bind(ctx)
    line = "Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?"
    first = agent_rings.ring(caller_agent_id="a-theo", reason="Possible water leak at pump 01",
                             opening_line=line)["ring"]
    clock["now"] += 5_000
    answer = agent_rings.answer(PHONE, first["ring_id"])
    clock["now"] += agent_rings.AGENT_INTERVAL_MS
    agent_rings.ring(caller_agent_id="a-theo", reason="The leak is getting worse", opening_line="It's me again.")
    clock["now"] += agent_rings.RING_TIMEOUT_MS
    agent_rings.expire_due()
    voip = apns.agent_ring_voip_payload(first, server_instance_id="host_fixture")
    return {"principal": PHONE, "events": stream.events, "pushes": pushes, "voip_push": voip, "answer": answer}


def test_the_fixture_is_what_the_host_emits(monkeypatch):
    produced = json.loads(json.dumps(_scenario(monkeypatch)))
    if os.environ.get("CLARP_UPDATE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(produced, indent=2, ensure_ascii=False) + "\n")
    assert json.loads(FIXTURE.read_text()) == produced
    assert [e["state"] for e in produced["events"]] == ["ringing", "answered", "ringing", "missed"]
    assert [p["ring_state"] for p in produced["pushes"]] == ["ringing", "answered", "ringing", "missed"]
