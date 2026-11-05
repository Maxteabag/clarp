"""Characterization of the Cartesia voice preview, the agent file browser's
roots and the agent MCP selection as server.py serves them today, before
the S3 moves of the voice-out and agents handlers. Cartesia synthesis, the
voice catalog and the MCP catalog are stubbed at their library boundary;
no network or provider is reached.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, ctype, payload = response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as error:
        status, ctype, payload = error.code, error.headers.get("Content-Type"), error.read()
    if ctype == "application/json":
        return status, json.loads(payload)
    return status, payload


@pytest.fixture
def cartesia(monkeypatch):
    from dataclasses import replace
    from lib import cartesia_tts, cartesia_voices, config, tts_registry
    cfg = replace(config.load(), cartesia_api_key="fixture-cartesia-key", tts_fallback="none")
    monkeypatch.setattr(config, "load", lambda *a, **kw: cfg)
    calls = []
    state = {"fail": False}
    monkeypatch.setattr(cartesia_voices, "cached_english_voice",
                        lambda voice_id: {"id": voice_id, "name": "Ada"} if voice_id == "v1" else None)
    monkeypatch.setattr(cartesia_voices, "has_cached_catalog", lambda: True)

    def synthesize(*, text, voice_id, out_path, api_key, model, **_kw):
        calls.append((text, voice_id))
        if state["fail"]:
            raise cartesia_tts.CartesiaError("quota")
        out_path.write_bytes(b"ID3fake-mp3")
    monkeypatch.setattr(cartesia_tts, "synthesize", synthesize)
    monkeypatch.setattr(tts_registry, "cartesia_synthesize", synthesize)
    return calls, state


def test_cartesia_preview_synthesizes_once_then_serves_the_cache(running_server, cartesia):
    base, _ctx, _srv = running_server
    calls, _state = cartesia
    assert _call(base, "GET", "/cartesia-voice-preview?id=unknown") == (404, b"not found")
    assert _call(base, "GET", "/cartesia-voice-preview?id=v1") == (200, b"ID3fake-mp3")
    assert _call(base, "GET", "/cartesia-voice-preview?id=v1") == (200, b"ID3fake-mp3")
    assert calls == [("Hello, I'm Ada. This is how I'll sound in Clarp.", "v1")]


def test_cartesia_preview_failure_and_unsafe_cache(running_server, cartesia, tmp_path):
    base, _ctx, _srv = running_server
    calls, state = cartesia
    state["fail"] = True
    assert _call(base, "GET", "/cartesia-voice-preview?id=v1") == (502, b"preview unavailable")
    cache = server_module._runtime_paths().cache_dir / "voice-previews"
    assert not any(cache.glob(".*.tmp"))
    for child in cache.iterdir():
        child.unlink()
    cache.rmdir()
    (tmp_path / "elsewhere").mkdir()
    cache.symlink_to(tmp_path / "elsewhere")
    assert _call(base, "GET", "/cartesia-voice-preview?id=v1") == (500, b"preview cache unavailable")
    assert len(calls) == 1


def _files(base, **query):
    return _call(base, "GET", "/agent-files?" + urllib.parse.urlencode(query))


def test_agent_files_roots(running_server, tmp_path, monkeypatch):
    base, _ctx, _srv = running_server
    (tmp_path / "sub").mkdir()
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert _files(base) == (400, {"error": "session required"})
    assert _files(base, session="nobody") == (404, {"error": "agent not found"})
    assert _files(base, session="claude", root="@elsewhere") == (
        400, {"error": "unknown directory root"})
    assert _files(base, session="claude", root="@workspace/../..") == (
        403, {"error": "directory escapes root"})
    assert _files(base, session="claude", root="relative/dir") == (
        400, {"error": "root path must be absolute"})
    assert _files(base, session="claude", root=str(tmp_path / "missing")) == (
        404, {"error": "agent workspace unavailable"})
    status, listing = _files(base, session="claude", root="@workspace/sub")
    assert status == 200 and set(listing) >= {"entries"}
    status, listing = _files(base, session="claude", root="@home")
    assert status == 200 and "docs" in [row["name"] for row in listing["entries"]]
    assert _files(base, session="claude", path="../outside") == (
        403, {"error": "invalid workspace-relative path"})


def test_agent_mcp_keeps_known_servers_in_request_order(running_server, monkeypatch):
    from lib import config, mcp_selection
    base, _ctx, _srv = running_server
    monkeypatch.setattr(config, "read_global_mcp_servers",
                        lambda: {"github": {}, "linear": {}, "slack": {}})
    post = lambda body: _call(base, "POST", "/agent-mcp", body)  # noqa: E731
    assert _call(base, "POST", "/agent-mcp", raw=b"{") == (400, {"error": "bad json"})
    assert post({}) == (400, {"error": "session required"})
    assert post({"session": "nobody", "mcp_servers": []}) == (404, {"error": "no such agent"})
    assert post({"session": "claude", "mcp_servers": "github"}) == (
        400, {"error": "mcp_servers must be a list"})
    assert post({"session": "claude", "mcp_servers": ["slack", "nope", " github ", "slack"]}) == (
        200, {"ok": True, "session": "claude", "mcp_servers": ["slack", "github"],
              "available_mcp_servers": ["github", "linear", "slack"]})
    from lib import agents
    stored = agents.get_by_session("claude")["mcp_servers"]
    assert mcp_selection.decode(stored) == (True, ["slack", "github"])
