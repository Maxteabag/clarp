"""Computer-owned Whisper guidance for voice transcription.

Whisper treats ``initial_prompt`` as preceding transcript text, not as a
general instruction block. Keep the prompt compact, put the most important
agent names last, and never ship a developer-specific vocabulary here.

Two independent settings drive the prompt: whether delegation transcriptions
are primed with the active agent names, and a user-maintained technical
glossary used for regular transcriptions.
"""

from __future__ import annotations

import math
import re

from . import settings_store

PROMPT_TOKEN_LIMIT = 223
MAX_PROMPT_CHARS = 600
MAX_GLOSSARY_CHARS = 2_000
_DELEGATION_NAMES_KEY = "transcription.guidance.delegation_names"
_GLOSSARY_KEY = "transcription.guidance.glossary"


def _clean_lines(raw: str, *, max_chars: int = MAX_GLOSSARY_CHARS) -> str:
    lines: list[str] = []
    for source_line in str(raw or "").splitlines():
        line = source_line.split("#", 1)[0].strip(" ,;\t")
        if line:
            lines.append(line)
    return "\n".join(lines)[:max_chars].rstrip()


def read_technical_glossary() -> str:
    return _clean_lines(settings_store.get_text(_GLOSSARY_KEY, default=""))


def delegation_agent_names_enabled() -> bool:
    stored = settings_store.get_text(_DELEGATION_NAMES_KEY, default="true")
    return stored.strip().lower() in {"1", "true", "yes", "on"}


def update_guidance(data: dict) -> tuple[bool, str]:
    names_enabled = data.get(
        "delegation_agent_names_enabled", delegation_agent_names_enabled())
    if not isinstance(names_enabled, bool):
        raise ValueError("delegation_agent_names_enabled must be boolean")
    glossary = _clean_lines(data.get(
        "technical_glossary", settings_store.get_text(_GLOSSARY_KEY)))
    settings_store.set_text(
        _DELEGATION_NAMES_KEY, "true" if names_enabled else "false")
    settings_store.set_text(_GLOSSARY_KEY, glossary)
    return names_enabled, glossary


def _unique_names(names: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in names:
        name = str(value or "").strip(" ,;\t")
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def build_initial_prompt(
    technical_glossary: str,
    active_agent_names: list[str],
    *,
    include_agent_names: bool,
    include_technical_glossary: bool,
    max_prompt_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """Build a bounded previous-transcript hint.

    The names come likeliest first (VocabService.active_agent_names) and are
    walked in that order (R109): a name is taken when the names part with it,
    "Agent names: " + the names joined by ", " + ".", is still at most
    ``max_prompt_chars`` characters (prefix, separators and the full stop
    counted) and at most PROMPT_TOKEN_LIMIT estimated tokens. A name that does
    not fit in what is left is skipped and the walk goes on, so one over-long
    name never ends the list; the same names give the same prompt."""
    if not include_agent_names and not include_technical_glossary:
        return ""
    name_prefix = "Agent names: "
    names: list[str] = []
    for name in _unique_names(active_agent_names):
        candidate = ", ".join([*names, name])
        if (len(name_prefix) + len(candidate) + 1 <= max_prompt_chars
                and estimated_prompt_tokens(f"{name_prefix}{candidate}.") <= PROMPT_TOKEN_LIMIT):
            names.append(name)
    name_text = ", ".join(names)
    names_prompt = (
        f"{name_prefix}{name_text}." if include_agent_names and name_text else "")

    if not include_technical_glossary:
        return names_prompt
    glossary = " ".join(_clean_lines(technical_glossary).splitlines())
    glossary_prefix = "Technical vocabulary: "
    remaining = max(0, max_prompt_chars - len(names_prompt) - 1)
    glossary_budget = max(0, remaining - len(glossary_prefix) - 1)
    glossary = glossary[:glossary_budget].rstrip(" ,;\t")
    while glossary:
        glossary_prompt = f"{glossary_prefix}{glossary}."
        prompt = " ".join(
            part for part in (glossary_prompt, names_prompt) if part)
        if estimated_prompt_tokens(prompt) <= PROMPT_TOKEN_LIMIT:
            return prompt
        glossary = glossary[:-8].rstrip(" ,;\t")
    return names_prompt


def estimated_prompt_tokens(prompt: str) -> int:
    """Conservative display estimate; actual Whisper tokenization may vary."""
    if not prompt:
        return 0
    lexical = len(re.findall(r"[\w]+|[^\w\s]", prompt, flags=re.UNICODE))
    return max(lexical, math.ceil(len(prompt.encode("utf-8")) / 3))


def settings_payload(active_agent_names: list[str]) -> dict:
    names_enabled = delegation_agent_names_enabled()
    glossary = read_technical_glossary()
    names = _unique_names(active_agent_names)
    delegation_prompt = build_initial_prompt(
        glossary, names,
        include_agent_names=names_enabled,
        include_technical_glossary=False,
    )
    regular_prompt = build_initial_prompt(
        glossary, names,
        include_agent_names=False,
        include_technical_glossary=True,
    )
    return {
        "settings": {
            "delegation_agent_names_enabled": names_enabled,
            "technical_glossary": glossary,
        },
        "defaults": {
            "delegation_agent_names_enabled": True,
            "technical_glossary": "",
        },
        "active_agent_names": names,
        "delegation_effective_prompt": delegation_prompt,
        "delegation_estimated_prompt_tokens": estimated_prompt_tokens(
            delegation_prompt),
        "regular_effective_prompt": regular_prompt,
        "regular_estimated_prompt_tokens": estimated_prompt_tokens(regular_prompt),
        "prompt_token_limit": PROMPT_TOKEN_LIMIT,
    }
