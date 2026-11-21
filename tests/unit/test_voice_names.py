"""Voice names and the configured language set (the live profile read null voice_label on 149
of 155 rows; an agent on Scribe with Norwegian and English read languages []).

voice_label comes from tables and a names file filled off the read path (voice_names): a
snapshot build opens no socket and adds no query, whether the file is missing, stale or full.
"""
from __future__ import annotations

import json
import pathlib
import socket

import pytest

from lib import snapshot, voice_names, voice_profile
from tests.unit.test_voice_profile import (ADA_CARTESIA, _cfg, _host, _profile_queries, _seed,  # noqa: F401
                                 quiet_snapshot)

OTHER = "c0000000-0000-4000-8000-0000000000ff"
NORSK = "c0000000-0000-4000-8000-00000000000b"


@pytest.fixture
def names_file(tmp_path, monkeypatch):
    path = tmp_path / "voice-names.json"
    monkeypatch.setenv("CLARP_VOICE_NAMES", str(path))
    return path


ELEVEN = "nPczCjzI2devNBz1zQrb"


def _refresh(cfg, path, *, pages, by_id=None, in_use=(), eleven_in_use=(), fail=False):
    asked = []

    def cartesia_all(key):
        asked.append(("all", key))
        if fail:
            raise OSError("down")
        return pages

    def one(provider):
        def fetch(key, voice_id):
            asked.append((provider, voice_id))
            return (by_id or {}).get(voice_id) or {}
        return fetch
    done = voice_names.refresh(cfg, path=path, cartesia_all=cartesia_all, cartesia_one=one("one"),
                               eleven_one=one("eleven"),
                               voices_in_use=lambda _cfg: {"cartesia": set(in_use),
                                                           "elevenlabs": set(eleven_in_use)})
    return done, asked


# ---------------------------------------------------------------- the file

def test_the_catalogue_and_the_voices_in_use_it_missed_are_written(names_file):
    done, asked = _refresh(_cfg("cartesia"), names_file,
                           pages=[{"id": ADA_CARTESIA, "name": "Brooke"}, {"id": "x", "name": ""}],
                           by_id={NORSK: {"id": NORSK, "name": "Ingrid"}},
                           in_use={ADA_CARTESIA, NORSK, OTHER})
    assert done == ["cartesia"]
    assert asked == [("all", "ck"), ("one", NORSK), ("one", OTHER)]   # only what the pages missed
    assert voice_names.names("cartesia") == {ADA_CARTESIA: "Brooke", NORSK: "Ingrid"}


def test_a_failed_fetch_keeps_the_file_and_no_key_fetches_nothing(names_file):
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    before = names_file.read_text()
    done, _ = _refresh(_cfg("cartesia"), names_file, pages=[], fail=True)
    assert done == [] and names_file.read_text() == before
    done, asked = _refresh(_cfg("cartesia", cartesia_api_key="", eleven_api_key=""), names_file,
                           pages=[], eleven_in_use={ELEVEN})
    assert (done, asked) == ([], [])


def test_elevenlabs_is_looked_up_by_id_for_the_voices_in_use_only(names_file):
    done, asked = _refresh(_cfg("elevenlabs", cartesia_api_key=""), names_file, pages=[],
                           by_id={ELEVEN: {"id": ELEVEN, "name": "Brian (account)"}}, eleven_in_use={ELEVEN})
    assert done == ["elevenlabs"] and asked == [("eleven", ELEVEN)]
    speech = voice_profile.speech(_host(_cfg("elevenlabs")),
                                  {"agent_id": "a", "persona": "Zed", "session": "zed",
                                   "voice_id": json.dumps({"elevenlabs": ELEVEN})})
    assert speech["voice_label"] == "Brian (account)"                        # the file beats the table


def test_one_refresh_at_a_time(names_file):
    assert voice_names._REFRESHING.acquire(blocking=False)
    try:
        done, asked = _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "B"}])
    finally:
        voice_names._REFRESHING.release()
    assert (done, asked) == ([], [])


def test_the_worker_never_starts_under_tests():
    assert voice_names.worker_enabled() is False


