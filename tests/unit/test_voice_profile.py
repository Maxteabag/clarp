"""voice_profile (issue 475, Host contract 111): what an app is shown is what the Host does.

The heart of it is the report's table 1.4 (stored voice shape x Host provider), run as a
test: for every row the profile's provider, voice and silence are compared with what the
real worker selects for that agent (tts_worker._route_has_voice, then tts_worker._synthesize
with stand-in providers). A second implementation that merely agrees today would not be
needed for this to pass; the resolver is the one the worker calls.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from lib import (agents as agents_db, config, db, deepgram_tts, gemini_tts, google_tts,
                 settings_store, snapshot, tts_mode, tts_registry, tts_service, tts_worker,
                 voice_profile)

CARTESIA_UUID = "a0000000-0000-4000-8000-00000000000a"
ADA_CARTESIA = "c0000000-0000-4000-8000-0000000000ad"
ELEVEN_ID = "nPczCjzI2devNBz1zQrb"
SHAPES = {
    "bare-cartesia-uuid": CARTESIA_UUID,
    "empty": "",
    "bare-eleven-id": ELEVEN_ID,
    "empty-map": "{}",
    "map-cartesia": json.dumps({"cartesia": CARTESIA_UUID}),
    "map-elevenlabs": json.dumps({"elevenlabs": ELEVEN_ID}),
    "map-gemini": json.dumps({"gemini": "Charon"}),
    "map-google": json.dumps({"google": "nb-NO-Chirp3-HD-Puck"}),
}
PROVIDERS = ("cartesia", "elevenlabs", "gemini", "google", "deepgram")
PERSONAS = ("Ada", "Zed")          # Ada is in the Host's persona lists, Zed in none


def _cfg(provider: str, **over) -> config.Config:
    values = dict(tts_provider=provider, tts_fallback="none",
                  cartesia_api_key="ck", eleven_api_key="ek", gemini_api_key="gk",
                  deepgram_api_key="dk", cartesia_voices={"Ada": ADA_CARTESIA},
                  gemini_voices={"Ada": "Puck"})
    values.update(over)
    return config.Config(**values)


def _host(cfg) -> voice_profile.HostView:
    return voice_profile.HostView.build(cfg, stored={})


def _stand_ins(monkeypatch, calls):
    def fake(name):
        def synth(*, text, out_path, on_chunk=None, **kw):
            calls.append((name, kw.get("voice_id") or kw.get("voice")))
            return 1
        return synth
    monkeypatch.setattr(tts_registry, "cartesia_synthesize", fake("cartesia"))
    monkeypatch.setattr(tts_service, "cartesia_synthesize_raw_pcm", fake("cartesia"))
    monkeypatch.setattr(tts_registry, "synthesize_streaming", fake("elevenlabs"))
    monkeypatch.setattr(gemini_tts, "synthesize", fake("gemini"))
    monkeypatch.setattr(deepgram_tts, "synthesize", fake("deepgram"))
    monkeypatch.setattr(google_tts, "synthesize", fake("google"))
    monkeypatch.setattr(google_tts, "refuse_if_collision", lambda cfg=None: None)
    monkeypatch.setattr(google_tts, "credentials",
                        lambda cfg: google_tts.Credentials("google_stt", "/adc.json", "p", ""))
    monkeypatch.setattr(google_tts, "collision_code", lambda cfg=None: None)
    monkeypatch.setattr(tts_worker, "_emit", lambda *a, **k: None)


def _worker_selects(cfg, agent, tmp_path, calls):
    """(provider, voice) the real worker speaks this agent's clip with, or None for silence."""
    route = tts_mode.route(cfg, agent)
    row = {"voice_id": agent["voice_id"], "text": "Hei", "agent_id": agent["agent_id"],
           "session": agent["session"], "trace_id": "t", "queue_id": 1, "source": "pwa"}
    if not tts_worker._route_has_voice(cfg, row, agent, route):
        return None
    calls.clear()
    try:
        tts_worker._synthesize(cfg=cfg, row=row, agent=agent, out_path=tmp_path / "c.mp3",
                               on_chunk=None, trace_id="t",
                               delivery_fields={"delivery": "chunked-file"}, clip_route=route)
    except Exception:  # noqa: BLE001 - a refusal before any provider call is silence
        return None
    return calls[-1] if calls else None


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("provider", PROVIDERS)
def test_table_1_4_profile_is_what_the_worker_speaks(monkeypatch, tmp_path, provider, shape, persona):
    calls: list = []
    _stand_ins(monkeypatch, calls)
    cfg = _cfg(provider)
    agent = {"agent_id": "a1", "persona": persona, "session": persona.lower(),
             "voice_id": SHAPES[shape]}
    shown = voice_profile.speech(_host(cfg), agent)
    spoken = _worker_selects(cfg, agent, tmp_path, calls)
    if shown["voice_id"]:
        assert spoken == (shown["provider"], shown["voice_id"])
        assert shown["silent_reason"] is None
    else:
        assert shown["silent_reason"] == voice_profile.NO_VOICE
        # Silence is either refused before audio, or a value handed to ElevenLabs that it
        # refuses (a JSON text, a Cartesia uuid, nothing): the profile says so up front.
        assert spoken is None or (spoken[0] == "elevenlabs"
                                  and not voice_profile.elevenlabs_id_usable(spoken[1]))


