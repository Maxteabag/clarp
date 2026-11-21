"""V2b (issue 476, ruling M): every creation path ends with a working voice, under the configured
provider AND its configured fallback; no existing agent changes; helpers, forks and Janitors get none."""
from __future__ import annotations

import dataclasses
import json
from collections import Counter
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


# ---------------------------------------------------------------- review 4 of 515

def _walk_start(name: str, size: int) -> int:
    import hashlib
    return int(hashlib.sha256(name.strip().casefold().encode()).hexdigest(), 16) % size


def _name_starting_at(index: int, size: int) -> str:
    return next(f"N{i}" for i in range(10_000) if _walk_start(f"N{i}", size) == index)


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_seventy_new_agents_get_seventy_voices_none_a_built_in_contacts(host, tmp_path, monkeypatch):
    """Finding 1 (Peter, 20 Nov: no two agents may sound the same): the default pool is the
    catalogue minus every voice a built-in contact, agent or contact holds; a built-in contact
    keeps its own voice for its own name."""
    built_in = {v for v in host.cartesia_voices.values() if v}
    assert len(built_in) >= 60
    catalogue = sorted(built_in | {f"cat-{i:03d}" for i in range(120)})
    monkeypatch.setattr(voice_pool, "_cartesia_catalogue", lambda language="en": catalogue)
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a[0]))
    personas.list_all()                                         # the built-in contacts exist
    service = AgentLifecycleService(_ctx(tmp_path))
    given = []
    for i in range(70):
        row = agents_db.get_by_session(service.create({"name": f"Newcomer{i}", "cwd": str(tmp_path)}).session)
        given.append(json.loads(row["voice_id"])["cartesia"])
    assert len(set(given)) == 70 and not set(given) & built_in
    assert "voicePoolExhausted" not in logged
    owner = next(n for n, v in host.cartesia_voices.items() if v and n.isalpha())
    row = agents_db.get_by_session(service.create({"name": owner, "cwd": str(tmp_path)}).session)
    speaks = voice_profile.speech(voice_profile.HostView.build(host, stored={}), row)["voice_id"]
    assert speaks == host.cartesia_voices[owner] and speaks not in given


def test_without_a_catalogue_the_built_in_list_and_no_false_alarm(monkeypatch):
    """No catalogue fetched yet: today's list; the log line only when active agents and
    custom contacts really use every pool voice."""
    monkeypatch.setattr(voice_pool, "_cartesia_catalogue", lambda language="en": [])
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a[0]))
    cfg = config.Config(tts_provider="cartesia")
    builtins = [{"name": n, "builtin": 1, "voice_id": json.dumps({"cartesia": v})}
                for n, v in cfg.cartesia_voices.items() if v]
    assert voice_pool.pool(cfg, "cartesia") == [v for v in dict.fromkeys(config.DEFAULT_CARTESIA_VOICES.values()) if v]
    voice_pool.assign(cfg, "Quentin", "{}", agents=[], contacts=builtins)
    assert "voicePoolExhausted" not in logged


def _slow_users(monkeypatch, delay=0.3):
    import time
    real = voice_pool.users

    def slow(*a, **k):
        out = real(*a, **k)
        time.sleep(delay)
        return out
    monkeypatch.setattr(voice_pool, "users", slow)


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_two_contacts_or_an_agent_and_a_contact_at_once_get_two_voices(host, tmp_path, monkeypatch):
    """Finding 2: every pick and its insert run under one lock (reviewer 4's slowed read)."""
    import threading
    pooled = dataclasses.replace(host, cartesia_pool=("v1", "v2", "v3", "v4", "v5", "v6"))
    monkeypatch.setattr(config, "load", lambda path=None: pooled)
    monkeypatch.setattr(config, "_CACHED", pooled)
    for i in range(2):
        agents_db.create_agent(persona=f"Occ{i}", voice_id=json.dumps({"cartesia": f"v{i+1}"}),
                               cwd=str(tmp_path), session=f"occ{i}")
    _slow_users(monkeypatch)
    out = {}

    def contact(name):
        out[name] = json.loads(personas.create(name=name)["voice_id"])["cartesia"]

    def agent(name):
        s = AgentLifecycleService(_ctx(tmp_path)).create({"name": name, "cwd": str(tmp_path)}).session
        out[name] = json.loads(agents_db.get_by_session(s)["voice_id"])["cartesia"]
    for pair in ((contact, "Cora", contact, "Dale"), (agent, "Ezra", contact, "Faye")):
        threads = [threading.Thread(target=pair[0], args=(pair[1],)),
                   threading.Thread(target=pair[2], args=(pair[3],))]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
    assert len({out["Cora"], out["Dale"]}) == 2 and len({out["Ezra"], out["Faye"]}) == 2
    assert len(set(out.values())) == 4 and not set(out.values()) & {"v1", "v2"}


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_a_relaunch_keeps_its_voice(host, tmp_path):
    """Finding 3: the replace_sid guard; a relaunch is not a new agent."""
    agents_db.create_agent(persona="Zed", voice_id="{}", cwd=str(tmp_path), session="zed")
    AgentLifecycleService(_ctx(tmp_path)).create({"name": "Zed", "replace_sid": "zed"})
    assert agents_db.get_by_session("zed")["voice_id"] == "{}"


