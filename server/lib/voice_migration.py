"""The voice migration (issue 476, V2a): a report first, and writes only where nothing changes.

For every agent and every saved contact: the stored voice value, the one stored shape it
would become (voice.canonical), what it is heard with today and what it would be heard with
after, under the Host's configured provider AND under its configured fallback alone. A row
whose two answers differ is an AUDIBLE change: --apply never writes it and names it; it is
the owner's to decide (V2c). The transcription settings get keys by agent id the same way:
each agent's value today is copied under its id; a key that several agents share is listed.

Nothing here calls a provider: the answers come from the resolver the speaking path uses
(voice_profile.speech).
"""
from __future__ import annotations

import dataclasses
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


def _row(host, kind: str, ident: dict, agent: dict) -> dict | None:
    old = str(agent.get("voice_id") or "")
    new = canonical(old)
    if new == old:
        return None
    today = _heard(host, agent)
    after = _heard(host, {**agent, "voice_id": new})
    return {"kind": kind, **ident, "old": old, "new": new, "today": today, "after": after,
            "audible": today != after}


def plan(cfg=None) -> dict[str, Any]:
    """The whole report; reads only."""
    from . import agents as agents_db, personas
    host = voice_profile.HostView.build(cfg)
    rows = []
    for agent in agents_db.list_agents():
        row = _row(host, "agent", {"agent_id": agent["agent_id"], "name": agent["persona"],
                                   "session": agent["session"]}, agent)
        if row:
            rows.append(row)
    for contact in personas.list_all():
        # A contact speaks through the agents made from it: judge it as one.
        as_agent = {"agent_id": "", "persona": contact["name"], "session": "",
                    "voice_id": contact.get("voice_id") or ""}
        row = _row(host, "contact", {"persona_id": contact["persona_id"], "name": contact["name"],
                                     "builtin": bool(contact.get("builtin"))}, as_agent)
        if row:
            rows.append(row)
    return {"voices": rows, "transcription": _transcription_plan(host, agents_db.list_agents()),
            "provider": host.cfg.tts_provider, "fallback": host.cfg.tts_fallback}


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


def apply(report: dict[str, Any]) -> dict[str, Any]:
    """Write the rows with no audible change; refuse the others by name."""
    from . import agents as agents_db, personas, settings_store, stt_language
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
