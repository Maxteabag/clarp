"""Unified provider-aware voice catalog for server and native clients."""
from __future__ import annotations

from urllib.parse import quote

from . import config
from .tts_providers import status as provider_status
from .voice import voice_map
from .voices import VOICE_CATALOG

ELEVEN_PREVIEW_MODEL = "eleven_multilingual_v2"
ELEVEN_PREVIEW_SPEED = 1.0

# Snapshots of Deepgram's English catalogue, used when no Deepgram key is
# configured (and by managed Clarp Voice, which reaches Flux through Audio
# Central rather than a Computer-local key). With a key, the live lists in
# deepgram_voices.py supersede these.
FLUX_VOICES = (
    ("flux-alexis-en", "Alexis", "American; clear, professional, calm"),
    ("flux-bree-en", "Bree", "American; friendly, sweet, confused"),
    ("flux-brittany-en", "Brittany", "American; confident, robotic, kind"),
    ("flux-brooke-en", "Brooke", "American; friendly, intelligent, fast"),
    ("flux-bruce-en", "Bruce", "American; friendly, kind, natural"),
    ("flux-cliff-en", "Cliff", "American; deep, confident, calm"),
    ("flux-cole-en", "Cole", "American; friendly, clear, interesting"),
    ("flux-colin-en", "Colin", "British; warm, friendly, trustworthy"),
    ("flux-conor-en", "Conor", "Irish; confident, deep, friendly"),
    ("flux-donovan-en", "Donovan", "American; professional, angry, calm"),
    ("flux-drew-en", "Drew", "American; confident, relaxed, soft"),
    ("flux-elise-en", "Elise", "American; clear, professional, calm"),
    ("flux-gemma-en", "Gemma", "British; friendly, kind, approachable"),
    ("flux-haley-en", "Haley", "American; clear, professional, caring"),
    ("flux-hannah-en", "Hannah", "American; clear, confident, thoughtful"),
    ("flux-heather-en", "Heather", "American; clear, engaging, energetic"),
    ("flux-jack-en", "Jack", "British; confident, thoughtful, friendly"),
    ("flux-kai-en", "Kai", "Singaporean; clear, calm, professional"),
    ("flux-kelsey-en", "Kelsey", "American; clear, professional, caring"),
    ("flux-kit-en", "Kit", "British; friendly, energetic, thoughtful"),
    ("flux-maeve-en", "Maeve", "Irish; friendly, energetic, confident"),
    ("flux-marcelo-en", "Marcelo", "Filipino; clear, calm, professional"),
    ("flux-marcus-en", "Marcus", "American; friendly, helpful, smooth"),
    ("flux-meena-en", "Meena", "Indian; empathetic, professional, calm"),
    ("flux-meghan-en", "Meghan", "American; friendly, nice, energetic"),
    ("flux-miles-en", "Miles", "American; clear, calm, professional"),
    ("flux-naveen-en", "Naveen", "Indian; clear, professional, knowledgeable"),
    ("flux-paige-en", "Paige", "American; clear, professional, calm"),
    ("flux-priya-en", "Priya", "Indian; confident, empathetic, professional"),
    ("flux-rufus-en", "Rufus", "British; friendly, confident, intelligent"),
    ("flux-sean-en", "Sean", "British; friendly, demanding, kind"),
    ("flux-sharon-en", "Sharon", "Australian; formal, calm, relaxed"),
    ("flux-sienna-en", "Sienna", "American; clear, professional, calm"),
    ("flux-tanner-en", "Tanner", "British; professional, bored, tired"),
    ("flux-wade-en", "Wade", "American; warm, confident, clear"),
    ("flux-wes-en", "Wes", "American; thoughtful, friendly, warm"),
)