def test_a_failed_fetch_never_drops_the_names_a_provider_had(names_file):
    """Reviewer 3's M4 (a refresh that replaces instead of merging): a refused ElevenLabs
    lookup and a failed Cartesia catalogue keep each provider's names."""
    names_file.write_text(json.dumps({"elevenlabs": {ELEVEN: "Old Name"},
                                      "cartesia": {ADA_CARTESIA: "Brooke"}}))

    def refused(key, voice_id):
        raise OSError("401 quota_exceeded")
    done = voice_names.refresh(_cfg("elevenlabs"), path=names_file,
                               cartesia_all=lambda key: (_ for _ in ()).throw(OSError("down")),
                               cartesia_one=refused, eleven_one=refused,
                               voices_in_use=lambda _cfg: {"cartesia": {OTHER}, "elevenlabs": {ELEVEN}})
    assert done == ["elevenlabs"]
    assert voice_names.names("elevenlabs") == {ELEVEN: "Old Name"}
    assert voice_names.names("cartesia") == {ADA_CARTESIA: "Brooke"}
    speech = voice_profile.speech(_host(_cfg("elevenlabs")),
                                  {"agent_id": "a", "persona": "Zed", "session": "zed",
                                   "voice_id": json.dumps({"elevenlabs": ELEVEN})})
    assert speech["voice_label"] == "Old Name"


def test_no_keys_no_call_and_the_file_untouched(names_file):
    """Reviewer 3's M10: without a Cartesia or an ElevenLabs key nothing is asked."""
    import os
    names_file.write_text(json.dumps({"cartesia": {ADA_CARTESIA: "Brooke"}}))
    os.utime(names_file, (1_700_000_000, 1_700_000_000))
    before = (names_file.read_bytes(), names_file.stat().st_mtime_ns)
    read_in_use = []

    def fetch(*a):
        read_in_use.append(("fetch",) + a)
        return {}
    done = voice_names.refresh(
        _cfg("cartesia", cartesia_api_key="", eleven_api_key=""), path=names_file,
        cartesia_all=lambda key: fetch(key) or [{"id": OTHER, "name": "New"}],
        cartesia_one=fetch, eleven_one=fetch,
        voices_in_use=lambda _cfg: read_in_use.append("in_use") or {"cartesia": {OTHER}, "elevenlabs": {ELEVEN}})
    assert done == [] and read_in_use == []          # no provider call, not even the read of voices in use
    assert (names_file.read_bytes(), names_file.stat().st_mtime_ns) == before
    # A Cartesia key alone: ElevenLabs is never asked, whatever ids are in use.
    done, asked = _refresh(_cfg("cartesia", eleven_api_key=""), names_file, pages=[],
                           in_use=set(), eleven_in_use={ELEVEN})
    assert [a for a in asked if a[0] == "eleven"] == []


def test_at_most_a_hundred_lookups_per_provider(names_file):
    """Reviewer 3's M5: 250 voices in use the catalogue misses -> exactly 100 by-id calls."""
    missing = {f"c0000000-0000-4000-8000-{i:012x}" for i in range(1000, 1250)}
    done, asked = _refresh(_cfg("cartesia", eleven_api_key=""), names_file, pages=[], in_use=missing)
    assert done == ["cartesia"]
    assert len([a for a in asked if a[0] == "one"]) == voice_names.MAX_LOOKUPS == 100


def test_the_file_is_parsed_once_while_it_is_unchanged(names_file, monkeypatch):
    """Reviewer 3's M2 (cost): names() and signature() reuse one parse per file state."""
    names_file.write_text(json.dumps({"cartesia": {ADA_CARTESIA: "Brooke"}}))
    real, parsed = voice_names._parse, []
    monkeypatch.setattr(voice_names, "_parse", lambda path: parsed.append(path) or real(path))
    for _ in range(5):
        assert voice_names.names("cartesia") == {ADA_CARTESIA: "Brooke"}
        assert voice_names.signature()
    assert len(parsed) == 1


def test_a_damaged_or_missing_file_is_no_names(names_file):
    assert voice_names.names("cartesia") == {}
    names_file.write_text("{not json")
    assert voice_names.names("cartesia") == {}


