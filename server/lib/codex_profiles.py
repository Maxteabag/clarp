"""Codex provider profiles: a Codex agent whose turns go to another provider.

The ``codex`` backend normally runs on the user's OpenAI/ChatGPT account from
``~/.codex``. A profile is an additive alternative for one agent: its own
Codex app-server process with its own ``CODEX_HOME`` (a generated
``config.toml`` that names the provider) and the provider key, read from the
user's keyring at spawn time, only in that process's environment.

Profiles are configured, not built in: one ``[codex_profiles.<id>]`` table per
profile in Clarp's ``config.toml``::

    [codex_profiles.example-eu]
    label = "Codex (EU)"
    detail = "Azure OpenAI, EU Data Zone"
    base_url = "https://<resource>.openai.azure.com/openai/v1"
    models = [{ id = "<deployment>", label = "<name>", efforts = ["low", "medium", "high"] }]
    default_model = "<deployment>"
    wire_api = "responses"
    key_header = "api-key"
    keyring_service = "<secret-tool service>"
    keyring_key = "<secret-tool key>"

The keyring fields are a reference (``secret-tool lookup service <service>
key <key>``), never the key. A table that holds a literal secret (a key,
token, secret or password field, or a value that looks like one), any other
field or a non-https URL is rejected and never offered or run. Clarp always
enforces analytics, feedback, update checks and web search off,
``requires_openai_auth = false`` and no OpenAI/Codex credential variables in
the profile's process. Profiles are read at spawn and catalog time.

The key is never written to disk, argv, the database, logs or error text;
``redact`` strips it from anything the profile's process prints back. See
docs/codex-eu-profile.md for what leaves the machine and what stays.

An agent's profile is the ``agents.provider_profile`` column (schema 118),
written by the creating INSERT and made immutable by a trigger ("" is the
default Codex account). A turn, a fork or a
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
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import managed_skills
from .revisioned_cache import RevisionedCache

REDACTED = "[redacted]"
# The one variable that carries a profile's key into its own app-server.
KEY_ENV = "CLARP_CODEX_PROFILE_API_KEY"


class ProfileMismatch(RuntimeError):
    """A turn, fork or resume would cross from one provider profile to another."""


class ProfileUnavailable(RuntimeError):
    """The profile cannot run: not configured, or its key is not in the keyring."""


# Shown with every profile in the catalogue: dictation and hands-free routing
# are the user's own input and reach Clarp's speech and routing providers.
OPERATING_RULE = "Use text input, not voice, for real cases with this agent."


@dataclass(frozen=True)
class ProfileModel:
    id: str
    label: str
    efforts: tuple[str, ...]


@dataclass(frozen=True)
class CodexProfile:
    id: str
    label: str
    detail: str
    base_url: str
    models: tuple[ProfileModel, ...]
    default_model: str
    key_header: str
    keyring_service: str
    keyring_key: str

    def has_model(self, model: str) -> bool:
        return any(m.id == model for m in self.models)

    def efforts_for(self, model: str = "") -> tuple[str, ...]:
        """The efforts ``model`` (default: the default model) accepts."""
        wanted = model or self.default_model
        return next((m.efforts for m in self.models if m.id == wanted), ())

    @property
    def efforts(self) -> tuple[str, ...]:
        return self.efforts_for()

    @property
    def keyring(self) -> tuple[str, ...]:
        return ("service", self.keyring_service, "key", self.keyring_key)


# --- the configuration --------------------------------------------------------

_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
_FIELDS = {"label", "detail", "base_url", "models", "default_model", "wire_api",
           "key_header", "keyring_service", "keyring_key"}
_REQUIRED = {"label", "base_url", "models", "default_model", "keyring_service", "keyring_key"}
_MODEL_FIELDS = {"id", "label", "efforts"}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
_KEYRING_ATTR = re.compile(r"[A-Za-z0-9._:/@-]{1,128}")
# A field whose name says it holds a credential.
_SECRET_FIELD = re.compile(r"(?i)key|token|secret|passw|credential|auth")
# A literal that looks like a credential: a known key prefix, a bearer token,
# or a long unbroken alphanumeric run (Azure keys are 32 hex or 84 letters
# and digits).
_SECRETISH = re.compile(
    r"(?i)\b(sk|pk|rk)-[A-Za-z0-9_-]{8,}|bearer\s+\S{8,}|[A-Za-z0-9]{32,}")
# The reason each invalid table was last logged for, so it is logged once
# per distinct reason (a RevisionedCache keyed by profile id).
_REJECTED: RevisionedCache[str, str] = RevisionedCache("codex_profile_rejections", 64)


def _check_fields(where: str, table: dict, allowed: set[str]) -> None:
    secret = sorted(name for name in set(table) - allowed if _SECRET_FIELD.search(str(name)))
    if secret:
        raise ValueError(f"{where} holds a literal secret ({', '.join(secret)}); "
                         "name the key in the keyring instead")
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown field(s) {', '.join(unknown)}")


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for v in value for item in _strings(v)]
    if isinstance(value, dict):
        return [item for v in value.values() for item in _strings(v)]
    return []


def _validate(profile_id: str, table) -> CodexProfile:
    """One ``[codex_profiles.<id>]`` table, or ValueError naming what is
    wrong (never a value from the table)."""
    if not _ID.fullmatch(profile_id or ""):
        raise ValueError("the profile id must be lowercase letters, digits and dashes")
    if not isinstance(table, dict):
        raise ValueError("a profile must be a table")
    _check_fields("the profile", table, _FIELDS)
    missing = sorted(_REQUIRED - set(table))
    if missing:
        raise ValueError("missing field(s) " + ", ".join(missing))
    for name, value in table.items():
        if name != "models" and not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if any(_SECRETISH.search(v) for v in _strings(value)):
            raise ValueError(f"{name} looks like a secret; put the key in the keyring")
    url = urlsplit(table["base_url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("base_url must be a plain https URL")
    if table.get("wire_api", "responses") != "responses":
        raise ValueError("wire_api must be 'responses'")
    header = table.get("key_header", "api-key")
    if not _HEADER.fullmatch(header) or header.lower() == "authorization":
        raise ValueError("key_header must be a header name such as 'api-key'")
    for name in ("keyring_service", "keyring_key"):
        if not _KEYRING_ATTR.fullmatch(table[name]):
            raise ValueError(f"{name} must be a keyring attribute value")
    raw_models = table["models"]
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("models must be a non-empty list of {id, label, efforts}")
    models = []
    for item in raw_models:
        if not isinstance(item, dict):
            raise ValueError("each model must be a table {id, label, efforts}")
        _check_fields("a model", item, _MODEL_FIELDS)
        model_id, model_label = item.get("id"), item.get("label", item.get("id"))
        efforts = item.get("efforts", [])
        if not isinstance(model_id, str) or not _MODEL.fullmatch(model_id):
            raise ValueError("a model id must be a deployment name")
        if not isinstance(model_label, str) or not 0 < len(model_label) <= 80:
            raise ValueError("a model label must be 1-80 characters")
        if (not isinstance(efforts, list) or not all(isinstance(e, str) for e in efforts)
                or set(efforts) - _EFFORTS):
            raise ValueError("model efforts must be reasoning effort names")
        models.append(ProfileModel(model_id, model_label, tuple(efforts)))
    if len({m.id for m in models}) != len(models):
        raise ValueError("model ids must be unique")
    if table["default_model"] not in {m.id for m in models}:
        raise ValueError("default_model must be one of models")
    label, detail = table["label"].strip(), str(table.get("detail") or "").strip()
    if not label or len(label) > 80 or len(detail) > 300:
        raise ValueError("label (1-80) or detail (0-300) has the wrong length")
    return CodexProfile(
        id=profile_id, label=label, detail=detail, base_url=table["base_url"],
        models=tuple(models), default_model=table["default_model"], key_header=header,
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
            _REJECTED.get_or_compute(str(profile_id), str(exc),
                                     lambda pid=str(profile_id), why=str(exc): _log_rejected(pid, why))
    return found


def _log_rejected(profile_id: str, reason: str) -> str:
    from .log import log
    log("codexProfileRejected", f"id={profile_id[:40]} reason={reason}")
    return reason


def get(profile_id: str) -> CodexProfile | None:
    return profiles().get(profile_id or "") if profile_id else None


def require(profile_id: str) -> CodexProfile:
    profile = get(profile_id)
    if profile is None:
        raise ProfileUnavailable(f"provider profile '{profile_id}' is not configured")
    return profile


# --- the agent's profile ---------------------------------------------------
#
# The profile is a column on the agent's row (schema 118): written by the
# creating INSERT, immutable by trigger. Readers take it from a row they
# already hold (``profile_of``); a bare id costs one primary-key read.

def profile_of(agent: dict | str | None) -> str:
    """The agent's profile id ("" = the CLI's own account), from its row when
    given one, else read by id (deleted agents included: they still own their
    threads)."""
    if isinstance(agent, dict) and "provider_profile" in agent:
        return str(agent.get("provider_profile") or "")
    agent_id = agent if isinstance(agent, str) else str((agent or {}).get("agent_id") or "")
    if not agent_id:
        return ""
    from .db import conn
    row = conn().execute("SELECT provider_profile FROM agents WHERE agent_id = ?",
                         (agent_id,)).fetchone()
    return str(row["provider_profile"]) if row else ""


def for_agent(agent_id: str) -> str:
    """The agent's profile id, "" for the default Codex account."""
    return profile_of(agent_id or "")


