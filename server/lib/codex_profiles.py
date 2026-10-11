"""Codex provider profiles: a Codex agent whose turns go to another provider.

The ``codex`` backend normally runs on the user's OpenAI/ChatGPT account from
``~/.codex``. A profile is an additive alternative for one agent: its own
Codex app-server process with its own ``CODEX_HOME`` (a generated
``config.toml`` that names the provider) and the provider key, read from the
user's keyring at spawn time, only in that process's environment.

The key is never written to disk, argv, the database, logs or error text;
``redact`` strips it from anything the profile's process prints back.

``azure-eu`` sends turns to Azure OpenAI in the EU Data Zone. See
docs/codex-eu-profile.md for what leaves the machine and what stays.

An agent's profile is stored once, at creation, in the settings table under
``agent.provider_profile.<agent_id>`` and never changes ("" is the default
Codex account). A turn, a fork or a resume across profiles is refused.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import pathlib
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from . import managed_skills, settings_store

SETTING_PREFIX = "agent.provider_profile."
AZURE_EU = "azure-eu"
REDACTED = "[redacted]"


class ProfileMismatch(RuntimeError):
    """A turn, fork or resume would cross from one provider profile to another."""


class ProfileUnavailable(RuntimeError):
    """The profile cannot run: its key is not in the keyring."""


@dataclass(frozen=True)
class CodexProfile:
    id: str
    label: str
    detail: str
    provider_name: str
    base_url: str
    # (deployment id, label); the first is the default.
    models: tuple[tuple[str, str], ...]
    efforts: tuple[str, ...]
    key_env: str
    # secret-tool attributes for ``secret-tool lookup``.
    keyring: tuple[str, ...]

    @property
    def default_model(self) -> str:
        return self.models[0][0]

    def has_model(self, model: str) -> bool:
        return any(model == known for known, _label in self.models)


PROFILES: dict[str, CodexProfile] = {
    AZURE_EU: CodexProfile(
        id=AZURE_EU,
        label="Codex (EU, Azure)",
        detail="Codex on Azure OpenAI, EU Data Zone (Sweden Central).",
        provider_name=("Azure OpenAI EU Data Zone "
                       "(irisbot-ticketmaster-ai-sc, Sweden Central)"),
        base_url="https://irisbot-ticketmaster-ai-sc.openai.azure.com/openai/v1",
        models=(("sol61-eu", "GPT-6.1 Sol (EU)"),),
        efforts=("low", "medium", "high", "xhigh"),
        key_env="AZURE_OPENAI_API_KEY",
        keyring=("service", "psa-reverify", "key", "azure-openai"),
    ),
}

# Variables that would let a profile process reach OpenAI on its own.
_OPENAI_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
               "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")

_LOCK = threading.Lock()
_AVAILABLE: dict[str, tuple[float, bool]] = {}
_AVAILABLE_TTL = 60.0


def get(profile_id: str) -> CodexProfile | None:
    return PROFILES.get(profile_id or "")


def require(profile_id: str) -> CodexProfile:
    profile = get(profile_id)
    if profile is None:
        raise ProfileMismatch(f"unknown provider profile: {profile_id}")
    return profile


# --- the agent's profile ---------------------------------------------------

def for_agent(agent_id: str) -> str:
    """The agent's profile id, "" for the default Codex account."""
    if not agent_id:
        return ""
    return settings_store.get_text(SETTING_PREFIX + agent_id)


def assign(agent_id: str, profile_id: str) -> None:
    """Record ``profile_id`` for a new agent. Immutable once set."""
    current = for_agent(agent_id)
    if current == profile_id:
        return
    if current or not profile_id:
        raise ProfileMismatch(
            f"agent {agent_id} runs on provider profile '{current or 'default'}'")
    require(profile_id)
    settings_store.set_text(SETTING_PREFIX + agent_id, profile_id)


def assigned() -> dict[str, str]:
    """agent_id -> profile for every agent with a profile."""
    return {key[len(SETTING_PREFIX):]: value
            for key, value in settings_store.get_prefix(SETTING_PREFIX).items()
            if value}


def is_profiled(agent: dict | str | None) -> bool:
    """Whether an agent (row or id) runs on a provider profile. Side channels
    that would send its content to another provider skip it."""
    agent_id = agent if isinstance(agent, str) else (agent or {}).get("agent_id", "")
    return bool(for_agent(str(agent_id or "")))


def profiled_sessions() -> dict[str, str]:
    """session -> agent_id for every live agent on a provider profile."""
    profiled = assigned()
    if not profiled:
        return {}
    from . import agents as agents_db
    rows = (agents_db.get_by_agent_id(agent_id) for agent_id in profiled)
    return {row["session"]: row["agent_id"] for row in rows if row}


def readable_by(target: dict | str | None, caller_agent_id: str) -> bool:
    """Whether the agent ``caller_agent_id`` (its own turn) may read
    ``target``'s content: only a caller on the same profile may read a
    profile agent. "" (the user, a device, the Host itself) always may."""
    target_profile = for_agent(
        target if isinstance(target, str) else str((target or {}).get("agent_id") or ""))
    if not target_profile or not caller_agent_id:
        return True
    return for_agent(caller_agent_id) == target_profile


# --- local-only work --------------------------------------------------------
#
# Work done for a profile agent outside its own turn (transcribing its
# dictation, routing a message addressed to it) runs inside ``local_only``:
# remote judgment models are not asked and the caller falls back to its local
# rule.