def test_a_test_never_reads_the_live_hosts_names_file(tmp_path, monkeypatch):
    """Review of 498 (a): the path is resolved per call; the session's own file, never the one
    under the Host's cache dir, whatever HOME says."""
    import os
    home = tmp_path / "home"
    live = home / ".cache" / "clarp" / "voice-names.json"
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({"cartesia": {ADA_CARTESIA: "Live Brooke"}}))
    monkeypatch.setenv("HOME", str(home))
    assert voice_names.names_path() == pathlib.Path(os.environ["CLARP_VOICE_NAMES"])
    assert os.environ["CLARP_TEST_GUARD_DIR"] in str(voice_names.names_path())
    speech = voice_profile.speech(_host(_cfg("cartesia")),
                                  {"agent_id": "a", "persona": "Ada", "session": "ada", "voice_id": "{}"})
    assert speech["voice_id"] == ADA_CARTESIA and speech["voice_label"] != "Live Brooke"


def test_the_same_names_rewritten_leave_the_version_alone(names_file):
    """Review of 498 (b1, b2): the version follows the content, and a refresh that finds the
    same names does not rewrite the file."""
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    version = _host(_cfg("cartesia")).version
    body = names_file.read_bytes()
    names_file.write_bytes(body)                                   # same bytes, new mtime
    assert _host(_cfg("cartesia")).version == version
    done, _ = _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    assert done == ["cartesia"] and names_file.read_bytes() == body
    assert _host(_cfg("cartesia")).version == version
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke 2"}])
    assert _host(_cfg("cartesia")).version != version             # a real change moves it


def test_a_start_with_a_fresh_file_calls_no_provider_and_a_stale_one_refreshes_once(names_file):
    """Review of 498 (b3)."""
    import os
    import time
    calls = []
    worker = voice_names.VoiceNamesWorker(first_after=0, refresh_fn=lambda: calls.append(1))
    assert worker.start_pass() is True and calls == [1]            # no file yet: due
    names_file.write_text(json.dumps({"cartesia": {ADA_CARTESIA: "Brooke"}}))
    assert worker.start_pass() is False and calls == [1]           # fresh: no provider call
    old = time.time() - voice_names.REFRESH_SECONDS - 60
    os.utime(names_file, (old, old))
    assert worker.start_pass() is True and calls == [1, 1]         # stale: one refresh


# ---------------------------------------------------------------- the labels

def test_cartesia_is_named_from_the_file(names_file):
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    agent = {"agent_id": "a", "persona": "Ada", "session": "ada", "voice_id": "{}"}
    speech = voice_profile.speech(_host(_cfg("cartesia")), agent)
    assert (speech["voice_id"], speech["voice_label"]) == (ADA_CARTESIA, "Brooke")
    other = voice_profile.speech(_host(_cfg("cartesia")),
                                 {**agent, "persona": "Zed", "voice_id": json.dumps({"cartesia": OTHER})})
    assert other["voice_id"] == OTHER and other["voice_label"] is None        # not in it: null


def test_gemini_a_designed_voice_reads_as_the_voice_picker_names_it(names_file):
    """The live gap (72 of 98 speaking rows): Voice Design ids had no label."""
    cfg = _cfg("gemini", gemini_voices={"Ada": "Puck", "Wilma": "voice_abc123def456",
                                        "Wanda": "voice_abc123def456"},
               gemini_vertex_voices={"Wilma": "voice_vertex_9", "Vera": "voice_vertex_only"})
    host = _host(cfg)
    label = lambda persona, stored="{}": voice_profile.speech(
        host, {"agent_id": "a", "persona": persona, "session": persona.lower(), "voice_id": stored})["voice_label"]
    from lib.voice_catalog import gemini_voices
    picker = {vid: name for vid, name, _ in gemini_voices(cfg)}
    assert label("Ada") == "Puck" == picker["Puck"]                          # prebuilt: its name
    assert label("Wilma") == picker["voice_abc123def456"] == "Wanda (designed)"   # the picker's words
    assert label("Zed", json.dumps({"gemini": "voice_abc123def456"})) == "Wanda (designed)"
    assert label("Zed", json.dumps({"gemini": "voice_vertex_9"})) == "Wanda (designed)"  # mapped back
    assert label("Zed", json.dumps({"gemini": "voice_vertex_only"})) == "Vera (designed)"
    assert label("Zed", json.dumps({"gemini": "Laomedeia"})) == "Laomedeia"
    # A design stored only on the agent: its own persona is not a voice name.
    assert label("Zed", json.dumps({"gemini": "voice_unknown12"})) is None


