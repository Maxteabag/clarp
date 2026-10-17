"""The built-in `google` provider wired into the Host: registry, catalog, route and contract.

Offline only: google_tts.synthesize is replaced by a recorder that keeps the core's own voice
check, so no token or request is ever attempted.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from lib import (config, google_tts, server_identity, tts_mode, tts_providers, tts_worker,
                 voice_catalog)

AOEDE, KORE, NOBODY = ("nb-NO-Chirp3-HD-Aoede", "nb-NO-Chirp3-HD-Kore",
                       "nb-NO-Chirp3-HD-Nobody")


@pytest.fixture(autouse=True)
def no_ambient_adc(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def configure(monkeypatch, tmp_path=None, **fields):
    if tmp_path is not None:
        adc = tmp_path / "adc.json"
        adc.write_text("{}")
        fields = {"google_tts_section": True, "google_tts_credentials": str(adc),
                  "google_tts_project": "tts-billing-project", **fields}
    monkeypatch.setattr(config, "_CACHED", config.Config(**fields))
    return config.load()


class Spoken:
    """Records every synthesis at the provider boundaries; google keeps its voice check."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict]] = []

        def google(*, text, voice, out_path, creds, on_chunk=None, **_kwargs):
            if voice not in google_tts.VOICES:
                raise google_tts.GoogleTTSError("voice_unavailable")
            self.calls.append(("google", voice))
            self.texts.append(text)
            pathlib.Path(out_path).write_bytes(b"ID3google")
            return 9

        def cartesia(*, voice_id, out_path, **_kwargs):
            self.calls.append(("cartesia", voice_id))
            pathlib.Path(out_path).write_bytes(b"ID3cartesia")
            return 11

        self.texts: list[str] = []
        monkeypatch.setattr(google_tts, "synthesize", google)
        monkeypatch.setattr(tts_worker, "cartesia_synthesize", cartesia)
        monkeypatch.setattr(tts_worker, "_emit",
                            lambda _source, event, **kw: self.events.append((event, kw.get("detail") or {})))

    def spoke(self):
        return [detail for event, detail in self.events if event == "spoke"]


def clip(cfg, tmp_path, *, voice_id, agent=None, text="Hei der."):
    agent = agent or {"agent_id": "a-sander", "persona": "Sander", "session": "sander-1"}
    route = tts_mode.route(cfg, agent)
    return tts_worker._synthesize(
        cfg=cfg, row={"text": text, "voice_id": voice_id, "session": agent["session"]},
        agent=agent, out_path=tmp_path / "clip.mp3", on_chunk=None, trace_id="",
        delivery_fields={"delivery": "chunked-file"}, clip_route=route)


# ---- registry, status, contract -------------------------------------------------------------

def test_google_is_a_builtin_provider_whose_status_names_its_source_but_no_secret(
        monkeypatch, tmp_path):
    configure(monkeypatch, tts_provider="cartesia")
    row = {r["id"]: r for r in tts_providers.status()["providers"]}["google"]
    assert (row["name"], row["custom"], row["streaming"], row["supports_preview"]) == (
        "Google Chirp 3 HD", False, False, True)
    assert row["capabilities"] == {"batch_ssml_breaks": True, "streaming": False}
    assert (row["available"], row["credential_source"], row["error"]) == (
        False, "google_stt", "google_credentials_missing")

    configure(monkeypatch, tmp_path, tts_provider="cartesia")
    row = {r["id"]: r for r in tts_providers.status()["providers"]}["google"]
    assert (row["available"], row["credential_source"], row["error"]) == (True, "google_tts", None)
    rendered = json.dumps(row)
    assert str(tmp_path) not in rendered and "tts-billing-project" not in rendered


def test_the_host_contract_advertises_google_at_its_reserved_number():
    assert server_identity.HOST_CONTRACT >= 69
    assert "tts_provider_google" in server_identity.FEATURES
    assert server_identity.FEATURE_CONTRACTS["tts_provider_google"] == 69


# ---- catalog ---------------------------------------------------------------------------------

def test_the_catalog_lists_every_google_voice_with_its_language_even_when_unavailable(
        monkeypatch):
    configure(monkeypatch, tts_provider="cartesia")
    agents = {"sander-1": {"name": "Sander", "voice_id": json.dumps(
        {"google": AOEDE, "custom.google-chirp": KORE})}}
    group = {p["id"]: p for p in voice_catalog.catalog(agents, "sander-1")["providers"]}["google"]
    assert group["name"] == "Google Chirp 3 HD"
    assert (group["available"], group["error"]) == (False, "google_credentials_missing")
    assert group["error"] in google_tts.CATALOG_ERROR_CODES
    assert group["capabilities"] == {"batch_ssml_breaks": True, "streaming": False}
    assert [v["id"] for v in group["voices"]] == list(google_tts.VOICES)
    assert {v["language"] for v in group["voices"]} == {"nb-NO"}
    current = [v["id"] for v in group["voices"] if v["current"]]
    assert current == [AOEDE]     # the google key only; the custom adapter's key is its own
    aoede = next(v for v in group["voices"] if v["id"] == AOEDE)
    assert (aoede["name"], aoede["preview_url"]) == (
        "Aoede", f"/voice-preview?provider=google&id={AOEDE}")