def assigned() -> dict[str, str]:
    """agent_id -> profile for every agent with a profile (one indexed read)."""
    from .db import conn
    return {row["agent_id"]: row["provider_profile"] for row in conn().execute(
        "SELECT agent_id, provider_profile FROM agents WHERE provider_profile != ''")}


def is_profiled(agent: dict | str | None) -> bool:
    """Whether an agent (row or id) runs on a provider profile, configured or
    not. Side channels that would send its content to another provider skip
    it; a profile Clarp no longer knows still counts (fail closed)."""
    return bool(profile_of(agent))


def profiled_sessions() -> dict[str, str]:
    """session -> agent_id for every live agent on a provider profile."""
    from .db import conn
    return {row["session"]: row["agent_id"] for row in conn().execute(
        "SELECT session, agent_id FROM agents WHERE provider_profile != '' "
        "AND deleted_at IS NULL")}


# --- pushes to Apple ---------------------------------------------------------
#
# APNs (and the relay in front of it) is outside the EU. A push about a profile
# agent carries this marker first, so the app's notification extension skips
# enrichment and intent donation, and generic text instead of its words.

PUSH_REPLY = "New reply"
PUSH_NEEDS_ANSWER = "Needs your answer"


def push_marker(agent: dict | str | None) -> dict:
    """``{provider_profile, privacy_restricted: true}`` for a profile agent,
    else {}."""
    profile = profile_of(agent)
    return {"provider_profile": profile, "privacy_restricted": True} if profile else {}


