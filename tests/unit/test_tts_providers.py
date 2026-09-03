from __future__ import annotations

import wave

from lib import config, tts_providers


def test_provider_catalog_reports_remote_client_and_disabled(monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="cartesia", tts_fallback="none",
        cartesia_api_key="key"))
    value = tts_providers.status()
    rows = {row["id"]: row for row in value["providers"]}
    assert set(rows) == {"cartesia", "elevenlabs", "deepgram", "none"}
    assert rows["cartesia"]["selected"] is True
    assert rows["cartesia"]["available"] is True


def test_local_wav_is_encoded_as_playable_mp3(tmp_path):
    source = tmp_path / "speech.wav"
    target = tmp_path / "speech.mp3"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(bytes(16_000 * 2 // 4))
    tts_providers._wav_to_mp3(source, target)
    assert target.stat().st_size > 100
    import av
    with av.open(str(target)) as container:
        assert container.streams.audio[0].codec_context.name.startswith("mp3")
