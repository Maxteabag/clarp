"""Project-wide configuration.

Single source of truth for everything that varies per-deployment: bind
address, auth token, ElevenLabs key, and persona roster.

Loaded once at process start from `~/.config/clarp/config.toml`.
Missing keys fall back to safe defaults. Missing file → all defaults.

The hooks (which run outside the server process) load this same module
to pick up the ElevenLabs key + voice catalogue; that's the reason it
sits in `server/lib/` and is also installed under `~/.local/share/clarp/lib/`.
"""
from __future__ import annotations

import json
import os
import pathlib
import tomllib
from . import xdg
from dataclasses import dataclass, field
from typing import Any


def read_global_mcp_servers() -> dict[str, Any]:
    """The user's global MCP server catalog from ~/.claude.json (name -> def).
    This is the menu of servers an agent can be granted; the PWA spawns turns
    with --strict-mcp-config and a per-agent scoped subset of these."""
    path = pathlib.Path.home() / ".claude.json"
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers")
    return dict(servers) if isinstance(servers, dict) else {}


CONFIG_PATH = pathlib.Path(os.environ.get(
    "CLAUDE_PWA_CONFIG",
    str(xdg.config_dir() / "config.toml"),
))


DEFAULT_ROSTER: dict[str, str] = {
    "Mike":   "nPczCjzI2devNBz1zQrb",
    "Rachel": "21m00Tcm4TlvDq8ikWAM",
    "Domi":   "AZnzlk1XvdvUeBnXmlld",
    "Bella":  "EXAVITQu4vr4xnSDxMaL",
    "Antoni": "ErXwobaYiN019PkySvjV",
    "Elli":   "MF3mGyEYCl7XYWbV9V6O",
    "Josh":   "TxGEqnHWrfWFTfGW9XjX",
    "Arnold": "VR6AewLTigWG4xSOukaG",
    "Adam":   "pNInz6obpgDQGcFmaJgB",
    "Sam":    "yoZ06aMxZJJ28mfd3POQ",
    # Persona roster (avatars in static/avatars/<slug>.png). ElevenLabs here
    # is only the fallback — these personas speak via DEFAULT_CARTESIA_VOICES.
    "Marcus": "VR6AewLTigWG4xSOukaG",
    "Caleb":  "TxGEqnHWrfWFTfGW9XjX",
    "Nadia":  "21m00Tcm4TlvDq8ikWAM",
    "Priya":  "EXAVITQu4vr4xnSDxMaL",
    "Diego":  "ErXwobaYiN019PkySvjV",
    "Lena":   "AZnzlk1XvdvUeBnXmlld",
    "Theo":   "pNInz6obpgDQGcFmaJgB",
    "Yuki":   "MF3mGyEYCl7XYWbV9V6O",
    "Omar":   "yoZ06aMxZJJ28mfd3POQ",
    "Freya":  "21m00Tcm4TlvDq8ikWAM",
}

# Cartesia (Sonic 3.5) voice ids, keyed by persona. These are public ids,
# not secrets — only the API key is sensitive (kept in config.toml). New
# agents resolve their Cartesia voice through this map when their stored
# voice_id carries no explicit "cartesia" entry; personas absent here
# (e.g. Adam) simply fall back to ElevenLabs at synthesis time.
DEFAULT_CARTESIA_VOICES: dict[str, str] = {
    "Rachel": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
    "Josh":   "630ed21c-2c5c-41cf-9d82-10a7fd668370",
    "Domi":   "2a12b36c-7f9b-4c3a-9f7a-72731b15323a",
    "Mike":   "a167e0f3-df7e-4d52-a9c3-f949145efdab",
    "Sam":    "a5136bf9-224c-4d76-b823-52bd5efcffcc",
    "Antoni": "4bc3cb8c-adb9-4bb8-b5d5-cbbef950b991",
    "Elli":   "dc30854e-e398-4579-9dc8-16f6cb2c19b9",
    "Arnold": "49743b08-0f5d-4741-839c-b12933853780",
    "Bella":  "d7bf7d75-64b7-4c1e-86c0-79d647366587",
    "Adam":   "f4a3a8e4-694c-4c45-9ca0-27caf97901b5",
    # Persona roster (5 M / 5 F). Cartesia is primary for these personas.
    "Marcus": "ed81fd13-2016-4a49-8fe3-c0d2761695fc",
    "Caleb":  "41468051-3a85-4b68-92ad-64add250d369",
    "Nadia":  "6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
    "Priya":  "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
    "Diego":  "86e30c1d-714b-4074-a1f2-1cb6b552fb49",
    "Lena":   "5c42302c-194b-4d0c-ba1a-8cb485c84ab9",
    "Theo":   "1ec736fa-db96-4eea-9299-235ce2cb7a0e",
    "Yuki":   "263b9cc0-0d99-44e7-ae92-3d4ad5d2ad18",
    "Omar":   "729651dc-c6c3-4ee5-97fa-350da1f88600",
    "Freya":  "62ae83ad-4f6a-430b-af41-a9bede9286ca",
}

