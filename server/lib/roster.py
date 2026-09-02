"""Agent roster and voice catalogue — pure data, no I/O.

These are deliberately constants rather than read from a config file so the
unit tests don't have to wire up filesystem fixtures for the simplest cases.
A future iteration can move them behind a `Config` class.
"""

# Predefined persona name → ElevenLabs voice id.
# The user creates agents by name from this roster; names outside it are
# rejected by the create-agent voice intent.
AGENT_ROSTER: dict[str, str] = {
    "Mike":   "nPczCjzI2devNBz1zQrb",
    "Rachel": "21m00Tcm4TlvDq8ikWAM",
    "Domi":   "AZnzlk1XvdvUeBnXmlld",
    "Bella":  "EXAVITQu4vr4xnSDxMaL",
    "Antoni": "ErXwobaYiN019PkySvjV",
    "Elli":   "MF3mGyEYCl7XYWbV9V6O",
    "Josh":   "TxGEqnHWrfWFTfGW9XjX",
    "Arnold": "VR6AewLTigWG4xSOukaG",
    "Adam":   "pNInz6obpgDQGcFmaJgB",
    "Sam":    "yoZ06aMxZJJ28mfd3POQ",
}


def lookup_persona(name: str) -> tuple[str | None, str | None]:
    """Case-insensitively look up a persona name in the roster.

    Returns (canonical_name, voice_id) or (None, None) if not present.
    """
    if not name:
        return None, None
    target = name.strip().lower()
    for canonical, voice in AGENT_ROSTER.items():
        if canonical.lower() == target:
            return canonical, voice
    return None, None
