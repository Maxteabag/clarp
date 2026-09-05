"""How a spoken turn chooses its agent when no name is obvious.

The AI router used to be the only answer to an unaddressed utterance, which
is a large hammer for "she said one name" and an expensive one for "she said
nothing". These modes make the choice explicit, and the AI one optional.
"""
from __future__ import annotations

import pytest

from lib import addressing


AGENTS = {
    "rachel-1": {"name": "Rachel"},
    "josh-2": {"name": "Josh"},
    "mike-3": {"name": "Mike"},
}


def test_the_default_is_the_ai_router_so_nothing_changes_on_upgrade():
    assert addressing.mode() in addressing.MODES
    assert addressing.DEFAULT_MODE == addressing.AI


def test_off_never_delegates_however_many_names_are_spoken():
    got = addressing.resolve(
        "Rachel and Josh should look at this", AGENTS,
        mode=addressing.OFF, recent=[])
    assert got.session is None
    assert got.needs_ai is False


def test_first_name_takes_the_earliest_speaker_mentioned():
    got = addressing.resolve(
        "Josh can you ask Rachel about the build", AGENTS,
        mode=addressing.FIRST_NAME, recent=[])
    assert got.session == "josh-2"


def test_most_names_takes_whoever_is_mentioned_most():
    got = addressing.resolve(
        "Rachel said the thing, so Rachel should answer, not Josh", AGENTS,
        mode=addressing.MOST_NAMES, recent=[])
    assert got.session == "rachel-1"


def test_most_names_breaks_a_tie_toward_the_more_recent_conversation():
    """A slight nudge, and only when counting cannot separate them."""
    got = addressing.resolve(
        "Rachel and Josh", AGENTS,
        mode=addressing.MOST_NAMES, recent=["josh-2", "rachel-1"])
    assert got.session == "josh-2"


def test_recency_never_overrides_a_clear_count():
    got = addressing.resolve(
        "Rachel, Rachel, and Josh", AGENTS,
        mode=addressing.MOST_NAMES, recent=["josh-2"])
    assert got.session == "rachel-1"


def test_no_name_under_a_name_mode_asks_rather_than_guessing():
    for mode in (addressing.FIRST_NAME, addressing.MOST_NAMES):
        got = addressing.resolve("can you check the build", AGENTS,
                                 mode=mode, recent=[])
        assert got.session is None
        assert got.needs_ai is False, "a name mode must not silently call the AI"


def test_ai_mode_defers_only_when_no_name_was_spoken():
    named = addressing.resolve("Josh look at this", AGENTS,
                               mode=addressing.AI, recent=[])
    assert named.session == "josh-2"
    assert named.needs_ai is False

    unnamed = addressing.resolve("look at this", AGENTS,
                                 mode=addressing.AI, recent=[])
    assert unnamed.session is None
    assert unnamed.needs_ai is True


def test_an_unknown_mode_falls_back_to_the_default_rather_than_failing():
    got = addressing.resolve("Josh hello", AGENTS, mode="nonsense", recent=[])
    assert got.session == "josh-2"


@pytest.mark.parametrize("mode", ["off", "first_name", "most_names", "ai"])
def test_every_documented_mode_is_accepted(mode):
    assert mode in addressing.MODES
