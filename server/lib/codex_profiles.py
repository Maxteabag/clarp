"""Codex provider profiles: a Codex agent whose turns go to another provider.

The ``codex`` backend normally runs on the user's OpenAI/ChatGPT account from
``~/.codex``. A profile is an additive alternative for one agent: its own
Codex app-server process with its own ``CODEX_HOME`` (a generated
``config.toml`` that names the provider) and the provider key, read from the
user's keyring at spawn time, only in that process's environment.

Profiles are configured, not built in: one ``[codex_profiles.<id>]`` table per
profile in Clarp's ``config.toml``::

    [codex_profiles.azure-eu]
    label = "Codex (EU, Azure)"
    detail = "Azure OpenAI, EU Data Zone"
    base_url = "https://<resource>.openai.azure.com/openai/v1"
    model = "<deployment>"
    efforts = ["low", "medium", "high", "xhigh"]
    wire_api = "responses"
    env_key_header = "api-key"
    keyring_service = "<secret-tool service>"
    keyring_key = "<secret-tool key>"

The keyring fields are a reference (``secret-tool lookup service <service>
key <key>``), never the key. A table with any other field, an http URL or a
value that looks like a secret is rejected and never offered or run. Clarp
always enforces analytics, feedback, update checks and web search off and
``requires_openai_auth = false`` for every profile.

The key is never written to disk, argv, the database, logs or error text;
``redact`` strips it from anything the profile's process prints back. See
docs/codex-eu-profile.md for what leaves the machine and what stays.

An agent's profile is stored at creation, in the same transaction as the
agent row, in the settings table under ``agent.provider_profile.<agent_id>``,
and never changes ("" is the default Codex account). A turn, a fork or a
resume across profiles is refused.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import managed_skills, settings_store

SETTING_PREFIX = "agent.provider_profile."
REDACTED = "[redacted]"
# The one variable that carries a profile's key into its own app-server.
KEY_ENV = "CLARP_CODEX_PROFILE_API_KEY"


class ProfileMismatch(RuntimeError):
    """A turn, fork or resume would cross from one provider profile to another."""


class ProfileUnavailable(RuntimeError):
    """The profile cannot run: not configured, or its key is not in the keyring."""


@dataclass(frozen=True)
class CodexProfile:
    id: str
    label: str
    detail: str
    base_url: str
    model: str
    efforts: tuple[str, ...]
    env_key_header: str
    keyring_service: str
    keyring_key: str

    @property
    def default_model(self) -> str:
        return self.model

    @property
    def models(self) -> tuple[tuple[str, str], ...]:
        return ((self.model, self.model),)

    def has_model(self, model: str) -> bool:
        return model == self.model

    @property
    def keyring(self) -> tuple[str, ...]:
        return ("service", self.keyring_service, "key", self.keyring_key)


# --- the configuration --------------------------------------------------------

_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
_FIELDS = {"label", "detail", "base_url", "model", "efforts", "wire_api",
           "env_key_header", "keyring_service", "keyring_key"}
_REQUIRED = {"label", "base_url", "model", "keyring_service", "keyring_key"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
_KEYRING_ATTR = re.compile(r"[A-Za-z0-9._:/@-]{1,128}")
# A literal that looks like a credential: a known key prefix, a bearer token,
# or a long unbroken alphanumeric run (Azure keys are 32 hex or 84 letters
# and digits).
_SECRETISH = re.compile(
    r"(?i)\b(sk|pk|rk)-[A-Za-z0-9_-]{8,}|bearer\s+\S{8,}|[A-Za-z0-9]{32,}")
_REJECTED: dict[str, str] = {}
_REJECTED_LOCK = threading.Lock()


def _validate(profile_id: str, table) -> CodexProfile:
    """One ``[codex_profiles.<id>]`` table, or ValueError naming what is
    wrong (never a value from the table)."""
    if not _ID.fullmatch(profile_id or ""):
        raise ValueError("the profile id must be lowercase letters, digits and dashes")
    if not isinstance(table, dict):
        raise ValueError("a profile must be a table")
    unknown = sorted(set(table) - _FIELDS)
    if unknown:
        raise ValueError("unknown field(s) " + ", ".join(unknown)
                         + "; a profile names its key in the keyring, never the key")
    missing = sorted(_REQUIRED - set(table))
    if missing:
        raise ValueError("missing field(s) " + ", ".join(missing))
    for name, value in table.items():
        values = value if name == "efforts" else [value]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{name} must be {'a list of strings' if name == 'efforts' else 'a string'}")
        if any(_SECRETISH.search(v) for v in values):
            raise ValueError(f"{name} looks like a secret; put the key in the keyring")
    url = urlsplit(table["base_url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("base_url must be a plain https URL")
    if table.get("wire_api", "responses") != "responses":
        raise ValueError("wire_api must be 'responses'")
    if not _MODEL.fullmatch(table["model"]):
        raise ValueError("model must be a deployment name")
    header = table.get("env_key_header", "api-key")
    if not _HEADER.fullmatch(header) or header.lower() == "authorization":
        raise ValueError("env_key_header must be a header name such as 'api-key'")
    for name in ("keyring_service", "keyring_key"):
        if not _KEYRING_ATTR.fullmatch(table[name]):
            raise ValueError(f"{name} must be a keyring attribute value")
    efforts = tuple(table.get("efforts") or ())
    if set(efforts) - _EFFORTS:
        raise ValueError("efforts must be reasoning effort names")
    label, detail = table["label"].strip(), str(table.get("detail") or "").strip()
    if not label or len(label) > 80 or len(detail) > 300:
        raise ValueError("label (1-80) or detail (0-300) has the wrong length")
    return CodexProfile(
        id=profile_id, label=label, detail=detail, base_url=table["base_url"],
        model=table["model"], efforts=efforts, env_key_header=header,
        keyring_service=table["keyring_service"], keyring_key=table["keyring_key"])


def profiles() -> dict[str, CodexProfile]:
    """The valid configured profiles. An invalid table is logged once (its
    id and reason) and left out."""
    from . import config
    found: dict[str, CodexProfile] = {}
    for profile_id, table in (config.load().codex_profiles or {}).items():
        try:
            found[profile_id] = _validate(str(profile_id), table)
        except ValueError as exc:
            with _REJECTED_LOCK:
                fresh = _REJECTED.get(str(profile_id)) != str(exc)
                _REJECTED[str(profile_id)] = str(exc)
            if fresh:
                from .log import log
                log("codexProfileRejected", f"id={str(profile_id)[:40]} reason={exc}")
    return found


def get(profile_id: str) -> CodexProfile | None:
    return profiles().get(profile_id or "") if profile_id else None


def require(profile_id: str) -> CodexProfile:
    profile = get(profile_id)
    if profile is None:
        raise ProfileUnavailable(f"provider profile '{profile_id}' is not configured")
    return profile


# --- the agent's profile ---------------------------------------------------

def for_agent(agent_id: str) -> str:
    """The agent's profile id, "" for the default Codex account."""
    if not agent_id:
        return ""
    return settings_store.get_text(SETTING_PREFIX + agent_id)


