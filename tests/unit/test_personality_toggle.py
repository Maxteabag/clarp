from lib import settings_store
from lib.codex_runner import persona_identity_instruction
from lib.personalities import KEY_ENABLED


def test_persona_identity_instruction_respects_personality_toggle():
    settings_store.set_bool(KEY_ENABLED, True)

    enabled = persona_identity_instruction("Domi", "domi-test")

    assert "You are the assistant persona named Domi." in enabled
    assert "Personality: bold and assertive" in enabled

    settings_store.set_bool(KEY_ENABLED, False)

    disabled = persona_identity_instruction("Domi", "domi-test")

    assert "You are the assistant persona named Domi." in disabled
    assert "Personality: bold and assertive" not in disabled
    assert "clarp-background-jobs" in disabled
    assert "continues after your final response" in disabled
    assert "Never set a visible status for foreground analysis" in disabled
    assert "2-3 words and under 20 characters" in disabled
    assert "job-upsert" not in disabled
    assert "agent_bg.py" not in disabled
