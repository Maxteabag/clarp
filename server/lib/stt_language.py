"""Transcription language: one default, with per-agent overrides.

`transcription.language` is the default ISO 639-1 code (`en` when unset).
`transcription.agent_languages` maps an agent name, persona or session
(lowercased) to its own code, so one agent can be spoken to in Norwegian
while the rest stay English. The engines are told the language explicitly:
Cartesia in particular translates speech into the language it is given
rather than detecting it. A clip uses the language of the agent whose chat
has focus, the same agent the transcript is recorded against.
"""
from __future__ import annotations

import contextlib
import threading
import contextvars
import json
import re

from . import settings_store

LANGUAGE_KEY = "transcription.language"
AGENT_LANGUAGES_KEY = "transcription.agent_languages"
# Per-agent transcription engine, e.g. Mochi on ElevenLabs Scribe because
# Cartesia Ink-Whisper turns short Norwegian clips into Icelandic or invents
# text on silence. Same keys as AGENT_LANGUAGES_KEY.
AGENT_ENGINES_KEY = "transcription.agent_engines"
DEFAULT_LANGUAGE = "en"
_CODE = re.compile(r"^[a-z]{2,3}$")
# The chat the clip being transcribed belongs to, set by the transcription
# pipeline for the duration of one engine call (same thread).
_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "transcription_session", default=None)


@contextlib.contextmanager
def bound_session(session: str | None):
    token = _SESSION.set(session or None)
    try:
        yield
    finally:
        _SESSION.reset(token)


def current_session() -> str | None:
    return _SESSION.get()


# The exact language a call-scoped clip is sent with (call_transcription),
# set by the pipeline for one engine call; None means "resolve as today".
_LANGUAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "transcription_language", default=None)


@contextlib.contextmanager
def bound_language(language: str | None):
    token = _LANGUAGE.set(language or None)
    try:
        yield
    finally:
        _LANGUAGE.reset(token)


def current_language() -> str | None:
    return _LANGUAGE.get()


# The language handed at the Host->engine boundary for one call-transcription
# attempt (docs/group-calls.md): what the Host passed, not what a provider did
# with it. An attempt whose boundary was not noted records null: unobserved,
# never evidence that the engine was or was not invoked.
_SENT_LOCK = threading.Lock()    # sends of one attempt may run on several threads
_HANDED: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "transcription_language_handed", default=None)


@contextlib.contextmanager
def capturing_handed():
    """A fresh record for one attempt, reset on exit or exception."""
    seen: dict = {}
    token = _HANDED.set(seen)
    try:
        yield seen
    finally:
        _HANDED.reset(token)


@contextlib.contextmanager
def sending_audio():
    """Around the very call that sends a recording to a provider (R175): the attempt
    counts as sent from here on, a reply or not, unless the request provably never left
    (the connection was refused, the host name did not resolve). Whatever an adapter does
    before this (decoding the clip, fetching a token, a missing project) sends nothing.
    Only records, and only inside a capture. The capture counts the sends that left, so
    pieces of one clip sent in parallel (Google) or a retry never undo another's send."""
    seen = _HANDED.get()
    if seen is not None:
        with _SENT_LOCK:
            seen["sent"] = seen.get("sent", 0) + 1
    try:
        yield
    except BaseException as error:
        if seen is not None and _never_left(error):
            with _SENT_LOCK:
                seen["sent"] = seen.get("sent", 1) - 1      # only this send never left
        raise


def _never_left(error: BaseException | None) -> bool:
    """Whether a failure happened before the request could leave the Host."""
    import socket
    import urllib.error
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, urllib.error.HTTPError):
            return False                                    # the provider answered
        if isinstance(error, (ConnectionRefusedError, socket.gaierror)):
            return True
        if isinstance(error, urllib.error.URLError) and isinstance(
                error.reason, (ConnectionRefusedError, socket.gaierror)):
            return True
        error = error.__cause__ or error.__context__
    return False


def was_sent(capture: dict) -> bool:
    """Whether the recording reached the provider in this captured attempt (R175)."""
    return capture.get("sent", 0) > 0


def note_handed(language: str | None) -> None:
    """At an engine boundary: the language handed (``auto`` = no constraint).
    Only records, and only inside a capture."""
    seen = _HANDED.get()
    if seen is not None:
        seen["language"] = language or "auto"


def parse_language(raw: str | None) -> str:
    """The stored default language, or DEFAULT_LANGUAGE when unset or invalid."""
    value = (raw or "").strip().lower()
    return value if _CODE.match(value) else DEFAULT_LANGUAGE


def selected() -> str:
    return parse_language(settings_store.get_text(LANGUAGE_KEY))


def parse_map(raw: str | None) -> dict[str, str]:
    """A stored {agent name, persona or session: value} map, keys lowercased."""
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip()
            for k, v in parsed.items() if str(k).strip() and str(v).strip()}


def _map(key: str) -> dict[str, str]:
    return parse_map(settings_store.get_text(key) or "{}")


def parse_agent_languages(raw: str | None) -> dict[str, str]:
    result = {}
    for key, value in parse_map(raw).items():
        try:
            result[key] = validate_agent(value)
        except ValueError:
            continue
    return result


