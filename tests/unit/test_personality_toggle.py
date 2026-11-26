from lib import settings_store
from lib.codex_runner import persona_identity_instruction
from lib.personalities import KEY_ENABLED


def test_persona_identity_instruction_respects_personality_toggle():
    settings_store.set_bool(KEY_ENABLED, True)

    enabled = persona_identity_instruction("Domi", "domi-test")

    assert "Your name is Domi." in enabled
    assert "domi-test" in enabled
    assert "Personality: bold and assertive" in enabled

    settings_store.set_bool(KEY_ENABLED, False)

    disabled = persona_identity_instruction("Domi", "domi-test")

    assert "Your name is Domi." in disabled
    assert "domi-test" in disabled
    assert "Personality: bold and assertive" not in disabled
    assert "job-upsert" not in disabled
    assert "agent_bg.py" not in disabled
