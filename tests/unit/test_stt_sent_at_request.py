"""R175: a provider is used up only once the recording actually left for it. The adapters
mark the send at the very request they make (stt_language.sending_audio); their own local
work before it (decoding the clip, a token, a missing project) and a connection that was
refused send nothing, so the provider's next model may still try. The real adapters run;
only the network edge (urlopen, the websocket) is a stand-in."""
from __future__ import annotations

import io
import json
import socket
import urllib.error
from types import SimpleNamespace

import pytest

from lib import cartesia_stt, cartesia_stt_ws, config, deepgram_stt, google_stt, stt_language
from lib import stt_providers
from lib import transcription_pipeline as tp

INK2, INKW, NOVA, CHIRP = "cartesia:ink-2", "cartesia:ink-whisper", "deepgram:nova-3", "google:chirp_3"


@pytest.fixture
def providers(monkeypatch):
    cfg = config.Config(cartesia_api_key="ck", eleven_api_key="ek", deepgram_api_key="dk",
                        google_stt_project="p")
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(stt_language, "current_language", lambda: "en")
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "stand-in-key")
    calls = []

    def model_transcribe(model, audio, ctype, prompt, wait=0.0):
        calls.append(model)
        return stt_providers.transcribe(model, audio, ctype, prompt or "")
    stt = SimpleNamespace(default_model_id="faster-whisper:small", transcribe_model_bytes=model_transcribe)
    return stt, calls


class _Reply:
    def __init__(self, payload):
        self.body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *a):
        return self.body.read(*a)


def _urlopen(outcome):
    def run(request, timeout=None):
        if isinstance(outcome, BaseException):
            raise outcome
        return _Reply(outcome)
    return run


def _http_500(url="https://api.cartesia.ai/stt"):
    return urllib.error.HTTPError(url, 500, "server error", {}, io.BytesIO(b"down"))


def _chat(stt, engines):
    return tp._run_engines(stt, call_stt=None, call_steps=[], requested_model=engines[0],
                           agent_fallbacks=engines[1:], focus="agent", audio_bytes=b"audio",
                           ctype="audio/webm", prompt=None, trace_id="t", vocab_run_id=0,
                           utterance_id="", client_ts=None, record_voice=lambda *a, **k: None)


def _socket_heard(monkeypatch):
    sent = []
    monkeypatch.setattr(cartesia_stt_ws, "pcm_16k_mono", lambda audio: b"\0\0" * 160)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: object())
    monkeypatch.setattr(cartesia_stt_ws, "transcribe_pcm", lambda sock, *, pcm: (sent.append(len(pcm)), "heard")[1])
    return sent


# ---- what counts as sent, adapter by adapter ------------------------------------------------

@pytest.mark.parametrize("outcome, sent", [
    (_http_500(), True),                                                       # the provider answered
    (urllib.error.URLError(ConnectionRefusedError(111, "refused")), False),
    (urllib.error.URLError(socket.gaierror(-2, "no such host")), False),
    (TimeoutError("timed out"), True),                                         # it may have left
])
@pytest.mark.parametrize("adapter", [deepgram_stt, cartesia_stt])
def test_an_http_adapter_counts_as_sent_only_once_the_request_left(monkeypatch, adapter, outcome, sent):
    monkeypatch.setattr(adapter.urllib.request, "urlopen", _urlopen(outcome))
    with stt_language.capturing_handed() as capture, pytest.raises(Exception):
        adapter.transcribe(audio_bytes=b"audio", content_type="audio/webm", api_key="k",
                           model="nova-3" if adapter is deepgram_stt else "ink-whisper", language="en")
    assert stt_language.was_sent(capture) is sent


def _google(monkeypatch, seconds):
    monkeypatch.setattr(google_stt, "decode_pcm", lambda audio, **k: b"\0\0" * 16000 * seconds)
    with stt_language.capturing_handed() as capture, pytest.raises(Exception):
        google_stt.transcribe(audio_bytes=b"audio", content_type="audio/webm", api_key="/adc.json",
                              model="chirp_3", language="en", project="p", location="global")
    return stt_language.was_sent(capture)


def test_googles_own_preparation_sends_nothing(monkeypatch):
    def no_token(*a, **k):
        raise google_stt.GoogleSTTError("could not read the credentials file")
    monkeypatch.setattr(google_stt, "access_token", no_token)
    assert _google(monkeypatch, 5) is False
    monkeypatch.setattr(google_stt, "_post", lambda *a, **k: (_ for _ in ()).throw(
        google_stt.GoogleSTTError("could not decode")))
    assert _google(monkeypatch, 5) is False                  # the token fails before any request


@pytest.mark.parametrize("seconds", [5, 130])                 # one piece, and pieces in threads
def test_google_counts_as_sent_once_its_request_left(monkeypatch, seconds):
    monkeypatch.setattr(google_stt, "access_token", lambda *a, **k: "token")
    monkeypatch.setattr(google_stt.urllib.request, "urlopen",
                        _urlopen(_http_500("https://speech.googleapis.com")))
    assert _google(monkeypatch, seconds) is True