AURA2_VOICES = (
    ("aura-2-amalthea-en", "Amalthea", "Filipino; engaging, natural, cheerful"),
    ("aura-2-andromeda-en", "Andromeda", "American; casual, expressive, comfortable"),
    ("aura-2-apollo-en", "Apollo", "American; confident, comfortable, casual"),
    ("aura-2-arcas-en", "Arcas", "American; natural, smooth, clear"),
    ("aura-2-aries-en", "Aries", "American; warm, energetic, caring"),
    ("aura-2-asteria-en", "Asteria", "American; clear, confident, knowledgeable"),
    ("aura-2-athena-en", "Athena", "American; calm, smooth, professional"),
    ("aura-2-atlas-en", "Atlas", "American; enthusiastic, confident, approachable"),
    ("aura-2-aurora-en", "Aurora", "American; cheerful, expressive, energetic"),
    ("aura-2-callista-en", "Callista", "American; clear, energetic, professional"),
    ("aura-2-cora-en", "Cora", "American; smooth, melodic, caring"),
    ("aura-2-cordelia-en", "Cordelia", "American; approachable, warm, polite"),
    ("aura-2-delia-en", "Delia", "American; casual, friendly, cheerful"),
    ("aura-2-draco-en", "Draco", "British; warm, approachable, trustworthy"),
    ("aura-2-electra-en", "Electra", "American; professional, engaging, knowledgeable"),
    ("aura-2-harmonia-en", "Harmonia", "American; empathetic, clear, calm"),
    ("aura-2-helena-en", "Helena", "American; caring, natural, positive"),
    ("aura-2-hera-en", "Hera", "American; smooth, warm, professional"),
    ("aura-2-hermes-en", "Hermes", "American; expressive, engaging, professional"),
    ("aura-2-hyperion-en", "Hyperion", "Australian; caring, warm, empathetic"),
    ("aura-2-iris-en", "Iris", "American; cheerful, positive, approachable"),
    ("aura-2-janus-en", "Janus", "American; southern, smooth, trustworthy"),
    ("aura-2-juno-en", "Juno", "American; natural, engaging, melodic"),
    ("aura-2-jupiter-en", "Jupiter", "American; expressive, knowledgeable, baritone"),
    ("aura-2-luna-en", "Luna", "American; friendly, natural, engaging"),
    ("aura-2-mars-en", "Mars", "American; smooth, patient, trustworthy"),
    ("aura-2-minerva-en", "Minerva", "American; positive, friendly, natural"),
    ("aura-2-neptune-en", "Neptune", "American; professional, patient, polite"),
    ("aura-2-odysseus-en", "Odysseus", "American; calm, smooth, comfortable"),
    ("aura-2-ophelia-en", "Ophelia", "American; expressive, enthusiastic, cheerful"),
    ("aura-2-orion-en", "Orion", "American; approachable, comfortable, calm"),
    ("aura-2-orpheus-en", "Orpheus", "American; professional, clear, confident"),
    ("aura-2-pandora-en", "Pandora", "British; smooth, calm, melodic"),
    ("aura-2-phoebe-en", "Phoebe", "American; energetic, warm, casual"),
    ("aura-2-pluto-en", "Pluto", "American; smooth, calm, empathetic"),
    ("aura-2-saturn-en", "Saturn", "American; knowledgeable, confident, baritone"),
    ("aura-2-selene-en", "Selene", "American; expressive, engaging, energetic"),
    ("aura-2-thalia-en", "Thalia", "American; clear, confident, energetic"),
    ("aura-2-theia-en", "Theia", "Australian; expressive, polite, sincere"),
    ("aura-2-vesta-en", "Vesta", "American; natural, expressive, patient"),
    ("aura-2-zeus-en", "Zeus", "American; deep, trustworthy, smooth"),
)


def _owners(agents: dict, provider: str, current_session: str) -> dict[str, str]:
    result = {}
    for session, info in agents.items():
        if session == current_session:
            continue
        voice_id = voice_map(str((info or {}).get("voice_id") or "")).get(provider)
        if voice_id:
            result[voice_id] = str((info or {}).get("name") or session)
    return result


