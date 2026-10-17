from __future__ import annotations

import wave

from lib import config, tts_providers


def test_provider_catalog_reports_remote_client_and_disabled(monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="cartesia", tts_fallback="none",
        cartesia_api_key="key"))
    value = tts_providers.status()
    rows = {row["id"]: row for row in value["providers"]}
    assert set(rows) == {"cartesia", "elevenlabs", "gemini", "deepgram", "google", "none"}
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


def test_wav_to_mp3_keeps_speech_at_128_kbit(tmp_path):
    """32 kbit/s (the encoder's default) made Chirp voices sound thin on calls (Peter, 17 Oct)."""
    import math
    import struct
    source = tmp_path / "speech.wav"
    target = tmp_path / "speech.mp3"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 7))) for i in range(24_000 * 2)))
    tts_providers._wav_to_mp3(source, target)
    import av
    with av.open(str(target)) as container:
        assert container.streams.audio[0].bit_rate >= 128_000
