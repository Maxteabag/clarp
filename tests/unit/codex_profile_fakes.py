"""Shared fakes for the Codex provider-profile tests: a synthetic profile and
keyring sentinel. Imported by tests/conftest.py's ``fake_codex_profile``
fixture and by tests that need the raw table (never ``from conftest import``:
in a full run that name resolves to another directory's conftest)."""
from __future__ import annotations

# A synthetic profile (lib.codex_profiles); no real resource, deployment or
# keyring entry appears.
FAKE_CODEX_PROFILE_ID = "example-eu"
FAKE_CODEX_PROFILE_KEY = "sk-SENTINEL-azure-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
FAKE_CODEX_PROFILE = {
    "label": "Codex (EU, example)",
    "detail": "Synthetic EU test provider",
    "base_url": "https://example.invalid/openai/v1",
    "models": [{"id": "eu-model", "label": "EU Model",
                "efforts": ["low", "medium", "high", "xhigh"]}],
    "default_model": "eu-model",
    "wire_api": "responses",
    "key_header": "api-key",
    "keyring_service": "clarp-test-keyring",
    "keyring_key": "example-eu",
}
