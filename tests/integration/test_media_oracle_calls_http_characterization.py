"""Characterization of Cartesia voice, Oracle call (v1 and v2) and portrait or
avatar content routes over HTTP, on a host with no Cartesia key, no live
Oracle call and no stored images, as server.py serves them today.

Written before the S3 moves (REFACTOR.md). Pins statuses, content types and
bodies of the HTTP layer.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, _running_context_server, fake_ctx)

TOKEN = "media-oracle-characterization-admin"


def _call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, ctype, raw = response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as error:
        status, ctype, raw = error.code, error.headers.get("Content-Type"), error.read()
    return status, json.loads(raw) if ctype == "application/json" else (ctype, raw)


@pytest.fixture
def admin_server(fake_ctx):
    yield from _running_context_server(fake_ctx.with_(auth_token=TOKEN))


NOT_FOUND = ("text/plain", b"not found")
GONE = (410, {"error": "Oracle call is no longer available"})


@pytest.mark.parametrize("method, path, body, expected", [
    ("GET", "/cartesia-voices", None, (200, {"available": False, "voices": []})),
    ("GET", "/cartesia-voice-preview", None, (404, NOT_FOUND)),
    ("GET", "/cartesia-voice-preview?voice_id=v1", None, (404, NOT_FOUND)),
    ("GET", "/oracle/calls/status", None, (200, {"delegations": []})),
    ("GET", "/oracle/calls/status?call_id=c1", None, (200, {"delegations": []})),
    ("POST", "/oracle/calls", {}, (400, {"error": "A bounded audio SDP offer is required"})),
    ("POST", "/oracle/calls/close", {}, (200, {"closed": True})),
    ("POST", "/oracle/calls/close", {"call_id": "c1"}, (200, {"closed": True})),
    ("POST", "/oracle/v2/calls/close", {}, GONE),
    ("POST", "/oracle/v2/calls/control", {}, GONE),
    ("POST", "/oracle/v2/calls/control", {"call_id": "c1", "action": "mute"}, GONE),
    ("GET", "/oracle/v2/calls/status", None, GONE),
    ("GET", "/oracle/v2/context/image", None, (404, ("text/plain", b"Image not found"))),
    ("GET", "/agent-portraits/p1/content", None, (404, NOT_FOUND)),
    ("GET", "/persona-avatars/p1", None, (404, NOT_FOUND)),
    ("GET", "/persona-avatars/Mike", None, (404, NOT_FOUND)),
])
def test_route_answers_without_media_or_live_calls(admin_server, method, path, body, expected):
    base, _ctx, _srv = admin_server
    assert _call(base, method, path, body) == expected
