"""Google Chirp 3 HD: one credential source, escaped break-only SSML, bounds and fixed errors.

Offline only: every test hands synthesize() a recording transport and token source.
"""
from __future__ import annotations

import base64
import io
import json
import time
import traceback
import urllib.error

import pytest

from lib import config, google_stt, google_tts

VOICE = "nb-NO-Chirp3-HD-Aoede"
AUDIO = bytes(range(256)) * 600   # 153,600 bytes, three on_chunk pieces


class Transport:
    """Records each request and answers with one canned reply (or raises)."""

    def __init__(self, status=200, payload=None, raises=None):
        self.status = status
        self.payload = (json.dumps({"audioContent": base64.b64encode(AUDIO).decode()}).encode()
                        if payload is None else payload)
        self.raises = raises
        self.calls = []

    def __call__(self, url, body, headers, deadline, limit):
        self.calls.append({"url": url, "body": json.loads(body), "headers": headers,
                           "limit": limit})
        if self.raises is not None:
            raise self.raises
        return self.status, self.payload


def token(credentials_file, *, timeout):
    assert credentials_file.endswith("adc.json") and timeout > 0
    return "access-token"


@pytest.fixture
def creds(tmp_path):
    adc = tmp_path / "adc.json"
    adc.write_text("{}")
    return google_tts.Credentials("google_tts", str(adc), "billing-project", "")


def speak(creds, tmp_path, text, transport, voice=VOICE, on_chunk=None):
    return google_tts.synthesize(text=text, voice=voice, out_path=tmp_path / "clips" / "c.mp3",
                                 creds=creds, on_chunk=on_chunk, transport=transport,
                                 token=token)


