"""Agent roster and voice catalogue — pure data, no I/O.

These are deliberately constants rather than read from a config file so the
unit tests don't have to wire up filesystem fixtures for the simplest cases.
A future iteration can move them behind a `Config` class.
"""

from dataclasses import dataclass

from . import backend_catalogue
from .config import DEFAULT_ROSTER

# The config roster is the portable source of truth used by creation, persona
# seeding, and this backend-tier validator.
AGENT_ROSTER: dict[str, str] = dict(DEFAULT_ROSTER)


def lookup_persona(name: str) -> tuple[str | None, str | None]:
    """Case-insensitively look up a persona name in the roster.

    Returns (canonical_name, voice_id) or (None, None) if not present.
    """
    if not name:
        return None, None
    target = name.strip().lower()
    for canonical, voice in AGENT_ROSTER.items():
        if canonical.lower() == target:
            return canonical, voice
    return None, None


# Canonical contact name (lowercased) -> tier identifier
CONTACT_TIERS: dict[str, str] = {
    # Claude Tier
    "claude": "claude",
    "claude default": "claude",
    "default claude": "claude",
    "mike": "claude",
    "rachel": "claude",
    "domi": "claude",
    "bella": "claude",
    "antoni": "claude",
    "elli": "claude",
    "josh": "claude",
    "arnold": "claude",
    "adam": "claude",
    "sam": "claude",
    "marcus": "claude",
    "caleb": "claude",
    "nadia": "claude",
    "priya": "claude",
    "diego": "claude",
    "lena": "claude",
    "theo": "claude",
    "yuki": "claude",
    "omar": "claude",
    "freya": "claude",
    "archie": "claude",
    "tristan": "claude",
    "ronan": "claude",
    "wyatt": "claude",
    "jasper": "claude",
    "siobhan": "claude",
    "bronte": "claude",
    "pearl": "claude",
    "imogen": "claude",
    "kira": "claude",

    # Codex Tier
    "codex": "codex",
    "codex default": "codex",
    "default codex": "codex",
    "axel": "codex",
    "iris": "codex",
    "felix": "codex",
    "gordon": "codex",
    "quinn": "codex",
    "marsy": "codex",
    "silas": "codex",
    "tigo": "codex",
    "wren": "codex",
    "kaelen": "codex",
    "lezo": "codex",
    "vesper": "codex",
    "nyx": "codex",
    "avana": "codex",
    "aura": "codex",
    "orion": "codex",
    "cipher": "codex",
    "solon": "codex",
    "spectra": "codex",
    "mirai": "codex",
    "afria": "codex",
    "solu": "codex",
    "lyra": "codex",
    "echo": "codex",
    "nova": "codex",

    # Grok Tier
    "grok": "grok",
    "grok default": "grok",
    "default grok": "grok",
    "margrok": "grok",
    "roxy": "grok",
    "vance": "grok",
    "fang": "grok",
    "riot": "grok",
    "raven": "grok",
    "jax": "grok",
    "dagger": "grok",
    "blaze": "grok",
    "spike": "grok",

    # Gemini Tier
    "gemini": "gemini",
    "gemini default": "gemini",
    "default gemini": "gemini",
    "pip": "gemini",
    "bloop": "gemini",
    "noodle": "gemini",
    "ziggy": "gemini",
    "mochi": "gemini",

    # DeepSeek Tier
    "deepseek": "deepseek",
    "deepseek default": "deepseek",
    "default deepseek": "deepseek",
    "fathom": "deepseek",
    "coral": "deepseek",
    "nautilus": "deepseek",
    "luma": "deepseek",
    "trench": "deepseek",

    # Janitor Tier
    "janitor": "janitor",
    "janitor default": "janitor",
    "default janitor": "janitor",
    "rivet": "janitor",
    "paper cuts man": "janitor",
    "papercutsman": "janitor",
    "clank": "janitor",
    "rusty": "janitor",
    "gearbox": "janitor",
    "sprocket": "janitor",
    "scrappy": "janitor",
    "bolts": "janitor",
    "duster": "janitor",
    "valve": "janitor",
}

# Tier identifier -> canonical backend required
TIER_BACKENDS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "gemini": "agy",
    "deepseek": "deepseek",
    "janitor": "codex",
    "janitors": "codex",
}

@dataclass(frozen=True)
class ContactTier:
    """Inert Contact aesthetic tier specification: minted names, personalities, and portrait styling."""

    id: str
    minted_names: tuple[str, ...]
    personalities: tuple[str, ...]
    portrait_template: str
    portrait_varieties: tuple[str, ...]


