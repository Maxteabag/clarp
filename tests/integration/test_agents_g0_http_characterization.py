"""Characterization of the agent and persona handlers' branches the full suite
did not reach, as server.py serves them today: G0 for the S3 agents recipe
entries (route-registry.md section 8). Library calls are stubbed at their
module only to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, authenticated_terminal_server, fake_ctx,
    running_server)


def _request(base, method, path, body=None, raw=None, token=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _call(base, method, path, body=None, raw=None, token=None):
    status, payload = _request(base, method, path, body, raw, token)
    return status, json.loads(payload or b"null")


@pytest.fixture
def lookup_with(monkeypatch):
    """identity.lookup answering `rows` for the given keys, the real lookup otherwise."""
    from lib import identity
    real = identity.lookup

    def install(rows):
        monkeypatch.setattr(identity, "lookup", lambda key: rows[key] if key in rows else real(key))
    return install


def test_agent_avatar_serves_the_file(running_server, lookup_with, tmp_path):
    avatar = tmp_path / "face.png"
    avatar.write_bytes(b"\x89PNG avatar")
    lookup_with({"a1": {"agent_id": "a1", "avatar_path": str(avatar)}})
    base, _ctx, _srv = running_server
    assert _request(base, "GET", "/avatars/a1") == (200, b"\x89PNG avatar")


def test_prompt_history_refusals(authenticated_terminal_server, monkeypatch):
    from lib import prompt_history
    base, ctx, _srv = authenticated_terminal_server
    get = lambda query: _call(base, "GET", "/identity/prompt-history?" + query,  # noqa: E731
                              token=ctx.auth_token)
    assert get("session_id=s1&limit=x") == (400, {"error": "invalid limit"})

    def bad_cursor(**_kwargs):
        raise ValueError("unknown before cursor")
    monkeypatch.setattr(prompt_history, "build_prompt_history", bad_cursor)
    assert get("session_id=s1&before=c9") == (400, {"error": "unknown before cursor"})
    monkeypatch.setattr(prompt_history, "build_prompt_history", lambda **_kwargs: None)
    assert get("session_id=s1") == (404, {"error": "session not found"})


def test_agent_file_workspace_refusals(running_server, lookup_with, tmp_path):
    base, _ctx, _srv = running_server
    lookup_with({"nocwd": {"agent_id": "n1", "cwd": ""},
                 "gone": {"agent_id": "g1", "cwd": str(tmp_path / "missing")}})
    get = lambda query: _call(base, "GET", "/agent-file?" + query)  # noqa: E731
    assert get("session=nobody&path=a.txt") == (404, {"error": "agent not found"})
    assert get("session=nocwd&path=a.txt") == (404, {"error": "agent workspace unavailable"})
    assert get("session=gone&path=a.txt") == (404, {"error": "agent workspace unavailable"})
    assert get("session=nocwd&path=a.txt&root=relative/dir") == (
        400, {"error": "root path must be absolute"})


@pytest.mark.parametrize("error, expected", [
    ("not_found", (404, {"error": "no such file"})),
    ("value", (403, {"error": "outside the workspace"})),
    ("os", (400, {"error": "unreadable"})),
])
def test_agent_file_read_failures(running_server, lookup_with, monkeypatch, tmp_path, error, expected):
    from lib import agent_files
    lookup_with({"w": {"agent_id": "w1", "cwd": str(tmp_path)}})

    def read_text_file(root, relative):
        raise {"not_found": agent_files.AgentFileNotFound("no such file"),
               "value": ValueError("outside the workspace"),
               "os": OSError("unreadable")}[error]
    monkeypatch.setattr(agent_files, "read_text_file", read_text_file)
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/agent-file?session=w&path=a.txt") == expected


def test_agent_archive(running_server):
    base, _ctx, _srv = running_server
    post = lambda body=None, raw=None: _call(base, "POST", "/agent-archive", body, raw)  # noqa: E731
    assert post(raw=b"[]") == (400, {"error": "bad json"})
    assert post({"session": "nobody", "archived": True}) == (404, {"error": "no such agent"})
    assert post({"session": "claude", "archived": True}) == (
        200, {"ok": True, "session": "claude", "archived": True})
    assert post({"session": "claude", "archived": False}) == (
        200, {"ok": True, "session": "claude", "archived": False})


def test_agent_assign_refusals(running_server, lookup_with, monkeypatch):
    from lib import contact_assignment
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/agent-assign", raw=b"[]") == (400, {"error": "bad json"})
    lookup_with({"jan": {"agent_id": "j1", "is_janitor": True}})
    status, body = _call(base, "POST", "/agent-assign", {"session": "jan"})
    assert status == 403 and body["code"] == "janitor_inspection_only"

    def taken(session, mode, name):
        raise ValueError("that name is taken")
    monkeypatch.setattr(contact_assignment, "assign_contact", taken)
    assert _call(base, "POST", "/agent-assign", {"session": "claude", "name": "Rachel"}) == (
        409, {"error": "that name is taken"})


def test_delete_persona_conflict(running_server, monkeypatch):
    from lib import personas

    def in_use(name):
        raise ValueError(f"{name} is in use")
    monkeypatch.setattr(personas, "delete", in_use)
    base, _ctx, _srv = running_server
    assert _call(base, "DELETE", "/personas/Rachel") == (409, {"error": "Rachel is in use"})


def test_agent_portraits_list_failure(running_server, monkeypatch):
    from lib import agent_portraits

    def unreadable(session, portrait_dir):
        raise agent_portraits.PortraitError("portrait store unreadable", status=503)
    monkeypatch.setattr(agent_portraits, "list_for_session", unreadable)
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/agent-portraits?session=claude") == (
        503, {"error": "portrait store unreadable"})
