"""Computer-owned Whisper guidance."""

from __future__ import annotations

from lib.vocab import (
    MAX_PROMPT_CHARS,
    PROMPT_TOKEN_LIMIT,
    build_initial_prompt,
    delegation_agent_names_enabled,
    estimated_prompt_tokens,
    settings_payload,
    update_guidance,
)


def test_default_prompts_keep_names_and_glossary_separate():
    payload = settings_payload(["Ada", "Lin"])
    assert payload["settings"]["delegation_agent_names_enabled"] is True
    assert payload["settings"]["technical_glossary"] == ""
    assert payload["delegation_effective_prompt"] == "Agent names: Ada, Lin."
    assert payload["regular_effective_prompt"] == ""


def test_user_can_enable_and_edit_technical_glossary():
    update_guidance({
        "delegation_agent_names_enabled": True,
        "technical_glossary": "SwiftUI\nCloudflare # comment\n",
    })
    payload = settings_payload(["Ada"])
    assert payload["settings"]["technical_glossary"] == "SwiftUI\nCloudflare"
    assert payload["delegation_effective_prompt"] == "Agent names: Ada."
    assert payload["regular_effective_prompt"] == (
        "Technical vocabulary: SwiftUI Cloudflare.")


def test_delegation_names_can_be_disabled_independently():
    update_guidance({
        "delegation_agent_names_enabled": False,
        "technical_glossary": "SwiftUI",
    })
    assert delegation_agent_names_enabled() is False
    payload = settings_payload(["Ada"])
    assert payload["delegation_effective_prompt"] == ""
    assert payload["regular_effective_prompt"] == "Technical vocabulary: SwiftUI."


def test_update_rejects_non_boolean_toggle():
    import pytest
    with pytest.raises(ValueError):
        update_guidance({"delegation_agent_names_enabled": "yes"})


def test_independent_prompts_do_not_mix_names_and_glossary():
    delegation = build_initial_prompt(
        "SwiftUI", ["Ada"],
        include_agent_names=True,
        include_technical_glossary=False,
    )
    regular = build_initial_prompt(
        "SwiftUI", ["Ada"],
        include_agent_names=False,
        include_technical_glossary=True,
    )
    assert delegation == "Agent names: Ada."
    assert regular == "Technical vocabulary: SwiftUI."


def test_prompt_is_bounded_and_keeps_names_at_the_end():
    prompt = build_initial_prompt(
        "technical-term " * 500,
        ["Ada", "Lin"],
        include_agent_names=True,
        include_technical_glossary=True,
    )
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert prompt.endswith("Agent names: Ada, Lin.")


def test_prompt_token_estimate_stays_within_whisper_limit_for_unicode():
    prompt = build_initial_prompt(
        "🧠" * 2_000,
        ["Ada", "Lin"],
        include_agent_names=True,
        include_technical_glossary=True,
    )
    assert estimated_prompt_tokens(prompt) <= PROMPT_TOKEN_LIMIT
    assert prompt.endswith("Agent names: Ada, Lin.")


def test_names_are_deduplicated_case_insensitively():
    prompt = build_initial_prompt(
        "", ["Ada", "ada", " Lin "],
        include_agent_names=True,
        include_technical_glossary=False,
    )
    assert prompt == "Agent names: Ada, Lin."