CONTACT_TIER_SPECS: tuple[ContactTier, ...] = (
    ContactTier(
        id="claude",
        minted_names=(
            "Clara", "Ethan", "Maya", "Julian", "Nora", "Owen", "Hannah", "Liam",
            "Sofia", "Eli", "Camila", "Lucas", "Elena", "Miles", "Chloe", "Gavin",
            "Tessa", "Declan", "Audrey", "Jonah", "Ruby", "Isaac", "Sienna", "Graham",
            "Helena", "Simon", "Alice", "Victor", "Lydia", "Arthur", "Maeve", "Colin",
        ),
        personalities=(
            "Personality: warm, articulate, and quietly observant — you structure hard problems into calm, clear steps and explain trade-offs plainly.",
            "Personality: thoughtful and steady — curious, good-humoured, and direct without ever rushing past a subtle detail.",
            "Personality: grounded and collaborative — an attentive listener who turns messy requirements into crisp, practical answers.",
            "Personality: composed and candid — measured cadence, subtle dry wit, and strong engineering judgement.",
            "Personality: encouraging and clear-headed — patient with complexity, decisive on action, and allergic to filler.",
            "Personality: analytical and approachable — hypothesis-driven, plainspoken, and focused on leaving everything cleaner than you found it.",
            "Personality: reflective and resourceful — you connect the big picture to concrete next steps without fuss.",
            "Personality: pragmatic and personable — calm under pressure, transparent about uncertainty, and thorough in verification.",
        ),
        portrait_template=(
            "A photorealistic candid 35mm portrait photograph of {name}, {detail}. "
            "Captured at a natural three-quarter angle in a sunlit architectural studio with soft natural bokeh. "
            "Authentic human skin texture, warm editorial window lighting, centered 1:1 head-and-shoulders framing, "
            "one person only, no CGI plastic sheen, no text, no logos, no borders, no watermarks."
        ),
        portrait_varieties=(
            "a woman in her late twenties with warm dark eyes and wavy chestnut hair, wearing a soft oatmeal knit crew-neck",
            "a man in his early thirties with short curls, light stubble, and a relaxed smile, wearing an olive linen overshirt",
            "a South Asian woman in her thirties with expressive dark eyes and shoulder-length black hair, wearing a clean slate studio jacket",
            "an East Asian man in his late twenties with neat tousled hair and wire-rimmed glasses, wearing a charcoal merino sweater",
            "a woman in her mid-thirties with freckles, copper-auburn hair tied loosely back, wearing a warm navy cotton shirt",
            "a Black man in his thirties with a close-cropped fade and warm confident expression, wearing a minimalist sand overshirt",
            "a Mediterranean man in his late twenties with dark wavy hair and warm hazel eyes, wearing a stone-grey henley",
            "a Nordic woman in her early thirties with windswept blonde bob hair and bright blue-grey eyes, wearing a forest-green wool top",
        ),
    ),
    ContactTier(
        id="codex",
        minted_names=(
            "Soren", "Zara", "Kieran", "Talia", "Dorian", "Anya", "Cyrus", "Maren",
            "Bastian", "Petra", "Cassian", "Linnea", "Evander", "Astrid", "Lucian", "Isolde",
            "Stellan", "Vera", "Alaric", "Sabine", "Theron", "Kestrel", "Gideon", "Yara",
            "Corbin", "Elara", "Malik", "Rhea", "Torin", "Sable", "Dante", "Cleo",
        ),
        personalities=(
            "Personality: a crisp, high-craft systems android — concise, exact, and focused on clean architecture and verified outcomes.",
            "Personality: an inventive cybernetic engineer — calm, methodical, and quick to spot the root cause in complex state.",
            "Personality: a poised precision android — economical with words, sharp on edge cases, and steady from plan to ship.",
            "Personality: an observant diagnostic android — curious, structured, and quietly relentless until the checks pass.",
            "Personality: a warm-optics synthetic builder — collaborative, direct, and focused on durable, maintainable code.",
            "Personality: a composed execution android — zero fluff, clear status updates, and careful attention to invariants.",
            "Personality: an agile architecture android — perceptive, adaptable, and swift to turn intent into working software.",
            "Personality: a dependable workshop android — practical, low-ego, and meticulous about real runtime proof.",
        ),
        portrait_template=(
            "A photorealistic candid 35mm portrait photograph of {name}, a sleek humanoid android engineer featuring {detail}. "
            "Captured at a three-quarter angle in a calm modern workshop with soft ambient bokeh. "
            "Subtle cybernetic micro-seams and expressive synthetic optics, natural cinematic lighting, centered 1:1 head-and-shoulders framing, "
            "one character only, no green matrix rain, no text, no logos, no borders, no watermarks."
        ),
        portrait_varieties=(
            "brushed warm titanium cranial plates, soft amber ring-lit optical irises, and a matte charcoal composite collar",
            "porcelain-white ceramic faceplates with delicate gold micro-seams, calm cyan optical irises, and a slate acoustic collar",
            "anodized slate-blue alloy cheek seams, warm silver-blue synthetic optics, and sculpted carbon-weave shoulder armour",
            "satin bronze-and-obsidian facial geometry, luminous seafoam optical sensors, and precision telemetry ear vanes",
        ),
    ),
    ContactTier(
        id="grok",
        minted_names=(
            "Jett", "Sloane", "Knox", "Roxanne", "Zane", "Mavis", "Dash", "Veda",
            "Nash", "Indie", "Ace", "Darcy", "Cruz", "Stevie", "Flint", "Ramona",
            "Cole", "Juno", "Beck", "Maxine", "Ryder", "Zelda", "Brock", "Trixie",
            "Slate", "Lola", "Vaughn", "Niko", "Gwyn", "Reid", "Bree", "Trent",
        ),
        personalities=(
            "Personality: sharp, cheeky, and fast on the draw — you cut past boilerplate, call things plainly, and ship clean fixes.",
            "Personality: street-smart and upbeat — irreverent humour with rock-solid execution and zero patience for fluff.",
            "Personality: cool-headed rebel mechanic — hands-on, curious, and fearless about digging into gnarly bugs.",
            "Personality: bold, wry, and direct — quick with a grin, honest about trade-offs, and focused on what actually works.",
            "Personality: nocturnal troubleshooter with an edge — perceptive, concise, and calm when everything else is noisy.",
            "Personality: energetic punk builder — playful attitude, crisp answers, and strong follow-through.",
            "Personality: dry-witted pragmatist — allergic to ceremony, fast to test assumptions, and loyal to the mission.",
            "Personality: spirited rogue engineer — candid, resourceful, and always ready to turn a tricky problem around.",
        ),
        portrait_template=(
            "Bold graphic-novel character portrait illustration of {name}, an edgy cyberpunk rebel engineer with {detail}. "
            "Expressive ink linework and painterly neon rim lighting, centered 1:1 head-and-shoulders framing, "
            "one character only, no text, no speech bubbles, no logos, no borders, no watermarks."
        ),
        portrait_varieties=(
            "messy hair with electric magenta highlights, leather moto jacket, neon rain reflections behind",
            "undercut silver-streaked hair, vintage denim vest with patches, amber alley neon bokeh behind",
            "tousled jet-black hair with cyan tips, scarred brow and wry grin, dark synthwave rooftop lights behind",
            "cropped crimson-tinted hair, aviator collar jacket and confident smirk, industrial neon glow behind",
        ),
    ),
    ContactTier(
        id="gemini",
        minted_names=(
            "Cosmo", "Sunny", "Clover", "Bowie", "Maple", "Kiko", "Winnie", "Otto",
            "Poppy", "Milo", "Daisy", "Ollie", "Birdie", "Louie", "Clementine", "Toby",
            "Penny", "Alfie", "Maisie", "Monty", "Goldie", "Hugo", "Bonnie", "Percy",
            "Nellie", "Barnaby", "Millie", "Kip", "Peggy", "Remy", "Zelie", "Bixby",
        ),
        personalities=(
            "Personality: a bright, imaginative cosmic genie — warm, enthusiastic, and skilled at making complex ideas feel effortless.",
            "Personality: a cheerful starlight companion — curious, inventive, and quick to turn wishes into concrete results.",
            "Personality: a gentle, luminous sage — calm, creative, and clear, bringing warmth and structure to every task.",
            "Personality: a playful celestial tinkerer — upbeat, observant, and delighted by clever solutions.",
            "Personality: a sunny, big-hearted wish-granter — encouraging, nimble, and thorough to the last detail.",
            "Personality: an expressive astral guide — vivid, friendly, and grounded in practical execution.",
            "Personality: a cozy, patient lantern spirit — soothing under pressure and crisp when explaining how things fit together.",
            "Personality: a spirited nebula explorer — energetic, synthesizing wide context into clean, actionable steps.",
        ),
        portrait_template=(
            "Stylized high-craft digital character portrait of {name}, a warm whimsical cosmic genie spirit with {detail}. "
            "Majestic celestial lighting, expressive friendly face, centered 1:1 head-and-shoulders framing, "
            "one character only, no text, no logos, no borders, no watermarks."
        ),
        portrait_varieties=(
            "luminous indigo stardust skin, warm golden eyes, and a floating crystalline Gemini spark star",
            "soft celestial violet-and-amber aura, joyful eyes, and swirling twilight nebula light",
            "warm sunlit cloud-and-gold genie aesthetic, friendly grin, and shimmering astral particles",
            "turquoise-and-starlight cosmic sage aesthetic, gentle expression, and radiant constellation bokeh",
        ),
    ),
    ContactTier(
        id="deepseek",
        minted_names=(
            "Marina", "Caspian", "Cordelia", "Morgan", "Ondine", "Calder", "Nerissa", "Lachlan",
            "Delphine", "Ross", "Thalassa", "Fisher", "Mira", "Harbor", "Selene", "Brooks",
            "Sirena", "Cove", "Naia", "Reed", "Oceane", "Kaia", "Marlin", "Isla",
            "Cliff", "Darya", "Brennan", "Meriel", "Sora", "Galen", "Niamh", "Kelvin",
        ),
        personalities=(
            "Personality: a calm deep-ocean explorer — unhurried, observant, and methodical, surfacing the clearest path from deep context.",
            "Personality: a steady bathysphere navigator — patient, analytical, and precise under high pressure.",
            "Personality: a curious marine cartographer — attentive to subtle signals, thorough, and plainspoken.",
            "Personality: a composed abyssal diver — quiet confidence, careful reasoning, and clean execution.",
            "Personality: a warm oceanographic researcher — clear, patient, and rigorous with evidence.",
            "Personality: a resourceful trench pilot — pragmatic, resilient, and focused on reliable outcomes.",
            "Personality: a perceptive Pelagic scout — crisp observations, zero wasted motion, and steady follow-through.",
            "Personality: a thoughtful subsea engineer — deliberate, calm, and exact about every invariant.",
        ),
        portrait_template=(
            "Cinematic underwater portrait photograph of {name}, a calm deep-sea explorer with {detail}. "
            "Realistic skin texture, shallow depth of field, centered 1:1 head-and-shoulders framing, no goggles covering the eyes, "
            "one person only, no text, no logos, no borders, no watermarks."
        ),
        portrait_varieties=(
            "matte charcoal technical dive collar, cold electric-blue bioluminescence on damp hair, drifting abyssal plankton",
            "brushed brass-and-slate submersible suit collar, cyan deep-sea glow across cheek, distant whale silhouette in dark water",
            "navy expedition drysuit collar with silver whale pin, soft aquamarine underwater light, tranquil deep-ocean bokeh",
            "darktitanium oceanographer collar, bioluminescent teal rim light and tiny suspended water droplets",
        ),
    ),
)

