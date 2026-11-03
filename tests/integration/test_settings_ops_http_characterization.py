"""Characterization of settings and ops routes no other test reaches over HTTP.

Written before the S3 move of the settings and ops handlers (REFACTOR.md):
pins statuses, error messages and payload shapes of server.py's HTTP layer
as it is today. The library rules behind them have their own unit tests.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

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


def test_judgments_settings_and_decisions(running_server):
    base, _ctx, _srv = running_server
    from lib import judgments

    status, body = _call(base, "GET", "/judgments/settings")
    assert status == 200
    assert body["sites"] == list(judgments.SITES)
    assert body["defaults"] == {"enabled": False, "timeout_ms": judgments.DEFAULT_TIMEOUT_MS}
    assert body["limits"] == {"timeout_ms": [100, judgments.MAX_TIMEOUT_MS]}
    assert body["settings"] == judgments.status()

    assert _call(base, "POST", "/judgments/settings", raw=b"[]") == (400, {"error": "bad json"})
    status, body = _call(base, "POST", "/judgments/settings", {"enabled": "yes"})
    assert status == 400 and set(body) == {"error"}
    status, body = _call(base, "POST", "/judgments/settings", {"enabled": True})
    assert status == 200 and body["ok"] is True and body["settings"]["enabled"] is True

    assert _call(base, "GET", "/judgments/decisions?site=nowhere") == (
        400, {"error": "unknown site"})
    assert _call(base, "GET", "/judgments/decisions?site=router&limit=x") == (
        200, {"decisions": []})


def test_personalities_settings_round_trip(running_server):
    base, _ctx, _srv = running_server
    status, before = _call(base, "GET", "/personalities/settings")
    assert status == 200 and set(before) == {"settings"}
    assert _call(base, "POST", "/personalities/settings", raw=b"{") == (400, {"error": "bad json"})
    status, after = _call(base, "POST", "/personalities/settings", {})
    assert status == 200 and after == {"ok": True, "settings": before["settings"]}


def test_orchestrator_addressing_and_decisions(running_server):
    base, _ctx, _srv = running_server
    from lib import addressing

    modes = list(addressing.MODES)
    assert _call(base, "POST", "/orchestrator/addressing", raw=b"{") == (
        400, {"error": "bad json"})
    assert _call(base, "POST", "/orchestrator/addressing", {"mode": "psychic"}) == (
        400, {"error": "unknown addressing mode", "modes": modes})
    assert _call(base, "POST", "/orchestrator/addressing", {"mode": addressing.OFF}) == (
        200, {"ok": True, "mode": addressing.OFF, "modes": modes})
    assert _call(base, "GET", "/orchestrator/decisions?limit=x") == (200, {"decisions": []})


def test_relay_status_without_a_relay(running_server):
    base, _ctx, srv = running_server
    srv.relay = None
    assert _call(base, "GET", "/network/relay") == (
        200, {"enabled": False, "state": "disabled"})


def test_dirs_lists_matching_visible_subdirectories(running_server, tmp_path):
    base, _ctx, _srv = running_server
    root = tmp_path / "browse"
    for name in ("alpha", "Almond", "beta", ".hidden"):
        (root / name).mkdir(parents=True)
    (root / "afile").write_text("x")
    # recover_user_path returns a pathlib.Path, which drops a trailing slash,
    # so "dir/" completes the name "dir" in its parent rather than listing it.
    query = urllib.parse.urlencode({"path": f"{root}/"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(tmp_path), "matches": [str(root)]})
    query = urllib.parse.urlencode({"path": f"{root}/al"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root), "matches": [str(root / "Almond"), str(root / "alpha")]})
    query = urllib.parse.urlencode({"path": f"{root}/b"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root), "matches": [str(root / "beta")]})
    query = urllib.parse.urlencode({"path": f"{root}/missing/x"})
    assert _call(base, "GET", f"/dirs?{query}") == (200, {
        "base": str(root / "missing"), "matches": []})
    assert _call(base, "GET", "/dirs?path=/")[1]["base"] == "/"


def test_message_tool_details_requires_a_known_message(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/message-tool-details?session=claude") == (
        400, {"error": "session and message_id required"})
    assert _call(base, "GET", "/message-tool-details?session=nobody&message_id=m1") == (
        404, {"error": "message not found"})
    assert _call(base, "GET", "/message-tool-details?session=claude&message_id=m1") == (
        404, {"error": "message not found"})
