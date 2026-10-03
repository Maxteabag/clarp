"""Per-agent humanness (0-10) of spoken replies: the Host setting, how it
resolves for an agent, and the delivery guidance each level produces."""
from __future__ import annotations

import pytest

from lib import agents, config, voice_humanness as vh


def test_default_is_the_earlier_conversational_delivery():
    assert vh.get() == {"default": 5, "agents": {}, "min": 0, "max": 10}
    assert vh.level_for({"name": "Jax", "session": "jax-1"}) == 5


def test_update_round_trips_and_resolves_by_name_persona_or_session(tmp_path):
    agent_id = agents.create_agent(persona="Ingrid", voice_id="V",
                                   cwd=str(tmp_path), session="ingrid-1")
    assert vh.update({"default": 10, "agents": {"Ingrid": 0, "theo": 3}}) == {
        "default": 10, "agents": {"ingrid": 0, "theo": 3}, "min": 0, "max": 10}
    assert vh.level_for(session="ingrid-1") == 0
    assert vh.level_for({"persona": "Theo"}) == 3
    assert vh.level_for({"name": "Someone"}) == 10

    assert vh.update({"agents": {"ingrid": None}})["agents"] == {"theo": 3}
    assert vh.level_for(agents.get_by_agent_id(agent_id)) == 10


@pytest.mark.parametrize("data", [
    {"default": 11}, {"default": -1}, {"default": "5"}, {"default": True},
    {"agents": {"jax": 12}}, {"agents": ["jax"]}, {"agents": {"": 4}}, [],
])
def test_update_rejects_out_of_range_and_leaves_state_alone(data):
    vh.update({"default": 7})
    with pytest.raises(ValueError):
        vh.update(data)
    assert vh.get()["default"] == 7 and vh.get()["agents"] == {}


def test_level_zero_is_neutral():
    text = vh.guidance(0, gemini=True)
    assert "Humanness: 0/10." in text
    assert "<vox>" not in text and "[laughing]" not in text


def test_level_five_keeps_occasional_fillers_without_tags_on_cartesia():
    text = vh.guidance(5, gemini=False)
    assert "occasional fillers" in text and "<vox>um</vox>" in text
    assert "[" not in text


def test_level_ten_is_very_human_and_content_first():
    text = vh.guidance(10, gemini=False)
    assert "Humanness: 10/10." in text
    assert "frequent natural fillers" in text and "self-correction" in text
    assert "never instead of the substance" in text
    assert "[laughing]" not in text   # Cartesia would read the tag aloud


def test_emotion_tags_are_offered_only_for_gemini_voices_inside_vox():
    text = vh.guidance(10, gemini=True)
    for tag in vh.EMOTION_TAGS:
        assert f"<vox>[{tag}]</vox>" in text
    assert "One tag at a time" in text
    assert "Never write pause tags" in text and "Say this" in text


def test_guidance_for_follows_the_agents_tts_route(monkeypatch):
    vh.update({"agents": {"ingrid": 10}})
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="cartesia",
        tts_agent_overrides={"ingrid": {"provider": "gemini"}}))
    assert "<vox>[sigh]</vox>" in vh.guidance_for({"name": "Ingrid"})
    assert "[sigh]" not in vh.guidance_for({"name": "Theo"})
