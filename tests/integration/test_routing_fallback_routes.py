"""What a router fallback answers, by how the request came (issue 360, PR 439 follow-up item 10).

An app's failed name delegation (POST /orchestrator/route-delegation) that the router falls back on
delivers nothing: dispatch is never called, the answer's dispatch is "" and its session is the one
asked for. A plain hands-free send that falls back is delivered once, directly, and its answer has
no orchestrator envelope. The router here is the real one; only its model, and the janitor's run
bookkeeping where a way needs it, are stood in for."""
from __future__ import annotations

import itertools
import json

import pytest

from lib import db, janitor_builtins, orchestrator
from tests.integration.test_server_di import _post, fake_ctx, running_server  # noqa: F401

# Each way, by the reason the router records for it.
WAYS = {"busy": "Message delegator is busy or paused",
        "policy changed": "Message delegator policy changed",
        "stale": "Message delegator configuration changed",
        "model failed": "orchestrator model failed"}
_ids = itertools.count()


@pytest.fixture
def routed(running_server, monkeypatch):  # noqa: F811
    """(base url, the turns started, the model's calls, ``way(name)``)."""
    base, _ctx, _srv = running_server
    orchestrator.update_settings({"enabled": True, "provider": "codex", "model": "gpt-5.3-codex-spark",
                                  "fallback_only": False})
    started, asked = [], []

    def start_turn(*, text, cwd, **metadata):
        started.append(text)
        return type("_FakeHandle", (), {"pid": 99})()
    from lib.backend.registry import by_id
    monkeypatch.setattr(by_id("claude"), "start_turn", start_turn)
    behaviour = {"fail": False}

    def model(packet, _settings):
        asked.append(packet["utterance"])
        if behaviour["fail"]:
            raise RuntimeError("the model is down")
        return {"kind": "agent_message", "target_session": "rachel", "confidence": 0.95, "reason": "x"}
    monkeypatch.setattr(orchestrator, "call_model", model)

    def way(name):
        if name == "busy":
            monkeypatch.setattr(janitor_builtins, "begin_run", lambda *a, **k: None)
        elif name == "policy changed":
            answers = iter((True, False))       # runs when asked; changed once it is claimed
            monkeypatch.setattr(orchestrator, "should_run", lambda *a, **k: next(answers, False))
        elif name == "stale":
            current = janitor_builtins.is_current
            monkeypatch.setattr(janitor_builtins, "is_current",
                                lambda run_id, **k: not asked and current(run_id, **k))
        elif name == "model failed":
            behaviour["fail"] = True
    return base, started, asked, way


def _send(base, path, client_id, text="send the report"):
    status, body = _post(base + path, {"text": text, "session": "claude", "hands_free": True,
                                       "client_msg_id": client_id})
    return status, json.loads(body)


def _decided():
    """The router's own record of each request: it ran, and this is what it decided."""
    return [row[0] for row in db.conn().execute(
        "SELECT final_action FROM orchestrator_decisions ORDER BY decision_id")]


def _reasons():
    return [row[0] for row in db.conn().execute(
        "SELECT reason FROM orchestrator_decisions ORDER BY decision_id")]


@pytest.mark.parametrize("name", WAYS)
def test_an_apps_request_the_router_falls_back_on_delivers_nothing(routed, name):
    base, started, _asked, way = routed
    way(name)
    status, body = _send(base, "/orchestrator/route-delegation", f"app-{next(_ids)}")
    assert status == 200, body
    assert started == []
    assert body["dispatch"] == "" and body["session"] == "claude"
    # In an app's request the router's one authority is to pick a recipient: a failed model is
    # a question back, not a fallback; every other way is the fallback itself.
    assert body["orchestrator"]["action"] == ("clarify" if name == "model failed" else "fallback")
    assert _decided() == [body["orchestrator"]["action"]] and _reasons() == [WAYS[name]]


@pytest.mark.parametrize("name", WAYS)
def test_a_plain_hands_free_send_the_router_falls_back_on_is_delivered_once_directly(routed, name):
    base, started, _asked, way = routed
    way(name)
    status, body = _send(base, "/send", f"voice-{next(_ids)}")
    assert status == 200, body
    assert started == ["send the report"]
    assert "orchestrator" not in body
    assert _decided() == ["fallback"] and _reasons() == [WAYS[name]]


def test_an_apps_request_retried_after_a_fallback_still_delivers_nothing(routed):
    base, started, _asked, way = routed
    way("policy changed")
    first = _send(base, "/orchestrator/route-delegation", "app-retried")
    again = _send(base, "/orchestrator/route-delegation", "app-retried")
    assert first[1]["orchestrator"]["action"] == again[1]["orchestrator"]["action"] == "fallback"
    assert started == [] and again[1]["dispatch"] == "" and again[1]["session"] == "claude"
    assert _decided() == ["fallback"]          # the retry is answered from the stored run


def test_a_plain_send_retried_after_its_routing_fell_back_is_delivered_once(routed):
    """The retry is answered from the stored run (action fallback), so the route sends it directly
    again; the turn dispatcher's own client id is what must keep it to one delivery."""
    base, started, asked, way = routed
    way("model failed")
    _send(base, "/send", "voice-retried")
    _send(base, "/send", "voice-retried")
    assert asked == ["send the report"] and _decided() == ["fallback"]
    assert started == ["send the report"]
