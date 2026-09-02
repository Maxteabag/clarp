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


def test_every_roster_voice_is_unique():
    """If two personas share a voice, the per-session priority queue breaks
    in fun ways. Pin the invariant."""
    voices = list(AGENT_ROSTER.values())
    assert len(voices) == len(set(voices))
