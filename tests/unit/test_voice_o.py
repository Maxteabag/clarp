"""Ruling O (held for the owner's yes): announcements, heralds and other lines outside a turn
take the turn's route and the configured fallback instead of always trying ElevenLabs."""
from __future__ import annotations

import pathlib

import pytest

from lib import agents as agents_db, config, tts_engine, tts_registry

ADA = "c0000000-0000-4000-8000-0000000000ad"
ELEVEN = "nPczCjzI2devNBz1zQrb"


# ---------------------------------------------------------------- O: one-off lines

def _one_off(monkeypatch, cfg, calls, fail_cartesia):
    from lib import cartesia_tts
    def cart(**kw):
        calls.append(("cartesia", kw.get("voice_id")))
        if fail_cartesia:
            raise cartesia_tts.CartesiaError("down")
        pathlib.Path(kw["out_path"]).write_bytes(b"a")
        return 1
    monkeypatch.setattr(tts_registry, "cartesia_synthesize", cart)
    monkeypatch.setattr(tts_engine, "synthesize_to_file",
                        lambda text, voice_id, out_path, **kw: calls.append(("elevenlabs", voice_id)))
    monkeypatch.setattr(agents_db, "get_by_session", lambda s: {"persona": "Ada", "session": s})


def test_an_announcement_obeys_fallback_none(monkeypatch, tmp_path):
    calls = []
    cfg = config.Config(tts_provider="cartesia", tts_fallback="none", cartesia_api_key="ck",
                        eleven_api_key="ek", cartesia_voices={"Ada": ADA})
    _one_off(monkeypatch, cfg, calls, fail_cartesia=True)
    with pytest.raises(Exception):
        tts_engine.ElevenLabsEngine(tmp_path, api_key="ek")._synthesize_audio(
            "Hei", ELEVEN, tmp_path / "o.mp3", "ada", cfg, tts_engine.LEGACY_ONE_OFF_LADDER)
    assert calls == [("cartesia", ADA)]                         # no second provider any more


def test_an_announcement_uses_the_configured_fallback(monkeypatch, tmp_path):
    calls = []
    cfg = config.Config(tts_provider="cartesia", tts_fallback="elevenlabs", cartesia_api_key="ck",
                        eleven_api_key="ek", cartesia_voices={"Ada": ADA})
    _one_off(monkeypatch, cfg, calls, fail_cartesia=True)
    tts_engine.ElevenLabsEngine(tmp_path, api_key="ek")._synthesize_audio(
        "Hei", ELEVEN, tmp_path / "o.mp3", "ada", cfg, tts_engine.LEGACY_ONE_OFF_LADDER)
    assert calls == [("cartesia", ADA), ("elevenlabs", ELEVEN)]