def write_for_new_agent(c, agent_id: str, profile_id: str) -> None:
    """Store ``profile_id`` inside the caller's transaction that writes the
    agent row (``agents.create_agent``). Immutable: an agent that already has
    another profile, or none while one is asked for, is refused."""
    current = settings_store.get_text_in(c, SETTING_PREFIX + agent_id)
    if current == profile_id:
        return
    if current:
        raise ProfileMismatch(f"agent {agent_id} runs on provider profile '{current}'")
    require(profile_id)
    settings_store.set_text_in(c, SETTING_PREFIX + agent_id, profile_id)


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

# OpenAI credentials a Codex home can hold. A profile home must hold none.
_OPENAI_CREDENTIALS = ("auth.json", ".credentials.json")


def root() -> pathlib.Path:
    raw = os.environ.get("CLARP_CODEX_PROFILES_DIR")
    if raw:
        return pathlib.Path(raw).expanduser()
    from .deployment import LAYOUT
    return LAYOUT.data_root / "codex-profiles"


def home(profile_id: str) -> pathlib.Path:
    if not _ID.fullmatch(profile_id or ""):
        raise ProfileMismatch(f"invalid provider profile id: {profile_id[:40]}")
    return root() / profile_id


def home_for_thread(thread_id: str) -> pathlib.Path | None:
    """The Codex home of the agent that bound ``thread_id``: its profile's
    home, or None for the default home. No other home is ever searched."""
    if not thread_id:
        return None
    from . import agents as agents_db
    profile = for_agent(agents_db.owner_of_backend_session(thread_id) or "")
    return home(profile) if profile else None


