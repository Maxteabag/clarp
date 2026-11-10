"""Characterization of the ops handlers' branches the full suite did not reach,
as server.py serves them today: G0 for the S3 ops recipe entry
(route-registry.md section 8). Library calls are stubbed at their module only
to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


@pytest.mark.parametrize("path", [
    "/devices", "/location/request", "/calendar/request", "/crash", "/remote-action"])
def test_bad_json(running_server, path):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", path, raw=b"[]") == (400, {"error": "bad json"})


def test_register_device_unexpected_failure(running_server, monkeypatch):
    from lib import apns

    def broken(*_args, **_kwargs):
        raise RuntimeError("keychain locked")
    monkeypatch.setattr(apns, "register_token", broken)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/devices", {"token": "ab12"}) == (
        500, {"error": "register failed"})


def test_crash_store_failure(running_server, monkeypatch):
    from lib import ios_diagnostics

    def broken(_data):
        raise ValueError("unreadable payload")
    monkeypatch.setattr(ios_diagnostics, "diagnostic_counts", broken)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/crash", {"crashDiagnostics": []}) == (
        500, {"error": "store failed"})


def test_clog_survives_an_unwritable_log_and_a_failing_emit(running_server, monkeypatch, tmp_path):
    from lib import diagnostics_settings, eventlog
    monkeypatch.setattr(diagnostics_settings, "accepts_client_uploads", lambda: True)
    monkeypatch.setenv("CLAUDE_PWA_CLIENT_LOG", str(tmp_path))   # a directory: open("a") raises
    emitted = []

    def emit(source, name, **kwargs):
        emitted.append((source, name))
        if source == "client":
            raise RuntimeError("eventlog full")
    monkeypatch.setattr(eventlog, "emit", emit)
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/clog", {"events": [
        {"event": "a", "detail": "x"}, {"event": "b", "detail": {"k": 1}}]}) == (
        200, {"ok": True})
    assert [e for e in emitted if e[0] == "client"] == [("client", "a"), ("client", "b")]
