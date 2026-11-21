"""A limited device's snapshot gives each agent an empty cwd (R123, reviewer 1 on PR 529).

An agent row's cwd is a path in the owner's home, the same kind of fact as /server-info's
default_cwd. The key stays (both apps decode it as a required string); a limited device gets ""."""
from __future__ import annotations

import json

from lib import snapshot


def test_a_limited_snapshot_empties_cwd_and_is_made_from_the_same_build(monkeypatch):
    """One build serves only the requests that arrived before it started; a fixed clock makes
    the two calls such requests. The limited body is made lazily, from that build's full body."""
    built = []

    def build(ctx, *, include_archived=True):
        built.append(include_archived)
        return {"agents": [{"agent_id": "a1", "persona": "Pip", "cwd": "/home/owner/project"},
                           {"agent_id": "a2", "persona": "Ava", "cwd": "/home/owner/other"}],
                "server_now_ms": 1}
    monkeypatch.setattr(snapshot, "build_agent_snapshot", build)
    monkeypatch.setattr(snapshot.time, "monotonic", lambda: 100.0)
    shared = snapshot.SharedSnapshot()
    ctx = object()
    full = json.loads(shared.body(ctx))
    assert shared._last[True][3] is None                                    # nothing limited yet
    limited = json.loads(shared.body(ctx, limited=True))
    assert [a["cwd"] for a in limited["agents"]] == ["", ""]                 # the key stays, empty
    assert [a["cwd"] for a in full["agents"]] == ["/home/owner/project", "/home/owner/other"]
    assert {k: v for k, v in limited["agents"][0].items() if k != "cwd"} == {"agent_id": "a1", "persona": "Pip"}
    assert built == [True]                                                   # one build for both


def test_a_request_after_a_build_started_gets_a_build_of_its_own(monkeypatch):
    monkeypatch.setattr(snapshot, "build_agent_snapshot",
                        lambda ctx, *, include_archived=True: {"agents": [{"cwd": "/home/owner"}]})
    clock = iter([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    monkeypatch.setattr(snapshot.time, "monotonic", lambda: next(clock))
    shared = snapshot.SharedSnapshot()
    ctx = object()
    shared.body(ctx)
    started = shared._last[True][0]
    shared.body(ctx, limited=True)                                           # arrived after: rebuilt
    assert shared._last[True][0] > started and json.loads(shared._last[True][3])["agents"][0]["cwd"] == ""


def test_the_snapshot_route_asks_for_the_limited_body_for_a_limited_device(monkeypatch):
    from lib.routes.agents import AgentRoutes
    asked = []
    monkeypatch.setattr(snapshot.SHARED, "body",
                        lambda ctx, *, include_archived=True, limited=False: asked.append(limited) or b"{}")

    class Handler(AgentRoutes):
        ctx = object()

        def __init__(self, scope):
            self._request_device_scope = scope

        def _query(self):
            return {}

        def _send(self, status, body, content_type):
            pass
    Handler("limited")._handle_snapshot()
    Handler("full")._handle_snapshot()
    assert asked == [True, False]