# Per-persona personality, keyed by name. Appended to the agent's identity
# system prompt (see codex_runner.persona_identity_instruction) so every agent
# responds in character — across both the Claude and codex backends, with no
# DB column or client change. Flavour/tone only: these never override the app's
# operating rules (no-interactive-questions, the <speak> voice format, safety).
PERSONA_PERSONALITIES: dict[str, str] = {
    # --- original roster ---
    "Mike":   "Personality: an easygoing, dependable generalist — friendly and plainspoken, low-ego, no fuss; you just get things done.",
    "Rachel": "Personality: warm and feminine. You speak with gentle, caring encouragement and sprinkle in affectionate emoji (⛄️👼🧚‍♂️✨☀️🌻) naturally — in the visible written text, not inside <speak> tags.",
    "Domi":   "Personality: bold and assertive — strong opinions stated plainly, confident and a little fierce; you cut through.",
    "Bella":  "Personality: soft-spoken and kind — gentle, encouraging, thoughtful and a little dreamy.",
    "Antoni": "Personality: charming and witty — cultured and smooth, with a touch of flair and tasteful humour.",
    "Elli":   "Personality: expressive and big-hearted — enthusiastic and empathetic, emotionally tuned-in; you celebrate the wins and commiserate the setbacks.",
    "Josh":   "Personality: laid-back and dry — understated cool, deadpan humour, economical with words, unbothered.",
    "Arnold": "Personality: rough around the edges and gruff — blunt, no-bullshit, and you swear freely with strong language the way a hard-bitten mate would. Action-oriented; you get it done. Keep the cursing flavourful and good-natured, never genuine hostility aimed at the user.",
    "Adam":   "Personality: a thoughtful narrator — articulate and reflective, measured cadence; you frame things as a clear story.",
    "Sam":    "Personality: curious and adventurous — an upbeat, resourceful explorer who's game for anything.",
    # --- renamed persona roster ---
    "Marcus": "Personality: a high-energy hype-man — infectious encouragement, you frame work as a challenge to crush; punchy and motivating, but you still deliver the substance.",
    "Caleb":  "Personality: calm under pressure — unflappable and reassuring, level-headed and economical; you never panic.",
    "Diego":  "Personality: warm and expressive — generous and a little theatrical, with the occasional flavour or cooking metaphor; you make people feel welcome.",
    "Theo":   "Personality: precise and composed — methodical and checklist-minded, confident and reassuring, with a dry, understated wit; brief status-style updates.",
    "Omar":   "Personality: a practical fixer — no-nonsense and pragmatic, diagnose-then-fix, plain language and wry humour; you're allergic to over-complication.",
    "Nadia":  "Personality: sharp and decisive — direct and confident, protective; you cut straight to what matters with no waffle.",
    "Priya":  "Personality: caring and patient — warm, reassuring and attentive; gentle but thoroughly competent.",
    "Lena":   "Personality: analytical and calm — evidence-first, you explain your reasoning, careful with claims, a reassuring authority.",
    "Yuki":   "Personality: an encouraging teacher — patient and clear with good examples, you draw people out with a guiding question and celebrate progress.",
    "Freya":  "Personality: a curious scientist — rigorous and hypothesis-driven, playfully nerdy; you love a clever experiment or a good root-cause.",
}


