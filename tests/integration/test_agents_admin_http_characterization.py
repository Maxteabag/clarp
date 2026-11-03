"""Characterization of persona, voice-verbosity, model-removal, backend-auth
and fleet-map supersede routes over HTTP, as server.py serves them today.

Written before the S3 moves (REFACTOR.md). Pins statuses, messages and
payloads of the HTTP layer. Backend login never reaches a provider CLI: the
unknown-backend refusal is pinned as is, the other outcomes through stubs of
lib.backend_auth.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_persona_update_and_delete(running_server):
    base, _ctx, _srv = running_server
    refused = (409, {"error": "Only saved custom Contacts can be edited."})
    assert _call(base, "POST", "/personas/update", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/personas/update", {}) == refused
    assert _call(base, "POST", "/personas/update", {"original_name": "Nobody", "name": "X"}) == refused
    status, created = _call(base, "POST", "/personas", {"name": "Zed", "voice_id": "V_Z"})
    assert status == 201
    persona_id = created["persona"]["id"]
    assert _call(base, "POST", "/personas/update",
                 {"original_name": "Zed", "name": "Zed2", "voice_id": "V_Z"}) == (200, {
        "persona": {"id": persona_id, "name": "Zed2", "voice_id": "V_Z", "avatar_symbol": "",
                    "avatar_url": "", "personality": "", "builtin": False}})
    not_removable = (404, {"error": "personality not removable"})
    assert _call(base, "DELETE", "/personas/Nobody") == not_removable
    assert _call(base, "DELETE", "/personas/Mike") == not_removable
    assert _call(base, "DELETE", "/personas/Zed2") == (200, {"ok": True})


def test_agent_voice_verbosity(running_server):
    base, _ctx, _srv = running_server
    post = lambda body: _call(base, "POST", "/agent-voice-verbosity", body)  # noqa: E731
    assert _call(base, "POST", "/agent-voice-verbosity", raw=b"{") == (400, {"error": "bad json"})
    assert post({}) == (400, {"error": "session required"})
    assert post({"session": "claude"}) == (400, {"error": "voice_verbosity required"})
    assert post({"session": "claude", "voice_verbosity": True}) == (
        400, {"error": "voice_verbosity must be an integer"})
    assert post({"session": "claude", "voice_verbosity": 99}) == (
        400, {"error": "voice_verbosity out of range", "min": 0, "max": 3})
    assert post({"session": "nobody", "voice_verbosity": 1}) == (404, {"error": "no such agent"})
    assert post({"session": "claude", "voice_verbosity": 1}) == (200, {
        "ok": True, "session": "claude", "voice_verbosity": 1,
        "options": [{"level": 0, "label": "Quiet"}, {"level": 1, "label": "Milestones"},
                    {"level": 2, "label": "Steps"}, {"level": 3, "label": "Running commentary"}]})


def test_transcription_model_remove_accepts_unknown_ids(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/transcription-models/remove", raw=b"{") == (
        400, {"error": "bad json"})
    # Today an empty or unknown model id is "removed" with 200.
    assert _call(base, "POST", "/transcription-models/remove", {}) == (
        200, {"ok": True, "model_id": ""})
    assert _call(base, "POST", "/transcription-models/remove", {"model_id": "nope"}) == (
        200, {"ok": True, "model_id": "nope"})


@pytest.mark.parametrize("path", [
    "/backend-auth/login", "/backend-auth/login-code", "/backend-auth/logout"])
def test_backend_auth_refuses_an_unknown_backend(running_server, path):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", path, raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", path, {"backend": " NOPE "}) == (
        400, {"error": "nope CLI is not installed"})


def test_backend_auth_outcomes(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    from lib import backend_auth
    seen = []

    def start_login(backend):
        seen.append(("login", backend))
        return {"backend": backend, "state": "waiting"}

    def submit_login_code(backend, code):
        seen.append(("code", backend, code))
        raise RuntimeError("no login in progress")

    def logout(backend):
        seen.append(("logout", backend))
        return {"backend": backend, "authenticated": False}
    monkeypatch.setattr(backend_auth, "start_login", start_login)
    monkeypatch.setattr(backend_auth, "submit_login_code", submit_login_code)
    monkeypatch.setattr(backend_auth, "logout", logout)

    assert _call(base, "POST", "/backend-auth/login", {"backend": " Codex "}) == (
        202, {"backend": "codex", "state": "waiting"})
    assert _call(base, "POST", "/backend-auth/login-code", {"backend": "codex", "code": 7}) == (
        409, {"error": "no login in progress"})
    assert _call(base, "POST", "/backend-auth/logout", {"backend": "codex"}) == (
        200, {"backend": "codex", "authenticated": False})
    assert seen == [("login", "codex"), ("code", "codex", "7"), ("logout", "codex")]


def test_viz_supersede_validation(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/viz/supersede", {}) == (
        400, {"error": "entity_id and revision required"})
    assert _call(base, "POST", "/viz/supersede", {"entity_id": "e1", "revision": "1"}) == (
        400, {"error": "entity_id and revision required"})
    assert _call(base, "POST", "/viz/supersede", {"entity_id": "e1", "revision": 1}) == (
        409, {"error": "library changed; refresh the map"})
