"""ElevenLabs voice catalogue and assignment helpers."""

from __future__ import annotations


# Wider catalogue used by the settings UI. Each persona's default voice from
# the AGENT_ROSTER also appears here so the picker shows it.
VOICE_CATALOG: list[dict] = [
    {"id": "nPczCjzI2devNBz1zQrb", "label": "Brian — warm male"},
    {"id": "21m00Tcm4TlvDq8ikWAM", "label": "Rachel — calm female"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "label": "Domi — strong female"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella — soft female"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni — well-rounded male"},
    {"id": "MF3mGyEYCl7XYWbV9V6O", "label": "Elli — emotional female"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "label": "Josh — deep male"},
    {"id": "VR6AewLTigWG4xSOukaG", "label": "Arnold — crisp male"},
    {"id": "pNInz6obpgDQGcFmaJgB", "label": "Adam — narrator male"},
    {"id": "yoZ06aMxZJJ28mfd3POQ", "label": "Sam — raspy male"},
    {"id": "ThT5KcBeYPX3keUQqHPh", "label": "Dorothy — pleasant British female"},
    {"id": "g5CIjZEefAph4nQFvHAz", "label": "Ethan — soft male"},
    {"id": "GBv7mTt0atIp3Br8iCZE", "label": "Thomas — calm British male"},
    {"id": "IKne3meq5aSn9XLyUdCD", "label": "Charlie — natural male"},
    {"id": "JBFqnCBsd6RMkjVDRZzb", "label": "George — mature male"},
    {"id": "N2lVS1w4EtoT3dr4eOWO", "label": "Callum — middle aged male"},
    {"id": "ODq5zmih8GrVes37Dizd", "label": "Patrick — deep male"},
    {"id": "SOYHLrjzK2X1ezoPC6cr", "label": "Harry — anxious male"},
    {"id": "XB0fDUnXU5powFXDhCwa", "label": "Charlotte — seductive female"},
    {"id": "XrExE9yKIg1WjnnlVkGX", "label": "Matilda — friendly female"},
    {"id": "Xb7hH8MSUJpSbSDYk0k2", "label": "Alice — confident British female"},
    {"id": "ZQe5CZNOzWyzPSCn5a3c", "label": "James — calm older male"},
    {"id": "bIHbv24MWmeRgasZH58o", "label": "Will — friendly male"},
    {"id": "cgSgspJ2msm6clMCkdW9", "label": "Jessica — expressive female"},
    {"id": "cjVigY5qzO86Huf0OWal", "label": "Eric — friendly older male"},
    {"id": "iP95p4xoKVk53GoZ742B", "label": "Chris — natural male"},
    {"id": "onwK4e9ZLuTAKqWW03F9", "label": "Daniel — authoritative British male"},
    {"id": "pFZP5JQG7iQjIQuC4Bku", "label": "Lily — warm British female"},
    {"id": "pqHfZKP75CvOlQylNhV4", "label": "Bill — strong older male"},
]


def voices_with_availability(agents: dict, for_session: str = "") -> list[dict]:
    """Annotate each voice with `taken_by` (the persona using it) so the
    settings UI can dim picked voices.

    The session being edited counts as not-taken (it's their current voice).
    """
    include_voice = (agents.get(for_session) or {}).get("voice_id")
    seen: set[str] = set()
    out: list[dict] = []
    for v in VOICE_CATALOG:
        vid = v["id"]
        if vid in seen:
            continue
        seen.add(vid)
        taken_by = None
        for sid, info in agents.items():
            if (info or {}).get("voice_id") == vid:
                taken_by = (info or {}).get("name") or sid
                break
        if vid == include_voice:
            taken_by = None
        out.append({**v, "taken_by": taken_by})
    return out