_LOCAL_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "codex_profile_local_only", default=False)


@contextlib.contextmanager
def local_only(active: bool = True):
    token = _LOCAL_ONLY.set(bool(active) or _LOCAL_ONLY.get())
    try:
        yield
    finally:
        _LOCAL_ONLY.reset(token)


def remote_allowed() -> bool:
    """False inside ``local_only``: no remote model may see this work."""
    return not _LOCAL_ONLY.get()


# --- the profile's CODEX_HOME ----------------------------------------------

def root() -> pathlib.Path:
    raw = os.environ.get("CLARP_CODEX_PROFILES_DIR")
    if raw:
        return pathlib.Path(raw).expanduser()
    from .deployment import LAYOUT
    return LAYOUT.data_root / "codex-profiles"


def home(profile_id: str) -> pathlib.Path:
    return root() / require(profile_id).id


def homes() -> list[pathlib.Path]:
    """Every profile home that exists, for transcript lookups."""
    return [path for path in (root() / pid for pid in PROFILES) if path.is_dir()]


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_toml(profile: CodexProfile) -> str:
    """The profile's ``config.toml``. Names the key's variable, never the key."""
    pid = profile.id
    return "\n".join((
        "# Generated by Clarp for the Codex provider profile "
        f"{_toml_string(pid)}. Rewritten at every app-server start.",
        f"model = {_toml_string(profile.default_model)}",
        f"model_provider = {_toml_string(pid)}",
        "check_for_update_on_startup = false",
        'web_search = "disabled"',
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[feedback]",
        "enabled = false",
        "",
        f"[model_providers.{_toml_string(pid)}]",
        f"name = {_toml_string(profile.provider_name)}",
        f"base_url = {_toml_string(profile.base_url)}",
        'wire_api = "responses"',
        f"env_http_headers = {{ \"api-key\" = {_toml_string(profile.key_env)} }}",
        "requires_openai_auth = false",
        "supports_websockets = false",
        "",
    ))


def prepare_home(profile_id: str) -> pathlib.Path:
    """Create the profile's private home (0700) with its config (0600) and a
    link to Clarp's managed Codex skills."""
    profile = require(profile_id)
    path = home(profile.id)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    text = config_toml(profile)
    target = path / "config.toml"
    if not target.is_file() or target.read_text() != text:
        fd, tmp = tempfile.mkstemp(dir=path, prefix=".config-", suffix=".toml")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    skills = path / "skills"
    source = pathlib.Path(managed_skills.CODEX_SKILLS)
    if source.is_dir() and not skills.exists() and not skills.is_symlink():
        skills.symlink_to(source, target_is_directory=True)
    return path


# --- the key ----------------------------------------------------------------

def _read_key(profile: CodexProfile) -> str:
    """The key from the user's keyring. Errors never carry its output."""
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", *profile.keyring],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileUnavailable(
            f"provider profile {profile.id}: the keyring could not be read "
            f"({type(exc).__name__})") from None
    key = result.stdout.strip() if result.returncode == 0 else ""
    if not key:
        raise ProfileUnavailable(
            f"provider profile {profile.id}: no key in the keyring "
            f"({' '.join(profile.keyring)})")
    return key


def spawn_env(profile_id: str, base: dict[str, str]) -> tuple[dict[str, str], str]:
    """The app-server environment for ``profile_id`` and the key it carries
    (kept by the caller only to redact it)."""
    profile = require(profile_id)
    key = _read_key(profile)
    env = {name: value for name, value in base.items() if name not in _OPENAI_ENV}
    env["CODEX_HOME"] = str(prepare_home(profile.id))
    env[profile.key_env] = key
    return env, key


def redact(text: str, key: str) -> str:
    if not key or not isinstance(text, str):
        return text
    return text.replace(key, REDACTED)


def available(profile_id: str) -> bool:
    """Whether the profile's key is in the keyring (cached for a minute)."""
    profile = get(profile_id)
    if profile is None:
        return False
    now = time.monotonic()
    with _LOCK:
        cached = _AVAILABLE.get(profile.id)
        if cached and now - cached[0] < _AVAILABLE_TTL:
            return cached[1]
    try:
        _read_key(profile)
        ok = True
    except ProfileUnavailable:
        ok = False
    with _LOCK:
        _AVAILABLE[profile.id] = (now, ok)
    return ok


def clear_cache() -> None:
    with _LOCK:
        _AVAILABLE.clear()


# --- threads ----------------------------------------------------------------

def owner_of_thread(thread_id: str) -> str:
    """The profile whose home holds this Codex thread, "" when none does."""
    if not thread_id:
        return ""
    for path in homes():
        sessions = path / "sessions"
        if sessions.is_dir() and next(sessions.rglob(f"*{thread_id}.jsonl"), None):
            return path.name
    return ""


# --- the catalogue ----------------------------------------------------------

def catalog() -> list[dict]:
    """``providers.codex.provider_profiles`` in GET /agent-model-options."""
    return [{
        "id": profile.id,
        "label": profile.label,
        "detail": profile.detail,
        "available": available(profile.id),
        "default_model": profile.default_model,
        "models": [{"id": model, "label": label,
                    "supported_efforts": list(profile.efforts)}
                   for model, label in profile.models],
    } for profile in PROFILES.values()]