def thread_in_home(profile_id: str, thread_id: str) -> bool:
    """Whether ``thread_id``'s rollout is in this profile's own home."""
    sessions = home(profile_id) / "sessions"
    return bool(thread_id) and sessions.is_dir() and next(
        sessions.rglob(f"*{thread_id}.jsonl"), None) is not None


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_toml(profile: CodexProfile) -> str:
    """The profile's ``config.toml``. Names the key's variable, never the key.
    The fixed lines are enforced for every profile."""
    pid = profile.id
    return "\n".join((
        "# Generated by Clarp for the Codex provider profile "
        f"{_toml_string(pid)}. Rewritten at every app-server start.",
        f"model = {_toml_string(profile.model)}",
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
        f"name = {_toml_string(profile.label)}",
        f"base_url = {_toml_string(profile.base_url)}",
        'wire_api = "responses"',
        f"env_http_headers = {{ {_toml_string(profile.env_key_header)} = "
        f"{_toml_string(KEY_ENV)} }}",
        "requires_openai_auth = false",
        "supports_websockets = false",
        "",
    ))


def prepare_home(profile_id: str) -> pathlib.Path:
    """Create the profile's private home (0700) with its config (0600) and a
    link to Clarp's managed Codex skills. A home that holds an OpenAI
    credential is refused: the profile never runs on an OpenAI login."""
    profile = require(profile_id)
    path = home(profile.id)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    found = [name for name in _OPENAI_CREDENTIALS
             if (path / name).exists() or (path / name).is_symlink()]
    if found:
        raise ProfileUnavailable(
            f"provider profile {profile.id}: its home holds an OpenAI credential "
            f"({', '.join(found)}); remove it")
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

def _lookup(profile: CodexProfile, *, read: bool) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["secret-tool", "lookup", *profile.keyring],
            stdout=subprocess.PIPE if read else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileUnavailable(
            f"provider profile {profile.id}: the keyring could not be read "
            f"({type(exc).__name__})") from None


def _read_key(profile: CodexProfile) -> str:
    """The key from the user's keyring. Errors never carry its output."""
    result = _lookup(profile, read=True)
    key = (result.stdout or "").strip() if result.returncode == 0 else ""
    if not key:
        raise ProfileUnavailable(f"provider profile {profile.id}: no key in the keyring")
    return key


def spawn_env(profile_id: str, base: dict[str, str]) -> tuple[dict[str, str], str]:
    """The app-server environment for ``profile_id`` and the key it carries
    (kept by the caller only to redact it). Nothing from the default Codex
    account (its home or any OpenAI variable) reaches it."""
    profile = require(profile_id)
    path = prepare_home(profile.id)
    key = _read_key(profile)
    env = {name: value for name, value in base.items()
           if not name.startswith(("OPENAI_", "CODEX_", "AZURE_OPENAI_"))}
    env["CODEX_HOME"] = str(path)
    env[KEY_ENV] = key
    return env, key


def redact(text: str, key: str) -> str:
    if not key or not isinstance(text, str):
        return text
    return text.replace(key, REDACTED)


_LOCK = threading.Lock()
_AVAILABLE: dict[str, tuple[float, bool]] = {}
_AVAILABLE_TTL = 60.0


def available(profile_id: str) -> bool:
    """Configured and its key is in the keyring. The lookup's exit status
    decides; the value is never read here. Cached for a minute."""
    profile = get(profile_id)
    if profile is None:
        return False
    now = time.monotonic()
    with _LOCK:
        cached = _AVAILABLE.get(profile.id)
        if cached and now - cached[0] < _AVAILABLE_TTL:
            return cached[1]
    try:
        ok = _lookup(profile, read=False).returncode == 0
    except ProfileUnavailable:
        ok = False
    with _LOCK:
        _AVAILABLE[profile.id] = (now, ok)
    return ok


def clear_cache() -> None:
    with _LOCK:
        _AVAILABLE.clear()
    with _REJECTED_LOCK:
        _REJECTED.clear()


# --- the catalogue ----------------------------------------------------------

def catalog() -> list[dict]:
    """``providers.codex.provider_profiles`` in GET /agent-model-options."""
    return [{
        "id": profile.id,
        "label": profile.label,
        "detail": profile.detail,
        "available": available(profile.id),
        "default_model": profile.model,
        "models": [{"id": profile.model, "label": profile.model,
                    "supported_efforts": list(profile.efforts)}],
    } for profile in profiles().values()]
