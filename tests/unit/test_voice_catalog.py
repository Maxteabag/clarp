from __future__ import annotations

from lib import config, voice_catalog


def test_deepgram_voice_reuses_deepgram_choices_without_local_key(
        monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="deepgram",
        deepgram_api_key="",
    ))
    monkeypatch.setattr(
        "lib.cartesia_voices.english_voices",
        lambda: [],
    )
    result = voice_catalog.catalog({
        "mike": {
            "name": "Mike",
            "voice_id": '{"deepgram":"flux-haley-en"}',
        },
    }, "mike")
    groups = {row["id"]: row for row in result["providers"]}

    assert groups["deepgram"]["available"] is False
    assert groups["deepgram"]["selected"] is True
    haley = next(
        row for row in groups["deepgram"]["voices"]
        if row["id"] == "flux-haley-en")
    assert haley["provider"] == "deepgram"
    assert haley["current"] is True
    assert haley["preview_url"].startswith("/voice-preview?provider=deepgram")
    assert any(
        row["id"].startswith("flux-")
        for row in groups["deepgram"]["voices"])


def test_direct_deepgram_remains_a_first_class_provider(monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="deepgram",
        deepgram_api_key="direct-key",
    ))
    monkeypatch.setattr("lib.cartesia_voices.english_voices", lambda: [])
    # No live Deepgram lookup: the picker falls back to the bundled snapshot.
    monkeypatch.setattr(
        "lib.deepgram_voices.english_voices", lambda **_kwargs: [])
    groups = {
        row["id"]: row for row in voice_catalog.catalog({})["providers"]
    }

    assert groups["deepgram"]["available"] is True
    assert groups["deepgram"]["selected"] is True
    assert groups["deepgram"]["voices"]
    assert any(
        row["id"].startswith("flux-")
        for row in groups["deepgram"]["voices"])
    assert any(
        row["id"].startswith("aura-2-")
        for row in groups["deepgram"]["voices"])


def test_eleven_previews_use_quality_model_at_natural_speed():
    assert voice_catalog.ELEVEN_PREVIEW_MODEL == "eleven_multilingual_v2"
    assert voice_catalog.ELEVEN_PREVIEW_SPEED == 1.0
