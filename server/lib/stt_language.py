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


def agent_languages() -> dict[str, str]:
    try:
        raw = json.loads(settings_store.get_text(AGENT_LANGUAGES_KEY) or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower()
            for k, v in raw.items()
            if str(k).strip() and _CODE.match(str(v).strip().lower())}


def language_for(session: str | None) -> str:
    """The language for clips spoken to `session`'s agent."""
    overrides = agent_languages()
    if session and overrides:
        from . import agents as agents_db
        agent = agents_db.get_by_session(session) or {}
        for key in (agent.get("name"), agent.get("persona"), session):
            if key and str(key).strip().lower() in overrides:
                return overrides[str(key).strip().lower()]
    return selected()


def validate(value) -> str:
    if not isinstance(value, str) or not _CODE.match(value.strip().lower()):
        raise ValueError("language must be an ISO 639-1 code such as 'en' or 'no'")
    return value.strip().lower()


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