def test_plain_text_is_one_mp3_request_for_the_voice_and_its_language(creds, tmp_path):
    transport = Transport()
    chunks = []
    size = speak(creds, tmp_path, "Hei på deg.", transport,
                 on_chunk=lambda index, data: chunks.append((index, data)))
    [call] = transport.calls
    assert call["url"] == "https://texttospeech.googleapis.com/v1/text:synthesize"
    assert call["body"] == {
        "input": {"text": "Hei på deg."},
        "voice": {"languageCode": "nb-NO", "name": VOICE},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    assert call["headers"]["Authorization"] == "Bearer access-token"
    assert call["headers"]["x-goog-user-project"] == "billing-project"
    assert size == len(AUDIO)
    assert (tmp_path / "clips" / "c.mp3").read_bytes() == AUDIO
    assert [index for index, _ in chunks] == [0, 1, 2]
    assert b"".join(data for _, data in chunks) == AUDIO


@pytest.mark.parametrize("text, ssml", [
    ('Først <break time="500ms"/> så', '<speak>Først <break time="500ms"/> så</speak>'),
    ('A <break time="1.5s"/> B', '<speak>A <break time="1500ms"/> B</speak>'),
    ('Lang <break time="20s"/> pause', '<speak>Lang <break time="3000ms"/> pause</speak>'),
    ('a < b & "c" <break time="300ms"/> <audio src="https://x/y.mp3"/> [sigh] <speed ratio="2"/>ok',
     '<speak>a &lt; b &amp; "c" <break time="300ms"/> &lt;audio src="https://x/y.mp3"/&gt;'
     '  ok</speak>'),
], ids=["ms", "seconds", "capped", "only-break-is-markup"])
def test_clarp_pauses_become_the_only_ssml_element(creds, tmp_path, text, ssml):
    transport = Transport()
    speak(creds, tmp_path, text, transport)
    assert transport.calls[0]["body"]["input"] == {"ssml": ssml}


def test_markup_without_a_usable_pause_is_sent_as_plain_text(creds, tmp_path):
    transport = Transport()
    speak(creds, tmp_path, 'Hei<break/>der <break time="0ms"/> og <break time="0.4ms"/> '
                           '<volume ratio="1"/>slutt', transport)
    assert transport.calls[0]["body"]["input"] == {"text": "Hei der og slutt"}


def test_text_over_the_input_limit_is_refused_counting_escaping_and_never_sent(creds, tmp_path):
    transport = Transport()
    fits = "x" * 4_960 + '<break time="1s"/>'   # <speak> + 4,960 + <break time="1000ms"/> = 4,997 bytes
    speak(creds, tmp_path, fits, transport)
    escaped_over = "&" * 1_000 + '<break time="1s"/>'   # 1,000 chars, 5,000+ bytes once escaped
    with pytest.raises(google_tts.GoogleTTSError) as refused:
        speak(creds, tmp_path, escaped_over, transport)
    assert refused.value.code == "text_too_long"
    assert len(transport.calls) == 1


def test_a_voice_off_the_allowlist_is_unavailable_and_never_replaced(creds, tmp_path):
    transport = Transport()
    for voice in ("nb-NO-Chirp3-HD-Nobody", "en-US-Chirp3-HD-Kore", ""):
        with pytest.raises(google_tts.GoogleTTSError) as error:
            speak(creds, tmp_path, "Hei", transport, voice=voice)
        assert error.value.code == "voice_unavailable"
    assert transport.calls == []


@pytest.mark.parametrize("status, code", [
    (401, "google_auth_failed"), (403, "google_auth_failed"), (429, "google_quota"),
    (400, "google_http_error"), (503, "google_http_error"),
])
def test_an_http_failure_is_one_request_and_a_fixed_code(creds, tmp_path, status, code):
    transport = Transport(status=status, payload=b"")
    with pytest.raises(google_tts.GoogleTTSError) as error:
        speak(creds, tmp_path, "Hei", transport)
    assert error.value.code == code
    assert len(transport.calls) == 1
    assert not (tmp_path / "clips" / "c.mp3").exists()


@pytest.mark.parametrize("raises, code", [
    (TimeoutError("budget"), "google_timeout"),
    (OSError("connection reset"), "google_request_failed"),
])
def test_a_transport_failure_is_a_fixed_code(creds, tmp_path, raises, code):
    with pytest.raises(google_tts.GoogleTTSError) as error:
        speak(creds, tmp_path, "Hei", Transport(raises=raises))
    assert error.value.code == code


@pytest.mark.parametrize("payload, code", [
    (b"not json", "google_bad_response"),
    (b'{"audioContent": 5}', "google_bad_response"),
    (b'{"audioContent": "!!!not base64"}', "google_bad_response"),
    (b'{"audioContent": ""}', "google_empty_audio"),
    (json.dumps({"audioContent": "A" * (google_tts.MAX_AUDIO_BYTES // 3 * 4 + 8)}).encode(),
     "google_audio_too_large"),
    (b" " * (google_tts.MAX_RESPONSE_BYTES + 1), "google_audio_too_large"),
], ids=["not-json", "not-a-string", "not-base64", "empty", "decoded-over-cap", "response-over-cap"])
def test_a_bad_or_oversized_reply_leaves_no_file_behind(creds, tmp_path, payload, code):
    transport = Transport(payload=payload)
    with pytest.raises(google_tts.GoogleTTSError) as error:
        speak(creds, tmp_path, "Hei", transport)
    assert error.value.code == code
    assert transport.calls[0]["limit"] == google_tts.MAX_RESPONSE_BYTES
    clips = tmp_path / "clips"
    assert not clips.exists() or list(clips.iterdir()) == []


@pytest.mark.parametrize("token_error, transport_error, code", [
    (google_stt.GoogleSTTError('Google token HTTP 400: {"error": "invalid_grant secret"}'), None,
     "google_auth_failed"),
    (google_stt.GoogleSTTError("cannot read Google credentials: [Errno 13] Permission denied: "
                               "'/home/someone/secret/adc.json'"), None, "google_auth_failed"),
    (None, OSError("connection to secret.example reset"), "google_request_failed"),
], ids=["token-reply", "credentials-path", "transport"])
def test_errors_never_carry_the_reply_the_path_or_their_cause(creds, tmp_path, token_error,
                                                               transport_error, code):
    def failing_token(credentials_file, *, timeout):
        raise token_error

    with pytest.raises(google_tts.GoogleTTSError) as error:
        google_tts.synthesize(text="Hei", voice=VOICE, out_path=tmp_path / "c.mp3", creds=creds,
                              transport=Transport(raises=transport_error),
                              token=failing_token if token_error else token)
    assert error.value.code == code
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert "secret" not in "".join(traceback.format_exception(error.value))   # what a log records


@pytest.mark.parametrize("message, code", [
    ("Google token HTTP 401: {}", "google_auth_failed"),
    ("Google token HTTP 429: {}", "google_quota"),
    ("Google token HTTP 503: {}", "google_http_error"),
    ("Google token request failed: <urlopen error timed out>", "google_timeout"),
    ("Google token request failed: <urlopen error [Errno 111] Connection refused>",
     "google_request_failed"),
    ("Google credentials must be an authorized_user ADC file", "google_auth_failed"),
], ids=["401", "429", "503", "timeout", "unreachable", "not-authorized-user"])
def test_a_token_failure_is_classified_by_what_went_wrong(creds, tmp_path, message, code):
    def failing_token(credentials_file, *, timeout):
        raise google_stt.GoogleSTTError(message)

    transport = Transport()
    with pytest.raises(google_tts.GoogleTTSError) as error:
        google_tts.synthesize(text="Hei", voice=VOICE, out_path=tmp_path / "c.mp3", creds=creds,
                              transport=transport, token=failing_token)
    assert error.value.code == code
    assert transport.calls == []


def test_unencodable_or_xml_invalid_characters_never_fail_the_request(creds, tmp_path):
    transport = Transport()
    speak(creds, tmp_path, "Hei\ud800 der", transport)
    speak(creds, tmp_path, 'a\x01b <break time="1s"/> c\x1f', transport)
    assert [call["body"]["input"] for call in transport.calls] == [
        {"text": "Hei? der"}, {"ssml": '<speak>a b <break time="1000ms"/> c</speak>'}]


def test_a_failed_write_leaves_no_file_behind(creds, tmp_path, monkeypatch):
    def refuse(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(google_tts.os, "replace", refuse)
    with pytest.raises(OSError):
        speak(creds, tmp_path, "Hei", Transport())
    assert list((tmp_path / "clips").iterdir()) == []


class FakeResponse:
    def __init__(self, body: bytes):
        self.status, self.body = 200, io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read1(self, size):
        return self.body.read(min(size, 1_000))   # a receive returns whatever arrived


class UnreadableBody(io.BytesIO):
    def read(self, *_args):
        raise AssertionError("the error body was read")


class FakeOpener:
    def __init__(self, result):
        self.result, self.opened = result, []

    def open(self, request, timeout):
        self.opened.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_the_transport_reads_at_most_one_byte_past_the_limit(monkeypatch):
    opener = FakeOpener(FakeResponse(b"x" * 5_000))
    monkeypatch.setattr(google_tts, "_OPENER", opener)
    status, body = google_tts._urllib_transport(
        google_tts.ENDPOINT, b"{}", {"Authorization": "Bearer t"}, time.monotonic() + 30, 2_500)
    assert (status, len(body)) == (200, 2_501)
    [(request, timeout)] = opener.opened
    assert (request.full_url, request.get_method()) == (google_tts.ENDPOINT, "POST")
    assert 0 < timeout <= google_tts.RECEIVE_TIMEOUT_SECONDS


def test_the_transport_returns_an_error_status_without_reading_its_body(monkeypatch):
    body = UnreadableBody(b'{"error": "secret"}')
    monkeypatch.setattr(google_tts, "_OPENER", FakeOpener(urllib.error.HTTPError(
        google_tts.ENDPOINT, 500, "Server Error", {}, body)))
    assert google_tts._urllib_transport(
        google_tts.ENDPOINT, b"{}", {}, time.monotonic() + 30, 2_500) == (500, b"")
    assert body.closed


def test_the_transport_stops_after_the_receive_that_passes_the_budget(monkeypatch):
    monkeypatch.setattr(google_tts, "_OPENER", FakeOpener(FakeResponse(b"x" * 5_000)))
    with pytest.raises(TimeoutError):
        google_tts._urllib_transport(google_tts.ENDPOINT, b"{}", {}, time.monotonic() - 1, 9_999)


def test_an_unconfigured_source_refuses_before_any_request(tmp_path):
    transport = Transport()
    for error_code in ("google_credentials_missing", "google_project_missing"):
        unconfigured = google_tts.Credentials("google_stt", "", "", error_code)
        with pytest.raises(google_tts.GoogleTTSError) as error:
            speak(unconfigured, tmp_path, "Hei", transport)
        assert error.value.code == error_code
    assert transport.calls == []


@pytest.fixture
def no_ambient_adc(monkeypatch, tmp_path):
    """Keep the Host user's real ADC file out of reach: no env path, an empty home."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def load(tmp_path, toml: str):
    path = tmp_path / "config.toml"
    path.write_text(toml)
    config.reset_cache()
    return config.load(path)


def test_an_explicit_google_tts_section_is_the_only_source(tmp_path, no_ambient_adc):
    tts_adc, stt_adc = tmp_path / "tts.json", tmp_path / "stt.json"
    tts_adc.write_text("{}")
    stt_adc.write_text("{}")
    stt = f'[google_stt]\nproject = "stt-project"\ncredentials = "{stt_adc}"\n'

    explicit = google_tts.credentials(load(tmp_path, stt + (
        f'[google_tts]\nproject = "tts-project"\ncredentials = "{tts_adc}"\n')))
    assert (explicit.source, explicit.credentials_file, explicit.project, explicit.error) == (
        "google_tts", str(tts_adc), "tts-project", "")

    no_project = google_tts.credentials(load(tmp_path, stt + f'[google_tts]\ncredentials = "{tts_adc}"\n'))
    assert (no_project.source, no_project.error) == ("google_tts", "google_project_missing")

    unreadable = google_tts.credentials(load(tmp_path, stt + (
        f'[google_tts]\nproject = "tts-project"\ncredentials = "{tmp_path / "missing.json"}"\n')))
    assert (unreadable.source, unreadable.credentials_file, unreadable.error) == (
        "google_tts", "", "google_credentials_missing")

    empty_section = google_tts.credentials(load(tmp_path, stt + "[google_tts]\n"))
    assert (empty_section.source, empty_section.error) == ("google_tts", "google_credentials_missing")

    not_a_table = google_tts.credentials(load(tmp_path, 'google_tts = "x"\n' + stt))
    assert (not_a_table.source, not_a_table.error) == ("google_tts", "google_credentials_missing")


def test_without_a_google_tts_section_it_speaks_with_google_stt(tmp_path, no_ambient_adc):
    stt_adc = tmp_path / "stt.json"
    stt_adc.write_text("{}")
    inherited = google_tts.credentials(load(tmp_path, (
        f'[google_stt]\nproject = "stt-project"\ncredentials = "{stt_adc}"\n')))
    assert (inherited.source, inherited.credentials_file, inherited.project, inherited.error) == (
        "google_stt", str(stt_adc), "stt-project", "")
    assert inherited.status() == {
        "credential_source": "google_stt", "configured": True, "error": None}

    nothing = google_tts.credentials(load(tmp_path, ""))
    assert (nothing.source, nothing.error) == ("google_stt", "google_credentials_missing")
    assert nothing.status() == {
        "credential_source": "google_stt", "configured": False,
        "error": "google_credentials_missing"}
