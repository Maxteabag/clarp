"""V2b (issue 476, ruling M): every creation path ends with a working voice, under the configured
provider AND its configured fallback; no existing agent changes; helpers, forks and Janitors get none."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from lib import (agents as agents_db, config, contact_assignment, contact_minting, personas,
                 roster_seed, voice_pool, voice_profile)
from lib.agent_lifecycle import AgentLifecycleService

SETUPS = [("cartesia", "none"), ("cartesia", "elevenlabs"), ("elevenlabs", "none"),
          ("elevenlabs", "cartesia"), ("gemini", "cartesia"), ("google", "cartesia"), ("deepgram", "none")]


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _ctx(tmp_path):
    return SimpleNamespace(agents_path=tmp_path / "unused.json", stream=_Stream(),
                           speak_announcement=lambda *a, **k: None)


@pytest.fixture
def host(monkeypatch, request):
    provider, fallback = request.param
    cfg = config.Config(tts_provider=provider, tts_fallback=fallback, cartesia_api_key="ck",
                        eleven_api_key="ek", gemini_api_key="gk", deepgram_api_key="dk",
                        google_stt_project="p")
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(config, "_CACHED", cfg)
    from lib import google_tts
    monkeypatch.setattr(google_tts, "collision_code", lambda cfg=None: None)
    monkeypatch.setattr(google_tts, "credentials",
                        lambda cfg: google_tts.Credentials("google_stt", "/adc.json", "p", ""))
    return cfg


def _heard(cfg, row) -> list:
    """silent_reason under the configured provider, and under the fallback alone."""
    agent = {"agent_id": row.get("agent_id", ""), "persona": row.get("persona") or row.get("name"),
             "session": row.get("session", ""), "voice_id": row.get("voice_id") or ""}
    out = [voice_profile.speech(voice_profile.HostView.build(cfg, stored={}), agent)["silent_reason"]]
    if cfg.tts_fallback not in ("none", cfg.tts_provider):
        alone = dataclasses.replace(cfg, tts_provider=cfg.tts_fallback, tts_fallback="none")
        out.append(voice_profile.speech(voice_profile.HostView.build(alone, stored={}), agent)["silent_reason"])
    return out


def _works(cfg, row):
    assert _heard(cfg, row) == [None] * len(_heard(cfg, row)), row


# ---------------------------------------------------------------- every creation path

@pytest.mark.parametrize("host", SETUPS, indirect=True)
def test_a_new_agent_under_a_name_nobody_has(host, tmp_path):
    result = AgentLifecycleService(_ctx(tmp_path)).create({"name": "Quentin", "cwd": str(tmp_path)})
    row = agents_db.get_by_session(result.session)
    _works(host, row)
    # Before V2b: the first roster voice, an ElevenLabs id every such agent shared.
    assert "nPczCjzI2devNBz1zQrb" not in row["voice_id"]


@pytest.mark.parametrize("host", SETUPS, indirect=True)
def test_a_new_agent_from_a_saved_contact_and_from_an_adopted_session(host, tmp_path):
    contact = personas.create(name="Wilma", voice_id="")                 # a contact with no voice
    _works(host, contact)
    result = AgentLifecycleService(_ctx(tmp_path)).create({"name": "Wilma", "cwd": str(tmp_path)})
    _works(host, agents_db.get_by_session(result.session))
    adopted = AgentLifecycleService(_ctx(tmp_path)).create(
        {"auto_contact": True, "cwd": str(tmp_path), "backend": "claude"})
    _works(host, agents_db.get_by_session(adopted.session))


@pytest.mark.parametrize("host", SETUPS, indirect=True)
def test_a_minted_contact_and_a_phones_placeholder(host):
    minted = contact_minting.mint_contact("claude", generate_avatar=False)
    _works(host, minted)
    placeholder = personas.create(name="Petra", voice_id=json.dumps({"cartesia": "auto-1234"}))
    assert "auto-" not in placeholder["voice_id"]
    _works(host, placeholder)


@pytest.mark.parametrize("host", SETUPS, indirect=True)
def test_an_anonymous_launch_and_an_assigned_contact(host, tmp_path):
    anon = AgentLifecycleService(_ctx(tmp_path)).create(
        {"anonymous": True, "cwd": str(tmp_path), "backend": "claude"})
    _works(host, agents_db.get_by_session(anon.session))
    plain = AgentLifecycleService(_ctx(tmp_path)).create({"name": "Ida", "cwd": str(tmp_path)})
    contact_assignment.assign_contact(plain.session, mode="create", name="Ingrid")
    _works(host, agents_db.get_by_session(plain.session))


@pytest.mark.parametrize("host", SETUPS, indirect=True)
def test_the_first_install_roster(host, tmp_path):
    db_rows = roster_seed.seed_defaults(
        "claude", cwd=tmp_path, voice_for=lambda name, voice: voice_pool.assign(host, name, voice))
    assert db_rows > 0
    for row in agents_db.list_agents():
        _works(host, row)


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_a_rename_keeps_the_voice_it_spoke_with(host, tmp_path):
    result = AgentLifecycleService(_ctx(tmp_path)).create(
        {"name": "Rachel", "cwd": str(tmp_path), "voice_id": "21m00Tcm4TlvDq8ikWAM"})
    row = agents_db.get_by_session(result.session)
    before = voice_profile.speech(voice_profile.HostView.build(host, stored={}), row)
    pinned = voice_pool.pinned_before_rename(host, row)
    after = voice_profile.speech(voice_profile.HostView.build(host, stored={}),
                                 {**row, "persona": "Someone Else", "voice_id": pinned})
    assert (after["provider"], after["voice_id"]) == (before["provider"], before["voice_id"])


# ---------------------------------------------------------------- what V2b does not touch

@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_helpers_forks_janitors_and_existing_agents_are_left_alone(host, tmp_path):
    parent = AgentLifecycleService(_ctx(tmp_path)).create({"name": "Pia", "cwd": str(tmp_path)})
    helper = AgentLifecycleService(_ctx(tmp_path)).create(
        {"name": "pia-helper", "cwd": str(tmp_path), "parent": parent.session, "role": "helper",
         "voice_id": "{}", "synthesize_audio": False})
    assert agents_db.get_by_session(helper.session)["voice_id"] == "{}"           # V2c: through the parent
    existing = agents_db.create_agent(persona="Zed", voice_id="{}", cwd=str(tmp_path), session="zed")
    AgentLifecycleService(_ctx(tmp_path)).create({"name": "Another", "cwd": str(tmp_path)})
    assert agents_db.get_by_agent_id(existing)["voice_id"] == "{}"                # untouched


# ---------------------------------------------------------------- the pool itself

def test_the_walk_is_stable_and_two_agents_do_not_share_while_a_voice_is_free():
    cfg = config.Config(tts_provider="cartesia")
    first = voice_pool.assign(cfg, "Quentin", "{}", agents=[], contacts=[])
    assert first == voice_pool.assign(cfg, "Quentin", "{}", agents=[], contacts=[])
    taken = [{"agent_id": "a", "persona": "Quentin", "voice_id": first}]
    second = voice_pool.assign(cfg, "Quentin", "{}", agents=taken, contacts=[])
    assert json.loads(second)["cartesia"] != json.loads(first)["cartesia"]


def test_an_exhausted_pool_gives_the_least_shared_voice(monkeypatch):
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a))
    cfg = config.Config(tts_provider="cartesia", cartesia_pool=("v1", "v2"))
    agents = [{"agent_id": "a", "persona": "A", "voice_id": '{"cartesia":"v1"}'},
              {"agent_id": "b", "persona": "B", "voice_id": '{"cartesia":"v1"}'},
              {"agent_id": "c", "persona": "C", "voice_id": '{"cartesia":"v2"}'}]
    assert json.loads(voice_pool.assign(cfg, "D", "{}", agents=agents, contacts=[]))["cartesia"] == "v2"
    assert logged and logged[0][0] == "voicePoolExhausted"


def test_given_voices_are_kept_and_nothing_is_added_when_nothing_is_missing():
    cfg = config.Config(tts_provider="cartesia", cartesia_voices={"Ada": "c-ada"})
    assert voice_pool.assign(cfg, "Ada", "V_ADA", agents=[], contacts=[]) == "V_ADA"
    assert json.loads(voice_pool.assign(cfg, "Zed", "V_ZED", agents=[], contacts=[]))["elevenlabs"] == "V_ZED"


@pytest.mark.parametrize("host", [("cartesia", "none"), ("google", "cartesia")], indirect=True)
def test_a_janitor_is_excluded_by_name_and_its_profile_says_why(host, tmp_path):
    """Ruling M excludes the Janitor role: no voice from the pool, and its profile names
    its own reason (voiceless_role), not no_voice, under every provider."""
    janitor = agents_db.create_agent(persona="Rivet", voice_id="", cwd=str(tmp_path), session="rivet")
    from lib import db
    db.conn().execute("UPDATE agents SET is_janitor = 1 WHERE agent_id = ?", (janitor,))
    row = agents_db.get_by_agent_id(janitor)
    sp = voice_profile.speech(voice_profile.HostView.build(host, stored={}), row)
    assert (sp["voice_id"], sp["silent_reason"]) == (None, "voiceless_role")
    assert row["voice_id"] == ""                                          # the pool never touched it