def test_the_profile_version_moves_when_names_arrive(names_file):
    before = _host(_cfg("cartesia")).version
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    assert _host(_cfg("cartesia")).version != before


# ---------------------------------------------------------------- configured_languages

def test_configured_languages_whatever_the_first_engine_is_handed():
    host = voice_profile.HostView.build(_cfg("cartesia"), stored={
        "transcription.engine": "elevenlabs:scribe_v2",
        "transcription.language": "no",
        "transcription.agent_engines": json.dumps({"bo": "elevenlabs:scribe_v2", "cy": "google:chirp_3"}),
        "transcription.agent_languages": json.dumps({"bo": "no,en", "cy": "no,en"})})
    hear = lambda persona: voice_profile.hearing(host, {"persona": persona, "session": persona.lower()})
    bo = hear("Bo")                                     # the owner's case: Scribe with a set
    assert (bo["languages"], bo["configured_languages"], bo["language_origin"]) == ([], ["no", "en"], "own")
    assert hear("Cy")["configured_languages"] == hear("Cy")["languages"] == ["no", "en"]
    local = voice_profile.HostView.build(_cfg("cartesia"), stored={
        "transcription.agent_languages": json.dumps({"dag": "no,en"})})
    dag = voice_profile.hearing(local, {"persona": "Dag", "session": "dag"})
    assert dag["configured_languages"] == ["no", "en"]
    assert hear("Eve")["configured_languages"] == ["no"]               # no set of its own: the Host's
    assert voice_profile.defaults(host)["hearing"]["configured_languages"] == ["no"]


# ---------------------------------------------------------------- the snapshot path

@pytest.mark.parametrize("state", ["missing", "stale", "full"])
def test_a_snapshot_build_opens_no_socket_and_adds_no_query(quiet_snapshot, monkeypatch, names_file, state):
    if state != "missing":
        _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    if state == "stale":
        names_file.write_text(json.dumps({"cartesia": {OTHER: "Old"}}))
    ids = _seed(3)

    def no_socket(*a, **k):
        raise AssertionError("a snapshot build opened a socket")
    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    rows = {r["agent_id"]: r for r in snapshot.build_agent_snapshot(None)["agents"]}
    label = rows[ids[0]]["voice_profile"]["speech"]["voice_label"]
    assert label == ("Brooke" if state == "full" else None)
    few, many = _profile_queries(monkeypatch, 4), _profile_queries(monkeypatch, 16)
    assert few == many and many <= 1


def test_a_build_with_five_thousand_names(quiet_snapshot, names_file, capsys):
    import time
    names = {f"c0000000-0000-4000-8000-{i:012x}": f"Voice {i}" for i in range(5000)}
    names[ADA_CARTESIA] = "Brooke"
    names_file.write_text(json.dumps({"cartesia": names}))
    ids = _seed(20)
    snapshot.build_agent_snapshot(None)                  # first build parses the file once
    started = time.perf_counter()
    rows = {r["agent_id"]: r for r in snapshot.build_agent_snapshot(None)["agents"]}
    took = time.perf_counter() - started
    assert rows[ids[0]]["voice_profile"]["speech"]["voice_label"] == "Brooke"
    with capsys.disabled():
        print(f"\nsnapshot build, 20 agents, 5,000 names: {took * 1000:.1f} ms")
    assert took < 2.0


# ---------------------------------------------------------------- archived agents

def test_an_event_about_an_archived_agent_carries_no_profile():
    from lib import agents as agents_db, db, events
    live = agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="ada-live")
    gone = agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="ada-gone")
    db.conn().execute("UPDATE agents SET archived_at = 1 WHERE agent_id = ?", (gone,))
    assert voice_profile.for_session("ada-gone") is events.OMIT
    assert voice_profile.for_session("ada-live") is not events.OMIT
    assert live


