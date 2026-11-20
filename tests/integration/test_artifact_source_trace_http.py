"""An artifact names the turn that published it only while that turn runs (R102), through the
real dispatcher and POST /artifacts.

A turn that completes normally is settled by the dispatcher (settled_at, outcome) and keeps
ended_at empty; only a Stop, a drain, a cancel or a handoff closes it. A card published after the
dispatcher settled the turn is placed by time, as one published after a Stop is. The trace is
the token's turn's and nothing the caller writes."""
from __future__ import annotations

import os

import pytest

from lib import db, provider_background_jobs as provider_jobs
from lib import turn_dispatch as td
from tests.integration.test_attention_questions_http import host, _request, server_module  # noqa: F401
from tests.unit.test_drain_pause_parity import Provider, World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


class _TokenProvider(Provider):
    """A per-turn backend as claude, grok, agy, muse, opencode and codex exec are: it records the
    turn's Host-issued token with the turn's trace where it starts the turn."""
    def __init__(self):
        super().__init__()
        self.tokens = {}

    def spawn_turn(self, backend, **kwargs):
        token = provider_jobs.new_turn_token()
        provider_jobs.turn_started(token, agent_id=kwargs["agent_id"], provider="claude",
                                   pid=os.getpid(), trace_id=kwargs["trace_id"])
        self.tokens[kwargs["trace_id"]] = token
        super().spawn_turn(backend, **kwargs)


def _world(tmp_path):
    world = World(tmp_path)
    world.provider = _TokenProvider()
    world.service.backends = world.provider
    return world


def _complete(world):
    """The provider's result, as the dispatcher receives it at a turn's normal end."""
    call = [c for c in world.provider.spawned if c["agent_id"] in world.provider.live][-1]
    world.provider.live.discard(call["agent_id"])
    call["on_result"]({"result": "done"})


def _publish(host, token="", **extra):
    headers = {"X-Clarp-Turn": token} if token else {}
    headers.update(extra.pop("headers", {}))
    body = {"session": "mike", "type": "document", "title": "A card", "payload": {"content": "words"}, **extra}
    status, row = _request(host, "/artifacts", body, headers=headers)
    assert status in (200, 201), row
    return row.get("artifact", row)["source_trace_id"]


def test_completed_normally_no_newer_turn_started_names_no_turn(host, tmp_path):
    world = _world(tmp_path)
    world.send("alpha")
    token = world.provider.tokens["t-alpha"]
    assert _publish(host, token) == "t-alpha"                       # inside the running turn
    _complete(world)                                                # settled; ended_at stays empty
    row = db.conn().execute(
        "SELECT ended_at, settled_at, outcome FROM turns WHERE trace_id='t-alpha'").fetchone()
    assert row["ended_at"] is None and row["settled_at"] is not None and row["outcome"] == "completed"
    assert _publish(host, token) == ""                              # after it: placed by time


def test_a_finished_turns_token_names_nothing_while_a_newer_turn_runs(host, tmp_path):
    world = _world(tmp_path)
    world.send("alpha")
    _complete(world)
    world.send("beta")
    assert _publish(host, world.provider.tokens["t-alpha"]) == ""
    assert _publish(host, world.provider.tokens["t-beta"]) == "t-beta"


@pytest.mark.parametrize("with_token", [False, True])
def test_a_trace_the_caller_writes_is_never_taken(host, tmp_path, with_token):
    world = _world(tmp_path)
    world.send("alpha")
    token = world.provider.tokens["t-alpha"] if with_token else ""
    trace = _publish(host, token, source_trace_id="t-forged", trace_id="t-forged",
                     headers={"X-Clarp-Trace": "t-forged", "X-Trace-Id": "t-forged"})
    assert trace == ("t-alpha" if with_token else "")


def test_a_card_published_by_the_turns_last_tool_call_comes_before_its_result(host, tmp_path):
    """The edge the settled rule could open: a publish made inside the turn is a tool call whose
    HTTP request returns before the tool does, and the provider's result (what settles the turn)
    comes only after the model has read the last tool's output. So the order is publish, then
    result: the card keeps its trace. A publish that a tool leaves running in the background and
    that lands after the result is a detached job, placed by time by the rule."""
    world = _world(tmp_path)
    world.send("alpha")
    token = world.provider.tokens["t-alpha"]
    last_tool_call = _publish(host, token)                          # the tool returns after this
    _complete(world)                                                # then the result
    assert last_tool_call == "t-alpha"


def test_parked_waiting_for_its_account_names_no_turn(host, tmp_path):
    """A turn parked waiting for an account keeps its row open and unsettled, and its process may
    still hold the token: not running (the Rust Host answers the same). When it resumes, its turn
    runs again under the same trace."""
    from lib import turn_lifecycle
    from lib.turn_lifecycle import TurnEvent
    world = _world(tmp_path)
    world.send("alpha")
    token = world.provider.tokens["t-alpha"]
    turn_lifecycle.try_transition(world.agent, TurnEvent.ACCOUNT_RECOVERY_WAIT, {
        "dispatch": "claude", "trace_id": "t-alpha", "account_recovery": "waiting",
        "message": "Waiting for an account with available usage"})
    row = db.conn().execute("SELECT ended_at, settled_at FROM turns WHERE trace_id='t-alpha'").fetchone()
    assert row["ended_at"] is None and row["settled_at"] is None
    assert _publish(host, token) == ""
    turn_lifecycle.try_transition(world.agent, TurnEvent.SPAWN_STARTED, {"trace_id": "t-alpha"})
    assert _publish(host, token) == "t-alpha"
