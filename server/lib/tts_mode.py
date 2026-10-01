"""Choose the TTS provider for one clip: live voice or quality voice.

Rule: if the agent's chat is open on a phone or desktop client, the clip
uses the live provider (`[tts] provider`, low first-audio latency). If not,
it uses the quality provider (`[tts] quality_provider`), which may take a
few seconds to start but nobody is waiting on it.

"Chat open" is the server's existing view of the user:
  * the focused agent (`focus` table, set when a client opens a chat), and
  * a client in the foreground right now: an iOS application-activity
    lease or an active desktop-presence lease (both expire after 45 s).

Focus is not cleared when a chat is closed, so "the last chat opened while
the app is on screen" counts as open. Per-agent `[tts.agents.<name>]`
entries can override either provider and the voice for a provider.
"""
from __future__ import annotations

from dataclasses import dataclass

from .log import log_exception

LIVE = "live"
QUALITY = "quality"
# Input-idle window for the iOS foreground lease. The lease itself expires
# 45 s after the last report, so this only rules out a phone that is
# foreground but untouched for a long time (e.g. a dock with auto-lock off).
_FOREGROUND_IDLE_SECONDS = 30 * 60


@dataclass(frozen=True)
class Route:
    mode: str               # LIVE or QUALITY
    provider: str
    voice: str | None       # per-agent voice override for `provider`
    fallback: str           # provider to retry with, "" for none
    chat_open: bool | None  # None when no quality provider applies


def chat_open(session: str) -> bool:
    """True when `session` is the focused chat and a client is in front."""
    try:
        from .focus import current_focus_session
        if not session or current_focus_session() != session:
            return False
        from . import application_activity, desktop_presence
        return bool(application_activity.active(_FOREGROUND_IDLE_SECONDS)
                    or desktop_presence.active())
    except Exception as e:  # noqa: BLE001 — presence must not drop audio
        log_exception("ttsModePresenceFail", e, detail=session)
        return True


def live_route(cfg, provider: str, voice: str | None = None,
               chat_is_open: bool | None = None) -> Route:
    return Route(LIVE, provider, voice, fallback_for(cfg, provider),
                 chat_is_open)


def route(cfg, agent: dict | None, session: str) -> Route:
    """Provider, voice override and fallback for one clip."""
    override = cfg.tts_override_for(agent)
    live = override.get("live_provider") or cfg.tts_provider
    quality = (override.get("quality_provider")
               or cfg.tts_quality_provider or live)
    if quality in ("", "none") or quality == live:
        return live_route(cfg, live, override.get(f"{live}_voice"))
    if chat_open(session):
        return live_route(cfg, live, override.get(f"{live}_voice"), True)
    # A quality clip that cannot be made is still spoken by the live voice.
    return Route(QUALITY, quality, override.get(f"{quality}_voice"),
                 fallback_for(cfg, quality) or live, False)


def fallback_for(cfg, provider: str) -> str:
    fallback = cfg.tts_fallback
    return "" if fallback in ("", "none", provider) else fallback
