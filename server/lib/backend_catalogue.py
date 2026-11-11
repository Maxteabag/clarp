"""Inert backend identity and declared catalogue values. No runtime imports."""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType

class AgentBackend:
    """Bundled backend ids this server build knows how to run.

    The live allow-list is ``ids()`` / ``/agent-model-options``.
    Clients treat ``backend`` as an open string so a newer Host can advertise
    a provider this app binary has never seen.
    """
    CLAUDE = "claude"
    CODEX = "codex"
    AGY = "agy"
    GROK = "grok"
    OPENCODE = "opencode"
    DEEPSEEK = "deepseek"
    MUSE = "muse"

    @classmethod
    def valid(cls) -> set[str]:
        return {cls.CLAUDE, cls.CODEX, cls.AGY, cls.GROK, cls.OPENCODE, cls.DEEPSEEK}

@dataclass(frozen=True)
class BackendBrand:
    """Chooser colours as ``#rrggbb`` strings.

    Served on the catalogue so a client never has to ship a palette per CLI:
    a new backend picks its own field and tint and every app renders it.
    """
    field_top: str
    field_bottom: str
    tint_dark: str
    tint_light: str

    def as_dict(self) -> dict[str, str]:
        return {
            "field_top": self.field_top,
            "field_bottom": self.field_bottom,
            "tint_dark": self.tint_dark,
            "tint_light": self.tint_light,
        }

DEFAULT_BRAND = BackendBrand("#2a3142", "#151820", "#a8b4c8", "#4a5568")
DEFAULT_SYMBOL = "cpu"
LOGIN_KINDS = ("none", "device_code", "cli", "api_key")
EFFORT_UIS = ("picker", "hidden", "folded_into_model")
EFFORT_SCOPES = ("model", "provider", "provider_flag")

@dataclass(frozen=True)
class ModelProbe:
    """Inert discovery policy; operations and parsers bind in discovery."""
    operation: str
    parser: str
    detail: str
    source_kind: str = "cli_probe"
    argv: tuple[str, ...] = ()
    timeout: float | None = None
    cache_path: str = ""
    auth_path: str = ""
    append_fallback: bool = False
    fallback_efforts: bool = False
    remember_model_ids: bool = False


@dataclass(frozen=True)
class BackendDefinition:
    """Declared defaults only; compaction support is execution-derived."""
    id: str = ''
    label: str = ''
    required_binary: str = ''
    supports_fork: bool = False
    supports_steer: bool = False
    supports_transcript_streaming: bool = False
    supports_routing: bool = True
    efforts: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    badge: str = ''
    detail: str = ''
    symbol: str = DEFAULT_SYMBOL
    brand: BackendBrand = DEFAULT_BRAND
    hidden: bool = False
    supports_mcp: bool = False
    supports_usage: bool = False
    login_kind: str = 'none'
    effort_ui: str = 'picker'
    effort_help: str = ''
    effort_scope: str = 'provider'
    runner: str = ''
    config_model_field: str = ''
    config_effort_field: str = ''
    config_account_switch_field: str = ''
    fallback_models: tuple[tuple[str, str], ...] = ()
    fallback_model_efforts: MappingProxyType[str, tuple[str, ...]] = MappingProxyType({})
    fallback_model_defaults: MappingProxyType[str, str] = MappingProxyType({})
    resumable: bool = True
    continues_lost_conversations: bool = False
    context_window: int | None = None
    api_providers: tuple[str, ...] = ()
    native_tool_explainer: bool = False
    janitor_default_model: str = ''
    model_family: str = ''
    model_providers: bool = False
    model_id_filter: str = ''
    probe: ModelProbe | None = None

DEFAULT_DEFINITION = BackendDefinition()

