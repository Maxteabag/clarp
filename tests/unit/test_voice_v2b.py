"""V2b (issue 476, ruling M): every creation path ends with a working voice, under the configured
provider AND its configured fallback; no existing agent changes; a helper speaks with its parent's
voice, a fork gets its own, a Janitor gets none."""
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


# ---------------------------------------------------------------- helpers, and what is not touched

@pytest.mark.parametrize("host", SETUPS, indirect=True)
def test_a_helper_speaks_with_its_parents_voice_and_takes_none_from_the_pool(host, tmp_path):
    """A helper is heard only when the user opens its chat; it sounds like the agent it works
    for, and helpers by the dozen never empty the pool."""
    service = AgentLifecycleService(_ctx(tmp_path))
    parent = agents_db.get_by_session(service.create({"name": "Pia", "cwd": str(tmp_path)}).session)
    helper = agents_db.get_by_session(service.create(
        {"name": "pia-helper", "cwd": str(tmp_path), "parent": parent["session"], "role": "helper",
         "voice_id": "{}", "synthesize_audio": False}).session)
    _works(host, helper)
    view = voice_profile.HostView.build(host, stored={})
    assert voice_profile.speech(view, helper)["voice_id"] == voice_profile.speech(view, parent)["voice_id"]
    for provider in voice_pool.needed_providers(host, "pia-helper"):
        assert json.loads(helper["voice_id"]).get(provider) == voice_pool._speaks(
            host, parent["voice_id"], "Pia", provider)


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_a_helper_of_a_voiceless_parent_still_gets_a_voice(host, tmp_path):
    parent = agents_db.create_agent(persona="Mute", voice_id="{}", cwd=str(tmp_path), session="mute")
    helper = AgentLifecycleService(_ctx(tmp_path)).create(
        {"name": "mute-helper", "cwd": str(tmp_path), "parent": parent, "role": "helper"})
    _works(host, agents_db.get_by_session(helper.session))
    assert agents_db.get_by_agent_id(parent)["voice_id"] == "{}"                  # the parent untouched


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_existing_agents_are_left_alone(host, tmp_path):
    existing = agents_db.create_agent(persona="Zed", voice_id="{}", cwd=str(tmp_path), session="zed")
    AgentLifecycleService(_ctx(tmp_path)).create({"name": "Another", "cwd": str(tmp_path)})
    assert agents_db.get_by_agent_id(existing)["voice_id"] == "{}"                # untouched


@pytest.mark.parametrize("host", [("cartesia", "none")], indirect=True)
def test_a_new_agent_given_a_bare_cartesia_uuid_is_stored_in_the_one_shape_and_heard(host, tmp_path):
    """A bare uuid kept bare reads as an ElevenLabs id, so under Cartesia the agent was silent."""
    uuid = "0b904166-a29f-4d2e-bb20-41ca302f98e9"
    row = agents_db.get_by_session(AgentLifecycleService(_ctx(tmp_path)).create(
        {"name": "Bjorn", "cwd": str(tmp_path), "voice_id": uuid}).session)
    assert row["voice_id"] == json.dumps({"cartesia": uuid}, separators=(",", ":"))
    assert voice_profile.speech(voice_profile.HostView.build(host, stored={}), row)["voice_id"] == uuid


# ---------------------------------------------------------------- the pool itself

def test_the_walk_is_stable_and_two_agents_do_not_share_while_a_voice_is_free():
    cfg = config.Config(tts_provider="cartesia")
    first = voice_pool.assign(cfg, "Quentin", "{}", agents=[], contacts=[])
    assert first == voice_pool.assign(cfg, "Quentin", "{}", agents=[], contacts=[])
    taken = [{"agent_id": "a", "persona": "Quentin", "voice_id": first}]
    second = voice_pool.assign(cfg, "Quentin", "{}", agents=taken, contacts=[])
    assert json.loads(second)["cartesia"] != json.loads(first)["cartesia"]


