"""Per-agent GPT-Live voices and the "put me through to X" pre-check."""
from __future__ import annotations

import pytest

from lib import oracle_voices


def test_every_mapped_agent_has_a_distinct_non_oracle_voice_of_its_gender():
    for persona, voice in oracle_voices.AGENT_VOICES.items():
        assert voice != oracle_voices.ORACLE_VOICE
        gender = oracle_voices.GENDERS[persona]
        pool = oracle_voices.MASCULINE if gender == "masculine" else oracle_voices.FEMININE
        assert voice in pool, persona


def test_voice_for_is_deterministic_and_never_marin():
    names = ["Theo", "Nadia", "Zorblax", "Margrok", "Pip", "unknown-7", "", "Marin"]
    for name in names:
        first = oracle_voices.voice_for(name)
        assert first == oracle_voices.voice_for(name)
        assert first != "marin"
        assert first in oracle_voices.MASCULINE + oracle_voices.FEMININE


def test_the_default_roster_mapping():
    assert oracle_voices.voice_for("Theo") == "meridian"
    assert oracle_voices.voice_for("theo") == "meridian"
    assert oracle_voices.voice_for("Omar") == "vesper"
    assert oracle_voices.voice_for("Marcus") == "cedar"
    assert oracle_voices.voice_for("Lena") == "gleam"
    assert oracle_voices.voice_for("Nadia") == "willow"


def test_unmapped_agents_draw_from_their_gender_pool():
    # Margrok and Roxy have Cartesia voices of known gender but no mapping.
    assert oracle_voices.voice_for("Margrok") in oracle_voices.MASCULINE
    assert oracle_voices.voice_for("Roxy") in oracle_voices.FEMININE


def test_unknown_gender_uses_the_cartesia_catalog_when_cached(monkeypatch):
    monkeypatch.setattr(oracle_voices, "_catalog_gender", lambda persona: "feminine")
    assert oracle_voices.voice_for("Zorblax") in oracle_voices.FEMININE
    monkeypatch.setattr(oracle_voices, "_catalog_gender", lambda persona: "masculine")
    assert oracle_voices.voice_for("Zorblax") in oracle_voices.MASCULINE


def test_config_override_wins_and_marin_or_unknown_voices_are_refused():
    assert oracle_voices.voice_for("Theo", {"theo": "ash"}) == "ash"
    assert oracle_voices.voice_for("Theo", {"Theo": "ash"}) == "ash"
    assert oracle_voices.voice_for("Theo", {"Theo": "marin"}) == "meridian"
    assert oracle_voices.voice_for("Theo", {"Theo": "not-a-voice"}) == "meridian"
    assert oracle_voices.voice_for("Newbie", {"newbie": "quartz"}) == "quartz"


def test_config_reads_the_agent_voices_table(tmp_path):
    from lib import config
    path = tmp_path / "config.toml"
    path.write_text('[oracle.agent_voices]\nTheo = "ash"\nNadia = "coral"\n')
    try:
        assert config.load(path).oracle_agent_voices == {"theo": "ash", "nadia": "coral"}
        assert config.load(tmp_path / "missing.toml").oracle_agent_voices == {}
    finally:
        config.reset_cache()


@pytest.mark.parametrize("words,expected", [
    ("Put me through to Theo", ("agent", "theo")),
    ("Okay, put me through to Theo please", ("agent", "theo")),
    ("Can you put me through to Theo?", ("agent", "theo")),
    ("Patch me through to Nadia", ("agent", "nadia")),
    ("Let me talk to Theo directly", ("agent", "theo")),
    ("I want to talk to Theo directly", ("agent", "theo")),
    ("I'd like to speak with Omar directly", ("agent", "omar")),
    ("Can I talk directly to Theo", ("agent", "theo")),
    ("Talk to Theo directly", ("agent", "theo")),
    ("Connect me directly to Theo", ("agent", "theo")),
    ("Switch me to Theo", ("agent", "theo")),
    ("Back to Oracle", ("oracle", None)),
    ("Okay, back to Oracle please", ("oracle", None)),
    ("Switch back", ("oracle", None)),
    ("Switch back to Oracle", ("oracle", None)),
    ("Put me back through to Oracle", ("oracle", None)),
    ("Let me talk to Oracle again", ("oracle", None)),
    ("Take me back to Oracle", ("oracle", None)),
    ("Put me through to Oracle", ("oracle", None)),
    # Work requests that merely mention talking to someone are not switches.
    ("Ask Theo to talk to Lena", None),
    ("Ask Theo to talk to Lena directly", None),
    ("Tell Theo to put me through to Lena", None),
    ("Have Omar talk to Theo directly about the deploy", None),
    ("Can Theo talk to Nadia directly", None),
    ("Talk to Theo about the deploy", None),
    ("Put me through to Theo and ask him about the deploy", None),
    ("I talked to Theo directly yesterday", None),
    ("switch back the config to the old port", None),
    ("go back to the oracle docs", None),
    ("Theo, switch the branch back", None),
])
def test_switch_request_classifier(words, expected):
    assert oracle_voices.switch_request(words) == expected


def test_contact_instructions_speak_as_the_agent_not_oracle():
    text = oracle_voices.contact_instructions("Theo")
    assert "You are the voice of Theo" in text
    assert "never claim to be Oracle" in text
    assert "word for word" in text
