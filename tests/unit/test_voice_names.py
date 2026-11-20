"""Voice names and the configured language set (the live profile read null voice_label on 149
of 155 rows; an agent on Scribe with Norwegian and English read languages []).

voice_label comes from tables and a names file filled off the read path (voice_names): a
snapshot build opens no socket and adds no query, whether the file is missing, stale or full.
"""
from __future__ import annotations

import json
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
    monkeypatch.setattr(voice_names, "PATH", path)
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


def test_a_damaged_or_missing_file_is_no_names(names_file):
    assert voice_names.names("cartesia") == {}
    names_file.write_text("{not json")
    assert voice_names.names("cartesia") == {}


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
