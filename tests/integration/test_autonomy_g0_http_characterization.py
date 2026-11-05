"""Characterization of the autonomy handlers' branches the full suite did not
reach (task plans, teams, heartbeat, dreaming), as server.py serves them today:
G0 for the S3 autonomy recipe entries (route-registry.md section 8). Library
calls are stubbed at their module only to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.patching import patch_shared
from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def _bare_handler():
    handler = object.__new__(server_module.Handler)
    errors, oks = [], []
    handler._json_error = lambda status, message: errors.append((status, message))
    handler._json_ok = lambda body: oks.append(body)
    return handler, errors, oks


def test_task_plans_for_a_session(running_server, monkeypatch):
    from lib import task_plans
    monkeypatch.setattr(task_plans, "list_for_session", lambda session: [{"session": session}])
    monkeypatch.setattr(task_plans, "active_for_session", lambda session: None)
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/task-plans?session=claude") == (
        200, {"plans": [{"session": "claude"}]})
    assert _call(base, "GET", "/task-plan?session=claude") == (200, {"plan": None})


def test_team_nudging_refusals(running_server, monkeypatch):
    from lib import team_store
    monkeypatch.setattr(team_store, "set_nudge_enabled", lambda team_id, enabled: None)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/team-nudging", raw=b"[]") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/team-nudging", {"team_id": "t1"}) == (
        400, {"error": "nudge_enabled required"})
    assert _call(base, "POST", "/team-nudging", {"team_id": "t1", "nudge_enabled": True}) == (
        404, {"error": "team not found"})


def test_team_delete_and_messages(running_server, monkeypatch):
    from lib import team_store
    seen = []
    monkeypatch.setattr(team_store, "delete_team", lambda team_id: True)
    monkeypatch.setattr(team_store, "list_team_messages",
                        lambda team_id, limit: seen.append(limit) or [])
    base, _ctx, _srv = running_server
    assert _call(base, "DELETE", "/teams/t1") == (200, {"ok": True})
    assert _call(base, "GET", "/teams/t1/messages?limit=x") == (
        200, {"team_id": "t1", "messages": []})
    assert seen == [100]


def test_team_routes_refuse_an_empty_team_id(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "DELETE", "/teams/") == (404, {"error": "team not found"})
    assert _call(base, "GET", "/teams//messages") == (404, {"error": "team not found"})


def test_agent_heartbeat_refusals(running_server):
    base, _ctx, _srv = running_server
    post = lambda body=None, raw=None: _call(base, "POST", "/agent-heartbeat", body, raw)  # noqa: E731
    assert post(raw=b"[]") == (400, {"error": "bad json"})
    assert post({"session": "claude"}) == (400, {"error": "heartbeat_enabled required"})
    assert post({"session": "claude", "heartbeat_enabled": "yes"}) == (
        400, {"error": "heartbeat_enabled must be boolean"})
    assert post({"session": "nobody", "heartbeat_enabled": True}) == (
        404, {"error": "no such agent"})


def test_agent_dreaming_refusals(running_server):
    base, _ctx, _srv = running_server
    post = lambda body=None, raw=None: _call(base, "POST", "/agent-dreaming", body, raw)  # noqa: E731
    assert post(raw=b"[]") == (400, {"error": "bad json"})
    assert post({"session": "claude"}) == (400, {"error": "dreaming_enabled required"})
    assert post({"session": "nobody", "dreaming_enabled": True}) == (
        404, {"error": "no such agent"})


def test_dreaming_runs_bad_limit_and_settings_bad_json(running_server, monkeypatch):
    from lib import dreaming
    seen = []
    patch_shared(monkeypatch, dreaming, "list_dream_runs",
                        lambda session, limit: seen.append(limit) or [])
    base, _ctx, _srv = running_server
    status, body = _call(base, "GET", "/dreaming/runs?limit=x")
    assert status == 200 and body["runs"] == [] and seen == [10]
    assert _call(base, "PUT", "/dreaming/settings", raw=b"[]") == (400, {"error": "bad json"})


def test_dreaming_backend_options_skip_unnamed_providers_and_survive_a_failed_catalog(monkeypatch):
    from lib import backends, provider_capabilities
    handler, _errors, _oks = _bare_handler()
    backend_id = backends.ids()[0]
    monkeypatch.setattr(provider_capabilities, "capability_catalog", lambda: {"providers": [
        {"id": ""}, {"id": backend_id, "models": [{"id": "m1", "label": "M1"}, {"label": "no id"}]}]})
    options = handler._dreaming_backend_options()
    assert options[0] == {"id": "", "label": "Same as the agent", "efforts": [], "models": []}
    assert next(o for o in options if o["id"] == backend_id)["models"] == [{"id": "m1", "label": "M1"}]

    def down():
        raise RuntimeError("probe failed")
    monkeypatch.setattr(provider_capabilities, "capability_catalog", down)
    options = handler._dreaming_backend_options()
    assert [o["id"] for o in options] == [""] + list(backends.ids())
    assert options[0] == {"id": "", "label": "Same as the agent", "efforts": [], "models": []}
    assert all(o["models"] == [] for o in options)
