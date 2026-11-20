"""V2a (issue 476): one stored voice shape, a migration that reports first, settings by agent id,
announcements that obey the fallback (ruling O), ElevenLabs' no-voice gate (ruling N)."""
from __future__ import annotations

import json
import pathlib

import pytest

from lib import (agents as agents_db, config, events, personas, settings_store, stt_language,
                 tts_engine, tts_mode, tts_queue, tts_registry, tts_worker, voice, voice_migration)
from lib.protocol import TurnSource

UUID = "a0000000-0000-4000-8000-00000000000a"
ADA = "c0000000-0000-4000-8000-0000000000ad"
ELEVEN = "nPczCjzI2devNBz1zQrb"


# ---------------------------------------------------------------- the one shape

@pytest.mark.parametrize("raw, provider", [
    (UUID, "cartesia"), (ELEVEN, "elevenlabs"), ("nb-NO-Chirp3-HD-Puck", "google"),
    ("Kore", None), ("", None), ("auto-1234", None)])
def test_a_bare_id_is_read_by_its_form(raw, provider):
    assert voice.infer_provider(raw) == provider


@pytest.mark.parametrize("raw, shape", [
    (UUID, '{"cartesia":"%s"}' % UUID), (ELEVEN, '{"elevenlabs":"%s"}' % ELEVEN),
    ("", "{}"), ("{}", "{}"), ("V_MIKE", '{"elevenlabs":"V_MIKE"}'),
    ('{"elevenlabs": "y", "cartesia": "x", "gemini": ""}', '{"cartesia":"x","elevenlabs":"y"}')])
def test_canonical(raw, shape):
    assert voice.canonical(raw) == shape


def test_merge_with_a_base_keeps_what_another_app_added_since_the_read():
    stored = json.dumps({"cartesia": "x", "gemini": "Kore"})          # gemini added by another app
    base = json.dumps({"cartesia": "x"})                               # what this phone read
    incoming = json.dumps({"cartesia": "z"})                           # it changed cartesia only
    assert json.loads(voice.merge_provider_maps(stored, incoming, base)) == {"cartesia": "z", "gemini": "Kore"}


def test_merge_with_a_base_removes_what_the_app_removed_on_purpose():
    stored = json.dumps({"cartesia": "x", "elevenlabs": "y"})
    base = json.dumps({"cartesia": "x", "elevenlabs": "y"})
    assert json.loads(voice.merge_provider_maps(stored, json.dumps({"cartesia": "x"}), base)) == {"cartesia": "x"}


def test_merge_without_a_base_never_removes_by_absence_and_an_empty_value_removes():
    stored = json.dumps({"cartesia": "x", "gemini": "Kore"})
    assert json.loads(voice.merge_provider_maps(stored, json.dumps({"cartesia": "z"}))) == {
        "cartesia": "z", "gemini": "Kore"}
    assert json.loads(voice.merge_provider_maps(stored, json.dumps({"gemini": ""}))) == {"cartesia": "x"}
    assert json.loads(voice.merge_provider_maps(stored, UUID)) == {"cartesia": UUID, "gemini": "Kore"}


def test_both_changed_the_same_provider_the_later_write_wins():
    stored = json.dumps({"cartesia": "other-app"})
    assert json.loads(voice.merge_provider_maps(stored, json.dumps({"cartesia": "mine"}),
                                                json.dumps({"cartesia": "old"}))) == {"cartesia": "mine"}


def test_a_contact_update_merges_per_provider():
    row = personas.create(name="Wanda", voice_id=json.dumps({"cartesia": "x", "gemini": "Kore"}))
    assert row["voice_id"] == '{"cartesia":"x","gemini":"Kore"}'
    personas.update(original_name="Wanda", name="Wanda", voice_id=json.dumps({"cartesia": "z"}),
                    base_voice_id=json.dumps({"cartesia": "x"}))
    assert json.loads(personas.get("Wanda")["voice_id"]) == {"cartesia": "z", "gemini": "Kore"}


# ---------------------------------------------------------------- the migration report

@pytest.fixture
def cartesia_host(monkeypatch):
    cfg = config.Config(tts_provider="cartesia", tts_fallback="elevenlabs", cartesia_api_key="ck",
                        eleven_api_key="ek", cartesia_voices={"Ada": ADA})
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    return cfg


