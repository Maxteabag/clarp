"""Per-agent model / reasoning-effort overrides: storage, validation, and the
effective-value resolution used at turn dispatch (per-agent wins → config
default → CLI default)."""
from lib import agents as agents_db
from lib import backends, config
from lib.turn_dispatch import _resolve_llm


# ---- storage round-trip ------------------------------------------------

def test_create_and_update_persist_model_and_effort():
    aid = agents_db.create_agent(persona="Mike", voice_id="v", cwd="/tmp",
                                 session="mike", model="opus", effort="high")
    a = agents_db.get_by_session("mike")
    assert (a["model"], a["effort"]) == ("opus", "high")

    # Live edit — the next get reflects it immediately (next turn reads fresh).
    agents_db.update_agent(aid, model="haiku", effort="low")
    a = agents_db.get_by_session("mike")
    assert (a["model"], a["effort"]) == ("haiku", "low")

    # Partial update leaves the untouched field alone.
    agents_db.update_agent(aid, effort="medium")
    a = agents_db.get_by_session("mike")
    assert (a["model"], a["effort"]) == ("haiku", "medium")


def test_per_agent_mcp_servers_persist_and_default_empty():
    import json
    aid = agents_db.create_agent(persona="Adam", voice_id="v", cwd="/tmp",
                                 session="adam")
    # New agents default to no MCP servers.
    assert json.loads(agents_db.get_by_session("adam")["mcp_servers"]) == []

    # The app sets a per-agent selection; the next get reflects it.
    agents_db.update_agent(aid, mcp_servers=json.dumps(["teams-local"]))
    assert json.loads(agents_db.get_by_session("adam")["mcp_servers"]) == ["teams-local"]

    # Clearing it back to none.
    agents_db.update_agent(aid, mcp_servers="[]")
    assert json.loads(agents_db.get_by_session("adam")["mcp_servers"]) == []


def test_per_agent_heartbeat_defaults_off_and_persists():
    aid = agents_db.create_agent(persona="Domi", voice_id="v", cwd="/tmp",
                                 session="domi")
    assert agents_db.get_by_session("domi")["heartbeat_enabled"] == 0

    agents_db.update_agent(aid, heartbeat_enabled=True)
    assert agents_db.get_by_session("domi")["heartbeat_enabled"] == 1

    agents_db.update_agent(aid, heartbeat_enabled=False)
    assert agents_db.get_by_session("domi")["heartbeat_enabled"] == 0


def test_per_agent_dreaming_defaults_off_and_persists():
    aid = agents_db.create_agent(persona="Domi", voice_id="v", cwd="/tmp",
                                 session="domi")
    agent = agents_db.get_by_session("domi")
    assert agent["dreaming_enabled"] == 0
    assert agent["dreaming_last_local_date"] is None

    agents_db.update_agent(aid, dreaming_enabled=True,
                           dreaming_last_local_date="2026-06-24")
    agent = agents_db.get_by_session("domi")
    assert agent["dreaming_enabled"] == 1
    assert agent["dreaming_last_local_date"] == "2026-06-24"

    agents_db.update_agent(aid, dreaming_enabled=False)
    assert agents_db.get_by_session("domi")["dreaming_enabled"] == 0


def test_per_agent_mute_defaults_off_and_persists():
    aid = agents_db.create_agent(persona="Domi", voice_id="v", cwd="/tmp",
                                 session="domi")
    assert agents_db.get_by_session("domi")["muted"] == 0

    agents_db.update_agent(aid, muted=True)
    assert agents_db.get_by_session("domi")["muted"] == 1

    agents_db.update_agent(aid, muted=False)
    assert agents_db.get_by_session("domi")["muted"] == 0


# ---- effort validation per backend ------------------------------------

def test_clean_effort_per_backend():
    assert backends.clean_effort(backends.CLAUDE, "xhigh") == "xhigh"
    assert backends.clean_effort(backends.CLAUDE, "MAX") == "max"     # normalized
    # `codex debug models` reports xhigh/max on every listed model and ultra on
    # the 5.6 family; the adapter used to stop at "high", which silently
    # downgraded a pinned xhigh agent to the CLI default.
    assert backends.clean_effort(backends.CODEX, "xhigh") == "xhigh"
    assert backends.clean_effort(backends.CODEX, "ultra") == "ultra"
    assert backends.clean_effort(backends.CODEX, "bogus") == ""
    assert backends.clean_effort(backends.CODEX, "low") == "low"
    assert backends.clean_effort(backends.AGY, "low") == "low"
    assert backends.clean_effort(backends.CLAUDE, "bogus") == ""


# ---- effective resolution ---------------------------------------------

def test_agent_override_wins_over_config(monkeypatch):
    monkeypatch.setattr(config, "_CACHED",
                        config.Config(claude_model="sonnet", claude_effort="low"))
    model, effort = _resolve_llm(
        {"model": "opus", "effort": "high"}, backends.CLAUDE)
    assert (model, effort) == ("opus", "high")


def test_config_default_fills_when_agent_unset(monkeypatch):
    monkeypatch.setattr(config, "_CACHED",
                        config.Config(codex_model="gpt-5-codex",
                                      codex_reasoning_effort="low"))
    model, effort = _resolve_llm({"model": "", "effort": ""}, backends.CODEX)
    assert (model, effort) == ("gpt-5-codex", "low")


def test_agy_global_model_resolves_without_effort_override(monkeypatch):
    monkeypatch.setattr(config, "_CACHED",
                        config.Config(agy_model="gemini-x"))
    model, effort = _resolve_llm({"model": "", "effort": ""}, backends.AGY)
    assert model == "gemini-x"
    assert effort == ""
