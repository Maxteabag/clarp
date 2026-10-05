"""Characterization tests for lib.voice_preamble (app-turn prompt preamble).

Pins the head/split sentinels, apply/strip round-trip, which instruction
blocks appear for spoken vs text turns, the persona identity line (built-in
personality, agent override, personalities switch), and the per-agent
narration clause.
"""
from __future__ import annotations

import pytest

from lib import agents, settings_store, voice_preamble as vp, voice_verbosity
from lib.personalities import KEY_ENABLED


@pytest.mark.parametrize("voice", [True, False])
@pytest.mark.parametrize("persona", ["", "Axel"])
def test_apply_then_strip_round_trips(voice, persona):
    text = "Run the tests\n\nand report."
    wrapped = vp.apply_voice_preamble(text, voice=voice, persona=persona)
    assert wrapped.startswith("[voice-mode] ")
    assert wrapped.endswith("\n\n--- user message ---\n" + text)
    assert vp.strip_voice_preamble(wrapped) == text


@pytest.mark.parametrize("value", ["plain message", "", "[voice-mode] without split", 42, None])
def test_strip_is_a_no_op_without_the_full_sentinel_pair(value):
    assert vp.strip_voice_preamble(value) is value


def test_text_turn_has_only_the_clarp_skills_guidance():
    from lib.clarp_guidance import CLARP_SKILLS_GUIDANCE
    body = vp.app_turn_instructions(voice=False)
    assert body == CLARP_SKILLS_GUIDANCE
    assert "<speak>" not in body and "<vox>" not in body


def test_voice_turn_adds_speak_and_natural_speech_blocks():
    from lib import voice_humanness
    body = vp.app_turn_instructions(voice=True)
    assert body.split("\n\n") == [vp._APP_TURN_GUIDANCE, vp._VOICE_INSTRUCTION,
                                   voice_humanness.guidance(voice_humanness.DEFAULT_LEVEL)]
    assert "<speak>" in vp._VOICE_INSTRUCTION and "occasional fillers" in body


def test_voice_turn_uses_the_agents_humanness_level(tmp_path):
    from lib import voice_humanness
    agents.create_agent(persona="Axel", voice_id="V", cwd=str(tmp_path), session="axel")
    voice_humanness.update({"agents": {"axel": 10}})
    assert "Humanness: 10/10." in vp.app_turn_instructions(voice=True, session="axel")
    assert "Humanness: 5/10." in vp.app_turn_instructions(voice=True)


def test_voice_defaults_true_in_apply():
    assert vp._VOICE_INSTRUCTION in vp.apply_voice_preamble("x")
    assert vp._VOICE_INSTRUCTION not in vp.apply_voice_preamble("x", voice=False)


def test_identity_empty_without_persona():
    assert vp.persona_identity_instruction("") == ""
    assert vp.persona_identity_instruction("   ", "sess") == ""
    assert vp.apply_voice_preamble("m", voice=False) == (
        "[voice-mode] " + vp._APP_TURN_GUIDANCE + "\n\n--- user message ---\nm")


def test_identity_uses_builtin_personality():
    from lib.config import persona_personality
    identity = vp.persona_identity_instruction("Axel")
    assert identity == "You are Axel. " + persona_personality("Axel")
    # The personality lookup is case-insensitive; the name is echoed as given.
    assert vp.persona_identity_instruction("axel") == "You are axel. " + persona_personality("Axel")


def test_identity_unknown_persona_is_name_only():
    assert vp.persona_identity_instruction("Zorblax") == "You are Zorblax."


def test_identity_prefers_agent_custom_personality(tmp_path):
    agent_id = agents.create_agent(persona="Axel", voice_id="v", cwd=str(tmp_path), session="axel-1")
    agents.update_agent(agent_id, personality="Personality: speaks only in haiku.")
    assert vp.persona_identity_instruction("Axel", "axel-1") == "You are Axel. Personality: speaks only in haiku."
    # Blank custom personality falls back to the built-in text.
    agents.update_agent(agent_id, personality="   ")
    from lib.config import persona_personality
    assert vp.persona_identity_instruction("Axel", "axel-1") == "You are Axel. " + persona_personality("Axel")


def test_identity_drops_personality_when_switch_is_off(tmp_path):
    agent_id = agents.create_agent(persona="Axel", voice_id="v", cwd=str(tmp_path), session="axel-1")
    agents.update_agent(agent_id, personality="Personality: custom.")
    settings_store.set_bool(KEY_ENABLED, False)
    assert vp.persona_identity_instruction("Axel", "axel-1") == "You are Axel."
    settings_store.set_bool(KEY_ENABLED, True)
    assert vp.persona_identity_instruction("Axel", "axel-1") == "You are Axel. Personality: custom."


def test_identity_unknown_session_is_tolerated():
    assert vp.persona_identity_instruction("Zorblax", "no-such-session") == "You are Zorblax."


def test_identity_is_placed_after_instructions_before_split():
    wrapped = vp.apply_voice_preamble("msg", voice=False, persona="Zorblax")
    assert wrapped == ("[voice-mode] " + vp._APP_TURN_GUIDANCE
                       + "\n\nYou are Zorblax.\n\n--- user message ---\nmsg")


def test_narration_clause_follows_agent_level(tmp_path):
    agent_id = agents.create_agent(persona="Iris", voice_id="v", cwd=str(tmp_path), session="iris-1")
    assert vp.app_turn_instructions(voice=True, session="iris-1") == vp.app_turn_instructions(voice=True)
    agents.update_agent(agent_id, voice_verbosity=voice_verbosity.STEPS)
    body = vp.app_turn_instructions(voice=True, session="iris-1")
    assert body.endswith("\n\n" + voice_verbosity.narration_clause(voice_verbosity.STEPS))
    # Narration is spoken-only: a text turn never carries it.
    assert vp.app_turn_instructions(voice=False, session="iris-1") == vp._APP_TURN_GUIDANCE


def test_narration_clause_missing_session_is_quiet():
    assert vp._narration_clause("") == ""
    assert vp._narration_clause("ghost-session") == ""


def test_narration_lookup_failure_never_breaks_the_turn(monkeypatch):
    def boom(session):
        raise RuntimeError("db gone")

    monkeypatch.setattr(agents, "get_by_session", boom)
    assert vp._narration_clause("any") == ""
    assert vp.app_turn_instructions(voice=True, session="any") == vp.app_turn_instructions(voice=True)


def test_strip_handles_the_quoted_prompt_opencode_stores():
    from lib.voice_preamble import apply_voice_preamble, strip_voice_preamble
    wrapped = apply_voice_preamble("Hi there", voice=False)
    assert strip_voice_preamble('"' + wrapped + '"') == "Hi there"
    assert strip_voice_preamble(wrapped) == "Hi there"
    assert strip_voice_preamble('"just a quoted message"') == '"just a quoted message"'


def test_voice_preamble_requests_conversational_delivery_for_all_speech():
    spoken = vp.apply_voice_preamble("Explain the result.", voice=True)

    assert "Every spoken response should sound conversational" in spoken
    assert "do not reserve them for uncertainty" in spoken
    assert "When you're unsure or working through something complex" not in spoken
    assert "few or no fillers" not in spoken
