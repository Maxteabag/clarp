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


def test_text_turn_has_only_the_question_restriction():
    body = vp.app_turn_instructions(voice=False)
    assert body == vp._NO_INTERACTIVE_QUESTIONS
    assert "<speak>" not in body and "<vox>" not in body
    assert "Never call AskUserQuestion" in body


def test_voice_turn_adds_speak_and_natural_speech_blocks():
    body = vp.app_turn_instructions(voice=True)
    assert body.split("\n\n") == [vp._NO_INTERACTIVE_QUESTIONS, vp._VOICE_INSTRUCTION, vp._NATURAL_SPEECH]
    assert "<speak>" in vp._VOICE_INSTRUCTION and "<vox>" in vp._NATURAL_SPEECH


def test_voice_defaults_true_in_apply():
    assert vp._VOICE_INSTRUCTION in vp.apply_voice_preamble("x")
    assert vp._VOICE_INSTRUCTION not in vp.apply_voice_preamble("x", voice=False)


def test_identity_empty_without_persona():
    assert vp.persona_identity_instruction("") == ""
    assert vp.persona_identity_instruction("   ", "sess") == ""
    assert vp.apply_voice_preamble("m", voice=False) == (
        "[voice-mode] " + vp._NO_INTERACTIVE_QUESTIONS + "\n\n--- user message ---\nm")


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
    assert wrapped == ("[voice-mode] " + vp._NO_INTERACTIVE_QUESTIONS
                       + "\n\nYou are Zorblax.\n\n--- user message ---\nmsg")


def test_narration_clause_follows_agent_level(tmp_path):
    agent_id = agents.create_agent(persona="Iris", voice_id="v", cwd=str(tmp_path), session="iris-1")
    assert vp.app_turn_instructions(voice=True, session="iris-1") == vp.app_turn_instructions(voice=True)
    agents.update_agent(agent_id, voice_verbosity=voice_verbosity.STEPS)
    body = vp.app_turn_instructions(voice=True, session="iris-1")
    assert body.endswith("\n\n" + voice_verbosity.narration_clause(voice_verbosity.STEPS))
    # Narration is spoken-only: a text turn never carries it.
    assert vp.app_turn_instructions(voice=False, session="iris-1") == vp._NO_INTERACTIVE_QUESTIONS


def test_narration_clause_missing_session_is_quiet():
    assert vp._narration_clause("") == ""
    assert vp._narration_clause("ghost-session") == ""


def test_narration_lookup_failure_never_breaks_the_turn(monkeypatch):
    def boom(session):
        raise RuntimeError("db gone")

    monkeypatch.setattr(agents, "get_by_session", boom)
    assert vp._narration_clause("any") == ""
    assert vp.app_turn_instructions(voice=True, session="any") == vp.app_turn_instructions(voice=True)
