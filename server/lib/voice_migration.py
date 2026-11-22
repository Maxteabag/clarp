"""The voice migration (issue 476, V2a): a report first, and writes only where nothing changes.

For every agent and every saved contact: the stored voice value, the one stored shape it
would become (voice.canonical), what it is heard with today and what it would be heard with
after, under the Host's configured provider AND under its configured fallback alone, and,
keys aside, under every provider the Host can speak with (each built-in and each installed
adapter, on its own): a stored value is read per provider, so a row that sounds the same
under today's provider may not under another one the Host moves to. A row with any
difference is an AUDIBLE change: --apply never writes it and names it; it is the owner's to
decide (V2c). --apply writes only the plan the release owner read: it takes the dry run's
digest and refuses when the plan has changed since. The transcription settings get keys by agent id the same way:
each agent's value today is copied under its id; a key that several agents share is listed.

Nothing here calls a provider: the answers come from the resolver the speaking path uses
(voice_profile.speech).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

from . import voice_profile
from .voice import canonical


def _heard(host: voice_profile.HostView, agent: dict) -> dict:
    """What the agent is heard with under the configured provider (fallback included),
    and under the configured fallback alone (as if the provider had none for it)."""
    sp = voice_profile.speech(host, agent)
    out = {"provider": [sp["provider"], sp["voice_id"], sp["silent_reason"]]}
    cfg = host.cfg
    if cfg.tts_fallback not in ("", "none", cfg.tts_provider):
        alone = dataclasses.replace(cfg, tts_provider=cfg.tts_fallback, tts_fallback="none",
                                    tts_agent_overrides={})
        fb = voice_profile.speech(dataclasses.replace(host, cfg=alone), agent)
        out["fallback"] = [fb["provider"], fb["voice_id"], fb["silent_reason"]]
    return out


def _per_provider_hosts(host: voice_profile.HostView) -> dict[str, voice_profile.HostView]:
    """For every provider the Host can speak with, a view of the Host as if it were the only
    one, with its key present (keys aside: what the stored value would give it)."""
    out = {}
    for provider in sorted(voice_profile.BUILTIN_PROVIDERS | set(host.adapters)):
        alone = dataclasses.replace(host.cfg, tts_provider=provider, tts_fallback="none",
                                    tts_agent_overrides={})
        out[provider] = dataclasses.replace(host, cfg=alone, keys={**host.keys, provider: True},
                                            google_refusal=None)
    return out


def _row(host, alone: dict, kind: str, ident: dict, agent: dict) -> dict | None:
    old = str(agent.get("voice_id") or "")
    new = canonical(old)
    if new == old:
        return None
    moved = {**agent, "voice_id": new}
    today = _heard(host, agent)
    after = _heard(host, moved)
    per_provider = {}
    for provider, view in alone.items():
        before_sp, after_sp = voice_profile.speech(view, agent), voice_profile.speech(view, moved)
        before_v = [before_sp["voice_id"], before_sp["silent_reason"]]
        after_v = [after_sp["voice_id"], after_sp["silent_reason"]]
        if before_v != after_v:
            per_provider[provider] = {"today": before_v, "after": after_v}
    return {"kind": kind, **ident, "old": old, "new": new, "today": today, "after": after,
            "per_provider": per_provider, "audible": today != after or bool(per_provider)}


def plan(cfg=None) -> dict[str, Any]:
    """The whole report; reads only."""
    from . import agents as agents_db, personas
    host = voice_profile.HostView.build(cfg)
    alone = _per_provider_hosts(host)
    rows = []
    for agent in agents_db.list_agents():
        row = _row(host, alone, "agent", {"agent_id": agent["agent_id"], "name": agent["persona"],
                                   "session": agent["session"]}, agent)
        if row:
            rows.append(row)
    for contact in personas.list_all():
        # A contact speaks through the agents made from it: judge it as one.
        as_agent = {"agent_id": "", "persona": contact["name"], "session": "",
                    "voice_id": contact.get("voice_id") or ""}
        row = _row(host, alone, "contact", {"persona_id": contact["persona_id"], "name": contact["name"],
                                     "builtin": bool(contact.get("builtin"))}, as_agent)
        if row:
            rows.append(row)
    report = {"voices": rows, "transcription": _transcription_plan(host, agents_db.list_agents()),
              "provider": host.cfg.tts_provider, "fallback": host.cfg.tts_fallback}
    report["digest"] = digest(report)
    return report


def digest(report: dict[str, Any]) -> str:
    """What --apply must be handed: the plan the dry run showed, as a short hash."""
    body = {k: report[k] for k in ("voices", "transcription", "provider", "fallback")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


class PlanChanged(Exception):
    """The plan at apply time is not the one the dry run showed."""


def _transcription_plan(host, agent_rows) -> dict:
    """Per agent, today's own engines and languages copied under its id. A key that
    matches several agents, or an agent matched by more than one key, is listed."""
    from . import stt_language
    out = {"engines": {}, "languages": {}, "shared_keys": [], "agents_with_several_keys": []}
    for setting, overrides in (("engines", host.agent_engines), ("languages", host.agent_languages)):
        holders: dict[str, list[str]] = {}
        for agent in agent_rows:
            keys = [k for k in (agent.get("persona"), agent.get("session"))
                    if k and str(k).strip().lower() in overrides]
            for key in keys:
                holders.setdefault(str(key).strip().lower(), []).append(agent["agent_id"])
            value = stt_language.match(overrides, agent)
            if value and str(agent["agent_id"]).lower() not in overrides:
                out[setting][agent["agent_id"]] = value
            if len({overrides[str(k).strip().lower()] for k in keys}) > 1:
                out["agents_with_several_keys"].append(
                    {"setting": setting, "agent_id": agent["agent_id"], "keys": keys})
        out["shared_keys"] += [{"setting": setting, "key": key, "agents": ids}
                               for key, ids in sorted(holders.items()) if len(ids) > 1]
    return out


def apply(report: dict[str, Any], *, reviewed: str) -> dict[str, Any]:
    """Write the rows with no audible change; refuse the others by name. ``report`` is the
    plan made now; ``reviewed`` the digest of the dry run that was read. Nothing is written
    when they differ (a row changed in between would be written unseen)."""
    from . import agents as agents_db, personas, settings_store, stt_language
    if not reviewed or digest(report) != reviewed:
        raise PlanChanged(f"the plan is {digest(report)}, the dry run read was {reviewed or 'none'}")
    written, refused = [], []
    for row in report["voices"]:
        label = f"{row['kind']} {row['name']}"
        if row["audible"]:
            refused.append(label)
            continue
        if row["kind"] == "agent":
            agents_db.update_agent(row["agent_id"], voice_id=row["new"])
        elif not row.get("builtin"):
            personas.set_stored_voice(row["persona_id"], row["new"])
        else:
            continue          # a built-in contact is rewritten from the configuration at start
        written.append(label)
    for setting, key in (("engines", stt_language.AGENT_ENGINES_KEY),
                         ("languages", stt_language.AGENT_LANGUAGES_KEY)):
        additions = report["transcription"][setting]
        if not additions:
            continue
        with settings_store.transaction():
            current = stt_language.parse_map(settings_store.get_text(key) or "{}")
            for agent_id, value in additions.items():
                current.setdefault(str(agent_id).lower(), value)
            settings_store.set_text(key, json.dumps(current, sort_keys=True, separators=(",", ":")))
        written += [f"{setting} {agent_id}" for agent_id in additions]
    return {"written": written, "refused_audible": refused}


def apply_at_start(cfg=None) -> dict[str, Any] | None:
    """At Host start, before any request: the rows with no audible change are brought to the
    one shape, the same rows ``--apply`` writes. The plan and the write are made in the same
    moment with nothing running between them, so the plan's own digest is the reviewed one.
    A row that would sound different under any provider is left as it is and logged by name:
    it stays the owner's to decide. None when there was nothing to do."""
    from .log import log
    report = plan(cfg)
    if not report["voices"] and not any(report["transcription"][k] for k in ("engines", "languages")):
        return None
    result = apply(report, reviewed=report["digest"])
    log("voiceShapeMigrated", f"written={len(result['written'])} "
        f"audible_left={len(result['refused_audible'])}"
        + "".join(f" left={name!r}" for name in result["refused_audible"]))
    return result
