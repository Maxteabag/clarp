"""Choose the TTS provider and voice for one clip.

Every clip uses `[tts] provider` unless the agent has a
`[tts.agents.<name or session>]` entry:

    [tts.agents.Ingrid]
    provider = "gemini"
    gemini_voice = "Charon"     # <provider>_voice: that provider's voice

An agent always speaks with one voice; the override only changes which.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    provider: str
    voice: str | None   # per-agent voice override for `provider`
    fallback: str       # provider to retry with, "" for none


def route(cfg, agent: dict | None) -> Route:
    """Provider, voice override and fallback for one agent's clip."""
    override = cfg.tts_override_for(agent)
    provider = override.get("provider") or cfg.tts_provider
    return Route(provider, override.get(f"{provider}_voice"),
                 fallback_for(cfg, provider))


def fallback_for(cfg, provider: str) -> str:
    fallback = cfg.tts_fallback
    return "" if fallback in ("", "none", provider) else fallback
