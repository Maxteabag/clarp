"""Agent roster and voice catalogue — pure data, no I/O.

These are deliberately constants rather than read from a config file so the
unit tests don't have to wire up filesystem fixtures for the simplest cases.
A future iteration can move them behind a `Config` class.
"""

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

# Canonical backend -> preferred Contact tier when minting a fresh Contact.
BACKEND_TIERS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "agy": "gemini",
    "gemini": "gemini",
    "opencode": "codex",
    "deepseek": "deepseek",
}

# Disjoint given-name pools for instant auto-created Contacts when a tier's
# shipped pool is occupied (or when the owner explicitly asks for a new Contact).
MINTED_CONTACT_NAMES: dict[str, tuple[str, ...]] = {
    "claude": (
        "Clara", "Ethan", "Maya", "Julian", "Nora", "Owen", "Hannah", "Liam",
        "Sofia", "Eli", "Camila", "Lucas", "Elena", "Miles", "Chloe", "Gavin",
        "Tessa", "Declan", "Audrey", "Jonah", "Ruby", "Isaac", "Sienna", "Graham",
        "Helena", "Simon", "Alice", "Victor", "Lydia", "Arthur", "Maeve", "Colin",
    ),
    "codex": (
        "Soren", "Zara", "Kieran", "Talia", "Dorian", "Anya", "Cyrus", "Maren",
        "Bastian", "Petra", "Cassian", "Linnea", "Evander", "Astrid", "Lucian", "Isolde",
        "Stellan", "Vera", "Alaric", "Sabine", "Theron", "Kestrel", "Gideon", "Yara",
        "Corbin", "Elara", "Malik", "Rhea", "Torin", "Sable", "Dante", "Cleo",
    ),
    "grok": (
        "Jett", "Sloane", "Knox", "Roxanne", "Zane", "Mavis", "Dash", "Veda",
        "Nash", "Indie", "Ace", "Darcy", "Cruz", "Stevie", "Flint", "Ramona",
        "Cole", "Juno", "Beck", "Maxine", "Ryder", "Zelda", "Brock", "Trixie",
        "Slate", "Lola", "Vaughn", "Niko", "Gwyn", "Reid", "Bree", "Trent",
    ),
    "gemini": (
        "Cosmo", "Sunny", "Clover", "Bowie", "Maple", "Kiko", "Winnie", "Otto",
        "Poppy", "Milo", "Daisy", "Ollie", "Birdie", "Louie", "Clementine", "Toby",
        "Penny", "Alfie", "Maisie", "Monty", "Goldie", "Hugo", "Bonnie", "Percy",
        "Nellie", "Barnaby", "Millie", "Kip", "Peggy", "Remy", "Zelie", "Bixby",
    ),
    "deepseek": (
        "Marina", "Caspian", "Cordelia", "Morgan", "Ondine", "Calder", "Nerissa", "Lachlan",
        "Delphine", "Ross", "Thalassa", "Fisher", "Mira", "Harbor", "Selene", "Brooks",
        "Sirena", "Cove", "Naia", "Reed", "Oceane", "Kaia", "Marlin", "Isla",
        "Cliff", "Darya", "Brennan", "Meriel", "Sora", "Galen", "Niamh", "Kelvin",
    ),
}

MINTED_CONTACT_TIERS: dict[str, str] = {
    name.casefold(): tier
    for tier, names in MINTED_CONTACT_NAMES.items()
    for name in names
}


def backend_to_tier(backend: str) -> str:
    """Return the Contact tier ('claude', 'codex', 'grok', 'gemini', 'deepseek') for a backend."""
    norm = (backend or "").strip().lower()
    return BACKEND_TIERS.get(norm, "claude")


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
