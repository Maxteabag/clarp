"""Model providers inside OpenCode: listing, API keys, device sign-in, sign-out.

OpenCode's stores live under a temp XDG root; the CLI and its server are faked.
Every key here is fake.
"""
from __future__ import annotations

import json
import stat
import threading

import pytest

from lib import harness_providers as hp
from lib import provider_capabilities

FAKE_KEY = "sk-fake-harness-provider-0000000000"

CATALOGUE = {
    "deepseek": {"id": "deepseek", "name": "DeepSeek", "env": ["DEEPSEEK_API_KEY"],
                 "doc": "https://api-docs.deepseek.com",
                 "models": {"deepseek-v4-pro": {"name": "DeepSeek V4 Pro",
                                                "variants": {"high": {}, "max": {}}}}},
    "huggingface": {"id": "huggingface", "name": "Hugging Face", "env": ["HF_TOKEN"],
                    "models": {"deepseek-ai/DeepSeek-V4.1-Flash": {"name": "DeepSeek V4.1 Flash"},
                               "zai-org/GLM-5.3": {"name": "GLM-5.3"}}},
    "groq": {"id": "groq", "name": "Groq", "env": ["GROQ_API_KEY"],
             "models": {"llama-4": {"name": "Llama 4"}}},
    "zz-obscure": {"id": "zz-obscure", "name": "Aardvark AI", "env": ["ZZ_KEY"],
                   "models": {"m": {"name": "M"}}},
}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    data, cache = tmp_path / "data", tmp_path / "cache"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    for var in ("DEEPSEEK_API_KEY", "HF_TOKEN", "GROQ_API_KEY", "ZZ_KEY"):
        monkeypatch.delenv(var, raising=False)
    (cache / "opencode").mkdir(parents=True)
    (cache / "opencode" / "models.json").write_text(json.dumps(CATALOGUE))
    monkeypatch.setattr(hp, "_executable", lambda: "/fake/opencode")
    monkeypatch.setattr(hp, "refresh_methods_in_background", lambda **_kw: None)
    changed = []
    monkeypatch.setattr(hp, "_credentials_changed", lambda: changed.append(True))
    hp.reset_for_tests()
    yield data / "opencode" / "auth.json", changed
    hp.reset_for_tests()


def _auth(path):
    return json.loads(path.read_text())


def test_api_key_is_stored_in_opencodes_store_and_never_returned(stores):
    auth, changed = stores
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"groq": {"type": "api", "key": "sk-fake-other"}}))

    row = hp.save_api_key("opencode", "huggingface", f"  {FAKE_KEY}\n")

    assert _auth(auth) == {"groq": {"type": "api", "key": "sk-fake-other"},
                           "huggingface": {"type": "api", "key": FAKE_KEY}}
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert row["signed_in"] is True and row["credential"] == "api"
    listing = hp.providers("opencode")
    assert FAKE_KEY not in json.dumps(row) + json.dumps(listing)
    assert changed == [True]


def test_listing_puts_signed_in_providers_first_then_popular(stores, monkeypatch):
    auth, _ = stores
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"groq": {"type": "api", "key": "sk-fake-other"}}))
    monkeypatch.setenv("HF_TOKEN", "hf_fake")

    rows = hp.providers("opencode")["providers"]

    assert [row["id"] for row in rows] == ["huggingface", "groq", "deepseek", "zz-obscure"]
    by_id = {row["id"]: row for row in rows}
    assert by_id["huggingface"]["credential"] == "env"
    assert by_id["huggingface"]["can_logout"] is False
    assert by_id["groq"]["can_logout"] is True
    assert by_id["deepseek"]["signed_in"] is False
    assert by_id["deepseek"]["methods"] == [
        {"index": -1, "type": "api", "label": "API key", "prompts": []}]


def test_deepseek_card_lists_providers_that_host_deepseek(stores):
    rows = hp.providers("deepseek")["providers"]
    assert {row["id"] for row in rows} == {"deepseek", "huggingface"}
    models = hp.models("deepseek", "huggingface")["models"]
    assert [m["id"] for m in models] == ["huggingface/deepseek-ai/DeepSeek-V4.1-Flash"]


def test_models_are_provider_prefixed_with_their_variants(stores):
    result = hp.models("opencode", "deepseek")
    assert result["models"] == [{
        "id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro",
        "model_provider": "deepseek", "supported_efforts": ["high", "max"]}]


@pytest.mark.parametrize("harness,provider,key", [
    ("claude", "deepseek", FAKE_KEY),
    ("opencode", "../etc", FAKE_KEY),
    ("opencode", "deepseek", ""),
    ("opencode", "deepseek", "sk-fake\nsecond-line"),
    ("opencode", "deepseek", 12),
])
def test_bad_sign_in_requests_are_refused_without_writing(stores, harness, provider, key):
    auth, _ = stores
    with pytest.raises(hp.ProviderError):
        hp.save_api_key(harness, provider, key)
    assert not auth.exists()


