from lib import settings_store
from lib.voice_preamble import persona_identity_instruction
from lib.personalities import KEY_ENABLED


def test_persona_identity_instruction_respects_personality_toggle():
    settings_store.set_bool(KEY_ENABLED, True)

    enabled = persona_identity_instruction("Domi", "domi-test")

    assert "You are Domi." in enabled
    assert "Personality: bold and assertive" in enabled

    settings_store.set_bool(KEY_ENABLED, False)

    disabled = persona_identity_instruction("Domi", "domi-test")

    assert "You are Domi." in disabled
    assert "Personality: bold and assertive" not in disabled
    assert "clarp-background-jobs" not in disabled
    assert "session id" not in disabled
    assert "Never set a visible status for foreground analysis" not in disabled
    assert "2-3 words and under 20 characters" not in disabled
    assert "job-upsert" not in disabled
    assert "agent_bg.py" not in disabled
