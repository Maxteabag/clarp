def test_a_restored_record_that_quotes_team_context_still_strips_cleanly():
    from lib import message_context, message_text
    from lib.message_context import (RESTORED_CONTEXT_OPEN, RESTORED_CONTEXT_CLOSE,
                                     TEAM_CONTEXT_OPEN, TEAM_CONTEXT_CLOSE,
                                     strip_injected_context)
    assert strip_injected_context is message_text.strip_injected_context
    assert message_context._strip_voice_markup is message_text._strip_voice_markup
    prompt = (f"{TEAM_CONTEXT_OPEN}\nteam\n{TEAM_CONTEXT_CLOSE}\n"
              f"{RESTORED_CONTEXT_OPEN}\nAssistant: the block starts with "
              f"{TEAM_CONTEXT_OPEN}\n{RESTORED_CONTEXT_CLOSE}\n\nAny news?")
    assert strip_injected_context(prompt) == "Any news?"