def test_sign_out_removes_only_that_provider(stores, monkeypatch):
    auth, _ = stores
    hp.save_api_key("opencode", "deepseek", FAKE_KEY)
    hp.save_api_key("opencode", "groq", "sk-fake-other")

    row = hp.logout("opencode", "deepseek")

    assert row["signed_in"] is False
    assert _auth(auth) == {"groq": {"type": "api", "key": "sk-fake-other"}}
    with pytest.raises(hp.ProviderConflict):
        hp.logout("opencode", "deepseek")
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    with pytest.raises(hp.ProviderConflict, match="environment"):
        hp.logout("opencode", "huggingface")


def test_only_methods_a_phone_can_finish_are_offered():
    raw = {"openai": [
        {"type": "oauth", "label": "ChatGPT Pro/Plus (browser)"},
        {"type": "oauth", "label": "ChatGPT Pro/Plus (headless)"},
        {"type": "api", "label": "Manually enter API Key"}],
        "azure": [{"type": "api", "label": "API key"},
                  {"type": "oauth", "label": "Microsoft Entra ID (Azure CLI)"}]}
    assert hp.usable_methods(raw) == {"openai": [
        {"index": 1, "type": "oauth", "label": "ChatGPT Pro/Plus (headless)", "prompts": []}]}


class _FakeClient:
    def __init__(self, mode, release=None):
        self.mode, self.release, self.calls = mode, release, []

    def request(self, method, path, body=None, *, timeout=30):
        self.calls.append((method, path, body))
        if path.endswith("/authorize"):
            return {"url": "https://github.com/login/device", "method": self.mode,
                    "instructions": "Enter code: AB12-CD34"}
        if self.release is not None:
            self.release.wait(5)
        return True


class _FakeServe:
    def __init__(self, client):
        self.client, self.closed = client, False

    def close(self):
        self.closed = True


def _wait_for(provider, status):
    for _ in range(200):
        if hp.task(provider)["status"] == status:
            return hp.task(provider)
        threading.Event().wait(0.01)
    raise AssertionError(hp.task(provider))


def test_device_sign_in_reports_the_code_and_completes_when_opencode_does(stores):
    _, changed = stores
    release = threading.Event()
    serve = _FakeServe(_FakeClient("auto", release))

    started = hp.start_oauth("opencode", "github-copilot", 0,
                             {"deploymentType": "github.com"},
                             serve_factory=lambda _exe: serve)

    assert started["status"] == "running"
    assert started["verification_url"] == "https://github.com/login/device"
    assert started["user_code"] == "AB12-CD34"
    assert started["awaiting_code"] is False
    assert hp.providers()["tasks"]["github-copilot"]["status"] == "running"
    assert serve.client.calls[0] == ("POST", "/provider/github-copilot/oauth/authorize",
                                     {"method": 0, "inputs": {"deploymentType": "github.com"}})
    release.set()
    _wait_for("github-copilot", "complete")
    assert serve.closed is True
    assert changed == [True]


def test_code_sign_in_takes_the_code_back(stores):
    serve = _FakeServe(_FakeClient("code"))
    started = hp.start_oauth("opencode", "anthropic", 0, serve_factory=lambda _exe: serve)
    assert started["awaiting_code"] is True

    done = hp.submit_code("opencode", "anthropic", "fake-code-123")

    assert done["status"] == "complete"
    assert serve.client.calls[-1] == ("POST", "/provider/anthropic/oauth/callback",
                                      {"method": 0, "code": "fake-code-123"})
    with pytest.raises(hp.ProviderConflict):
        hp.submit_code("opencode", "anthropic", "again")


def test_sign_in_that_opencode_refuses_fails_without_leaking_the_body(stores):
    class Broken:
        def request(self, *_a, **_kw):
            raise RuntimeError('POST /x: HTTP 400 {"key": "sk-fake-should-not-show"}')

    serve = _FakeServe(Broken())
    result = hp.start_oauth("opencode", "openai", 1, serve_factory=lambda _exe: serve)
    assert result["status"] == "failed"
    assert "sk-fake-should-not-show" not in result["error"]
    assert serve.closed is True


def test_catalogue_names_each_opencode_models_provider(stores, monkeypatch):
    hp.save_api_key("opencode", "deepseek", FAKE_KEY)
    row = provider_capabilities._with_model_providers("opencode", {"models": [
        {"id": "huggingface/deepseek-ai/DeepSeek-V4.1-Flash"}, {"id": "opencode/big-pickle"}]})
    assert [m["model_provider"] for m in row["models"]] == ["huggingface", "opencode"]
    assert row["harness_providers"] is True
    assert row["model_providers"] == [
        {"id": "huggingface", "name": "Hugging Face", "signed_in": False},
        {"id": "opencode", "name": "opencode", "signed_in": True},
        {"id": "deepseek", "name": "DeepSeek", "signed_in": True},
    ]
