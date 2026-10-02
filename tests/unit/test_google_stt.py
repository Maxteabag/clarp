"""Google Chirp 3: ADC token, sync recognize, long clips in pieces, catalogue."""
from __future__ import annotations

import base64
import io
import json
import threading
import urllib.parse
import wave

import pytest

from lib import config, google_stt, stt_language, stt_providers


class Response:
    def __init__(self, payload: dict):
        self.body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, size=-1):
        return self.body.read(size)


def wav_seconds(seconds: float, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))
    return buffer.getvalue()


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


@pytest.fixture
def chirp(monkeypatch, tmp_path):
    """A configured Host and a fake Google that answers by piece length."""
    creds = tmp_path / "adc.json"
    creds.write_text(json.dumps({
        "type": "authorized_user", "refresh_token": "rt",
        "client_id": "cid", "client_secret": "cs"}))
    monkeypatch.setattr(config, "_CACHED", config.Config(
        google_stt_project="proj", google_stt_location="eu",
        google_stt_credentials=str(creds)))
    monkeypatch.setattr(google_stt, "_TOKENS", {})
    calls = {"token": [], "recognize": []}
    lock = threading.Lock()

    def fake_urlopen(request, timeout):
        with lock:
            if request.full_url == google_stt.TOKEN_URL:
                calls["token"].append(urllib.parse.parse_qs(request.data.decode()))
                return Response({"access_token": "tok", "expires_in": 3599})
            body = json.loads(request.data)
            seconds = round(wav_duration(base64.b64decode(body["content"])))
            calls["recognize"].append({
                "url": request.full_url, "headers": dict(request.header_items()),
                "config": body["config"], "seconds": seconds})
        if seconds < 1:
            return Response({})  # silence: Chirp returns no results
        return Response({"results": [
            {"alternatives": [{"transcript": f"piece of {seconds} s"}]}]})

    monkeypatch.setattr(google_stt.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_short_clip_is_one_recognize_call_with_adc_token_and_language(chirp):
    with stt_language.bound_session(None):
        stt_providers.update_settings({"language": "no"})
        text, terminal, duration = stt_providers.transcribe(
            "google:chirp_3", wav_seconds(3), "audio/wav", "")
        stt_providers.transcribe("google:chirp_3", wav_seconds(2), "audio/wav", "")
    assert (text, terminal, round(duration)) == ("piece of 3 s", False, 3)
    # One refresh-token exchange, then the cached token is reused.
    assert chirp["token"] == [{"grant_type": ["refresh_token"], "refresh_token": ["rt"],
                               "client_id": ["cid"], "client_secret": ["cs"]}]
    call = chirp["recognize"][0]
    assert call["url"] == ("https://eu-speech.googleapis.com/v2/projects/proj/"
                           "locations/eu/recognizers/_:recognize")
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["headers"]["X-goog-user-project"] == "proj"
    assert call["config"]["model"] == "chirp_3"
    assert call["config"]["languageCodes"] == ["nb-NO"]


def test_clip_over_a_minute_is_sent_in_ordered_pieces(chirp):
    text, _, duration = stt_providers.transcribe(
        "google:chirp_3", wav_seconds(130), "audio/wav", "")
    assert sorted(c["seconds"] for c in chirp["recognize"]) == [20, 55, 55]
    assert text == "piece of 55 s piece of 55 s piece of 20 s"
    assert round(duration) == 130


def test_silence_transcribes_to_nothing(chirp):
    assert stt_providers.transcribe(
        "google:chirp_3", wav_seconds(0.4), "audio/wav", "")[0] == ""


def test_language_codes_map_to_bcp47():
    assert google_stt.language_tag("no") == "nb-NO"
    assert google_stt.language_tag("en") == "en-US"
    assert google_stt.language_tag("da") == "da-DK"
    assert google_stt.language_tag("en-GB") == "en-GB"


def test_catalogue_lists_chirp_available_only_with_credentials_and_project(
        chirp, monkeypatch, tmp_path):
    def available():
        rows = {r["id"]: r for r in stt_providers.status()["models"]}
        return rows["google:chirp_3"]["available"]

    assert available() is True
    cfg = config._CACHED
    monkeypatch.setattr(config, "_CACHED", config.Config(
        google_stt_credentials=cfg.google_stt_credentials))
    assert available() is False  # no project
    monkeypatch.setattr(config, "_CACHED", config.Config(
        google_stt_project="proj",
        google_stt_credentials=str(tmp_path / "missing.json")))
    assert available() is False  # unreadable credentials
    assert stt_providers.update_settings(
        {"agent_engines": {"Mochi": "google:chirp_3"}})["agent_engines"] == {
            "mochi": "google:chirp_3"}
