"""Provider-keyed voice resolution.

An agent's `voice_id` is provider-agnostic at the storage layer but
provider-specific at synthesis time. Two on-disk forms exist:

  * plain string  ->  an ElevenLabs voice id (the `[roster]` config form)
  * JSON object   ->  {"elevenlabs": "<id>", "cartesia": "<id>"}

`resolve_voice(raw, provider)` collapses both to "the id to use for this
provider, or None". Synthesis is the only place that interprets the field;
everywhere else (storage, dedup, metadata) treats it as an opaque token,
so the JSON form rides through untouched.
"""
from __future__ import annotations

import json

ELEVENLABS = "elevenlabs"
CARTESIA = "cartesia"
DEEPGRAM = "deepgram"


def voice_map(raw: str | None) -> dict[str, str]:
    """Parse a stored `voice_id` into a {provider: id} map.

    A JSON object is taken as-is. A bare id string is an ElevenLabs id.
    """
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items() if v}
    return {ELEVENLABS: s}


def resolve_voice(raw: str | None, provider: str) -> str | None:
    """Return the voice id to use for `provider`, or None if the agent
    has no voice configured for it."""
    return voice_map(raw).get(provider) or None


def merge_voice(raw: str | None, provider: str, voice_id: str) -> str:
    """Update one provider voice without discarding other provider choices."""
    mapping = voice_map(raw)
    if voice_id.strip():
        mapping[provider.strip().lower()] = voice_id.strip()
    else:
        mapping.pop(provider.strip().lower(), None)
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))