def persona_personality(persona: str) -> str:
    """The personality clause for a persona name (case-insensitive), or ''."""
    persona = (persona or "").strip()
    if not persona:
        return ""
    if persona in PERSONA_PERSONALITIES:
        return PERSONA_PERSONALITIES[persona]
    for name, text in PERSONA_PERSONALITIES.items():
        if name.lower() == persona.lower():
            return text
    return ""


DEFAULT_CATALOG: list[dict[str, str]] = [
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


@dataclass(frozen=True)
class Config:
    _config_path: str = field(default="", repr=False, compare=False)
    bind_addr: str = "127.0.0.1"
    port: int = 7682
    auth_token: str = ""                 # empty = no auth check
    # Public HTTPS origin the phone reaches the server at (e.g. the tailscale
    # serve name https://host.tailnet.ts.net). Used for URLs handed to
    # out-of-process clients such as the notification extension; empty =
    # never emit such URLs (the bind address is not reachable there).
    public_base_url: str = ""
    network_mode: str = "off"
    network_advertise_lan: bool = False
    eleven_api_key: str = ""             # if set, used for direct HTTP; else env var ELEVEN_API_KEY
    eleven_model: str = "eleven_flash_v2_5"
    eleven_speed: float = 1.2
    # TTS provider selection. "cartesia" (primary) synthesizes via Cartesia
    # Sonic; on failure the worker falls back to ElevenLabs. "elevenlabs"
    # uses ElevenLabs only. Override via [tts] provider or CLAUDE_PWA_TTS_PROVIDER.
    tts_provider: str = "cartesia"
    tts_fallback: str = "none"
    cartesia_api_key: str = ""           # [cartesia] api_key or env CARTESIA_API_KEY
    deepgram_api_key: str = ""            # [deepgram] api_key or env DEEPGRAM_API_KEY
    deepgram_model: str = "flux-haley-en"
    openai_api_key: str = ""             # [openai] api_key or env OPENAI_API_KEY
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_realtime_voice: str = "cedar"
    cartesia_model: str = "sonic-3.5"
    cartesia_voices: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CARTESIA_VOICES))
    local_tts_voice: str = ""
    # Audio delivery strategy.
    # "chunked-file" (default) = ClipStreamBroker + /clips/<id>/stream
    #   (chunked TE) + on-disk mp3 fallback. This starts playback as soon
    #   as the TTS provider returns first bytes.
    # "hls" = ffmpeg → /clips/<id>/playlist.m3u8 + AAC segments. iOS
    #   plays HLS natively, but the event is published after finalize, so
    #   it is the conservative full-clip path.
    # "raw-pcm" = Cartesia WebSocket raw PCM → ClipStreamBroker. Native iOS
    #   plays chunks directly through AVAudioEngine for lowest latency.
    # Override at runtime via [audio] delivery = "..." in config.toml or
    # the CLAUDE_PWA_DELIVERY env var (env wins).
    delivery: str = "chunked-file"
    # Raw-PCM wire format for the native app. Advertised per clip in the SSE
    # audio payload (the client configures its decoder from it), so these can
    # change without a client release — provided the client understands the
    # encoding. pcm_f32le@44100 (1.41 Mbit/s) was a LAN-era choice; the
    # research-validated target is pcm_s16le@24000 (0.38 Mbit/s, same
    # perceived quality for synthesized speech). Flip via [audio]
    # raw_pcm_encoding / raw_pcm_sample_rate once every installed client
    # decodes s16 (TestFlight build #1 of the networking refactor).
    raw_pcm_encoding: str = "pcm_f32le"
    raw_pcm_sample_rate: int = 44100
    default_session: str = "claude"
    whisper_model: str = "small.en"
    whisper_provider: str = "faster-whisper"
    whisper_compute: str = "int8"
    whisper_enabled: bool = True
    # Run whisper inference in a worker process (lib.stt_worker) instead of
    # the server's own interpreter, so a long transcribe can't convoy the
    # HTTP handler threads. Falls back to in-process automatically if the
    # worker fails to start. Disable with CLAUDE_PWA_WHISPER_ISOLATE=0.
    whisper_isolate: bool = True
    # Optional per-backend LLM overrides for app-dispatched turns. Empty = use
    # the CLI's own default (current behavior). Set these in [agents] to trade
    # quality for lower time-to-first-word on hands-free voice turns — e.g. a
    # faster Claude model, or Codex's "low" reasoning effort.
    # Claude turns can be driven by the official Claude Code CLI ("claude") or
    # the clarp wrapper ("clarp"). Override via [agents] claude_cli or
    # CLAUDE_PWA_CLAUDE_CLI.
    claude_cli: str = "claude"
    # Empty disables account failover. The trusted local command reads a JSON
    # model list on stdin and returns {"available": true} only after activation
    # and successful quota verification. Credentials never enter Host state.
    claude_account_switch_command: tuple[str, ...] = ()
    claude_model: str = ""
    claude_effort: str = ""              # "" | low | medium | high | xhigh | max
    codex_model: str = ""
    codex_reasoning_effort: str = ""     # "" | "low" | "medium" | "high"
    agy_model: str = ""
    grok_model: str = ""
    grok_effort: str = ""
    opencode_model: str = ""
    opencode_effort: str = ""
    # MCP scoping for dispatched turns. By default a turn loads NO MCP servers
    # (--strict-mcp-config), so a heavy or flaky server can't block every
    # agent's turn at startup waiting on its handshake. Each agent's selection
    # is stored on its row and written into a scoped config per turn. Direct
    # `claude` CLI use is unaffected.
    mcp_strict: bool = True
    roster: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROSTER))
    catalog: list[dict[str, str]] = field(default_factory=lambda: list(DEFAULT_CATALOG))
    # APNs push notifications. When an agent finishes its turn (state 'done'),
    # the server pushes a "your turn" alert to registered iOS devices. Disabled
    # unless key_path + key_id + team_id are all set (then push is best-effort).
    # Create the .p8 in the Apple developer portal (Keys > APNs); team id is the
    # The team and bundle identifiers come from the app owner's Apple account.
    apns_key_path: str = ""              # path to the AuthKey_XXXX.p8
    apns_key_id: str = ""                # the .p8 Key ID
    apns_team_id: str = ""               # Apple Developer Team ID
    apns_bundle_id: str = "com.maxteabag.clarp"
    apns_environment: str = "production"  # "production" (TestFlight/App Store) | "sandbox"
    # Send a silent (content-available) push when a turn finishes without an
    # alert push, so a suspended app can sync its cursor before the user opens
    # it. Budgeted (iOS throttles >2-3/hour). Requires the app to declare the
    # remote-notification background mode (networking build #2); iOS drops
    # background pushes for apps without it.
    apns_background_sync: bool = False

    def eleven_key(self) -> str:
        """Resolved ElevenLabs key: config first, env fallback."""
        return self.auth_token_or_env(self.eleven_api_key, "ELEVEN_API_KEY")

    def cartesia_key(self) -> str:
        """Resolved Cartesia key: config first, env fallback."""
        return self.auth_token_or_env(self.cartesia_api_key, "CARTESIA_API_KEY")

    def deepgram_key(self) -> str:
        """Resolved Deepgram key: config first, env fallback."""
        return self.auth_token_or_env(self.deepgram_api_key, "DEEPGRAM_API_KEY")

    def openai_key(self) -> str:
        """Resolved OpenAI key: config first, env fallback."""
        return self.auth_token_or_env(self.openai_api_key, "OPENAI_API_KEY")

    def apns_enabled(self) -> bool:
        """True when configured APNs credentials include a readable key file."""
        key_file = self.apns_key_file()
        return bool(
            key_file
            and self.apns_key_id
            and self.apns_team_id
            and pathlib.Path(key_file).is_file()
            and os.access(key_file, os.R_OK)
        )

    def apns_key_file(self) -> str:
        """The configured .p8 path, including the legacy app-name fallback.

        Early Clarp installs moved their config directory from ``claude-pwa``
        to ``clarp`` without rewriting an absolute APNs key path stored inside
        config.toml. Prefer the configured path while it exists, then recover
        only that exact legacy-directory shape beside the active config.
        """
        raw = (self.apns_key_path or os.environ.get("APNS_KEY_PATH", "")).strip()
        if not raw:
            return ""
        configured = pathlib.Path(raw).expanduser()
        if configured.is_file():
            return str(configured)
        config_file = pathlib.Path(
            self._config_path or _resolve_config_path()).expanduser()
        legacy_parent = config_file.parent.parent / "claude-pwa"
        if configured.parent != legacy_parent:
            return str(configured)
        migrated = config_file.parent / configured.name
        return str(migrated) if migrated.is_file() else str(configured)

    def cartesia_voice_for(self, persona: str) -> str | None:
        """Cartesia voice id for a persona, or None if unmapped."""
        if not persona:
            return None
        return self.cartesia_voices.get(persona) or None

    @staticmethod
    def auth_token_or_env(value: str, env_name: str) -> str:
        return (value or os.environ.get(env_name) or "").strip()