CLAUDE = BackendDefinition(
    probe=ModelProbe("file", "parse_claude_models", "claude account model cache", source_kind="cli_cache", cache_path="claude_model_cache_path", auth_path="claude_model_cache_path", append_fallback=True, fallback_efforts=True),
    id='claude',
    label='Claude',
    required_binary='claude',
    badge='BackendClaude',
    detail='Runs on Claude Code.',
    symbol='sparkles',
    brand=BackendBrand('#e08b6a', '#c9603d', '#d97757', '#b85433'),
    supports_fork=True,
    supports_transcript_streaming=True,
    supports_mcp=True,
    supports_usage=True,
    login_kind='cli',
    efforts=('low', 'medium', 'high', 'xhigh', 'max'),
    effort_scope='model',
    runner='clarp',
    config_model_field='claude_model',
    config_effort_field='claude_effort',
    config_account_switch_field='claude_account_switch_command',
    context_window=1000000,
    fallback_models=(
        ('fable', 'Fable'),
        ('opus', 'Opus'),
        ('sonnet', 'Sonnet'),
        ('haiku', 'Haiku'),
        ('claude-fable-5-2', 'Claude Fable 5.2'),
            ('claude-fable-5-1', 'Claude Fable 5.1'),
        ('claude-opus-5-5', 'Claude Opus 5.5'),
        ('claude-opus-5', 'Claude Opus 5'),
        ('claude-sonnet-5-5', 'Claude Sonnet 5.5'),
        ('claude-sonnet-5', 'Claude Sonnet 5'),
        ('claude-haiku-4-5', 'Claude Haiku 4.5'),
        ('claude-opus-4-8', 'Claude Opus 4.8'),
        ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
    ),
)

_CODEX_ULTRA_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_CODEX_MAX_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
_CODEX_XHIGH_EFFORTS = ('low', 'medium', 'high', 'xhigh')

CODEX = BackendDefinition(
    probe=ModelProbe(
        "command",
        "parse_codex_models",
        "codex debug models",
        argv=("debug", "models"),
        timeout=2.5,
        auth_path="codex_auth_path",
        append_fallback=True,
        fallback_efforts=True,
    ),
    id='codex',
    label='Codex',
    required_binary='codex',
    badge='BackendCodex',
    detail='Runs on the Codex CLI.',
    symbol='terminal',
    brand=BackendBrand('#2b2f3c', '#14161d', '#c0caf5', '#3c4257'),
    supports_fork=True,
    supports_steer=True,
    supports_usage=True,
    login_kind='device_code',
    efforts=_CODEX_ULTRA_EFFORTS,
    effort_scope='model',
    runner='codex',
    config_model_field='codex_model',
    config_effort_field='codex_reasoning_effort',
    config_account_switch_field='codex_account_switch_command',
    api_providers=('openai',),
    native_tool_explainer=True,
    janitor_default_model='gpt-6-sol',
    model_family='codex',
    fallback_models=(
        ('gpt-6.1-sol', 'GPT-6.1 Sol'),
        ('gpt-6-astra', 'GPT-6 Astra'),
        ('gpt-6.1-terra', 'GPT-6.1 Terra'),
        ('gpt-6-sol', 'GPT-6 Sol'),
        ('gpt-6-luna', 'GPT-6 Luna'),
        ('gpt-5.6-sol', 'GPT-5.6 Sol'),
        ('gpt-5.6-terra', 'GPT-5.6 Terra'),
        ('gpt-5.6-luna', 'GPT-5.6 Luna'),
        ('gpt-5.5', 'GPT-5.5'),
    ),
    fallback_model_efforts=MappingProxyType({
        'gpt-6.1-sol': _CODEX_ULTRA_EFFORTS,
        'gpt-6-astra': _CODEX_ULTRA_EFFORTS,
        'gpt-6.1-terra': _CODEX_ULTRA_EFFORTS,
        'gpt-6-sol': _CODEX_ULTRA_EFFORTS,
        'gpt-6-luna': _CODEX_MAX_EFFORTS,
        'gpt-5.6-sol': _CODEX_ULTRA_EFFORTS,
        'gpt-5.6-terra': _CODEX_ULTRA_EFFORTS,
        'gpt-5.6-luna': _CODEX_MAX_EFFORTS,
        'gpt-5.5': _CODEX_XHIGH_EFFORTS,
    }),
    fallback_model_defaults=MappingProxyType({
        'gpt-6.1-sol': 'medium',
        'gpt-6-astra': 'medium',
        'gpt-6.1-terra': 'low',
        'gpt-6-sol': 'medium',
        'gpt-6-luna': 'medium',
        'gpt-5.6-sol': 'medium',
        'gpt-5.6-terra': 'medium',
        'gpt-5.6-luna': 'medium',
        'gpt-5.5': 'medium',
    }),
)

