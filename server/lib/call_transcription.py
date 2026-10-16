"""Call transcription policy (docs/group-calls.md "Transcription in a call").

A clip is transcribed with the call policy only when its request names the
call (`X-Clarp-Call-Id`) and that call is live, the requester's, on this
Host, and the captured session is a reachable participant. A present header
is checked before any provider sees the audio and refuses explicitly; an
absent header is ordinary chat transcription, unchanged.

Language and engines resolve per field, each with its source:
- language: the agent's own (`agent_languages`) > `call_languages` (in a
  call) > the global default;
- engines: the agent's own (`agent_engines`) > `call_engines` (in a call) >
  the device's `X-Transcription-Model` > the global engine.
Both call settings are unset by default; nothing here sets them.
"""
from __future__ import annotations

import json
import re

from . import settings_store, stt_language
from .group_calls import CallError, REACHABLE

CALL_LANGUAGES_KEY = "transcription.call_languages"
CALL_ENGINES_KEY = "transcription.call_engines"
# Providers that take a bounded language set; every other engine takes one code.
SET_PROVIDERS = frozenset({"google", "elevenlabs"})
_CALL_ID = re.compile(r"^gcl_[0-9a-f]{32}$")


def _server_id() -> str:
    from .server_identity import server_id
    return server_id()


# ---- settings ----------------------------------------------------------------------

def call_languages() -> str:
    raw = (settings_store.get_text(CALL_LANGUAGES_KEY) or "").strip()
    try:
        return stt_language.validate_agent(raw) if raw else ""
    except ValueError:
        return ""


def call_engines() -> list[str]:
    raw = settings_store.get_text(CALL_ENGINES_KEY) or ""
    return [e.strip() for e in raw.split(",") if e.strip()]


def single_code(engine: str) -> bool:
    return engine.split(":", 1)[0] not in SET_PROVIDERS


def validate_settings(languages, engines, *, is_valid,
                      global_engine: str = "") -> tuple[str | None, str | None]:
    """Normalised (languages, engines) to store; None leaves one unchanged and
    "" unsets it. A language set with any single-code engine is refused."""
    if languages is not None:
        if not isinstance(languages, str):
            raise ValueError("call_languages must be a string of ISO language codes")
        languages = stt_language.validate_agent(languages) if languages.strip() else ""
    if engines is not None:
        if not isinstance(engines, str):
            raise ValueError("call_engines must be a comma-separated string of engines")
        listed = [e.strip() for e in engines.split(",") if e.strip()]
        for engine in listed:
            if not is_valid(engine):
                raise ValueError(f"unknown transcription engine: {engine}")
        engines = ",".join(listed)
    effective_languages = call_languages() if languages is None else languages
    effective_engines = call_engines() if engines is None else [e for e in engines.split(",") if e]
    if "," in effective_languages and any(single_code(e) for e in effective_engines):
        raise ValueError("call_languages is a language set, but a call engine takes a single language")
    if effective_languages:
        # The engines a call clip would use: the call's own, else the global
        # engine (an alias resolved to the real model). None that can take
        # the language means the policy can never work.
        usable = effective_engines or [global_engine or "server-default"]
        if not any("skip" not in step for step in plan(usable, effective_languages)):
            raise ValueError(f"call_languages {effective_languages!r} can never work with "
                             + ", ".join(concrete(e) for e in usable))
    return languages, engines


# ---- scope (fail closed) -------------------------------------------------------------

def _find_call(call_id: str) -> tuple[str, dict] | None:
    """(principal, call) from the Host call records, read-only."""
    from .group_calls import KEY, _principals
    for principal in _principals():
        raw = settings_store.get(KEY + principal)
        for call in (json.loads(raw).get("calls", []) if raw else []):
            if call.get("call_id") == call_id:
                return principal, call
    return None


def validate_scope(principal: str, call_id: str, session: str) -> dict:
    """The call a request's `X-Clarp-Call-Id` names, or CallError: 400 bad
    call id, 404 unknown call, 403 not your call, 409 call ended, 409 not a
    call participant. Membership comes only from the call record."""
    call_id = str(call_id or "").strip()
    if not _CALL_ID.match(call_id):
        raise CallError(400, "bad call id")
    found = _find_call(call_id)
    if found is None:
        raise CallError(404, "unknown call", call_id=call_id)
    owner, call = found
    if owner != principal or call.get("host_id") != _server_id():
        raise CallError(403, "not your call", call_id=call_id)
    if call.get("state") != "live":
        raise CallError(409, "call ended", call_id=call_id, reason=call.get("reason") or "ended")
    session = str(session or "").strip()
    part = next((p for p in call.get("participants", []) if p.get("session") == session), None)
    if not session or part is None or part.get("state") not in REACHABLE:
        raise CallError(409, "not a call participant", call_id=call_id, session=session)
    return {"call_id": call_id, "session": session, "principal": principal}


# ---- resolution ------------------------------------------------------------------------

