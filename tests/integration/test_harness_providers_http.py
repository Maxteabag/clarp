"""The /harness-providers routes over the real HTTP server, OpenCode faked."""
from __future__ import annotations

import json
import urllib.error

import pytest

from lib import harness_providers as hp
from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _get, _post, fake_ctx, running_server, server_module)

FAKE_KEY = "sk-fake-http-provider-0000000000"


@pytest.fixture
def opencode(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "cache" / "opencode").mkdir(parents=True)
    (tmp_path / "cache" / "opencode" / "models.json").write_text(json.dumps({
        "deepseek": {"name": "DeepSeek", "env": ["DEEPSEEK_API_KEY"],
                     "models": {"deepseek-v4-pro": {"name": "DeepSeek V4 Pro"}}}}))
    monkeypatch.setattr(hp, "_executable", lambda: "/fake/opencode")
    monkeypatch.setattr(hp, "refresh_methods_in_background", lambda **_kw: None)
    monkeypatch.setattr(hp, "_credentials_changed", lambda: None)
    hp.reset_for_tests()
    yield tmp_path / "data" / "opencode" / "auth.json"
    hp.reset_for_tests()


def _error(call):
    with pytest.raises(urllib.error.HTTPError) as caught:
        call()
    return caught.value.code, json.loads(caught.value.read())


def test_sign_in_list_and_sign_out_over_http(running_server, opencode):
    base = running_server[0]

    status, body = _post(base + "/harness-providers/login",
                         {"harness": "opencode", "provider": "deepseek", "key": FAKE_KEY})
    assert status == 200
    assert FAKE_KEY not in body.decode()
    assert json.loads(body)["provider"]["signed_in"] is True
    assert json.loads(opencode.read_text())["deepseek"] == {"type": "api", "key": FAKE_KEY}

    status, body = _get(base + "/harness-providers?harness=deepseek")
    listing = json.loads(body)
    assert status == 200 and FAKE_KEY not in body.decode()
    assert listing["harness"] == "deepseek" and listing["store_harness"] == "opencode"
    assert listing["providers"][0]["id"] == "deepseek"
    assert listing["providers"][0]["credential"] == "api"

    status, body = _get(base + "/harness-providers/models?harness=opencode&provider=deepseek")
    assert json.loads(body)["models"][0]["id"] == "deepseek/deepseek-v4-pro"

    status, body = _post(base + "/harness-providers/logout",
                         {"harness": "opencode", "provider": "deepseek"})
    assert json.loads(body)["provider"]["signed_in"] is False
    assert json.loads(opencode.read_text()) == {}


def test_bad_requests_get_400_and_impossible_ones_409(running_server, opencode):
    base = running_server[0]
    assert _error(lambda: _post(base + "/harness-providers/login",
                                {"harness": "claude", "provider": "deepseek", "key": FAKE_KEY})
                  ) == (400, {"error": "claude has no model providers"})
    assert _error(lambda: _post(base + "/harness-providers/login",
                                {"provider": "deepseek", "method": "first"}))[0] == 400
    assert _error(lambda: _post(base + "/harness-providers/logout",
                                {"provider": "deepseek"}))[0] == 409
    assert _error(lambda: _post(base + "/harness-providers/login-code",
                                {"provider": "deepseek", "code": "abc"}))[0] == 409
    assert not opencode.exists()


def test_limited_devices_cannot_reach_provider_sign_in():
    handler = server_module.Handler
    limited = type("H", (handler,), {"__init__": lambda self: None,
                                     "_request_device_scope": "limited"})()
    for path, method in (("/harness-providers", "GET"), ("/harness-providers/login", "POST"),
                         ("/harness-providers/logout", "POST")):
        assert handler._device_forbidden(limited, path, method) is True