# ---------------------------------------------------------------- the names follow-up (live finds, 21 Nov)

def test_the_same_names_keep_the_start_up_refresh_away(names_file):
    """Reviewer 3's V6: an unchanged daily check marks the file, or after a day every
    restart would refresh again."""
    import os
    import time
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    old = time.time() - voice_names.REFRESH_SECONDS - 60
    os.utime(names_file, (old, old))
    assert voice_names.start_due() is True
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    assert voice_names.start_due() is False


def test_todays_catalogue_is_replaced_and_names_are_kept(names_file):
    """The pool hands out only what the provider offers today."""
    _refresh(_cfg("cartesia"), names_file, pages=[
        {"id": ADA_CARTESIA, "name": "Brooke", "language": "en", "is_public": True, "is_owner": False},
        {"id": NORSK, "name": "Ingrid", "language": "no", "is_public": False, "is_owner": True}])
    assert voice_names.catalogue("cartesia") == {
        ADA_CARTESIA: {"language": "en", "public": True, "owner": False},
        NORSK: {"language": "no", "public": False, "owner": True}}      # as each row said
    _refresh(_cfg("cartesia"), names_file, pages=[{"id": ADA_CARTESIA, "name": "Brooke"}])
    assert list(voice_names.catalogue("cartesia")) == [ADA_CARTESIA]
    assert voice_names.names("cartesia")[NORSK] == "Ingrid"           # a name is still a name
    assert voice_names.catalogue("elevenlabs") is None


def test_a_deleted_voice_in_use_is_gone_logged_once_and_heard_as_unavailable(names_file, monkeypatch, tmp_path):
    """Live find (21 Nov): Cartesia had deleted 26 voices agents spoke with; they were silent
    and nothing said why. A 404 for a voice in use marks it gone, logs voiceGone once with
    the agents that hold it, and the profile says voice_unavailable."""
    import urllib.error
    from lib import agents as agents_db
    agents_db.create_agent(persona="Gus", voice_id=json.dumps({"cartesia": OTHER}), cwd=str(tmp_path), session="gus")
    logged = []
    monkeypatch.setattr("lib.log.log", lambda *a, **k: logged.append(a))

    def deleted(key, voice_id):
        raise urllib.error.HTTPError("https://api.cartesia.ai/voices/x", 404, "Not Found", None, None)
    for _ in range(2):
        voice_names.refresh(_cfg("cartesia"), path=names_file,
                            cartesia_all=lambda key: [{"id": ADA_CARTESIA, "name": "Brooke"}],
                            cartesia_one=deleted, eleven_one=deleted,
                            voices_in_use=lambda _cfg: {"cartesia": {OTHER}, "elevenlabs": set()})
    assert voice_names.gone("cartesia") == {OTHER}
    gone_lines = [a for a in logged if a[0] == "voiceGone"]
    assert len(gone_lines) == 1 and f"provider=cartesia id={OTHER} agents=Gus" in gone_lines[0][1]
    speech = voice_profile.speech(_host(_cfg("cartesia")),
                                  {"agent_id": "a", "persona": "Gus", "session": "gus",
                                   "voice_id": json.dumps({"cartesia": OTHER})})
    assert (speech["voice_id"], speech["silent_reason"]) == (None, "voice_unavailable")


def test_elevenlabs_is_never_asked_for_an_id_it_cannot_hold(names_file):
    """Live find: ~43 lookups per refresh for bare Cartesia uuids, all 400 Bad Request."""
    done, asked = _refresh(_cfg("cartesia", cartesia_api_key=""), names_file, pages=[],
                           eleven_in_use={ELEVEN, ADA_CARTESIA, '{"cartesia":"x"}'})
    assert [a for a in asked if a[0] == "eleven"] == [("eleven", ELEVEN)]


def test_a_per_agent_override_voice_is_looked_up_too():
    cfg = _cfg("cartesia", tts_agent_overrides={"zed": {"provider": "cartesia", "cartesia_voice": OTHER},
                                                 "bo": {"provider": "elevenlabs", "elevenlabs_voice": ELEVEN}})
    used = voice_names.in_use(cfg)
    assert OTHER in used["cartesia"] and ELEVEN in used["elevenlabs"]
