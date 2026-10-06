def test_a_restored_record_that_quotes_team_context_still_strips_cleanly():
    from lib.message_context import (RESTORED_CONTEXT_OPEN, RESTORED_CONTEXT_CLOSE,
                                     TEAM_CONTEXT_OPEN, TEAM_CONTEXT_CLOSE,
                                     strip_injected_context)
    prompt = (f"{TEAM_CONTEXT_OPEN}\nteam\n{TEAM_CONTEXT_CLOSE}\n"
              f"{RESTORED_CONTEXT_OPEN}\nAssistant: the block starts with "
              f"{TEAM_CONTEXT_OPEN}\n{RESTORED_CONTEXT_CLOSE}\n\nAny news?")
    assert strip_injected_context(prompt) == "Any news?"