def test_the_socket_models_decode_sends_nothing(monkeypatch):
    def no_audio(audio):
        raise cartesia_stt_ws.CartesiaSTTStreamError("uploaded media has no audio stream")
    monkeypatch.setattr(cartesia_stt_ws, "pcm_16k_mono", no_audio)
    with stt_language.capturing_handed() as capture, pytest.raises(Exception):
        cartesia_stt_ws.transcribe(audio_bytes=b"audio", content_type="audio/webm", api_key="k",
                                   model="ink-2", language="en")
    assert not stt_language.was_sent(capture)


# ---- what it means for the next model ------------------------------------------------------

def test_a_socket_models_own_decode_failure_leaves_cartesia_for_its_batch_model(providers, monkeypatch):
    stt, calls = providers
    def no_audio(audio):
        raise cartesia_stt_ws.CartesiaSTTStreamError("uploaded media has no audio stream")
    monkeypatch.setattr(cartesia_stt_ws, "pcm_16k_mono", no_audio)
    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", _urlopen({"text": "heard", "duration": 1.0}))
    text, *_ = _chat(stt, [INK2, INKW])
    assert calls == [INK2, INKW] and text == "heard"


def test_a_refused_connection_leaves_the_provider_for_its_next_model(providers, monkeypatch):
    stt, calls = providers
    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen",
                        _urlopen(urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
    sent = _socket_heard(monkeypatch)
    text, *_ = _chat(stt, [INKW, INK2])
    assert calls == [INKW, INK2] and sent and text == "heard"


def test_a_failure_after_the_request_left_uses_the_provider_up(providers, monkeypatch):
    stt, calls = providers
    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", _urlopen(_http_500()))
    sent = _socket_heard(monkeypatch)
    monkeypatch.setattr(deepgram_stt.urllib.request, "urlopen", _urlopen(
        {"results": {"channels": [{"alternatives": [{"transcript": "from nova"}]}]}}))
    text, *_ = _chat(stt, [INKW, INK2, NOVA])
    assert calls == [INKW, NOVA] and sent == [] and text == "from nova"


# ---- the call path ---------------------------------------------------------------------------

def _call(stt, engines):
    from lib import call_transcription
    steps = call_transcription.plan(engines, "en")
    call_stt = {"language": {"value": "en"}, "engines": {"value": engines}, "requested_model": None}
    try:
        tp._run_call_steps(stt, steps, call_stt, focus="agent", audio_bytes=b"audio",
                           ctype="audio/webm", prompt=None)
        raised = None
    except Exception as e:  # noqa: BLE001
        raised = type(e).__name__
    return [(a["engine"], a["outcome"], a.get("reason")) for a in call_stt["report"]["attempts"]], raised


def test_in_a_call_a_refusal_before_contact_leaves_the_providers_next_model(providers, monkeypatch):
    stt, calls = providers
    def no_audio(audio):
        raise cartesia_stt_ws.CartesiaSTTStreamError("uploaded media has no audio stream")
    monkeypatch.setattr(cartesia_stt_ws, "pcm_16k_mono", no_audio)
    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", _urlopen({"text": "heard", "duration": 1.0}))
    attempts, raised = _call(stt, [INK2, INKW])
    assert raised is None and calls == [INK2, INKW]
    assert [(engine, outcome) for engine, outcome, _ in attempts] == [(INK2, "error"), (INKW, "ok")]


def test_in_a_call_the_models_left_after_the_last_reached_attempt_are_reported(providers, monkeypatch):
    stt, calls = providers
    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", _urlopen(_http_500()))
    sent = _socket_heard(monkeypatch)
    attempts, raised = _call(stt, [INKW, INK2])
    assert raised is not None and calls == [INKW] and sent == []
    assert [(engine, outcome) for engine, outcome, _ in attempts] == [(INKW, "error"), (INK2, "skipped")]
    assert attempts[1][2] == "provider_already_sent"


def test_a_piece_refused_at_connect_does_not_undo_anothers_send(monkeypatch):
    # Google's pieces of a long clip share one attempt: one got through (HTTP 500), another
    # was refused at connect; the attempt was still sent.
    outcomes = [_http_500("https://speech.googleapis.com"),
                urllib.error.URLError(ConnectionRefusedError(111, "refused"))]
    import threading
    lock = threading.Lock()

    def urlopen(request, timeout=None):
        with lock:
            raise outcomes.pop(0) if outcomes else _http_500()
    monkeypatch.setattr(google_stt, "access_token", lambda *a, **k: "token")
    monkeypatch.setattr(google_stt.urllib.request, "urlopen", urlopen)
    assert _google(monkeypatch, 110) is True                      # two pieces


def test_a_send_that_never_left_does_not_undo_one_that_overlapped_it():
    # Two pieces in flight at once: the first to start is refused at connect only after the
    # second already reached the provider. The attempt was sent.
    with stt_language.capturing_handed() as capture:
        with pytest.raises(urllib.error.URLError), stt_language.sending_audio():      # piece A
            with pytest.raises(urllib.error.HTTPError), stt_language.sending_audio():  # piece B
                raise _http_500()
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    assert stt_language.was_sent(capture)
    with stt_language.capturing_handed() as capture:
        with pytest.raises(urllib.error.URLError), stt_language.sending_audio():
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    assert not stt_language.was_sent(capture)
