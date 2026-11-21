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
import re

ELEVENLABS = "elevenlabs"
CARTESIA = "cartesia"
DEEPGRAM = "deepgram"
GEMINI = "gemini"
GOOGLE = "google"


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


# The one stored shape (issue 476): a provider map, compact JSON with sorted keys;
# "{}" is "no voice of its own". A bare id is read by its form when it is written.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ELEVENLABS_ID = re.compile(r"^[A-Za-z0-9]{20}$")
# A Chirp 3 HD voice name, "<language>-Chirp3-HD-<Name>" (google_tts.VOICES has the allowed ones).
_CHIRP_NAME = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-Chirp3-HD-[A-Z][A-Za-z]+$")


def infer_provider(voice_id: str | None) -> str | None:
    """The provider a bare voice id belongs to, by its form, or None when the
    form says nothing: a uuid is Cartesia's, a Chirp 3 HD name Google's, twenty
    letters and digits ElevenLabs'."""
    value = (voice_id or "").strip()
    if not value:
        return None
    if _UUID.match(value):
        return CARTESIA
    if _CHIRP_NAME.match(value):
        return GOOGLE
    if _ELEVENLABS_ID.match(value):
        return ELEVENLABS
    return None


def canonical(raw: str | None) -> str:
    """``raw`` in the one stored shape. A bare id goes under the provider its form
    names, else under ElevenLabs (how a bare value has always been read)."""
    text = (raw or "").strip()
    if text and not text.startswith("{"):
        provider = infer_provider(text) or ELEVENLABS
        return json.dumps({provider: text}, sort_keys=True, separators=(",", ":"))
    return json.dumps(voice_map(text), sort_keys=True, separators=(",", ":"))


def merge_provider_maps(stored: str | None, incoming: str | None,
                        base: str | None = None) -> str:
    """A contact's incoming map merged into the stored one, per provider (ruling H).

    With ``base`` (the map the client last read), a provider whose incoming value
    equals the base was not touched and keeps the stored value; one that differs was
    changed and takes the incoming value; one in the base but not in the incoming map
    was removed on purpose; one in the stored map but in neither was added by someone
    else since the read and stays. Without ``base`` nothing is removed by absence:
    the providers present are set, and an empty value removes one.

    The stored value and the base are read as they always have been (voice_map: a bare
    value is an ElevenLabs id); only the incoming value is read by its form. An edit never
    moves a stored bare id to another provider: that is the migration's, behind its
    audible check."""
    have = voice_map(stored)
    want = voice_map(canonical(incoming)) if (incoming or "").strip() not in {"", "{}"} else {}
    raw_incoming = {}
    text = (incoming or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            raw_incoming = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            raw_incoming = {}
    if base is None:
        merged = dict(have)
        for provider, value in want.items():
            merged[provider] = value
        for provider, value in raw_incoming.items():
            if value in ("", None):
                merged.pop(str(provider), None)
        return json.dumps(merged, sort_keys=True, separators=(",", ":"))
    was = voice_map(base)
    merged = dict(have)
    for provider in set(was) | set(want):
        if want.get(provider) == was.get(provider):
            continue                                  # not touched by this client
        if provider in want:
            merged[provider] = want[provider]         # changed (or added) by it
        else:
            merged.pop(provider, None)                # removed by it on purpose
    return json.dumps(merged, sort_keys=True, separators=(",", ":"))
