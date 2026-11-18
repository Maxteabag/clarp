"""The sentences the router speaks itself, each under the key its cached clip is stored by.

One place for both readers: lib.orchestrator speaks these texts, and
scripts/generate_orchestrator_phrases.py records and synthesizes them. A cached clip for one of
these sentences plays only when its stored text is this sentence (issue 360, R76): a row written
for an older or another text is never played in its place.
"""
from __future__ import annotations

PHRASES = {
    # The five keys the phrase cache already knows; texts unchanged.
    "clarify_target": "Was that for me?",
    "ambiguous_target": "I am not sure who that was for.",
    "repeat_missing": "I do not have anything recent to replay.",
    "control_unknown": "I am not sure what control action you meant.",
    "agent_control_unknown": "I am not sure how to do that yet.",
    # What the router answers when it refuses or cannot do what was said (issue 360).
    "no_such_agent": "I do not have an agent by that name.",
    "held_message_gone": "That message is no longer waiting. Please say it again.",
    "cannot_check": "I cannot check on that agent from here.",
    "cannot_command": "I cannot do that for that agent from here.",
    "which_agent": "Which agent did you mean?",
    "unsure_command": "I am not sure what you wanted me to do.",
    "no_voice_commands": "That agent does not accept voice commands.",
    "fork_in_app": "Please fork agents from the app.",
    "name_taken": "I already have an agent by that name.",
    "command_refused": "I could not do that.",
}

# A fixed sentence's key, by its text.
KEY_OF = {text: key for key, text in PHRASES.items()}