# ---- route and synthesis ---------------------------------------------------------------------

@pytest.mark.parametrize("stored, has_voice", [
    ("", True),                                        # never chose: the documented default
    (json.dumps({"cartesia": "c-1"}), True),           # no google voice yet: the default
    (json.dumps({"google": AOEDE}), True),
    (json.dumps({"google": NOBODY}), False),           # a saved voice off the list: never Kore
], ids=["no-voice", "other-provider-only", "allowlisted", "unknown-saved"])
def test_only_an_unknown_saved_google_voice_leaves_the_route_without_a_voice(
        monkeypatch, stored, has_voice):
    cfg = configure(monkeypatch, tts_provider="google")
    agent = {"agent_id": "a-sander", "persona": "Sander", "session": "sander-1"}
    assert tts_worker._route_has_voice(
        cfg, {"voice_id": stored}, agent, tts_mode.route(cfg, agent)) is has_voice


@pytest.mark.parametrize("stored, overrides, expected", [
    ("", {}, KORE),
    (json.dumps({"google": AOEDE, "custom.google-chirp": "nb-NO-Chirp3-HD-Charon"}), {}, AOEDE),
    (json.dumps({"google": AOEDE}), {"sander": {"provider": "google",
                                                "google_voice": "nb-NO-Chirp3-HD-Puck"}},
     "nb-NO-Chirp3-HD-Puck"),
], ids=["default-for-a-new-choice", "stored-google-key", "agent-override"])
def test_a_google_clip_speaks_the_agents_google_voice(monkeypatch, tmp_path, stored, overrides,
                                                       expected):
    spoken = Spoken(monkeypatch)
    cfg = configure(monkeypatch, tts_provider="google", tts_agent_overrides=overrides)
    assert clip(cfg, tmp_path, voice_id=stored) == 9
    assert spoken.calls == [("google", expected)]
    assert spoken.spoke() == [{"provider": "google", "voice": expected}]


def test_a_google_clip_keeps_pauses_but_not_emotion_tags(monkeypatch, tmp_path):
    spoken = Spoken(monkeypatch)
    cfg = configure(monkeypatch, tts_provider="google")
    clip(cfg, tmp_path, voice_id="", text='Hei [laughing] <break time="1s"/> der.')
    assert spoken.texts == ['Hei <break time="1s"/> der.']


def test_an_unknown_saved_voice_takes_only_the_configured_fallback_and_says_who_spoke(
        monkeypatch, tmp_path):
    spoken = Spoken(monkeypatch)
    cfg = configure(monkeypatch, tts_provider="google", tts_fallback="cartesia",
                    cartesia_api_key="cartesia-key")
    clip(cfg, tmp_path, voice_id=json.dumps({"google": NOBODY, "cartesia": "c-sander"}))
    assert spoken.calls == [("cartesia", "c-sander")]
    assert spoken.spoke() == [{"provider": "cartesia", "voice": "c-sander", "fallback_for": "google"}]


def test_without_a_fallback_an_unknown_saved_voice_fails_and_nothing_speaks(monkeypatch, tmp_path):
    spoken = Spoken(monkeypatch)
    cfg = configure(monkeypatch, tts_provider="google")
    with pytest.raises(google_tts.GoogleTTSError) as error:
        clip(cfg, tmp_path, voice_id=json.dumps({"google": NOBODY}))
    assert error.value.code == "voice_unavailable"
    assert spoken.calls == [] and spoken.spoke() == []


@pytest.mark.parametrize("code, message", [
    ("google_quota", "Voice paused: Google Chirp 3 HD quota exceeded."),
    ("google_auth_failed: HTTP 401", "Voice unavailable: Google Chirp 3 HD sign-in failed."),
    ("google_credentials_missing", "Voice unavailable: Google Chirp 3 HD is not configured."),
    ("voice_unavailable", "Voice unavailable: this Google Chirp 3 HD voice is not available."),
    ("text_too_long", "Voice synthesis failed."),
], ids=["quota", "auth-401", "not-configured", "unknown-voice", "other"])
def test_a_google_failure_is_announced_as_google_not_elevenlabs(monkeypatch, code, message):
    published = []
    monkeypatch.setattr(tts_worker.events, "broadcast", lambda _stream, event: published.append(event))
    tts_worker._publish_tts_error(object(), {"session": "sander-1", "agent_id": "a-sander"},
                                  {"persona": "Sander"}, code)
    assert [event["message"] for event in published] == [message]
