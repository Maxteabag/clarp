"""Characterization of POST /focus, GET and POST /managed-skills and
POST /location as server.py serves them today, before the S3 move of the
settings and ops handlers. Library boundaries are stubbed only to reach the
handler's own branches (errors, container mode, herald on or off).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, _running_context_server, fake_ctx,
    running_server)


def _call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


OK = (200, {"ok": True})


class _Herald:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def set_focus(self, session):
        self.calls.append(session)
        if self.fail:
            raise RuntimeError("herald down")


@pytest.fixture
def focus_db(monkeypatch):
    from lib import agents
    calls = []
    state = SimpleNamespace(fail=False)

    def set_focus(agent_id):
        calls.append(agent_id)
        if state.fail:
            raise RuntimeError("db locked")
    monkeypatch.setattr(agents, "set_focus", set_focus)
    state.calls = calls
    return state


def test_focus_without_herald(running_server, focus_db):
    from lib import agents
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/focus", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/focus", {}) == (400, {"error": "session required"})
    assert _call(base, "POST", "/focus", {"session": "nobody"}) == OK
    assert focus_db.calls == []
    assert _call(base, "POST", "/focus", {"session": "claude"}) == OK
    assert focus_db.calls == [agents.get_by_session("claude")["agent_id"]]
    focus_db.fail = True
    assert _call(base, "POST", "/focus", {"session": "claude"}) == OK


@pytest.mark.parametrize("herald_fails", [False, True])
def test_focus_with_herald(fake_ctx, focus_db, herald_fails):
    herald = _Herald(fail=herald_fails)
    for base, _ctx, _srv in _running_context_server(fake_ctx.with_(herald=herald)):
        assert _call(base, "POST", "/focus", {"session": "nobody"}) == OK
        assert _call(base, "POST", "/focus", {"session": "claude"}) == OK
    assert herald.calls == ["nobody", "claude"]
    assert len(focus_db.calls) == 1


def test_managed_skills_status_variants(running_server, monkeypatch):
    from lib import managed_skills, personal_skills
    base, _ctx, _srv = running_server
    monkeypatch.setattr(managed_skills, "status", lambda: [{"id": "managed"}])
    monkeypatch.setattr(personal_skills, "status", lambda: [{"id": "personal"}])
    assert _call(base, "GET", "/managed-skills") == (200, {"skills": [{"id": "managed"}]})
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    assert _call(base, "GET", "/managed-skills") == (
        200, {"skills": [{"id": "managed"}, {"id": "personal"}]})

    def broken():
        raise OSError("skills dir unreadable")
    monkeypatch.setattr(managed_skills, "status", broken)
    assert _call(base, "GET", "/managed-skills") == (503, {"error": "skills dir unreadable"})


@pytest.mark.parametrize("outcome, expected", [
    ("ok", (200, {"ok": True, "skill": {"id": "s1", "enabled": True}})),
    ("refused", (409, {"error": "not a managed skill"})),
    ("io", (500, {"error": "read-only"})),
])
def test_managed_skills_update(running_server, monkeypatch, outcome, expected):
    from lib import managed_skills
    base, _ctx, _srv = running_server

    def set_enabled(skill_id, enabled):
        if outcome == "refused":
            raise ValueError("not a managed skill")
        if outcome == "io":
            raise OSError("read-only")
        return {"id": skill_id, "enabled": enabled}
    monkeypatch.setattr(managed_skills, "set_enabled", set_enabled)
    assert _call(base, "POST", "/managed-skills", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/managed-skills", {"skill_id": "s1", "enabled": "yes"}) == (
        400, {"error": "skill_id and boolean enabled are required"})
    assert _call(base, "POST", "/managed-skills", {"skill_id": " s1 ", "enabled": True}) == expected


def test_set_location_errors(running_server, monkeypatch):
    from lib import location
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/location", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/location", {"session": "claude"}) == (
        400, {"error": "lat/lng required"})
    assert _call(base, "POST", "/location", {"session": "claude", "lat": "n", "lng": 1}) == (
        400, {"error": "lat/lng required"})

    def broken(*_args):
        raise RuntimeError("disk full")
    monkeypatch.setattr(location, "set_location", broken)
    assert _call(base, "POST", "/location", {"session": "claude", "lat": 1, "lng": 2}) == (
        500, {"error": "store failed"})