def test_contacts_count_as_users_and_archived_agents_do_not():
    """Finding 3: a contact's voice is taken; an archived agent's is free again."""
    cfg = config.Config(tts_provider="cartesia", cartesia_pool=("v1", "v2"), cartesia_voices={})
    name = _name_starting_at(0, 2)                       # its walk starts at v1
    contact = [{"name": "Cora", "voice_id": '{"cartesia":"v1"}'}]
    assert json.loads(voice_pool.assign(cfg, name, "{}", agents=[], contacts=contact))["cartesia"] == "v2"
    archived = [{"agent_id": "a", "persona": "Old", "voice_id": '{"cartesia":"v1"}', "archived_at": 1}]
    assert json.loads(voice_pool.assign(cfg, name, "{}", agents=archived, contacts=[]))["cartesia"] == "v1"


def test_the_walk_starts_at_the_names_hash():
    """Finding 3: one name's pick, pinned."""
    pool = tuple(f"v{i}" for i in range(1, 8))
    cfg = config.Config(tts_provider="cartesia", cartesia_pool=pool, cartesia_voices={})
    name = _name_starting_at(5, len(pool))
    assert json.loads(voice_pool.assign(cfg, name, "{}", agents=[], contacts=[]))["cartesia"] == pool[5]
    assert voice_pool.pick(cfg, "cartesia", "Quentin", Counter()) == pool[_walk_start("Quentin", len(pool))]


def test_the_pool_hands_out_only_todays_catalogue(tmp_path, monkeypatch):
    """Live find (21 Nov): Cartesia deleted voices agents spoke with. A voice still remembered
    by name but missing from today's catalogue, or answered 404, is never handed out."""
    names_file = tmp_path / "voice-names.json"
    monkeypatch.setenv("CLARP_VOICE_NAMES", str(names_file))
    public_en = {"language": "en", "public": True, "owner": False}
    names_file.write_text(json.dumps({
        "cartesia": {"old-deleted": "Old", "today-1": "One", "today-2": "Two", "gone-1": "Gone"},
        "_catalogue": {"cartesia": {"today-1": public_en, "today-2": public_en, "gone-1": public_en}},
        "_gone": {"cartesia": ["gone-1"]}}))
    cfg = config.Config(tts_provider="cartesia", cartesia_voices={})
    assert voice_pool.pool(cfg, "cartesia") == ["today-1", "today-2"]
    picks = {json.loads(voice_pool.assign(cfg, f"Name{i}", "{}", agents=[], contacts=[]))["cartesia"]
             for i in range(20)}
    assert picks <= {"today-1", "today-2"}


def test_never_the_owners_own_voice_nor_another_languages_and_norwegian_gets_norwegian(tmp_path, monkeypatch):
    """Review 4 of 515 (second pass): the pool takes only public catalogue rows that are not the
    account's own (a voice Peter cloned is never handed out), in the agent's speech language
    (its language set first, R168): an agent set to Norwegian gets a Norwegian voice."""
    from lib import settings_store, stt_language
    names_file = tmp_path / "voice-names.json"
    monkeypatch.setenv("CLARP_VOICE_NAMES", str(names_file))
    row = lambda language, public=True, owner=False: {"language": language, "public": public, "owner": owner}
    names_file.write_text(json.dumps({"cartesia": {}, "_catalogue": {"cartesia": {
        "en-public": row("en"), "en-owned": row("en", owner=True), "en-private": row("en", public=False),
        "no-public": row("no"), "nb-public": row("nb"), "de-public": row("de")}}}))
    cfg = config.Config(tts_provider="cartesia", cartesia_voices={})
    assert voice_pool.pool(cfg, "cartesia", "en") == ["en-public"]
    assert voice_pool.pool(cfg, "cartesia", "no") == ["nb-public", "no-public"]
    english = {json.loads(voice_pool.assign(cfg, f"Name{i}", "{}", agents=[], contacts=[]))["cartesia"]
               for i in range(10)}
    assert english == {"en-public"}                                 # never owned, private or German
    settings_store.set_text(stt_language.AGENT_LANGUAGES_KEY, json.dumps({"ola": "no,en"}))
    ola = json.loads(voice_pool.assign(cfg, "Ola", "{}", agents=[], contacts=[]))["cartesia"]
    assert ola in {"no-public", "nb-public"}