AGY = BackendDefinition(
    probe=ModelProbe("command", "parse_agy_models", "agy models", argv=("models",), timeout=5.0, remember_model_ids=True),
    id='agy',
    label='Antigravity',
    required_binary='agy',
    continues_lost_conversations=True,
    aliases=('antigravity',),
    badge='BackendAntigravity',
    detail='Runs on Antigravity.',
    symbol='circle.hexagongrid',
    brand=BackendBrand('#1d2742', '#0e1424', '#4c8ef7', '#2b6ed6'),
    efforts=('low', 'medium', 'high'),
    effort_ui='folded_into_model',
    effort_help='Included in model choice',
    effort_scope='provider_flag',
    runner='agy',
    config_model_field='agy_model',
    model_family='gemini',
    fallback_models=(
        ('gemini-4-carbon-high', 'Gemini 4 Carbon (High)'),
        ('gemini-4-carbon-medium', 'Gemini 4 Carbon (Medium)'),
        ('gemini-4-carbon-low', 'Gemini 4 Carbon (Low)'),
        ('gemini-4-argon-high', 'Gemini 4 Argon (High)'),
        ('gemini-4-argon-medium', 'Gemini 4 Argon (Medium)'),
        ('gemini-4-argon-low', 'Gemini 4 Argon (Low)'),
        ('gemini-3.8-flash-high', 'Gemini 3.8 Flash (High)'),
        ('gemini-3.8-flash-medium', 'Gemini 3.8 Flash (Medium)'),
        ('gemini-3.8-flash-low', 'Gemini 3.8 Flash (Low)'),
        ('gemini-3.7-flash-high', 'Gemini 3.7 Flash (High)'),
        ('gemini-3.7-flash-medium', 'Gemini 3.7 Flash (Medium)'),
        ('gemini-3.7-flash-low', 'Gemini 3.7 Flash (Low)'),
        ('gemini-3.6-flash-high', 'Gemini 3.6 Flash (High)'),
        ('gemini-3.6-flash-medium', 'Gemini 3.6 Flash (Medium)'),
        ('gemini-3.6-flash-low', 'Gemini 3.6 Flash (Low)'),
        ('gemini-3.1-pro-high', 'Gemini 3.1 Pro (High)'),
        ('gemini-3.1-pro-low', 'Gemini 3.1 Pro (Low)'),
        ('claude-opus-5-5-low', 'Claude Opus 5.5 (Low)'),
        ('claude-opus-5-5-medium', 'Claude Opus 5.5 (Medium)'),
        ('claude-opus-5-5-high', 'Claude Opus 5.5 (High)'),
        ('claude-sonnet-5-5-low', 'Claude Sonnet 5.5 (Low)'),
        ('claude-sonnet-5-5-medium', 'Claude Sonnet 5.5 (Medium)'),
        ('claude-sonnet-5-5-high', 'Claude Sonnet 5.5 (High)'),
        ('gpt-oss-120b-medium', 'GPT-OSS 120B (Medium)'),
    ),
)

GROK = BackendDefinition(
    probe=ModelProbe("command", "parse_grok_models", "grok models", argv=("models",), timeout=5.0),
    id='grok',
    label='Grok',
    required_binary='grok',
    badge='BackendGrok',
    detail='Runs on Grok Build.',
    symbol='x.circle',
    brand=BackendBrand('#1a1a1a', '#0a0a0a', '#e8e8e8', '#222222'),
    efforts=('low', 'medium', 'high'),
    runner='grok',
    config_model_field='grok_model',
    config_effort_field='grok_effort',
    model_family='grok',
    login_kind='device_code',
    fallback_models=(
        ('grok-4.6', 'Grok 4.6'),
        ('grok-4.5', 'Grok 4.5'),
    ),
)

