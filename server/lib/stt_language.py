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


def selected() -> str:
    value = (settings_store.get_text(LANGUAGE_KEY) or "").strip().lower()
    return value if _CODE.match(value) else DEFAULT_LANGUAGE


def _map(key: str) -> dict[str, str]:
    try:
        raw = json.loads(settings_store.get_text(key) or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip()
            for k, v in raw.items() if str(k).strip() and str(v).strip()}


def agent_languages() -> dict[str, str]:
    return {k: v.lower() for k, v in _map(AGENT_LANGUAGES_KEY).items()
            if _CODE.match(v.lower())}


def agent_engines() -> dict[str, str]:
    return _map(AGENT_ENGINES_KEY)


def _for_session(overrides: dict[str, str], session: str | None) -> str | None:
    if session and overrides:
        from . import agents as agents_db
        agent = agents_db.get_by_session(session) or {}
        for key in (agent.get("name"), agent.get("persona"), session):
            if key and str(key).strip().lower() in overrides:
                return overrides[str(key).strip().lower()]
    return None


def language_for(session: str | None) -> str:
    """The language for clips spoken to `session`'s agent."""
    return _for_session(agent_languages(), session) or selected()


def engines_for(session: str | None) -> list[str]:
    """The agent's own transcription engines in fallback order, or [].

    A value may list several engines, comma-separated: the first is used and
    the next ones only when it fails (e.g. "google:chirp_3,elevenlabs:scribe_v2").
    """
    value = _for_session(agent_engines(), session) or ""
    return [e.strip() for e in value.split(",") if e.strip()]


def engine_for(session: str | None) -> str | None:
    """The agent's first transcription engine, or None for the global one."""
    engines = engines_for(session)
    return engines[0] if engines else None


def validate(value) -> str:
    if not isinstance(value, str) or not _CODE.match(value.strip().lower()):
        raise ValueError("language must be an ISO 639-1 code such as 'en' or 'no'")
    return value.strip().lower()


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
        elif not isinstance(engine, str) or not all(
                is_valid(e.strip()) for e in engine.split(",") if e.strip()) \
                or not engine.strip(" ,"):
            raise ValueError(f"unknown transcription engine: {engine}")
        else:
            merged[key] = ",".join(e.strip() for e in engine.split(",") if e.strip())
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
        else:
            merged[key] = validate(code)
    return json.dumps(merged, sort_keys=True, separators=(",", ":"))