def resolve(session: str, *, in_call: bool, requested_model: str = "") -> dict:
    """Per-field language and engines with their sources, plus the device
    header's fate. Outside a call the call policy never applies."""
    agent_language = stt_language._for_session(stt_language.agent_languages(), session)
    policy_language = call_languages() if in_call else ""
    if agent_language:
        language = {"value": agent_language, "source": "agent"}
    elif policy_language:
        language = {"value": policy_language, "source": "call_policy"}
    else:
        language = {"value": stt_language.selected(), "source": "global"}
    agent_engines = stt_language.engines_for(session)
    policy_engines = call_engines() if in_call else []
    if agent_engines:
        engines = {"value": agent_engines, "source": "agent"}
    elif policy_engines:
        engines = {"value": policy_engines, "source": "call_policy"}
    elif requested_model:
        engines = {"value": [requested_model], "source": "device"}
    else:
        engines = {"value": [_global_engine()], "source": "global"}
    # Plan on the engines as named (an alias stays recognisable), report the
    # real models (docs/group-calls.md "Transcription in a call").
    planned = list(engines["value"])
    engines = {"value": [concrete(e) for e in planned], "source": engines["source"]}
    requested = None
    if requested_model:
        applied = engines["source"] == "device"
        requested = {"value": requested_model, "applied": applied,
                     "reason": None if applied else engines["source"]}
    return {"language": language, "engines": engines, "requested_model": requested, "_plan": planned}


def _global_engine() -> str:
    from . import stt_providers
    engine = stt_providers.selected_engine()
    return engine if stt_providers.is_cloud_model(engine) else "server-default"


DEFAULT_ALIASES = frozenset({"server-default", "local", ""})


def concrete(engine: str) -> str:
    """The real model behind an engine name: the server default (and its
    aliases) is the configured local model."""
    if engine in DEFAULT_ALIASES:
        from .config import load
        cfg = load()
        return f"{cfg.whisper_provider}:{cfg.whisper_model}"
    return engine


def capability(engine: str) -> str:
    """What an engine actually does with the language it is given:
    ``set`` (a bounded set), ``single`` (one code), ``en_only`` (English, the
    only language it transcribes) or ``none`` (it never receives a code: a
    local multilingual model auto-detects, so a call language is not applied)."""
    from . import stt_providers, transcription_catalog
    if stt_providers.is_cloud_model(engine):
        return "set" if not single_code(engine) else "single"
    row = transcription_catalog.model_by_id(engine)
    if row is not None:
        return "en_only" if list(row.get("languages") or []) == ["en"] else "none"
    return "en_only" if engine.endswith(".en") else "none"


def plan(engines: list[str], language: str) -> list[dict]:
    """Per engine, in order: the language it really receives (a set only to
    a set-capable engine, else its first code; English only to an
    English-only model) or a skip when it cannot take the language at all."""
    codes = language.split(",")
    steps = []
    for named in engines:
        engine = concrete(named)
        kind = capability(engine)
        if kind == "set":
            sent = language
        elif kind == "single":
            sent = codes[0]
        elif kind == "en_only":
            sent = "en" if codes[0] == "en" else None
        else:
            sent = None
        if sent is None:
            steps.append({"engine": engine, "skip": "language_incompatible"})
            continue
        step = {"engine": engine, "language": sent, "adapted": sent != language}
        if named in DEFAULT_ALIASES:
            step["local_default"] = True
        steps.append(step)
    return steps


_NO_ATTRIBUTE = object()


def unchanged_plan(engines: list[str], session: str, stt=None) -> list[dict]:
    """Without a call policy, in-call transcription is today's (§12.2): the
    same engine chain, nothing skipped or bound. Each step only records the
    language the engine actually receives: a cloud engine today's resolved
    language for the session; a local engine its own (`stt.language`: "en"
    for an English-only model) or "auto" when it detects the language."""
    from . import stt_providers
    steps = []
    for named in engines:
        engine = concrete(named)
        if stt_providers.is_cloud_model(engine):
            received = stt_language.language_for(session)
        else:
            own = getattr(stt, "language", _NO_ATTRIBUTE) if named in DEFAULT_ALIASES else _NO_ATTRIBUTE
            if own is _NO_ATTRIBUTE:
                own = "en" if capability(engine) == "en_only" else None
            received = own or "auto"
        step = {"engine": engine, "language": None, "received": received, "adapted": False}
        if named in DEFAULT_ALIASES:
            step["local_default"] = True
        steps.append(step)
    return steps


def policy_configured() -> bool:
    return bool(call_languages() or call_engines())


def participants_view(call: dict) -> dict | None:
    """``{session: {language, engines}}`` for a call's participants, or None
    when every field is the global default (nothing to show)."""
    view = {}
    for part in call.get("participants", []):
        resolved = resolve(part["session"], in_call=True)
        view[part["session"]] = {"language": resolved["language"], "engines": resolved["engines"]}
    if all(v["language"]["source"] == "global" and v["engines"]["source"] == "global" for v in view.values()):
        return None
    return view
