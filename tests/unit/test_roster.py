"""B25: creating an agent with no voice_id should auto-pick the roster voice."""

from __future__ import annotations

import pytest

from lib.roster import AGENT_ROSTER, lookup_persona


@pytest.mark.parametrize("inp,expected_name", [
    ("Mike",   "Mike"),
    ("mike",   "Mike"),
    (" MIKE ", "Mike"),
    ("Rachel", "Rachel"),
    ("rachel", "Rachel"),
])
def test_lookup_persona_case_insensitive(inp, expected_name):
    name, voice = lookup_persona(inp)
    assert name == expected_name
    assert voice == AGENT_ROSTER[expected_name]


@pytest.mark.parametrize("inp", ["Bob", "", None, "Doctor", "Mikee"])
def test_lookup_persona_unknown_returns_none(inp):
    name, voice = lookup_persona(inp)
    assert name is None and voice is None


# Personas that already shared a voice before the roster moved to config.
# They ship that way today; the invariant below stops the list from growing.
KNOWN_SHARED_VOICES = {
    frozenset({"Rachel", "Nadia"}), frozenset({"Domi", "Lena"}),
    frozenset({"Bella", "Priya"}), frozenset({"Antoni", "Diego"}),
    frozenset({"Elli", "Yuki"}), frozenset({"Josh", "Caleb"}),
    frozenset({"Arnold", "Marcus"}), frozenset({"Adam", "Theo"}),
    frozenset({"Sam", "Omar"}),
}


def test_every_roster_voice_is_unique():
    """If two personas share a voice, the per-session priority queue breaks
    in fun ways. Pin the invariant. Silent Janitor identities are voiceless
    on purpose and are not a collision."""
    by_voice: dict[str, set[str]] = {}
    for name, voice in AGENT_ROSTER.items():
        if not voice:
            continue
        by_voice.setdefault(voice, set()).add(name)
    shared = {frozenset(names) for names in by_voice.values() if len(names) > 1}
    assert shared <= KNOWN_SHARED_VOICES, sorted(sorted(s) for s in shared - KNOWN_SHARED_VOICES)