def test_the_table_s_surprises_are_shown(monkeypatch):
    def show(provider, shape, persona="Zed"):
        agent = {"agent_id": "a", "persona": persona, "session": "s", "voice_id": SHAPES[shape]}
        return voice_profile.speech(_host(_cfg(provider)), agent)
    monkeypatch.setattr(google_tts, "collision_code", lambda cfg=None: None)
    monkeypatch.setattr(google_tts, "credentials",
                        lambda cfg: google_tts.Credentials("google_stt", "/adc.json", "p", ""))
    # A bare Cartesia uuid is never spoken as stored: the persona's voice, or silence.
    assert show("cartesia", "bare-cartesia-uuid", "Ada")["voice_id"] == ADA_CARTESIA
    assert show("cartesia", "bare-cartesia-uuid")["silent_reason"] == "no_voice"
    # Under ElevenLabs a map without an elevenlabs entry is silent.
    assert show("elevenlabs", "map-cartesia")["silent_reason"] == "no_voice"
    # Under Google every agent without its own Google voice gets the one default, not streamed.
    google = show("google", "map-cartesia")
    assert (google["voice_id"], google["origin"], google["streaming"]) == (
        google_tts.DEFAULT_VOICE, "default", False)
    assert show("google", "map-google")["origin"] == "own"


def test_override_fallback_off_and_missing_key():
    agent = {"agent_id": "a", "persona": "Zed", "session": "zed", "voice_id": "{}"}
    cfg = _cfg("cartesia", tts_agent_overrides={"zed": {"provider": "gemini", "gemini_voice": "Kore"}})
    shown = voice_profile.speech(_host(cfg), agent)
    assert (shown["provider"], shown["voice_id"], shown["origin"]) == ("gemini", "Kore", "own")
    # Cartesia has no voice for Zed; the configured fallback speaks, as the worker would.
    cfg = _cfg("cartesia", tts_fallback="elevenlabs")
    shown = voice_profile.speech(_host(cfg), {**agent, "voice_id": ELEVEN_ID})
    assert (shown["provider"], shown["voice_id"]) == ("elevenlabs", ELEVEN_ID)
    assert voice_profile.speech(_host(_cfg("none")), agent)["silent_reason"] == "provider_off"
    no_key = voice_profile.speech(_host(_cfg("cartesia", cartesia_api_key="")),
                                  {**agent, "persona": "Ada"})
    assert no_key["silent_reason"] == "not_configured" and no_key["voice_id"] is None


# ---------------------------------------------------------------- hearing

def _plan_of_the_pipeline(session):
    from lib import transcription_pipeline
    model, fallbacks = transcription_pipeline._plan_engines(
        focus=session, requested_model="", in_call=False, audio_bytes=b"")
    return model, fallbacks


@pytest.mark.parametrize("engines,cloud", [
    ("", "local"),
    ("", "elevenlabs:scribe_v2"),
    ("google:chirp_3,elevenlabs:scribe_v2", "local"),
    ("elevenlabs:scribe_v2", "deepgram:nova-3"),
])
def test_hearing_is_the_pipelines_plan(monkeypatch, engines, cloud):
    monkeypatch.setattr(config, "load", lambda path=None: _cfg("cartesia"))
    agent_id = agents_db.create_agent(persona="Ada", voice_id="", cwd="/tmp", session="ada-1",
                                      backend="codex")
    settings_store.set_text("transcription.engine", cloud)
    if engines:
        settings_store.set_text("transcription.agent_engines", json.dumps({"ada": engines}))
    model, fallbacks = _plan_of_the_pipeline("ada-1")
    cfg = _cfg("cartesia")
    local = f"{cfg.whisper_provider}:{cfg.whisper_model}"
    shown = voice_profile.hearing(voice_profile.HostView.build(cfg),
                                  agents_db.get_by_agent_id(agent_id))
    assert [e["id"] for e in shown["engines"]] == [model or local, *fallbacks]
    assert shown["engine_origin"] == ("own" if engines else "default")
    assert shown["device_may_override"] is (not engines)


def test_hearing_languages():
    cfg = _cfg("cartesia")
    host = voice_profile.HostView.build(cfg, stored={
        "transcription.agent_engines": json.dumps({"ada": "google:chirp_3", "bo": "elevenlabs:scribe_v2"}),
        "transcription.agent_languages": json.dumps({"ada": "no,en", "bo": "no,en", "cy": "no"})})
    hear = lambda persona: voice_profile.hearing(host, {"persona": persona, "session": persona.lower()})
    assert (hear("Ada")["languages"], hear("Ada")["language_origin"]) == (["no", "en"], "own")
    assert hear("Bo")["languages"] == []                   # Scribe is handed no language for a set
    # A local .en model hears English whatever the agent's setting says.
    assert (hear("Cy")["languages"], hear("Cy")["language_origin"]) == (["en"], "default")