OPENCODE = BackendDefinition(
    probe=ModelProbe("listing", "parse_opencode_models", "opencode models"),
    id='opencode',
    label='OpenCode',
    required_binary='opencode',
    aliases=('open-code', 'opencode-ai'),
    badge='BackendOpenCode',
    detail='Runs on OpenCode.',
    symbol='chevron.left.forwardslash.chevron.right',
    brand=BackendBrand('#16352b', '#0b1c16', '#5ee4b5', '#1f8a65'),
    efforts=('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'),
    effort_scope='model',
    runner='opencode',
    config_model_field='opencode_model',
    config_effort_field='opencode_effort',
    model_providers=True,
    fallback_models=(
        ('opencode/big-pickle', 'Big Pickle'),
        ('huggingface/deepseek-ai/DeepSeek-V4.1-Flash', 'DeepSeek V4.1 Flash (Hugging Face)'),
        ('fireworks-ai/accounts/fireworks/models/kimi-k3', 'Kimi K3 (Fireworks)'),
        ('huggingface/zai-org/GLM-5.3', 'GLM-5.3 (Hugging Face)'),
    ),
)

DEEPSEEK = replace(OPENCODE,
    probe=ModelProbe("listing", "parse_deepseek_models", "opencode models (deepseek)"),
    id='deepseek',
    label='DeepSeek',
    required_binary='opencode',
    aliases=('deep-seek',),
    badge='BackendDeepSeek',
    detail='Runs DeepSeek models through OpenCode.',
    symbol='water.waves',
    brand=BackendBrand('#4d6bfe', '#2b47d6', '#6f88ff', '#3554e6'),
    config_model_field='deepseek_model',
    config_effort_field='deepseek_effort',
    model_family='deepseek',
    model_id_filter='deepseek',
    fallback_models=(
        ('fireworks-ai/accounts/fireworks/routers/deepseek-flash-latest', 'DeepSeek Flash (latest, Fireworks)'),
        ('fireworks-ai/accounts/fireworks/models/deepseek-v4p1-flash', 'DeepSeek V4.1 Flash (Fireworks)'),
        ('huggingface/deepseek-ai/DeepSeek-V4-Pro', 'DeepSeek V4 Pro (Hugging Face)'),
        ('huggingface/deepseek-ai/DeepSeek-V4.1-Flash', 'DeepSeek V4.1 Flash (Hugging Face)'),
    ),
)

MUSE = BackendDefinition(
    probe=ModelProbe("directory", "parse_muse_models", "muse model catalogue cache", source_kind="cli_cache", cache_path="muse_model_catalog_dir"),
    id='muse',
    label='Muse Code',
    required_binary='muse',
    aliases=('muse-code', 'musecode'),
    badge='BackendMuse',
    detail='Runs on Meta Muse Code.',
    symbol='sparkles',
    brand=BackendBrand('#1c2b4a', '#0d1526', '#8fb4ff', '#2b4a8a'),
    efforts=('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    effort_scope='model',
    runner='muse',
    config_model_field='muse_model',
    config_effort_field='muse_effort',
    model_family='muse',
    fallback_models=(
        ('muse-wonder-1.0', 'Muse Wonder 1.0'),
    ),
)

DEFINITIONS = (CLAUDE, CODEX, AGY, GROK, OPENCODE, DEEPSEEK, MUSE,)
_BY_ID = MappingProxyType({row.id: row for row in DEFINITIONS})
_ALIASES = MappingProxyType({alias: row.id for row in DEFINITIONS for alias in row.aliases})
DEFAULT = AgentBackend.CLAUDE

def ids() -> tuple[str, ...]:
    return tuple(_BY_ID)

def normalize(backend: str | None) -> str:
    b = (backend or "").strip().lower()
    b = _ALIASES.get(b, b)
    return b if b in _BY_ID else DEFAULT

def is_valid(backend: str | None) -> bool:
    b = (backend or "").strip().lower()
    return _ALIASES.get(b, b) in _BY_ID

def get(backend: str | None) -> BackendDefinition | None:
    return _BY_ID[normalize(backend)] if is_valid(backend) else None

def for_provider(provider: str) -> str:
    for row in DEFINITIONS:
        if provider in row.api_providers:
            return row.id
    return provider
