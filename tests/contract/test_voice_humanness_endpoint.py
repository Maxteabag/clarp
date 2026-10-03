"""GET/POST /voice-humanness on the real server (docs/compatibility.md, 46)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def _call(base, path, body=None):
    request = urllib.request.Request(
        base + path, method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_voice_humanness_round_trip(core_server):
    base = core_server["base"]
    features = _call(base, "/server-info")[1]["capabilities"]["features"]
    assert "voice_humanness" in features
    assert _call(base, "/voice-humanness") == (
        200, {"default": 5, "agents": {}, "min": 0, "max": 10})
    assert _call(base, "/voice-humanness", {"default": 10, "agents": {"Rachel": 3}}) == (
        200, {"default": 10, "agents": {"rachel": 3}, "min": 0, "max": 10})
    assert _call(base, "/voice-humanness", {"agents": {"rachel": 11}})[0] == 400
    assert _call(base, "/voice-humanness", {"agents": {"rachel": None}})[1]["agents"] == {}
    assert _call(base, "/voice-humanness")[1]["default"] == 10