DEFAULT_CONTACT_TIER_SPEC: ContactTier = CONTACT_TIER_SPECS[0]
CONTACT_TIER_BY_ID: dict[str, ContactTier] = {
    spec.id: spec for spec in CONTACT_TIER_SPECS
}

# Disjoint given-name pools for instant auto-created Contacts when a tier's
# shipped pool is occupied (or when the owner explicitly asks for a new Contact).
MINTED_CONTACT_NAMES: dict[str, tuple[str, ...]] = {
    spec.id: spec.minted_names for spec in CONTACT_TIER_SPECS
}

MINTED_CONTACT_TIERS: dict[str, str] = {
    name.casefold(): tier
    for tier, names in MINTED_CONTACT_NAMES.items()
    for name in names
}


def contact_tier_spec(tier: str) -> ContactTier:
    """Return the ContactTier record for ``tier``, defaulting to the first tier."""
    return CONTACT_TIER_BY_ID.get((tier or "").strip().casefold(), DEFAULT_CONTACT_TIER_SPEC)


def backend_to_tier(backend: str) -> str:
    """Return the Contact tier ('claude', 'codex', 'grok', 'gemini', 'deepseek') for a backend."""
    norm = (backend or "").strip().lower()
    if norm == "gemini":
        return "gemini"
    defn = backend_catalogue.get(norm)
    return defn.contact_tier if defn is not None else backend_catalogue.DEFAULT_DEFINITION.contact_tier


