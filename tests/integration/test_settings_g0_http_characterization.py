"""Characterization of the settings handlers' branches the full suite did not
reach, as server.py serves them today: G0 for the second S3 recipe entry
(route-registry.md section 8). Library calls are stubbed at their module only
to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

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


def test_automation_settings_post_saves_the_flag(running_server, monkeypatch):
    from lib import automation_settings
    base, _ctx, _srv = running_server
    monkeypatch.setattr(automation_settings, "update",
                        lambda value: {"special_treatment": value, "stub": True})
    assert _call(base, "POST", "/automation-settings", {"special_treatment": True}) == (
        200, {"special_treatment": True, "stub": True})


def test_favorite_paths_bad_limit_falls_back_to_five(running_server, monkeypatch):
    from lib import agents
    base, _ctx, _srv = running_server
    seen = []
    monkeypatch.setattr(agents, "favorite_paths", lambda limit: seen.append(limit) or ["/p"])
    assert _call(base, "GET", "/favorite-paths?limit=x") == (200, {"paths": ["/p"]})
    assert seen == [5]


def test_backend_auth_logout_conflict(running_server, monkeypatch):
    from lib import backend_auth

    def busy(backend):
        raise RuntimeError(f"{backend} is signing in")
    monkeypatch.setattr(backend_auth, "logout", busy)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/backend-auth/logout", {"backend": " Codex "}) == (
        409, {"error": "codex is signing in"})


def test_harness_provider_login_and_code(running_server, monkeypatch):
    from lib import harness_providers
    base, _ctx, _srv = running_server
    seen = []
    monkeypatch.setattr(harness_providers, "start_oauth",
                        lambda *args: seen.append(("oauth", args)) or {"pending": True})
    monkeypatch.setattr(harness_providers, "submit_code",
                        lambda *args: seen.append(("code", args)) or {"ok": True})
    for path in ("/harness-providers/login", "/harness-providers/login-code"):
        assert _call(base, "POST", path, raw=b"[]") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/harness-providers/login", {
        "harness": "opencode", "provider": "p", "method": 0, "inputs": {"a": 1}}) == (
        202, {"pending": True})
    assert _call(base, "POST", "/harness-providers/login-code", {
        "harness": "opencode", "provider": "p", "code": "123"}) == (200, {"ok": True})
    assert seen == [("oauth", ("opencode", "p", 0, {"a": 1})),
                    ("code", ("opencode", "p", "123"))]


def test_harness_provider_store_write_failure(running_server, monkeypatch):
    from lib import harness_providers

    def unwritable(*_args):
        raise OSError("read-only file system")
    monkeypatch.setattr(harness_providers, "save_api_key", unwritable)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/harness-providers/login", {
        "harness": "opencode", "provider": "p", "key": "k"}) == (
        500, {"error": "OpenCode's credential store could not be written"})


def test_paired_device_revoke_refusals(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/paired-devices/revoke", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/paired-devices/revoke", {"device_id": "nobody"}) == (
        404, {"error": "device not found"})


@pytest.mark.parametrize("path, message", [
    ("/desktop-presence", "authenticated desktop required"),
    ("/application-activity", "authentication required"),
])
def test_presence_and_activity_refuse_an_unvalidated_request(running_server, path, message):
    """The test server's requests pass the gate without a validated token, so
    the handler's own check answers."""
    base, _ctx, _srv = running_server
    assert _call(base, "POST", path, {}) == (401, {"error": message})


# A limited or validated full device in process: the handlers' scope and body checks.

@pytest.mark.parametrize("name", ["_handle_desktop_presence", "_handle_application_activity"])
@pytest.mark.parametrize("scope, expected", [
    ("limited", (403, "full device access required")),
    ("full", (400, "object required")),
])
def test_presence_and_activity_scope_and_body_checks(name, scope, expected):
    handler = object.__new__(server_module.Handler)
    handler._request_auth_validated = True
    handler._request_device_scope = scope
    handler._read_json = lambda: None
    errors = []
    handler._json_error = lambda status, message: errors.append((status, message))
    getattr(handler, name)()
    assert errors == [expected]
