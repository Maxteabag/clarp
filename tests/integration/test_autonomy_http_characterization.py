"""Characterization of the schedule and agent-goal routes over HTTP.

Written before the S3 move of the autonomy handlers (REFACTOR.md). Pins
today's statuses, messages and payloads, including the PATCH route that
skips the device scope gate (HTTP-02, pinned in the route goldens).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)

NEVER = "0 0 1 1 *"  # once a year, never inside a test


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_schedule_lifecycle(running_server):
    base, _ctx, _srv = running_server

    assert _call(base, "POST", "/agent-schedules", raw=b"[]") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/agent-schedules", {"session": "claude"}) == (
        400, {"error": "session, name, cron_expression, and prompt are required"})
    status, body = _call(base, "POST", "/agent-schedules", {
        "session": "claude", "name": "N", "cron": "not cron", "prompt": "p"})
    assert status == 400 and set(body) == {"error"}
    status, created = _call(base, "POST", "/agent-schedules", {
        "session": "claude", "name": "Nightly", "cron_expression": NEVER,
        "prompt": "check", "enabled": False})
    assert status == 201 and created["ok"] is True
    schedule = created["schedule"]
    schedule_id = schedule["schedule_id"]

    status, listed = _call(base, "GET", "/agent-schedules?session=claude")
    assert status == 200 and len(listed["schedules"]) == 1
    assert _call(base, "GET", "/agent-schedules?session=rachel") == (200, {"schedules": []})

    assert _call(base, "PATCH", f"/schedules/{schedule_id}", raw=b"[]") == (
        400, {"error": "bad json"})
    assert _call(base, "PATCH", "/schedules/nope", {"name": "x"}) == (
        404, {"error": "schedule not found"})
    status, body = _call(base, "PATCH", f"/schedules/{schedule_id}", {"cron": "bad"})
    assert status == 400 and set(body) == {"error"}
    status, patched = _call(base, "PATCH", f"/schedules/{schedule_id}",
                            {"name": " Renamed ", "prompt": " again ", "enabled": "yes"})
    assert status == 200 and patched["ok"] is True
    assert patched["schedule"]["name"] == "Renamed"
    assert patched["schedule"]["prompt"] == "again"

    assert _call(base, "DELETE", "/schedules/nope") == (404, {"error": "schedule not found"})
    assert _call(base, "DELETE", f"/schedules/{schedule_id}") == (200, {"ok": True})

    assert _call(base, "POST", "/agent-schedules/delete", raw=b"[]") == (
        400, {"error": "bad json"})
    assert _call(base, "POST", "/agent-schedules/delete", {}) == (
        400, {"error": "schedule_id required"})
    assert _call(base, "POST", "/agent-schedules/delete", {"schedule_id": schedule_id}) == (
        200, {"ok": False})


def test_agent_goal_get(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/agent-goal") == (400, {"error": "session required"})
    assert _call(base, "GET", "/agent-goal?session=nobody") == (404, {"error": "no such agent"})
    assert _call(base, "GET", "/agent-goal?session=claude") == (
        200, {"session": "claude", "goal": None})


def test_agent_goal_actions_validate_before_the_backend(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/agent-goal", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/agent-goal", {}) == (400, {"error": "session required"})
    assert _call(base, "POST", "/agent-goal", {"session": "claude"}) == (
        400, {"error": "objective required"})
    assert _call(base, "POST", "/agent-goal/pause", {"session": "nobody"}) == (
        404, {"error": "no such agent"})


@pytest.mark.parametrize("outcome, expected_status", [
    ("unsupported", 501), ("invalid", 400), ("crash", 502), ("ok", 200)])
def test_agent_goal_actions_relay_the_backend(running_server, monkeypatch,
                                              outcome, expected_status):
    base, _ctx, _srv = running_server
    from lib import backends
    calls = []

    def goal(backend, agent_id, action, *, objective, stream):
        calls.append((backend, action, objective))
        if outcome == "unsupported":
            raise backends.GoalUnsupported("no goals here")
        if outcome == "invalid":
            raise ValueError("bad objective")
        if outcome == "crash":
            raise RuntimeError("backend said no")
        return {"objective": objective, "status": "active"}
    monkeypatch.setattr(backends, "goal", goal)

    status, body = _call(base, "POST", "/agent-goal/resume", {"session": "claude"})
    assert status == expected_status
    assert calls and calls[0][1:] == ("resume", "")
    if outcome == "ok":
        assert body == {"ok": True, "session": "claude", "action": "resume",
                        "goal": {"objective": "", "status": "active"}}
    else:
        assert body == {"error": {"unsupported": "no goals here", "invalid": "bad objective",
                                  "crash": "backend said no"}[outcome]}
    for path, action in (("/agent-goal", "start"), ("/agent-goal/clear", "clear")):
        calls.clear()
        _call(base, "POST", path, {"session": "claude", "objective": "ship it"})
        assert calls[0][1:] == (action, "ship it")
