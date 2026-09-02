"""Session-keyed agent store backed by the SQLite source of truth.

The HTTP layer uses this adapter when it needs to synchronize a complete
session map. Domain code should reach for `lib.agents` directly.

Roster + voice-catalogue helpers are thin wrappers around `lib.config`.
"""
from __future__ import annotations

import pathlib
from typing import Any

from . import agents as _agents
from .config import load as load_config
from .log import log
from .protocol import AgentBackend
from . import xdg


# Kept as a stable dependency-injection hint for callers that pass a store
# location. SQLite is the source of truth.
AGENTS_FILE = xdg.config_dir() / "agents.json"


# ---- roster + catalogue helpers ---------------------------------------

def get_roster() -> dict[str, str]:
    return dict(load_config().roster)


def get_catalog() -> list[dict[str, str]]:
    return list(load_config().catalog)


def get_voice_pool() -> list[str]:
    return list(get_roster().values())


AGENT_ROSTER = get_roster()
VOICE_CATALOG = get_catalog()
AGENT_VOICE_POOL = get_voice_pool()


# ---- session-map API --------------------------------------------------

def load_agents(path: pathlib.Path = AGENTS_FILE) -> dict[str, dict[str, Any]]:
    """Return the agents map in the {session: {...}} shape."""
    return _agents.session_dict()


def save_agents(data: dict[str, dict[str, Any]],
                path: pathlib.Path = AGENTS_FILE) -> None:
    """Sync a complete session map back into the DB.

    Three buckets:
      - session in `data` not in DB → create_agent
      - session in DB not in `data` → soft_delete
      - both                              → update_voice if it changed
    """
    current = _agents.session_dict()
    new = data or {}
    incoming = set(new.keys())
    existing = set(current.keys())

    for session in incoming - existing:
        info = new[session] or {}
        try:
            _agents.create_agent(
                persona=str(info.get("name") or session),
                voice_id=str(info.get("voice_id") or ""),
                cwd=str(info.get("cwd") or str(pathlib.Path.home())),
                session=session,
                backend=str(info.get("backend") or AgentBackend.CLAUDE),
            )
        except Exception as e:  # noqa: BLE001 — write path, log + continue
            log("agentCreateSyncFail", f"{session} :: {e}")

    for session in existing - incoming:
        agent_id = current[session]["agent_id"]
        _agents.soft_delete(agent_id)

    for session in incoming & existing:
        info = new[session] or {}
        agent_id = current[session]["agent_id"]
        new_persona = str(info.get("name") or current[session]["name"])
        new_voice = str(info.get("voice_id") or current[session]["voice_id"])
        new_cwd = str(info.get("cwd") or current[session]["cwd"])
        cur_backend = current[session].get("backend") or AgentBackend.CLAUDE
        new_backend = str(info.get("backend") or cur_backend)
        if (new_persona != current[session]["name"]
                or new_voice != current[session]["voice_id"]
                or new_cwd != current[session]["cwd"]
                or new_backend != cur_backend):
            _agents.update_agent(agent_id, persona=new_persona,
                                 voice_id=new_voice, cwd=new_cwd,
                                 backend=new_backend)


def pick_unused_voice(agents: dict, pool: list[str] | None = None) -> str:
    """Pick a voice not already in use; cycle through the pool if all taken."""
    pool = pool if pool is not None else get_voice_pool()
    used = {(info or {}).get("voice_id") for info in agents.values()}
    for v in pool:
        if v not in used:
            return v
    log("pickUnusedVoiceCycle", f"all {len(pool)} voices in use")
    return pool[len(agents) % len(pool)]
