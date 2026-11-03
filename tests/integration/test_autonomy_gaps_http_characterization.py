"""Characterization of the remaining autonomy routes' branches as server.py
serves them today, before the S3 move of the autonomy handlers: task-plan
actions, the goal ledger and bookkeeping delegations, schedule toggling,
manual dream runs and team membership.

Library boundaries that would start work (dream runs, bookkeeping listener
units, goal mutations) are stubbed. Three handlers send no response at all to
a body that is not a JSON object; that pre-existing behaviour is pinned as is.
"""
from __future__ import annotations

import http.client
import json
import urllib.parse

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _call(base, method, path, body=None, raw=None, timeout=5.0):
    url = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    try:
        connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        if response.getheader("Content-Type") == "application/json":
            return response.status, json.loads(payload)
        return response.status, payload
    except (TimeoutError, OSError):
        return "no response", None
    finally:
        connection.close()


@pytest.mark.parametrize("path", [
    "/task-plan/action", "/goal-ledger/delegations", "/goal-ledger/delegations/stop"])
def test_bad_json_gets_no_response(running_server, path):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", path, raw=b"[1]", timeout=1.5) == ("no response", None)


def test_task_plan_action(running_server, monkeypatch):
    from lib import goal_ledger, task_goal_state
    base, _ctx, _srv = running_server
    actors = []

    def mutate(plan_id, *, revision, action, data):
        if plan_id == "stale":
            raise ValueError("revision is stale")
        return {"plan_id": plan_id, "revision": revision + 1, "action": action, "data": data}
    real_acting_as = goal_ledger.acting_as

    def acting_as(actor):
        actors.append(actor)
        return real_acting_as(actor)
    monkeypatch.setattr(task_goal_state, "mutate", mutate)
    monkeypatch.setattr(goal_ledger, "acting_as", acting_as)
    post = lambda body: _call(base, "POST", "/task-plan/action", body)  # noqa: E731
    assert post({"plan_id": "p1"}) == (409, {"error": "revision required"})
    assert post({"plan_id": "stale", "revision": 3}) == (409, {"error": "revision is stale"})
    assert post({"plan_id": "p1", "revision": 3, "action": "pause"}) == (200, {
        "plan": {"plan_id": "p1", "revision": 4, "action": "pause", "data": {}}})
    assert actors == ["user", "user"]


def test_goal_ledger_events_and_delegations(running_server, monkeypatch):
    from lib import goal_ledger
    base, _ctx, _srv = running_server
    monkeypatch.setattr(goal_ledger, "events",
                        lambda plan_id, *, after, limit: {"plan_id": plan_id, "after": after,
                                                          "limit": limit, "events": []})
    monkeypatch.setattr(goal_ledger, "delegations_for", lambda agent_id: [{"id": "d1"}])
    assert _call(base, "GET", "/goal-ledger") == (400, {"error": "plan_id required"})
    assert _call(base, "GET", "/goal-ledger?plan_id=p1&after=x") == (
        400, {"error": "after and limit are integers"})
    assert _call(base, "GET", "/goal-ledger?plan_id=p1&after=5") == (
        200, {"plan_id": "p1", "after": 5, "limit": 200, "events": []})
    assert _call(base, "GET", "/goal-ledger/delegations?session=nobody") == (
        404, {"error": "agent not found"})
    assert _call(base, "GET", "/goal-ledger/delegations?session=claude") == (
        200, {"delegations": [{"id": "d1"}]})


def test_goal_delegation_enable_and_stop(running_server, monkeypatch):
    from lib import bookkeeping_listener, goal_ledger
    base, _ctx, _srv = running_server
    seen = []

    def enable(principal, delegate, reason):
        if reason == "refuse":
            raise goal_ledger.LedgerError("already delegated")
        seen.append(("enable", principal["session"], delegate["session"], reason))
        return {"delegation_id": "d1"}

    def stop(delegation_id, reason):
        if delegation_id != "d1":
            raise goal_ledger.LedgerError("no such delegation")
        return {"delegation_id": delegation_id, "state": "stopped"}
    monkeypatch.setattr(goal_ledger, "enable", enable)
    monkeypatch.setattr(goal_ledger, "stop", stop)
    monkeypatch.setattr(bookkeeping_listener, "launch", lambda delegation_id: "started")
    monkeypatch.setattr(bookkeeping_listener, "stop_unit", lambda delegation_id: "stopped")
    post = lambda path, body: _call(base, "POST", path, body)  # noqa: E731
    assert post("/goal-ledger/delegations", {"principal": "claude"}) == (
        404, {"error": "principal or delegate not found"})
    assert post("/goal-ledger/delegations",
                {"principal": "claude", "delegate": "rachel", "reason": "refuse"}) == (
        409, {"error": "already delegated"})
    assert post("/goal-ledger/delegations",
                {"principal": "claude", "delegate": "rachel", "reason": "books"}) == (
        200, {"delegation_id": "d1", "listener": "started"})
    assert seen == [("enable", "claude", "rachel", "books")]
    assert post("/goal-ledger/delegations/stop", {"delegation_id": "d9"}) == (
        409, {"error": "no such delegation"})
    assert post("/goal-ledger/delegations/stop", {"delegation_id": "d1"}) == (
        200, {"delegation_id": "d1", "state": "stopped", "listener": "stopped"})


def test_schedule_toggle(running_server):
    base, _ctx, _srv = running_server
    post = lambda body: _call(base, "POST", "/agent-schedules/toggle", body)  # noqa: E731
    assert _call(base, "POST", "/agent-schedules/toggle", raw=b"[]") == (
        400, {"error": "bad json"})
    assert post({"schedule_id": "s1", "enabled": "no"}) == (
        400, {"error": "schedule_id and enabled boolean required"})
    assert post({"schedule_id": "nope", "enabled": False}) == (
        404, {"error": "schedule not found"})
    status, created = _call(base, "POST", "/agent-schedules", {
        "session": "claude", "name": "N", "cron_expression": "0 0 1 1 *", "prompt": "p"})
    assert status == 201
    status, toggled = post({"schedule_id": created["schedule"]["schedule_id"], "enabled": False})
    assert status == 200 and toggled["ok"] is True and toggled["schedule"]["enabled"] in (False, 0)


def test_manual_dream_run(running_server, monkeypatch):
    from lib import dreaming
    base, _ctx, _srv = running_server

    def start_manual_run(session, *, seed_strategy, context_dose):
        if session == "busy":
            raise ValueError("a dream is already running")
        return {"run_id": "r1", "session": session, "seed_strategy": seed_strategy,
                "context_dose": context_dose, "seed_material": None, "extra": "dropped"}
    monkeypatch.setattr(dreaming, "start_manual_run", start_manual_run)
    post = lambda body: _call(base, "POST", "/dreaming/run", body)  # noqa: E731
    assert _call(base, "POST", "/dreaming/run", raw=b"{") == (400, {"error": "bad json"})
    assert post({}) == (400, {"error": "session required"})
    assert post({"session": "busy"}) == (409, {"error": "a dream is already running"})
    assert post({"session": "claude", "seed_strategy": " walk ", "context_dose": "low"}) == (200, {
        "ok": True, "run_id": "r1", "session": "claude", "seed_strategy": "walk",
        "context_dose": "low", "seed_material": None})


def test_team_add_member(running_server):
    from lib import agents
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/teams/t1/members", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/teams/t1/members", {}) == (400, {"error": "agent_id required"})
    agent_id = agents.get_by_session("claude")["agent_id"]
    assert _call(base, "POST", "/teams/nope/members", {"agent_id": agent_id}) == (
        404, {"error": "team or agent not found"})
