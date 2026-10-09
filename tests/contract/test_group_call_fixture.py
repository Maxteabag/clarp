"""The frozen group-call fixture (contract/fixtures/group-call.json) is what
the Host actually emits.

One scenario runs through the real ``group_calls`` module: a call starts with
Theo and Mike, an utterance reaches Mike, Nadia is added, Theo is held, the
user transfers to Omar, Nadia leaves and the call ends. Ids and the clock are
pinned, so the ``group-call`` events and the ``GET /calls`` snapshots must
equal the fixture exactly. The native clients decode the same file.
Regenerate with CLARP_UPDATE_FIXTURES=1 after an intended, additive change,
and tell the native clients.
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import uuid

from lib import db, group_calls

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "contract/fixtures/group-call.json"
START = 1_791_160_000_000
PHONE = "device_fixture"


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        if event["type"] == "group-call":
            self.events.append(json.loads(json.dumps(event)))


def _scenario(monkeypatch) -> dict:
    clock = itertools.count(START, 1000)
    monkeypatch.setattr(group_calls, "now_ms", lambda: next(clock))
    ids = itertools.count(1)
    monkeypatch.setattr(group_calls.uuid, "uuid4", lambda: uuid.UUID(int=next(ids)))
    monkeypatch.setattr(group_calls, "_host_id", lambda: "host_fixture")
    for persona, session in (("Theo", "theo-97e5"), ("Mike", "mike-86db"),
                             ("Nadia", "nadia-1a2b"), ("Omar", "omar-5e6f")):
        db.conn().execute(
            "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at)"
            " VALUES (?,?,?,?,?,?)", ("a-" + persona.lower(), persona, "v", "/tmp", session, START))
    stream = _Stream()
    group_calls.bind(type("Ctx", (), {"stream": stream})())
    snapshots = [group_calls.snapshot(PHONE)]
    call_id = group_calls.start(PHONE, ["Theo", "Mike"], request_id="fixture-1")["call"]["call_id"]
    group_calls.route(PHONE, call_id, "mike-86db")
    group_calls.change(PHONE, "add", "Nadia", by="mike-86db")
    group_calls.change(PHONE, "hold", "Theo")
    snapshots.append(group_calls.snapshot(PHONE))
    group_calls.change(PHONE, "transfer", "Omar")
    group_calls.change(PHONE, "remove", "Nadia")
    group_calls.end(PHONE)
    snapshots.append(group_calls.snapshot(PHONE))
    return {"principal": PHONE, "events": stream.events, "snapshots": snapshots}


def test_the_fixture_is_what_the_host_emits(monkeypatch):
    produced = _scenario(monkeypatch)
    if os.environ.get("CLARP_UPDATE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(produced, indent=2, ensure_ascii=False) + "\n")
    assert json.loads(FIXTURE.read_text()) == produced
    actions = [e["change"]["action"] for e in produced["events"]]
    assert actions == ["start", "route", "add", "hold", "transfer", "remove", "end"]