def test_the_report_marks_what_would_sound_different_and_apply_writes_only_the_rest(cartesia_host):
    same = agents_db.create_agent(persona="Ada", voice_id=ADA, cwd="/tmp", session="ada")       # bare = its map voice
    differs = agents_db.create_agent(persona="Ada", voice_id=UUID, cwd="/tmp", session="ada-2")  # bare, another voice
    eleven = agents_db.create_agent(persona="Zed", voice_id=ELEVEN, cwd="/tmp", session="zed")
    done = agents_db.create_agent(persona="Bo", voice_id='{"cartesia":"x"}', cwd="/tmp", session="bo")
    report = voice_migration.plan()
    rows = {r.get("agent_id"): r for r in report["voices"] if r["kind"] == "agent"}
    assert done not in rows                                     # already in the one shape
    assert rows[same]["audible"] is False and rows[eleven]["audible"] is False
    assert rows[differs]["audible"] is True                     # Cartesia would speak the stored uuid
    assert rows[differs]["today"]["provider"][1] == ADA and rows[differs]["after"]["provider"][1] == UUID
    result = voice_migration.apply(report)
    assert "agent Ada" in result["refused_audible"]
    assert agents_db.get_by_agent_id(same)["voice_id"] == '{"cartesia":"%s"}' % ADA
    assert agents_db.get_by_agent_id(eleven)["voice_id"] == '{"elevenlabs":"%s"}' % ELEVEN
    assert agents_db.get_by_agent_id(differs)["voice_id"] == UUID          # refused: untouched
    left = [r for r in voice_migration.plan()["voices"] if r["kind"] == "agent"]
    assert [r["agent_id"] for r in left] == [differs]                    # only the audible row remains


def test_transcription_settings_are_copied_under_agent_ids(cartesia_host):
    a = agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="ada")
    b = agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="ada-2")
    settings_store.set_text(stt_language.AGENT_LANGUAGES_KEY, json.dumps({"ada": "no,en"}))
    report = voice_migration.plan()
    assert report["transcription"]["languages"] == {a: "no,en", b: "no,en"}
    assert report["transcription"]["shared_keys"][0]["key"] == "ada"
    voice_migration.apply(report)
    stored = json.loads(settings_store.get_text(stt_language.AGENT_LANGUAGES_KEY))
    assert stored["ada"] == "no,en" and stored[a.lower()] == "no,en"
    assert stt_language.language_for("ada-2") == "no,en"                    # unchanged for everyone


def test_a_save_under_a_name_updates_the_agent_id_keys_it_names():
    a = agents_db.create_agent(persona="Ada", voice_id="{}", cwd="/tmp", session="ada")
    settings_store.set_text(stt_language.AGENT_LANGUAGES_KEY, json.dumps({a.lower(): "en"}))
    merged = json.loads(stt_language.merge_agent_languages({"Ada": "no"}))
    assert merged["ada"] == "no" and merged[a.lower()] == "no"              # never shadowed by the old id key


# ---------------------------------------------------------------- named vectors (ruling 12:32)
# "No voice of its own = the provider's default voice where the provider has exactly one,
# silence (no_voice) where it has none; a migrated agent must not fall silent where it spoke."

def _worker_speaks(monkeypatch, tmp_path, cfg, agent):
    from lib import deepgram_tts, google_tts
    calls = []
    record = lambda name: (lambda *, text, out_path, on_chunk=None, **kw: (
        calls.append((name, kw.get("voice_id") or kw.get("voice"))), 1)[1])
    monkeypatch.setattr(deepgram_tts, "synthesize", record("deepgram"))
    monkeypatch.setattr(google_tts, "synthesize", record("google"))
    monkeypatch.setattr(google_tts, "refuse_if_collision", lambda cfg=None: None)
    monkeypatch.setattr(google_tts, "credentials",
                        lambda cfg: google_tts.Credentials("google_stt", "/adc.json", "p", ""))
    monkeypatch.setattr(tts_worker, "_emit", lambda *a, **k: None)
    route = tts_mode.route(cfg, agent)
    row = {"voice_id": agent["voice_id"], "text": "Hei", "agent_id": agent["agent_id"],
           "session": agent["session"], "trace_id": "t", "queue_id": 1, "source": "pwa"}
    if not tts_worker._route_has_voice(cfg, row, agent, route):
        return None
    tts_worker._synthesize(cfg=cfg, row=row, agent=agent, out_path=tmp_path / "c.mp3", on_chunk=None,
                           trace_id="t", delivery_fields={"delivery": "chunked-file"}, clip_route=route)
    return calls[-1]


