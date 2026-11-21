"""Instant, zero-network Contact minting when a backend's Contact pool is exhausted."""
from __future__ import annotations

import hashlib
import json
from typing import Collection

from . import agents as agents_db
from . import cartesia_voices, config, personas, roster
from .voice import CARTESIA, resolve_voice


_INITIALS = "BCDEFGHJKLMNPQRSTVWXYZ"


def pick_available_voice(name: str = "") -> str:
    """Voices for a new contact named ``name``: the pool's choice for the configured
    provider and fallback (voice_pool), in the one stored shape. No network I/O."""
    from . import voice_pool
    return voice_pool.assign(config.load(), name, "{}")


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
    voice_id = pick_available_voice(name)
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