def test_an_exhausted_pool_reuses_the_least_recently_used_voice_and_logs_it(monkeypatch):
    """v2 has one holder, v1 two; but v2's holder spoke an hour ago and v1's last holder a day
    ago: the least recently used voice is v1. An agent without a clip counts from its creation."""
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a))
    hour, day = 3_600_000, 86_400_000
    monkeypatch.setattr(voice_pool, "_last_clips", lambda: {"a": 10 * day - day, "c": 10 * day - hour})
    cfg = config.Config(tts_provider="cartesia", cartesia_pool=("v1", "v2"), cartesia_voices={})
    agents = [{"agent_id": "a", "persona": "A", "voice_id": '{"cartesia":"v1"}', "created_at": 1},
              {"agent_id": "b", "persona": "B", "voice_id": '{"cartesia":"v1"}', "created_at": 2,
               "archived_at": 3},
              {"agent_id": "c", "persona": "C", "voice_id": '{"cartesia":"v2"}', "created_at": 1}]
    assert json.loads(voice_pool.assign(cfg, "D", "{}", agents=agents, contacts=[]))["cartesia"] == "v1"
    assert [a[0] for a in logged] == ["voicePoolExhausted"]
    assert "reused=v1" in logged[0][1] and f"last_used_ms={9 * day}" in logged[0][1]
    # Without any clip: the voice whose holders were created longest ago.
    monkeypatch.setattr(voice_pool, "_last_clips", lambda: {})
    agents[2]["created_at"] = 0
    assert json.loads(voice_pool.assign(cfg, "D", "{}", agents=agents, contacts=[]))["cartesia"] == "v2"


def test_given_voices_are_kept_and_nothing_is_added_when_nothing_is_missing():
    cfg = config.Config(tts_provider="cartesia", cartesia_voices={"Ada": "c-ada"})
    assert voice_pool.assign(cfg, "Ada", "V_ADA", agents=[], contacts=[]) == '{"elevenlabs":"V_ADA"}'
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


def test_without_a_catalogue_the_curated_pool_and_no_false_alarm(monkeypatch):
    """No catalogue fetched yet: the curated English pool as it ships; a built-in contact's
    voice is not handed out while a pool voice is free; no exhaustion."""
    monkeypatch.setattr(voice_pool, "_cartesia_catalogue", lambda language="en": [])
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a[0]))
    cfg = config.Config(tts_provider="cartesia")
    builtins = [{"name": n, "builtin": 1, "voice_id": json.dumps({"cartesia": v})}
                for n, v in cfg.cartesia_voices.items() if v]
    curated = [row["id"] for row in voice_pool._curated_rows()]
    assert voice_pool.pool(cfg, "cartesia") == curated
    picked = json.loads(voice_pool.assign(cfg, "Quentin", "{}", agents=[], contacts=builtins))["cartesia"]
    assert picked in curated and picked not in set(cfg.cartesia_voices.values())
    assert "voicePoolExhausted" not in logged
    # A language the curated pool does not cover, with no catalogue: the built-in list.
    assert voice_pool.pool(cfg, "cartesia", "no") == [
        v for v in dict.fromkeys(config.DEFAULT_CARTESIA_VOICES.values()) if v]


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


def test_contacts_and_archived_agents_count_as_users():
    """A contact's voice is taken, and so is an archived agent's: it can be restored with it
    (issue 476: no two agents may sound the same, archived ones included)."""
    cfg = config.Config(tts_provider="cartesia", cartesia_pool=("v1", "v2"), cartesia_voices={})
    name = _name_starting_at(0, 2)                       # its walk starts at v1
    contact = [{"name": "Cora", "voice_id": '{"cartesia":"v1"}'}]
    assert json.loads(voice_pool.assign(cfg, name, "{}", agents=[], contacts=contact))["cartesia"] == "v2"
    archived = [{"agent_id": "a", "persona": "Old", "voice_id": '{"cartesia":"v1"}', "archived_at": 1}]
    assert json.loads(voice_pool.assign(cfg, name, "{}", agents=archived, contacts=[]))["cartesia"] == "v2"


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


def test_real_api_rows_are_picked_and_private_owned_or_unknown_never(tmp_path, monkeypatch):
    """Live bug (21 Nov): rows in Cartesia's real shape (access, visibility, is_owner; no
    is_public) must be eligible; a private, an owned and an unknown-owner row never are; and a
    language the catalogue has no eligible voice for is logged before the built-in fallback."""
    from lib import voice_names
    names_file = tmp_path / "voice-names.json"
    monkeypatch.setenv("CLARP_VOICE_NAMES", str(names_file))
    base = {"access": "public", "visibility": "all", "is_owner": False, "status": "active", "name": "x"}
    unknown = {k: v for k, v in base.items() if k != "is_owner"}
    rows = [{"id": "en-public", "language": "en", **base},
            {"id": "en-private", "language": "en", **base, "access": "private", "visibility": "owner"},
            {"id": "en-owned", "language": "en", **base, "is_owner": True},
            {"id": "en-unknown", "language": "en", **unknown}]
    cfg = config.Config(tts_provider="cartesia", cartesia_api_key="ck", cartesia_voices={})
    voice_names.refresh(cfg, path=names_file, cartesia_all=lambda key: rows,
                        cartesia_one=lambda key, v: {}, eleven_one=lambda key, v: {},
                        voices_in_use=lambda _cfg: {"cartesia": set(), "elevenlabs": set()})
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a))
    assert voice_pool.pool(cfg, "cartesia", "en") == ["en-public"]
    picks = {json.loads(voice_pool.assign(cfg, f"Name{i}", "{}", agents=[], contacts=[]))["cartesia"]
             for i in range(10)}
    assert picks == {"en-public"}
    assert not [a for a in logged if a[0] == "voicePoolNoCatalogueVoice"]
    voice_pool.pool(cfg, "cartesia", "no")                          # no Norwegian row at all
    assert [a[1] for a in logged if a[0] == "voicePoolNoCatalogueVoice"] == [
        "provider=cartesia language=no eligible=0 total=4"]


