from types import SimpleNamespace

from lib import controller_narration as cn


def test_normalize_text_collapses_whitespace_and_rejects_long_or_empty():
    assert cn.normalize_text("  Skip.\n Drops   the reply. ") == "Skip. Drops the reply."
    assert cn.normalize_text("") == ""
    assert cn.normalize_text("x" * 241) == ""
    assert cn.normalize_text("bad\x00byte\x07") == "badbyte"


def test_cache_key_changes_with_every_input():
    base = cn.cache_key(text="Skip.", voice="v1", model="sonic-3.5")
    assert base == cn.cache_key(text="Skip.", voice="v1", model="sonic-3.5")
    assert base != cn.cache_key(text="Skip", voice="v1", model="sonic-3.5")
    assert base != cn.cache_key(text="Skip.", voice="v2", model="sonic-3.5")
    assert base != cn.cache_key(text="Skip.", voice="v1", model="sonic-4")


def test_voice_is_the_narration_personas_configured_cartesia_voice():
    cfg = SimpleNamespace(cartesia_voice_for=lambda persona: {"Rachel": "voice-r"}.get(persona))
    assert cn.voice_id(cfg) == "voice-r"
    assert cn.voice_id(SimpleNamespace(cartesia_voice_for=lambda persona: None)) == ""