def _three_voiceless(monkeypatch, cfg):
    from lib import google_tts, voice_profile
    monkeypatch.setattr(google_tts, "collision_code", lambda cfg=None: None)
    monkeypatch.setattr(google_tts, "credentials",
                        lambda cfg: google_tts.Credentials("google_stt", "/adc.json", "p", ""))
    rows = [{"agent_id": f"a{i}", "persona": f"Nobody {i}", "session": f"n{i}", "voice_id": "{}"}
            for i in range(3)]
    projection = voice_profile.Projection(voice_profile.HostView.build(cfg, stored={}), rows)
    return rows, [projection.for_row(r)["speech"] for r in rows]


def test_vector_deepgram_default_voice_is_spoken_shown_as_default_and_shared(monkeypatch, tmp_path):
    cfg = config.Config(tts_provider="deepgram", deepgram_api_key="dk")
    rows, speech = _three_voiceless(monkeypatch, cfg)
    assert _worker_speaks(monkeypatch, tmp_path, cfg, rows[0]) == ("deepgram", cfg.deepgram_model)
    assert {(s["provider"], s["voice_id"], s["origin"], s["silent_reason"], s["shared_with"]) for s in speech} == {
        ("deepgram", cfg.deepgram_model, "default", None, 2)}


def test_vector_google_default_voice_is_spoken_shown_as_default_and_shared(monkeypatch, tmp_path):
    from lib import google_tts
    cfg = config.Config(tts_provider="google", google_stt_project="p")
    rows, speech = _three_voiceless(monkeypatch, cfg)
    assert _worker_speaks(monkeypatch, tmp_path, cfg, rows[0]) == ("google", google_tts.DEFAULT_VOICE)
    assert {(s["provider"], s["voice_id"], s["origin"], s["silent_reason"], s["shared_with"]) for s in speech} == {
        ("google", google_tts.DEFAULT_VOICE, "default", None, 2)}


def test_vector_a_google_voice_off_the_allowlist_is_silence_no_voice(monkeypatch, tmp_path):
    from lib import google_tts, voice_profile
    cfg = config.Config(tts_provider="google", google_stt_project="p")
    monkeypatch.setattr(google_tts, "collision_code", lambda cfg=None: None)
    monkeypatch.setattr(google_tts, "credentials",
                        lambda cfg: google_tts.Credentials("google_stt", "/adc.json", "p", ""))
    agent = {"agent_id": "a", "persona": "Zed", "session": "zed",
             "voice_id": json.dumps({"google": "nb-NO-Chirp3-HD-Nobody"})}
    assert _worker_speaks(monkeypatch, tmp_path, cfg, agent) is None          # ended before it is announced
    sp = voice_profile.speech(voice_profile.HostView.build(cfg, stored={}), agent)
    assert (sp["voice_id"], sp["silent_reason"]) == (None, "no_voice")
# ---------------------------------------------------------------- N: ElevenLabs' no-voice gate

def test_elevenlabs_without_a_voice_ends_before_it_is_announced(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(events, "broadcast", lambda stream, event: sent.append(dict(event)))
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="elevenlabs", eleven_api_key="ek"))
    called = []
    monkeypatch.setattr(tts_registry, "synthesize_streaming", lambda **kw: called.append(kw))
    agent_id = agents_db.create_agent(persona="Zed", voice_id='{"cartesia":"x"}', cwd=str(tmp_path), session="zed")
    for n in range(3):
        tts_queue.enqueue(agent_id=agent_id, text=f"S{n}.", voice_id='{"cartesia":"x"}', session="zed",
                          source=TurnSource.PWA, trace_id="t1")
    (tmp_path / "audio").mkdir()
    while tts_worker.synth_one(audio_dir=tmp_path / "audio", stream=object()):
        pass
    assert called == []                                         # ElevenLabs was never asked
    assert [e["type"] for e in sent if e.get("type") in ("audio", "tts-error")] == ["tts-error"]
    assert sent[-1]["reason"] == "no_voice"


def test_deepgram_keeps_its_default_voice():
    cfg = config.Config(tts_provider="deepgram", deepgram_api_key="dk")
    route = tts_mode.route(cfg, {"persona": "Zed"})
    row = {"voice_id": "{}"}
    assert tts_worker._route_has_voice(cfg, row, {"persona": "Zed"}, route) is True


