"""STT providers: catalogue, the two switches, and cloud dispatch."""
from __future__ import annotations

import io
import json
import urllib.parse

import pytest

from lib import config, stt_providers
from lib import cartesia_stt, deepgram_stt, eleven_stt


class Response:
    def __init__(self, payload: dict):
        self.body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, size=-1):
        return self.body.read(size)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        deepgram_api_key="dg", eleven_api_key="el"))


def test_status_lists_every_provider_and_marks_availability_by_credential(keys):
    value = stt_providers.status()
    rows = {row["id"]: row for row in value["providers"]}
    assert set(rows) == {"deepgram", "elevenlabs", "cartesia"}
    assert rows["deepgram"]["available"] is True
    assert rows["cartesia"]["available"] is False
    assert value["engine"] == "local"
    assert value["turn_taking"] == "native"
    models = {m["id"]: m for m in value["models"]}
    assert models["deepgram:nova-3"]["budget"] == {
        "unit": "terms", "capacity": 50, "max_term_chars": None}
    assert models["cartesia:ink-whisper"]["budget"]["capacity"] == 0
    assert models["cartesia:ink-2"]["budget"]["capacity"] == 100
    assert models["elevenlabs:scribe_v2"]["turn_detection"] == "native"


def test_provider_turn_taking_needs_an_engine_that_detects_turns(keys):
    with pytest.raises(ValueError):
        stt_providers.update_settings({"turn_taking": "provider"})
    with pytest.raises(ValueError):
        stt_providers.update_settings(
            {"engine": "elevenlabs:scribe_v2", "turn_taking": "provider"})
    value = stt_providers.update_settings(
        {"engine": "deepgram:nova-3", "turn_taking": "provider"})
    assert value["engine"] == "deepgram:nova-3"
    assert value["turn_taking"] == "provider"
    assert stt_providers.selected_engine() == "deepgram:nova-3"


def test_unknown_engines_and_strategies_are_rejected(keys):
    for bad in ({"engine": "deepgram:nova-9"}, {"engine": "nonsense"},
                {"turn_taking": "psychic"}, {"engine": 3}):
        with pytest.raises(ValueError):
            stt_providers.update_settings(bad)
    # Local model ids belong to the installed-model registry, so they pass.
    assert stt_providers.update_settings(
        {"engine": "faster-whisper:large-v3-turbo"})["engine"] == "faster-whisper:large-v3-turbo"