def restrict_push(payload: dict, agent: dict | str | None, *, alert_body: str = "") -> dict:
    """``payload`` for an ordinary agent; for a profile agent the marker
    first, the alert's body generic and no subtitle."""
    marker = push_marker(agent)
    if not marker:
        return payload
    alert = (payload.get("aps") or {}).get("alert")
    if isinstance(alert, dict):
        alert["body"] = alert_body or PUSH_REPLY
        alert.pop("subtitle", None)
    return {**marker, **{key: value for key, value in payload.items() if key not in marker}}


def may_post(sender_agent_id: str, target_agent_id: str) -> bool:
    """Whether an agent may send to another: a profile agent's posts go only
    to agents on its own profile; anyone may post to a profile agent."""
    sender = for_agent(sender_agent_id or "")
    return not sender or for_agent(target_agent_id or "") == sender


BOUNDARY = "provider_profile_boundary"


def readable_by(target: dict | str | None, caller_agent_id: str) -> bool:
    """Whether the agent ``caller_agent_id`` (its own turn) may read
    ``target``'s content: only a caller on the same profile may read a
    profile agent. "" (the user, a device, the Host itself) always may."""
    target_profile = for_agent(
        target if isinstance(target, str) else str((target or {}).get("agent_id") or ""))
    if not target_profile or not caller_agent_id:
        return True
    return for_agent(caller_agent_id) == target_profile


# --- what another agent's turn may receive ------------------------------------
#
# A request or event stream that carries an agent's turn identity is that
# agent's provider reading. Lists and events it receives drop everything that
# names an agent on another provider profile; the user's devices (no turn
# identity) are not filtered. Only identifying requests can be filtered: every
# agent runs as the same Unix user (docs/codex-eu-profile.md).

_OWNER_KEYS = frozenset({"agent_id", "session", "source_agent_id", "sender_agent_id",
                         "principal_agent_id", "delegate_agent_id", "target_agent_id",
                         "from_agent_id", "to_agent_id", "parent_agent_id"})


def hidden_from(caller_agent_id: str) -> tuple[frozenset[str], frozenset[str]]:
    """(agent ids, sessions) of the profile agents ``caller_agent_id`` may not
    read; empty for the user (no caller)."""
    if not caller_agent_id:
        return frozenset(), frozenset()
    from .db import conn
    rows = conn().execute(
        "SELECT agent_id, session FROM agents WHERE provider_profile != '' "
        "AND provider_profile != ?", (profile_of(caller_agent_id),)).fetchall()
    return (frozenset(row["agent_id"] for row in rows),
            frozenset(row["session"] for row in rows))


