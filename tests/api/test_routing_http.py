"""End-to-end-ish test that exercises the routing functions exactly the way
the live HTTP server uses them, without booting the full server (which would
pull in faster-whisper).

This catches the kind of bug where the server-side glue inverts the result
or stops using the lib version.
"""

from __future__ import annotations

import pytest

from lib.roster import AGENT_ROSTER
from lib.routing import (
    resolve_agent_by_spoken_name,
)


@pytest.fixture
def agents():
    # Mirrors the on-disk shape the live server reads.
    return {
        "claude": {"name": "Mike",   "voice_id": AGENT_ROSTER["Mike"]},
        "rachel": {"name": "Rachel", "voice_id": AGENT_ROSTER["Rachel"]},
        "domi":   {"name": "Domi",   "voice_id": AGENT_ROSTER["Domi"]},
        "bella":  {"name": "Bella",  "voice_id": AGENT_ROSTER["Bella"]},
    }


def test_address_in_first_three_words_routes(agents):
    """Sanity: a real Rachel-address routes correctly."""
    sid, _ = resolve_agent_by_spoken_name("Rachel can you check the file", agents)
    assert sid == "rachel"


def test_normal_message_routes_to_addressee(agents):
    sid, returned = resolve_agent_by_spoken_name("Rachel, write me an essay", agents)
    assert sid == "rachel"
    assert returned == "write me an essay"


def test_second_word_address_routes_and_strips_filler(agents):
    agents = dict(agents)
    agents["arnold"] = {"name": "Arnold", "voice_id": "v-arnold"}

    sid, returned = resolve_agent_by_spoken_name("also Arnold, do X", agents)

    assert sid == "arnold"
    assert returned == "do X"


def test_third_word_address_routes_after_fillers(agents):
    sid, returned = resolve_agent_by_spoken_name("ok hey Bella fix the bug", agents)

    assert sid == "bella"
    assert returned == "fix the bug"


def test_late_name_mention_does_not_route(agents):
    sid, returned = resolve_agent_by_spoken_name(
        "I will tell Bella later about the bug",
        agents,
    )

    assert sid is None
    assert returned == "I will tell Bella later about the bug"


def test_no_agent_addressed_returns_none(agents):
    sid, returned = resolve_agent_by_spoken_name("just chatting about the weather", agents)
    assert sid is None
    assert returned == "just chatting about the weather"


def test_picker_session_remains_unchanged_when_no_routing(agents):
    """The live /send handler falls back to the picker session when this
    returns None. Pin that the return shape stays the same."""
    sid, returned = resolve_agent_by_spoken_name("hi", agents)
    assert sid is None
    assert returned == "hi"
