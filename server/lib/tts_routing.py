"""TTS routing decisions used by the Stop-hook worker.

The hook decides where to put the generated mp3 based on the per-session TTS
mode. For `pwa` mode we additionally gate on the app session belonging to a
registered agent so an unrelated Claude Code instance cannot enqueue audio.
"""
from __future__ import annotations


def should_emit_to_pwa_dir(session: str, agents: dict | None) -> bool:
    """Return True if a TTS clip from `session` should land in the PWA's
    shared audio cache.

    - empty/missing app session → False
    - app session not in the registry → False
    - app session in the registry → True
    """
    if not session:
        return False
    if not agents:
        return False
    return session in agents
