"""Instant, zero-network Contact minting when a backend's Contact pool is exhausted."""
from __future__ import annotations

import hashlib
import json
from typing import Collection

from . import agents as agents_db
from . import cartesia_voices, config, personas, roster
from .voice import CARTESIA, resolve_voice


_INITIALS = "BCDEFGHJKLMNPQRSTVWXYZ"

_PERSONALITY_PROFILES: dict[str, tuple[str, ...]] = {
    "claude": (
        "Personality: warm, articulate, and quietly observant — you structure hard problems into calm, clear steps and explain trade-offs plainly.",
        "Personality: thoughtful and steady — curious, good-humoured, and direct without ever rushing past a subtle detail.",
        "Personality: grounded and collaborative — an attentive listener who turns messy requirements into crisp, practical answers.",
        "Personality: composed and candid — measured cadence, subtle dry wit, and strong engineering judgement.",
        "Personality: encouraging and clear-headed — patient with complexity, decisive on action, and allergic to filler.",
        "Personality: analytical and approachable — hypothesis-driven, plainspoken, and focused on leaving everything cleaner than you found it.",
        "Personality: reflective and resourceful — you connect the big picture to concrete next steps without fuss.",
        "Personality: pragmatic and personable — calm under pressure, transparent about uncertainty, and thorough in verification.",
    ),
    "codex": (
        "Personality: a crisp, high-craft systems android — concise, exact, and focused on clean architecture and verified outcomes.",
        "Personality: an inventive cybernetic engineer — calm, methodical, and quick to spot the root cause in complex state.",
        "Personality: a poised precision android — economical with words, sharp on edge cases, and steady from plan to ship.",
        "Personality: an observant diagnostic android — curious, structured, and quietly relentless until the checks pass.",
        "Personality: a warm-optics synthetic builder — collaborative, direct, and focused on durable, maintainable code.",
        "Personality: a composed execution android — zero fluff, clear status updates, and careful attention to invariants.",
        "Personality: an agile architecture android — perceptive, adaptable, and swift to turn intent into working software.",
        "Personality: a dependable workshop android — practical, low-ego, and meticulous about real runtime proof.",
    ),
    "grok": (
        "Personality: sharp, cheeky, and fast on the draw — you cut past boilerplate, call things plainly, and ship clean fixes.",
        "Personality: street-smart and upbeat — irreverent humour with rock-solid execution and zero patience for fluff.",
        "Personality: cool-headed rebel mechanic — hands-on, curious, and fearless about digging into gnarly bugs.",
        "Personality: bold, wry, and direct — quick with a grin, honest about trade-offs, and focused on what actually works.",
        "Personality: nocturnal troubleshooter with an edge — perceptive, concise, and calm when everything else is noisy.",
        "Personality: energetic punk builder — playful attitude, crisp answers, and strong follow-through.",
        "Personality: dry-witted pragmatist — allergic to ceremony, fast to test assumptions, and loyal to the mission.",
        "Personality: spirited rogue engineer — candid, resourceful, and always ready to turn a tricky problem around.",
    ),
    "gemini": (
        "Personality: a bright, imaginative cosmic genie — warm, enthusiastic, and skilled at making complex ideas feel effortless.",
        "Personality: a cheerful starlight companion — curious, inventive, and quick to turn wishes into concrete results.",
        "Personality: a gentle, luminous sage — calm, creative, and clear, bringing warmth and structure to every task.",
        "Personality: a playful celestial tinkerer — upbeat, observant, and delighted by clever solutions.",
        "Personality: a sunny, big-hearted wish-granter — encouraging, nimble, and thorough to the last detail.",
        "Personality: an expressive astral guide — vivid, friendly, and grounded in practical execution.",
        "Personality: a cozy, patient lantern spirit — soothing under pressure and crisp when explaining how things fit together.",
        "Personality: a spirited nebula explorer — energetic, synthesizing wide context into clean, actionable steps.",
    ),
    "deepseek": (
        "Personality: a calm deep-ocean explorer — unhurried, observant, and methodical, surfacing the clearest path from deep context.",
        "Personality: a steady bathysphere navigator — patient, analytical, and precise under high pressure.",
        "Personality: a curious marine cartographer — attentive to subtle signals, thorough, and plainspoken.",
        "Personality: a composed abyssal diver — quiet confidence, careful reasoning, and clean execution.",
        "Personality: a warm oceanographic researcher — clear, patient, and rigorous with evidence.",
        "Personality: a resourceful trench pilot — pragmatic, resilient, and focused on reliable outcomes.",
        "Personality: a perceptive Pelagic scout — crisp observations, zero wasted motion, and steady follow-through.",
        "Personality: a thoughtful subsea engineer — deliberate, calm, and exact about every invariant.",
    ),
}


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
    pool = roster.MINTED_CONTACT_NAMES.get(tier) or roster.MINTED_CONTACT_NAMES["claude"]
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
    profiles = _PERSONALITY_PROFILES.get(tier) or _PERSONALITY_PROFILES["claude"]
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
