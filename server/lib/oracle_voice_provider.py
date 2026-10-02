"""Which voice model an Oracle v2 call runs on: GPT-Live or Gemini Live.

OpenAI's GPT-Live stays the default. Gemini Live is the alternative, chosen in
`[oracle] voice_provider` or, overriding that, by the phone through
`POST /oracle/voice-provider`. A choice that cannot run on this Host (Gemini
without a Gemini key) falls back to OpenAI rather than failing the call, so
selecting Gemini can never leave Oracle without a voice.

Podcast detours and the WebRTC "tinkered" engine stay on OpenAI.
"""
from __future__ import annotations

from . import settings_store

KEY = "oracle.voice_provider"
PROVIDERS = ("openai", "gemini")
DEFAULT_MODEL = "gemini-3.8-live"


def stored() -> str | None:
    value = (settings_store.get(KEY) or "").strip().lower()
    return value if value in PROVIDERS else None


def selected(cfg) -> str:
    """The configured choice, whether or not it can run here."""
    value = stored() or str(getattr(cfg, "oracle_voice_provider", "openai") or "").lower()
    return value if value in PROVIDERS else "openai"


def available(cfg) -> list[str]:
    return ["openai"] + (["gemini"] if cfg.gemini_key() else [])


def effective(cfg) -> str:
    """The provider a new call uses: the choice, or OpenAI when it cannot run."""
    choice = selected(cfg)
    return choice if choice in available(cfg) else "openai"


def gemini_model(cfg) -> str:
    return str(getattr(cfg, "oracle_gemini_model", "") or DEFAULT_MODEL)


def gemini_voice(cfg) -> str:
    """`[oracle] gemini_voice`, else the Oracle persona's Gemini voice, else the Gemini default."""
    return (str(getattr(cfg, "oracle_gemini_voice", "") or "")
            or cfg.gemini_voice_for("Oracle") or cfg.gemini_voice or "Kore")


def set_provider(value: str) -> None:
    value = str(value or "").strip().lower()
    if value not in PROVIDERS:
        raise ValueError("voice provider must be one of: " + ", ".join(PROVIDERS))
    settings_store.set_text(KEY, value)


def status(cfg) -> dict:
    choice = selected(cfg)
    current = effective(cfg)
    return {"provider": current, "selected": choice, "default": "openai",
            "available": available(cfg),
            "fallback_reason": None if choice == current else "Gemini Live needs a Gemini API key on this Host",
            "gemini": {"model": gemini_model(cfg), "voice": gemini_voice(cfg)}}
