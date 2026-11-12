"""Instant, zero-network Contact minting when a backend's Contact pool is exhausted."""
from __future__ import annotations

import hashlib
import json
from typing import Collection

from . import agents as agents_db
from . import cartesia_voices, config, personas, roster
from .voice import CARTESIA, resolve_voice


_INITIALS = "BCDEFGHJKLMNPQRSTVWXYZ"


def candidate_cartesia_voice_ids() -> list[str]:
    """Return all locally known Cartesia voice UUIDs without network I/O."""
    cfg = config.load()
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in (
        *cartesia_voices.cached_voice_ids(),
        *cfg.cartesia_voices.values(),
        *config.DEFAULT_CARTESIA_VOICES.values(),
    ):
        voice_id = str(raw or "").strip()
        if voice_id and voice_id not in seen:
            seen.add(voice_id)
            ordered.append(voice_id)
    return ordered


def pick_available_voice(*, occupied_voices: Collection[str] | None = None) -> str:
    """Choose a Cartesia voice JSON string preferring unoccupied voices (<0.1ms, no network)."""
    candidates = candidate_cartesia_voice_ids()
    if not candidates:
        return "{}"
    active_voices = {
        v for a in agents_db.list_agents()
        if (v := resolve_voice(a.get("voice_id") or "", CARTESIA))
    }
    if occupied_voices is None:
        persona_voices = {
            v for p in personas.list_all()
            if (v := resolve_voice(p.get("voice_id") or "", CARTESIA))
        }
        all_taken = active_voices | persona_voices
    else:
        all_taken = {str(v).strip() for v in occupied_voices if str(v).strip()}

    for candidate in candidates:
        if candidate not in all_taken:
            return json.dumps({CARTESIA: candidate}, separators=(",", ":"))
    for candidate in candidates:
        if candidate not in active_voices:
            return json.dumps({CARTESIA: candidate}, separators=(",", ":"))
    fallback = candidates[len(all_taken) % len(candidates)]
    return json.dumps({CARTESIA: fallback}, separators=(",", ":"))


def pick_given_name(
    backend: str = "claude",
    *,
    occupied_names: Collection[str] = (),
    existing_names: Collection[str] | None = None,
) -> tuple[str, str]:
    """Pick a fresh, natural given name and its tier for ``backend``."""
    tier = roster.backend_to_tier(backend)
    pool = roster.contact_tier_spec(tier).minted_names
    taken = {str(n).strip().casefold() for n in occupied_names if str(n).strip()}
    taken.update(roster.CONTACT_TIERS.keys())
    if existing_names is None:
        taken.update(str(p["name"]).strip().casefold() for p in personas.list_all())
    else:
        taken.update(str(n).strip().casefold() for n in existing_names if str(n).strip())

    def _free(candidate: str) -> bool:
        folded = candidate.casefold()
        return folded not in taken and personas.get(candidate) is None

    for candidate in pool:
        if _free(candidate):
            return candidate, tier
    for initial in _INITIALS:
        for base in pool:
            candidate = f"{base} {initial}"
            if _free(candidate):
                return candidate, tier
    for suffix in range(2, 10_000):
        for base in pool:
            candidate = f"{base} {suffix}"
            if _free(candidate):
                return candidate, tier
    raise RuntimeError("exhausted minted contact name space")


def default_personality(name: str, tier: str) -> str:
    profiles = roster.contact_tier_spec(tier).personalities
    digest = int.from_bytes(hashlib.sha256(name.strip().casefold().encode()).digest()[:4], "big")
    return profiles[digest % len(profiles)]


def mint_contact(
    backend: str = "claude",
    *,
    occupied_names: Collection[str] = (),
    generate_avatar: bool = True,
) -> dict:
    """Create a durable named Contact immediately and kick off background avatar generation."""
    name, tier = pick_given_name(backend, occupied_names=occupied_names)
    voice_id = pick_available_voice()
    personality = default_personality(name, tier)
    row = personas.create(
        name=name,
        voice_id=voice_id,
        avatar_symbol="",
        personality=personality,
    )
    if generate_avatar and row:
        from . import persona_avatar_generation
        persona_avatar_generation.start_best_effort(
            row["name"], tier=tier, backend=backend)
    return row