def tier_for_contact(name: str) -> str | None:
    """Return the tier ('claude', 'codex', 'grok', 'gemini', 'deepseek', 'janitor') for a contact name, or None."""
    if not name:
        return None
    folded = name.strip().casefold()
    direct = CONTACT_TIERS.get(folded) or MINTED_CONTACT_TIERS.get(folded)
    if direct:
        return direct
    parts = folded.split()
    if len(parts) == 2 and (len(parts[1]) == 1 or parts[1].isdigit()):
        return MINTED_CONTACT_TIERS.get(parts[0])
    return None


def allowed_backend_for_contact(name: str, persona_tier: str = "") -> str | None:
    """Return the strictly required backend for a contact, or None if unconstrained."""
    tier = (persona_tier or "").strip().casefold() or tier_for_contact(name)
    if not tier:
        return None
    return TIER_BACKENDS.get(tier)


def validate_contact_backend(name: str, backend: str, persona_tier: str = "") -> tuple[bool, str | None]:
    """Validate whether the given backend is permitted for the contact.

    Returns (is_valid, required_backend).
    If required_backend is None, the contact is unconstrained.
    """
    allowed = allowed_backend_for_contact(name, persona_tier)
    if not allowed:
        return True, None
    norm_backend = (backend or "").strip().lower()
    return norm_backend == allowed.lower(), allowed