def _rows(provider: str, values, agents: dict, current_session: str,
          *, preview_provider: str | None = None) -> list[dict]:
    owners = _owners(agents, provider, current_session)
    current = voice_map(str((agents.get(current_session) or {}).get("voice_id") or ""))
    return [{
        "id": voice_id,
        "name": name,
        "description": description,
        "provider": provider,
        "taken_by": owners.get(voice_id),
        "current": current.get(provider) == voice_id,
        "preview_url": (
            "/voice-preview?provider="
            f"{quote(preview_provider or provider, safe='')}&id={quote(voice_id, safe='')}"),
    } for voice_id, name, description in values]


def catalog(agents: dict, current_session: str = "") -> dict:
    status = provider_status()
    # A live catalogue when the Computer holds the key, the bundled English
    # snapshot otherwise — so an unconfigured provider still lists voices to
    # choose from, and a configured one is never stale.
    from .deepgram_voices import english_voices as deepgram_english
    from .elevenlabs_voices import english_voices as elevenlabs_english
    deepgram_live = deepgram_english() or list(FLUX_VOICES + AURA2_VOICES)
    elevenlabs_live = elevenlabs_english() or [
        (row["id"], row["label"].split(" — ", 1)[0], row["label"])
        for row in VOICE_CATALOG]
    voices: dict[str, list[dict]] = {
        "elevenlabs": _rows(
            "elevenlabs", elevenlabs_live, agents, current_session),
        "deepgram": _rows(
            "deepgram", deepgram_live, agents, current_session),
    }
    try:
        from .cartesia_voices import english_voices
        cartesia = english_voices()
        owners = _owners(agents, "cartesia", current_session)
        current_info = agents.get(current_session) or {}
        current_cartesia = voice_map(str(
            current_info.get("voice_id") or "")).get("cartesia")
        if not current_cartesia:
            current_cartesia = config.load().cartesia_voice_for(str(
                current_info.get("name") or current_info.get("persona") or ""))
        voices["cartesia"] = [{
            "id": row["id"], "name": row["name"],
            "description": row.get("description") or row.get("tagline") or "",
            "provider": "cartesia", "taken_by": owners.get(row["id"]),
            "current": current_cartesia == row["id"],
            "preview_url": f"/voice-preview?provider=cartesia&id={row['id']}",
        } for row in cartesia]
    except Exception:
        voices["cartesia"] = []
    custom_errors: dict[str, str] = {}
    from .custom_tts_adapters import AdapterError, discover, voices as adapter_voices
    from .tts_providers import VALID_IDS
    for manifest in discover(reserved_ids=VALID_IDS):
        try:
            values = [
                (item["id"], item["name"], item.get("description", ""))
                for item in adapter_voices(manifest)
            ]
            voices[manifest.id] = _rows(
                manifest.id, values, agents, current_session)
        except AdapterError as exc:
            voices[manifest.id] = []
            custom_errors[manifest.id] = str(exc)[:300]
    providers = []
    for row in status["providers"]:
        provider = row["id"]
        if provider == "none":
            continue
        error = custom_errors.get(provider, "")
        providers.append({
            "id": provider, "name": row.get("name", provider.title()),
            "description": row.get("description", ""),
            "kind": row.get("kind", "provider"),
            "custom": bool(row.get("custom")),
            "supports_preview": bool(row.get("supports_preview", True)),
            "allows_custom_voice": bool(row.get("allows_custom_voice")),
            "available": bool(row.get("available")) and not error,
            "installed": bool(row.get("installed")),
            "selected": provider == status["provider"],
            "fallback": provider == status["fallback"],
            "voices": voices.get(provider, []),
            "error": error or None,
        })
    return {
        "provider": status["provider"], "fallback": status["fallback"],
        "voice_available": any(
            row["available"] and (row["selected"] or row["fallback"])
            for row in providers),
        "providers": providers,
    }