def test_deepgram_sends_repeated_keyterms_and_reads_the_transcript(keys, monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = request.data
        return Response({"metadata": {"duration": 3.2}, "results": {"channels": [
            {"alternatives": [{"transcript": "Hello Clarp."}]}]}})

    monkeypatch.setattr(deepgram_stt.urllib.request, "urlopen", fake_urlopen)
    text, terminal, duration = stt_providers.transcribe(
        "deepgram:nova-3", b"audio", "audio/wav", "Clarp, Knut Thomas")
    assert (text, terminal, duration) == ("Hello Clarp.", True, 3.2)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert query["model"] == ["nova-3"]
    assert query["keyterm"] == ["Clarp", "Knut Thomas"]
    assert seen["headers"]["Authorization"] == "Token dg"
    assert seen["headers"]["Content-type"] == "audio/wav"
    assert seen["body"] == b"audio"


def test_elevenlabs_posts_multipart_with_keyterms(keys, monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = request.data
        return Response({"text": "hei Clarp", "words": [
            {"text": "hei", "start": 0.0, "end": 0.4},
            {"text": "Clarp", "start": 0.5, "end": 1.1}]})

    monkeypatch.setattr(eleven_stt.urllib.request, "urlopen", fake_urlopen)
    text, terminal, duration = stt_providers.transcribe(
        "elevenlabs:scribe_v2", b"AUDIO", "audio/mp4", "Clarp")
    assert (text, terminal, duration) == ("hei Clarp", False, 1.1)
    assert seen["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
    assert seen["headers"]["Xi-api-key"] == "el"
    body = seen["body"]
    assert b'name="model_id"\r\n\r\nscribe_v2' in body
    assert b'name="keyterms"\r\n\r\nClarp' in body
    assert b'filename="audio.m4a"' in body and b"AUDIO" in body


def test_cartesia_ignores_terms_and_sends_version_header(monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config(cartesia_api_key="ca"))
    seen = {}

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["body"] = request.data
        return Response({"type": "transcript", "text": "Are we ready to ship Clarp?",
                         "duration": 0.9})

    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", fake_urlopen)
    text, terminal, duration = stt_providers.transcribe(
        "cartesia:ink-whisper", b"x", "audio/webm", "Clarp")
    assert (text, terminal, duration) == ("Are we ready to ship Clarp?", True, 0.9)
    assert seen["headers"]["Cartesia-version"] == cartesia_stt.CARTESIA_VERSION
    assert seen["headers"]["Authorization"] == "Bearer ca"
    assert b"Clarp" not in seen["body"]


def test_missing_key_and_unknown_model_fail_loudly(monkeypatch):
    monkeypatch.setattr(config, "_CACHED", config.Config())
    with pytest.raises(RuntimeError):
        stt_providers.transcribe("deepgram:nova-3", b"x", "audio/wav", "")
    with pytest.raises(ValueError):
        stt_providers.transcribe("deepgram:nova-9", b"x", "audio/wav", "")
    assert stt_providers.is_cloud_model("faster-whisper:small.en") is False
    assert stt_providers.split_terms(" a, b ,, c ") == ["a", "b", "c"]



@pytest.fixture
def cartesia_by_language(monkeypatch):
    """Fake Cartesia batch STT that answers in the language it was asked for."""
    monkeypatch.setattr(config, "_CACHED", config.Config(cartesia_api_key="ca"))
    asked = []
    answers = {"en": "Hi Peter, how do I sound?", "no": "Hei Peter, hvordan høres jeg ut?"}

    def fake_urlopen(request, timeout):
        language = request.data.split(b'name="language"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
        asked.append(language)
        return Response({"text": answers[language], "duration": 1.0})

    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", fake_urlopen)
    return asked


def test_focused_agent_language_overrides_the_default(cartesia_by_language, tmp_path):
    from lib import agents
    asked = cartesia_by_language
    mochi = agents.create_agent(persona="Mochi", voice_id="", cwd=str(tmp_path), session="mochi")
    agents.create_agent(persona="Mike", voice_id="", cwd=str(tmp_path), session="mike")
    stt_providers.update_settings({"agent_languages": {"Mochi": "no"}})

    agents.set_focus(mochi)
    # Cartesia translates into the language it is given, so Mochi must get "no".
    assert stt_providers.transcribe("cartesia:ink-whisper", b"x", "audio/wav", "")[0] \
        == "Hei Peter, hvordan høres jeg ut?"
    agents.set_focus(agents.get_by_session("mike")["agent_id"])
    assert stt_providers.transcribe("cartesia:ink-whisper", b"x", "audio/wav", "")[0] \
        == "Hi Peter, how do I sound?"
    assert asked == ["no", "en"]


def test_language_settings_round_trip_and_reject_bad_values(keys):
    status = stt_providers.update_settings({"language": "EN", "agent_languages": {"Mochi": "no", "Rachel": "sv"}})
    assert status["language"] == "en"
    assert status["agent_languages"] == {"mochi": "no", "rachel": "sv"}
    status = stt_providers.update_settings({"agent_languages": {"Rachel": None}})
    assert status["agent_languages"] == {"mochi": "no"}
    for bad in ({"language": "auto"}, {"language": "norsk"}, {"agent_languages": ["no"]},
                {"agent_languages": {"Mochi": "norsk"}}):
        with pytest.raises(ValueError):
            stt_providers.update_settings(bad)


def test_recording_chat_beats_the_shared_focus(cartesia_by_language, tmp_path):
    from lib import agents, client_chat, stt_language
    asked = cartesia_by_language
    agents.create_agent(persona="Mochi", voice_id="", cwd=str(tmp_path), session="mochi")
    orion = agents.create_agent(persona="Orion", voice_id="", cwd=str(tmp_path), session="orion")
    stt_providers.update_settings({"agent_languages": {"Mochi": "no"}})
    agents.set_focus(orion)            # moved by someone else's activity
    client_chat.record("device-1", "mochi")
    assert client_chat.current("device-1") == "mochi" and client_chat.current("device-2") == ""
    with stt_language.bound_session(client_chat.current("device-1")):
        stt_providers.transcribe("cartesia:ink-whisper", b"x", "audio/wav", "")
    assert asked == ["no"]


def test_agent_engine_settings_round_trip_and_reject_unknown(keys):
    status = stt_providers.update_settings({"agent_engines": {"Mochi": "elevenlabs:scribe_v2"}})
    assert status["agent_engines"] == {"mochi": "elevenlabs:scribe_v2"}
    with pytest.raises(ValueError):
        stt_providers.update_settings({"agent_engines": {"Mochi": "cartesia:ink-9"}})
    assert stt_providers.update_settings({"agent_engines": {"Mochi": None}})["agent_engines"] == {}


def test_scribe_is_asked_not_to_tag_audio_events(keys, monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["body"] = request.data
        return Response({"text": "Hallo, kan du høre meg?"})

    monkeypatch.setattr(eleven_stt.urllib.request, "urlopen", fake_urlopen)
    stt_providers.transcribe("elevenlabs:scribe_v2", b"x", "audio/wav", "")
    assert b'name="tag_audio_events"\r\n\r\nfalse' in seen["body"]