def mentions(value, ids: frozenset[str], sessions: frozenset[str]) -> bool:
    """Whether ``value`` (any JSON-like structure) names one of those agents."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _OWNER_KEYS and isinstance(item, str) and (item in ids or item in sessions):
                return True
            if key in ids or mentions(item, ids, sessions):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(mentions(item, ids, sessions) for item in value)
    return False


def prune(value, ids: frozenset[str], sessions: frozenset[str]):
    """``value`` without any list element (at any depth) that names one of
    those agents, and without keys that are their ids."""
    if not ids and not sessions:
        return value
    if isinstance(value, dict):
        return {key: prune(item, ids, sessions) for key, item in value.items()
                if key not in ids}
    if isinstance(value, list):
        return [prune(item, ids, sessions) for item in value
                if not mentions(item, ids, sessions)]
    return value


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


def home_for(*, agent_id: str | None = None, thread_id: str = "") -> pathlib.Path | None:
    """The one Codex home to read for an agent's thread: its profile's home,
    or None for the CLI's own home. With ``agent_id`` the agent's stored
    profile decides; without it, the agent that bound ``thread_id`` does.
    No other home is ever searched."""
    if agent_id is None:
        if not thread_id:
            return None
        from . import agents as agents_db
        profile = agents_db.profile_of_backend_session(thread_id)
    else:
        profile = for_agent(agent_id or "")
    return home(profile) if profile else None


def home_for_thread(thread_id: str) -> pathlib.Path | None:
    return home_for(thread_id=thread_id)


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
        f"name = {_toml_string(profile.label)}",
        f"base_url = {_toml_string(profile.base_url)}",
        'wire_api = "responses"',
        f"env_http_headers = {{ {_toml_string(profile.key_header)} = "
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


# What the profile process (and every tool its turns run) may not inherit from
# the Host: another provider's credentials, any credential-shaped variable that
# is not its own key, and proxies (which would see its traffic).
_VENDOR_PREFIXES = ("OPENAI_", "CODEX_", "AZURE_OPENAI_", "ANTHROPIC_", "CLAUDE_CODE_",
                    "GEMINI_", "GOOGLE_", "ELEVEN", "CARTESIA", "DEEPGRAM", "FIREWORKS",
                    "GROQ", "XAI_", "GROK", "OPENROUTER", "MISTRAL", "COHERE",
                    "TOGETHER", "PERPLEXITY", "DEEPSEEK", "TYPESAFE", "HF_", "HUGGINGFACE")
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_KEY", "_ACCESS_KEY",
                    "_PASSWORD", "_CREDENTIALS")
_PROXIES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"}


def _inherits(name: str) -> bool:
    """Whether a Host variable may pass into a profile's process."""
    upper = name.upper()
    return not (upper.startswith(_VENDOR_PREFIXES) or upper.endswith(_SECRET_SUFFIXES)
                or upper in _PROXIES or upper == "API_KEY")


def spawn_env(profile_id: str, base: dict[str, str]) -> tuple[dict[str, str], str]:
    """The app-server environment for ``profile_id`` and the key it carries
    (kept by the caller only to redact it). No other provider's credential,
    no credential-shaped variable other than its own key, and no proxy
    reaches it (``_inherits``)."""
    profile = require(profile_id)
    path = prepare_home(profile.id)
    key = _read_key(profile)
    env = {name: value for name, value in base.items() if _inherits(name)}
    env["CODEX_HOME"] = str(path)
    env[KEY_ENV] = key
    return env, key


def redact(text: str, key: str) -> str:
    if not key or not isinstance(text, str):
        return text
    return text.replace(key, REDACTED)


# Whether each profile's key is in the keyring, per profile definition and
# minute (the revision), so the catalogue does not run secret-tool per call.
_AVAILABLE: RevisionedCache[str, bool] = RevisionedCache("codex_profile_available", 32)
_AVAILABLE_TTL = 60.0


def _key_present(profile: CodexProfile) -> bool:
    try:
        return _lookup(profile, read=False).returncode == 0
    except ProfileUnavailable:
        return False


def available(profile_id: str) -> bool:
    """Configured and its key is in the keyring. The lookup's exit status
    decides; the value is never read here. Cached for a minute."""
    profile = get(profile_id)
    if profile is None:
        return False
    revision = (profile, int(time.monotonic() // _AVAILABLE_TTL))
    return _AVAILABLE.get_or_compute(profile.id, revision, lambda: _key_present(profile))


def clear_cache() -> None:
    _AVAILABLE.reset()
    _REJECTED.reset()


# --- the catalogue ----------------------------------------------------------

def catalog() -> list[dict]:
    """``providers.codex.provider_profiles`` in GET /agent-model-options."""
    return [{
        "id": profile.id,
        "label": profile.label,
        "detail": " ".join(part for part in (profile.detail, OPERATING_RULE) if part),
        "operating_rule": OPERATING_RULE,
        "available": available(profile.id),
        "default_model": profile.default_model,
        "models": [{"id": m.id, "label": m.label, "supported_efforts": list(m.efforts)}
                   for m in profile.models],
    } for profile in profiles().values()]