# ---------------------------------------------------------------- the curated Cartesia pool

def test_the_curated_pool_follows_its_rules():
    """One voice per id and per name, English, native accents only, no emotion variants."""
    import pathlib
    data = json.loads(voice_pool.CURATED_PATH.read_text())
    rows = data["voices"]
    assert data["provider"] == "cartesia" and data["language"] == "en" and len(rows) >= 150
    assert len({r["id"] for r in rows}) == len(rows)
    assert len({r["name"].casefold() for r in rows}) == len(rows)
    assert {r["country"] for r in rows} <= {"US", "GB", "AU", "CA", "NZ", "IE", "ZA", "SG", "IN"}
    assert {r["gender"] for r in rows} >= {"feminine", "masculine"}
    # Neighbours differ: no long run of one gender.
    genders = [r["gender"] for r in rows]
    assert max(len(list(g)) for _, g in __import__("itertools").groupby(genders)) <= 30
    assert voice_pool.curated_names()[rows[0]["id"]] == rows[0]["name"]
    assert pathlib.Path(voice_pool.CURATED_PATH).name == "cartesia_voice_pool.json"


def test_the_curated_pool_skips_what_todays_catalogue_no_longer_offers(tmp_path, monkeypatch):
    rows = voice_pool._curated_rows()
    first, second, third = (str(r["id"]) for r in rows[:3])
    names_file = tmp_path / "voice-names.json"
    monkeypatch.setenv("CLARP_VOICE_NAMES", str(names_file))
    public_en = {"language": "en", "public": True, "owner": False}
    names_file.write_text(json.dumps({"cartesia": {}, "_format": 2,
        "_catalogue": {"cartesia": {first: public_en, second: public_en, "other": public_en}},
        "_gone": {"cartesia": [second]}}))
    cfg = config.Config(tts_provider="cartesia", cartesia_voices={})
    assert voice_pool.pool(cfg, "cartesia", "en") == [first]          # third: not offered; second: deleted
    assert third not in voice_pool.pool(cfg, "cartesia", "en")


def test_the_build_script_applies_the_rules():
    import importlib.util, pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "build_cartesia_voice_pool.py"
    spec = importlib.util.spec_from_file_location("build_pool", path)
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    native = [{"accent": "british", "locale": "en-GB", "is_native": True}]
    base = {"language": "en", "access": "public", "visibility": "all", "is_owner": False,
            "status": "active", "country": "GB", "accents": native, "gender": "feminine"}
    rows = [
        {**base, "id": "1", "name": "Ada", "tagline": "Warm Guide", "description": "Warm", "created_at": "1"},
        {**base, "id": "2", "name": "Ada", "tagline": "Sad Warm Guide", "description": "Warm", "created_at": "2"},
        {**base, "id": "3", "name": "Bo", "tagline": "Night Warden", "description": "Deep"},
        {**base, "id": "4", "name": "Cy", "tagline": "Guide", "description": "x", "country": "IT"},
        {**base, "id": "5", "name": "Di", "tagline": "Guide", "description": "x", "is_owner": True},
        {**base, "id": "6", "name": "Ed", "tagline": "Guide", "description": "x", "language": "de"},
        {**base, "id": "7", "name": "Fa", "tagline": "Guide", "description": "x",
         "accents": [{"accent": "x", "locale": "es-MX", "is_native": True}]},
        {**base, "id": "8", "name": "Gil", "tagline": "Calm Guide", "description": "Calm",
         "gender": "masculine", "country": "US"},
    ]
    out = build.build(rows)
    assert sorted(v["id"] for v in out["voices"]) == ["1", "8"]