def agent_languages() -> dict[str, str]:
    return parse_agent_languages(settings_store.get_text(AGENT_LANGUAGES_KEY) or "{}")


def agent_engines() -> dict[str, str]:
    return _map(AGENT_ENGINES_KEY)


def match(overrides: dict[str, str], agent: dict | None) -> str | None:
    """The override for this agent row: by its id, then name, persona and session.
    The hearing path and the profile (voice_profile) both use this."""
    if not overrides or not agent:
        return None
    # By the agent's id first (issue 476: settings keyed by agent_id), then as before.
    for key in (agent.get("agent_id"), agent.get("name"), agent.get("persona"), agent.get("session")):
        if key and str(key).strip().lower() in overrides:
            return overrides[str(key).strip().lower()]
    return None


def _for_session(overrides: dict[str, str], session: str | None) -> str | None:
    if session and overrides:
        from . import agents as agents_db
        agent = dict(agents_db.get_by_session(session) or {})
        agent["session"] = session
        return match(overrides, agent)
    return None


def language_for(session: str | None) -> str:
    """The language for clips spoken to `session`'s agent."""
    return _for_session(agent_languages(), session) or selected()


def split_engines(value: str | None) -> list[str]:
    """The engines of a stored value in order, each once: a list saved with a repeat before
    saving dropped them is read without it (R175)."""
    return list(dict.fromkeys(e.strip() for e in (value or "").split(",") if e.strip()))


def engines_for(session: str | None) -> list[str]:
    """The agent's own transcription engines in fallback order, or [].

    A value may list several engines, comma-separated: the first is used and
    the next ones only when it fails (e.g. "google:chirp_3,elevenlabs:scribe_v2").
    """
    return split_engines(_for_session(agent_engines(), session))


def engine_for(session: str | None) -> str | None:
    """The agent's first transcription engine, or None for the global one."""
    engines = engines_for(session)
    return engines[0] if engines else None


def validate(value) -> str:
    if not isinstance(value, str) or not _CODE.match(value.strip().lower()):
        raise ValueError("language must be an ISO 639-1 code such as 'en' or 'no'")
    return value.strip().lower()


def validate_agent(value) -> str:
    """One ISO code, or a bounded expected-language set such as 'no,pt'.

    The global default remains a single language. Google restricts detection
    to the set; Scribe detects automatically when used as its fallback.
    """
    if not isinstance(value, str):
        raise ValueError("agent language must be a string of ISO language codes")
    codes = list(dict.fromkeys(validate(code) for code in value.split(",")))
    if len(codes) > 3:
        raise ValueError("agent language set accepts at most three language codes")
    return ",".join(codes)


def _agent_ids_for(key: str) -> list[str]:
    """The ids of the agents a written key names (persona or session), so a value saved
    under a name also lands under each agent's id and is never shadowed by an older one."""
    try:
        from . import agents as agents_db
        return [str(a["agent_id"]).lower() for a in agents_db.list_agents()
                if key in {str(a.get("persona") or "").strip().lower(),
                           str(a.get("session") or "").strip().lower(),
                           str(a.get("agent_id") or "").strip().lower()}]
    except Exception as exc:  # noqa: BLE001 - a save never fails on the lookup
        from .log import log_exception
        log_exception("sttAgentIdLookupFail", exc, detail=key)
        return []


def merge_agent_engines(value, *, is_valid) -> str:
    """Apply `{agent: engine id}` changes (empty or null removes one)."""
    if not isinstance(value, dict):
        raise ValueError("agent_engines must be an object of agent -> engine")
    merged = agent_engines()
    for agent, engine in value.items():
        key = str(agent).strip().lower()
        if not key:
            raise ValueError("agent_engines keys must be agent names")
        if engine in (None, ""):
            merged.pop(key, None)
            for agent_id in _agent_ids_for(key):
                merged.pop(agent_id, None)
        elif not isinstance(engine, str) or not all(
                is_valid(e.strip()) for e in engine.split(",") if e.strip()) \
                or not engine.strip(" ,"):
            raise ValueError(f"unknown transcription engine: {engine}")
        else:
            # An engine listed twice is kept once, at its first place (R175).
            merged[key] = ",".join(dict.fromkeys(e.strip() for e in engine.split(",") if e.strip()))
            for agent_id in _agent_ids_for(key):
                merged[agent_id] = merged[key]
    return json.dumps(merged, sort_keys=True, separators=(",", ":"))


def merge_agent_languages(value) -> str:
    """Apply `{agent: code}` changes (empty or null removes one); returns JSON."""
    if not isinstance(value, dict):
        raise ValueError("agent_languages must be an object of agent -> language")
    merged = agent_languages()
    for agent, code in value.items():
        key = str(agent).strip().lower()
        if not key:
            raise ValueError("agent_languages keys must be agent names")
        if code in (None, ""):
            merged.pop(key, None)
            for agent_id in _agent_ids_for(key):
                merged.pop(agent_id, None)
        else:
            merged[key] = validate_agent(code)
            for agent_id in _agent_ids_for(key):
                merged[agent_id] = merged[key]
    return json.dumps(merged, sort_keys=True, separators=(",", ":"))
