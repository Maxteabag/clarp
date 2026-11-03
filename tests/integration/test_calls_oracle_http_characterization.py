"""Characterization of Oracle contact, voice provider, handoff and group-call
change routes over HTTP, as server.py serves them today.

Written before the S3 move of the calls and Oracle handlers (REFACTOR.md).
Pins statuses, messages and payloads of the HTTP layer; group_calls,
oracle_contact and oracle_voice_provider have their own unit tests.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, _running_context_server, fake_ctx)

TOKEN = "calls-characterization-admin"


def _call(base, method, path, body=None, raw=None, token=TOKEN):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


@pytest.fixture
def admin_server(fake_ctx):
    yield from _running_context_server(fake_ctx.with_(auth_token=TOKEN))


def test_oracle_contact_round_trip(admin_server):
    base, _ctx, _srv = admin_server
    empty = {"session": "", "persona": "", "stale": ""}
    assert _call(base, "GET", "/oracle/contact") == (200, empty)
    assert _call(base, "POST", "/oracle/contact", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/oracle/contact", {"session": "nobody"}) == (409, {
        "error": "'nobody' is not a live agent; choose a contact from the roster"})
    mike = {"session": "claude", "persona": "Mike", "stale": ""}
    assert _call(base, "POST", "/oracle/contact", {"session": "claude"}) == (200, mike)
    assert _call(base, "GET", "/oracle/contact") == (200, mike)
    assert _call(base, "POST", "/oracle/contact", {"session": ""}) == (200, empty)


def test_oracle_voice_provider_without_a_gemini_key(admin_server):
    base, _ctx, _srv = admin_server
    gemini = {"model": "str", "voice": "str"}
    status, before = _call(base, "GET", "/oracle/voice-provider")
    assert status == 200
    assert {k: before[k] for k in ("provider", "selected", "default", "available",
                                   "fallback_reason")} == {
        "provider": "openai", "selected": "openai", "default": "openai",
        "available": ["openai"], "fallback_reason": None}
    assert {k: type(v).__name__ for k, v in before["gemini"].items()} == {
        k: v for k, v in gemini.items()}
    assert _call(base, "POST", "/oracle/voice-provider", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/oracle/voice-provider", {"provider": "x"}) == (
        400, {"error": "voice provider must be one of: openai, gemini"})
    status, after = _call(base, "POST", "/oracle/voice-provider", {"provider": "gemini"})
    assert status == 200
    assert (after["selected"], after["provider"], after["fallback_reason"]) == (
        "gemini", "openai", "Gemini Live needs a Gemini API key on this Host")


def test_oracle_handoff_snapshot_and_ack_validation(admin_server):
    base, _ctx, _srv = admin_server
    status, snapshot = _call(base, "GET", "/oracle/handoff")
    assert status == 200
    assert set(snapshot) == {"principal", "host_id", "server_now", "handoff", "active"}
    assert (snapshot["principal"], snapshot["handoff"], snapshot["active"]) == (
        "administrator", None, None)
    assert _call(base, "POST", "/oracle/handoffs/ack", raw=b"{") == (400, {"error": "bad json"})
    assert _call(base, "POST", "/oracle/handoffs/ack", {}) == (400, {
        "ok": False, "error": "handoff_id, generation and a known phase are required"})


@pytest.mark.parametrize("path", [
    "/calls/hold", "/calls/resume", "/calls/switch", "/calls/transfer", "/calls/remove"])
def test_call_changes_without_a_live_call(admin_server, path):
    base, _ctx, _srv = admin_server
    assert _call(base, "POST", path, raw=b"[]") == (400, {"error": "bad json"})
    assert _call(base, "POST", path, {}) == (400, {"ok": False, "error": "agent required"})
    assert _call(base, "POST", path, {"agent": "claude"}) == (
        404, {"ok": False, "error": "no live call"})
    assert _call(base, "POST", path, {}, token=None) == (401, {"error": "unauthorized"})


def test_voip_token_registration(admin_server):
    base, _ctx, _srv = admin_server
    assert _call(base, "POST", "/calls/ring/voip-token", {}) == (
        200, {"ok": True, "registered": False})
    assert _call(base, "POST", "/calls/ring/voip-token",
                 {"token": "ab" * 32, "environment": "production"}) == (
        200, {"ok": True, "registered": True})


def test_open_host_answers_the_handlers_own_401(fake_ctx):
    """A host without an admin token admits local requests at the gate; the
    calls and handoff handlers then refuse the unvalidated caller themselves."""
    for base, _ctx, _srv in _running_context_server(fake_ctx):
        assert _call(base, "POST", "/calls/hold", {"agent": "claude"}, token=None) == (
            401, {"error": "Group calls require full-device authentication"})
        assert _call(base, "GET", "/oracle/handoff", token=None) == (
            401, {"error": "Oracle requires authenticated full-device access"})