# ---------------------------------------------------------------- snapshot

def _seed(count: int, *, voice_id: str = "{}", persona: str = "Ada") -> list[str]:
    ids = []
    for i in range(count):
        ids.append(agents_db.create_agent(persona=f"{persona}" if i == 0 else f"{persona}-{i:04x}",
                                          voice_id=voice_id, cwd="/tmp",
                                          session=f"vp-{count}-{i}", backend="codex"))
    return ids


@pytest.fixture
def quiet_snapshot(monkeypatch):
    from lib import agent_ring_live, backends, personas
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    monkeypatch.setattr(personas, "ensure_builtins", lambda: None)
    monkeypatch.setattr(snapshot, "_exhausted_backends", lambda: {})
    monkeypatch.setattr(agent_ring_live, "user_call_profiles", lambda **kw: {})
    monkeypatch.setattr(config, "load", lambda path=None: _cfg("cartesia"))


def test_rows_carry_the_profile_and_sharing(quiet_snapshot):
    ids = _seed(3)                                   # Ada and two Ada-xxxx: one archetype voice
    lone = agents_db.create_agent(persona="Zed", voice_id="{}", cwd="/tmp", session="zed",
                                  backend="codex")
    archived = agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="gone",
                                      backend="codex")
    db.conn().execute("UPDATE agents SET archived_at=1 WHERE agent_id=?", (archived,))
    snap = snapshot.build_agent_snapshot(None)
    rows = {r["agent_id"]: r for r in snap["agents"]}
    assert rows[ids[0]]["voice_profile"]["speech"]["voice_id"] == ADA_CARTESIA
    assert rows[ids[0]]["voice_profile"]["speech"]["shared_with"] == 2
    assert rows[lone]["voice_profile"]["speech"]["silent_reason"] == "no_voice"
    assert rows[lone]["voice_profile"]["speech"]["shared_with"] == 0
    assert "voice_profile" not in rows[archived]                       # an archived agent speaks to nobody
    listing, _order = snapshot.build_archived_listing(None)
    assert "voice_profile" not in listing[0]                           # the listed row is the same row
    assert snap["voice_defaults"]["speech"]["provider"] == "cartesia"
    assert isinstance(snap["voice_profile_version"], str) and snap["voice_profile_version"]


class _Counter:
    def __init__(self):
        self.n = 0

    def __call__(self, statement):
        self.n += 1


def _queries(monkeypatch, *, profiles: bool) -> int:
    if not profiles:
        monkeypatch.setattr(voice_profile.Projection, "__init__",
                            lambda self, host, rows: setattr(self, "host", host))
        monkeypatch.setattr(voice_profile.Projection, "for_row", lambda self, a: {})
    counter = _Counter()
    db.conn().set_trace_callback(counter)
    try:
        snapshot.build_agent_snapshot(None)
    finally:
        db.conn().set_trace_callback(None)
    return counter.n


def _profile_queries(monkeypatch, count: int) -> int:
    """Queries the profiles add to one snapshot build of ``count`` agents."""
    db.conn().execute("DELETE FROM agents")
    _seed(count)
    snapshot.build_agent_snapshot(None)      # first build repairs and caches; not measured
    with monkeypatch.context() as m:
        without = _queries(m, profiles=False)
    return _queries(monkeypatch, profiles=True) - without


def test_profiles_add_no_query_per_agent(quiet_snapshot, monkeypatch):
    few, many = _profile_queries(monkeypatch, 4), _profile_queries(monkeypatch, 16)
    assert few == many and many <= 1


def test_the_query_count_kills_a_per_agent_reader(quiet_snapshot, monkeypatch):
    """The mutant: a resolver that reads the settings for every agent. The count above must
    see it, or the test proves nothing."""
    real = voice_profile.hearing

    def per_agent(host, agent):
        settings_store.get_text("transcription.agent_engines")
        return real(host, agent)
    monkeypatch.setattr(voice_profile, "hearing", per_agent)
    few, many = _profile_queries(monkeypatch, 4), _profile_queries(monkeypatch, 16)
    assert many > few


def test_a_configuration_edit_tells_open_apps_once(monkeypatch):
    from lib import events
    sent = []
    monkeypatch.setattr(events, "broadcast", lambda stream, event: sent.append(dict(event)))
    cfg = {"now": _cfg("cartesia")}
    monkeypatch.setattr(config, "load", lambda path=None: cfg["now"])
    first = voice_profile.watch_config(None, None)
    assert sent == []                                     # the first look only remembers
    assert voice_profile.watch_config(None, first) == first and sent == []
    cfg["now"] = _cfg("google")
    voice_profile.watch_config(None, first)
    assert [e["kind"] for e in sent] == ["voice-profile"]


def test_the_creation_answer_and_events_carry_the_profile():
    agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="ada-c", backend="codex")
    item = voice_profile.for_session("ada-c")
    assert set(item) == {"speech", "hearing"} and item["speech"]["shared_with"] == 0
    from lib import events
    assert voice_profile.for_session("nobody") is events.OMIT