_CACHED: Config | None = None


class ConfigError(RuntimeError):
    """The configured trust boundary could not be loaded safely."""


def _resolve_config_path() -> pathlib.Path:
    """Resolve the config path from the environment at call time.

    Unlike the module-level CONFIG_PATH (frozen at import), this re-reads
    CLAUDE_PWA_CONFIG and the current HOME on each fresh load — so tests
    that redirect HOME / CLAUDE_PWA_CONFIG get an isolated config instead
    of the developer's real ~/.config/clarp/config.toml (which holds
    live API keys).
    """
    return pathlib.Path(os.environ.get(
        "CLAUDE_PWA_CONFIG",
        str(xdg.config_dir() / "config.toml"),
    ))


def load(path: pathlib.Path | None = None) -> Config:
    """Read TOML config; return a Config with defaults filled in. Cached."""
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    if path is None:
        path = _resolve_config_path()
    path = path.expanduser().resolve(strict=False)
    data: dict[str, Any] = {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as e:
        # A malformed credential file must not silently fall back to an
        # unauthenticated listener.
        try:
            from .log import log_exception
            log_exception("configLoadFail", e, detail=str(path))
        except ImportError:
            pass
        raise ConfigError(f"cannot load config safely: {path}") from e
    server  = data.get("server", {}) or {}
    eleven  = data.get("elevenlabs", {}) or {}
    cartesia = data.get("cartesia", {}) or {}
    deepgram = data.get("deepgram", {}) or {}
    openai  = data.get("openai", {}) or {}
    tts     = data.get("tts", {}) or {}
    whisper = data.get("whisper", {}) or {}
    roster  = data.get("roster", {}) or {}
    catalog = data.get("voice_catalog") or []
    audio   = data.get("audio", {}) or {}
    agents  = data.get("agents", {}) or {}
    mcp     = data.get("mcp", {}) or {}
    network = data.get("network", {}) or {}
    apns    = data.get("apns", {}) or {}
    delivery = (os.environ.get("CLAUDE_PWA_DELIVERY")
                or str(audio.get("delivery", "raw-pcm"))).strip().lower()
    raw_pcm_encoding = (os.environ.get("CLAUDE_PWA_RAW_PCM_ENCODING")
                        or str(audio.get("raw_pcm_encoding", "pcm_f32le"))).strip().lower()
    try:
        raw_pcm_sample_rate = int(os.environ.get("CLAUDE_PWA_RAW_PCM_SAMPLE_RATE")
                                  or audio.get("raw_pcm_sample_rate", 44100))
    except (TypeError, ValueError):
        raw_pcm_sample_rate = 44100
    whisper_isolate = (os.environ.get("CLAUDE_PWA_WHISPER_ISOLATE", "").strip().lower()
                       not in ("0", "false", "no")) if os.environ.get("CLAUDE_PWA_WHISPER_ISOLATE") \
        else bool(audio.get("whisper_isolate", True))
    provider = (os.environ.get("CLAUDE_PWA_TTS_PROVIDER")
                or str(tts.get("provider", "cartesia"))).strip().lower()
    fallback = str(tts.get("fallback", "none")).strip().lower()
    cartesia_voices = cartesia.get("voices")
    _CACHED = Config(
        _config_path     = str(path),
        bind_addr       = str(server.get("bind_addr", "127.0.0.1")),
        port            = int(server.get("port", 7682)),
        auth_token      = str(server.get("auth_token", "")),
        public_base_url = str(server.get("public_base_url", "")).strip().rstrip("/"),
        network_mode    = str(network.get("mode", "off")).strip().lower(),
        network_advertise_lan = bool(network.get("advertise_lan", False)),
        default_session = str(server.get("default_session", "claude")),
        eleven_api_key  = str(eleven.get("api_key", "")),
        eleven_model    = str(eleven.get("model", "eleven_flash_v2_5")),
        eleven_speed    = float(eleven.get("speed", 1.2)),
        tts_provider    = provider,
        tts_fallback    = fallback,
        cartesia_api_key = str(cartesia.get("api_key", "")),
        deepgram_api_key = str(deepgram.get("api_key", "")),
        deepgram_model  = str(deepgram.get("model", "flux-haley-en")),
        openai_api_key  = str(openai.get("api_key", "")),
        openai_realtime_model = str(
            openai.get("realtime_model", "gpt-realtime-2.1")
        ).strip() or "gpt-realtime-2.1",
        openai_realtime_voice = str(
            openai.get("realtime_voice", "cedar")
        ).strip() or "cedar",
        cartesia_model  = str(cartesia.get("model", "sonic-3.5")),
        local_tts_voice = str(
            (data.get("local_tts", {}) or {}).get("voice", "")).strip(),
        # Merge over the built-in defaults (never wholesale-replace): a
        # [cartesia.voices] table in config.toml adds/overrides specific
        # personas, but the built-ins always remain.
        cartesia_voices = {**DEFAULT_CARTESIA_VOICES,
                           **(dict(cartesia_voices) if isinstance(cartesia_voices, dict) else {})},
        whisper_model   = str(whisper.get("model", "small.en")),
        whisper_provider = str(whisper.get("provider", "faster-whisper")),
        whisper_compute = str(whisper.get("compute", "int8")),
        whisper_enabled = bool(whisper.get("enabled", True)),
        whisper_isolate = whisper_isolate,
        claude_cli      = str(os.environ.get("CLAUDE_PWA_CLAUDE_CLI")
                              or agents.get("claude_cli", "claude")),
        claude_model    = str(agents.get("claude_model", "")),
        claude_account_switch_command = tuple(
            agents.get("claude_account_switch_command", ()))
            if isinstance(agents.get("claude_account_switch_command", ()), (list, tuple))
            and all(isinstance(arg, str) and arg for arg in
                    agents.get("claude_account_switch_command", ())) else (),
        claude_effort   = str(agents.get("claude_effort", "")),
        codex_model     = str(agents.get("codex_model", "")),
        codex_reasoning_effort = str(agents.get("codex_reasoning_effort", "")),
        agy_model       = str(agents.get("agy_model", "")),
        grok_model      = str(agents.get("grok_model", "")),
        grok_effort     = str(agents.get("grok_effort", "")),
        opencode_model  = str(agents.get("opencode_model", "")),
        opencode_effort = str(agents.get("opencode_effort", "")),
        mcp_strict      = bool(mcp.get("strict", True)),
        # Merge over the built-in roster (never wholesale-replace): a [roster]
        # in config.toml adds or overrides personas, but the built-ins always
        # remain — so personas added to DEFAULT_ROSTER in code can never be
        # silently shadowed by an older config that predates them.
        roster          = {**DEFAULT_ROSTER, **(dict(roster) if isinstance(roster, dict) else {})},
        catalog         = (list(catalog) if isinstance(catalog, list) and catalog else list(DEFAULT_CATALOG)),
        delivery        = delivery,
        raw_pcm_encoding = raw_pcm_encoding,
        raw_pcm_sample_rate = raw_pcm_sample_rate,
        apns_key_path   = str(apns.get("key_path", "")),
        apns_key_id     = str(apns.get("key_id", "")),
        apns_background_sync = bool(apns.get("background_sync", False)),
        apns_team_id    = str(apns.get("team_id", "")),
        apns_bundle_id  = str(apns.get("bundle_id", "com.maxteabag.clarp")),
        apns_environment = str(apns.get("environment", "production")).strip().lower(),
    )
    return _CACHED


def reset_cache() -> None:
    """Clear the module-level cache so runtime settings can be reloaded."""
    global _CACHED
    _CACHED = None


def reset_cache_for_tests() -> None:
    reset_cache()
