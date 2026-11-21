"""R168: Cartesia speaks the agent's language.

Every Cartesia request carries the agent's speech language (the first code of its
language set, else the Host's default) when the configured model speaks it, else "en"
as before. Other providers are untouched. The transports are stand-ins: the HTTP
response for /tts/bytes and the websocket for raw PCM; each test reads the body sent.
"""
from __future__ import annotations

import json
import threading

import pytest

from lib import cartesia_tts, cartesia_ws, config, settings_store, tts_mode, tts_registry, tts_service
from lib.tts_provider import AudioSink

AGENT = {"persona": "Ingrid", "session": "ingrid-1", "voice_id": ""}


@pytest.fixture(autouse=True)
def _join_raw_attempts(monkeypatch):
    attempts = []

    class AttemptThread(threading.Thread):
        def start(self):
            attempts.append(self)
            super().start()
    monkeypatch.setattr(cartesia_ws, "_AttemptThread", AttemptThread, raising=False)
    yield
    for attempt in attempts:
        attempt.join(5)


class _Response:
    def __init__(self):
        self.chunks = [b"mp3"]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.chunks.pop(0) if self.chunks else b""


class _WS:
    def __init__(self, sent):
        self.sent = sent
        self.frames = [json.dumps({"type": "chunk", "data": "cGNt", "done": True,
                                   "status_code": 206, "context_id": "ctx"})]

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def settimeout(self, timeout):
        pass

    def recv(self):
        return self.frames.pop(0)

    def close(self):
        pass


def _setup(monkeypatch, *, languages: str = "", host: str = "", model: str = "sonic-3.6"):
    settings_store.set_text("transcription.agent_languages",
                            json.dumps({"ingrid": languages} if languages else {}))
    settings_store.set_text("transcription.language", host)
    cfg = config.Config(tts_provider="cartesia", cartesia_api_key="k", cartesia_model=model,
                        eleven_api_key="e")
    monkeypatch.setattr(tts_registry, "provider_voice",
                        lambda *a, **k: type("V", (), {"voice": "voice-1"})())
    sent = []

    def urlopen(request, timeout):
        sent.append(json.loads(request.data))
        return _Response()
    monkeypatch.setattr(cartesia_tts.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(cartesia_ws, "_open_ws", lambda url, **kw: _WS(sent))
    return cfg, sent


def _speak(cfg, tmp_path, *, raw_pcm=False, agent=AGENT, provider="cartesia"):
    route = tts_mode.Route(provider, None, "")
    fields = ({"delivery": "raw-pcm", "audio_format": {"encoding": "pcm_f32le", "sample_rate": 44100,
                                                       "channels": 1, "container": "raw"}}
              if raw_pcm else {"delivery": "chunked-file"})
    sink = AudioSink(tmp_path / ("clip.pcm" if raw_pcm else "clip.mp3"), trace_id="t",
                     delivery_fields=fields)
    return tts_service.synth("Hei, dette er en test.", route, sink, "queue", cfg=cfg, raw_voice="",
                             agent=agent, emit=lambda *a, **k: None, context=lambda: {})


@pytest.mark.parametrize("raw_pcm", [False, True], ids=["rest", "websocket"])
@pytest.mark.parametrize("languages, model, expected", [
    ("no", "sonic-3.6", "no"),
    ("no,en", "sonic-3.6", "no"),
    ("nb", "sonic-3.6", "no"),
    ("pt", "sonic-3.6", "pt"),
    ("xx", "sonic-3.6", "en"),             # a code the model lacks
    ("no", "sonic-2", "en"),               # sonic-2 does not speak Norwegian
    ("en", "sonic-3.6", "en"),             # an English agent is unchanged
])
def test_the_request_carries_the_agents_language(monkeypatch, tmp_path, raw_pcm, languages,
                                                  model, expected):
    cfg, sent = _setup(monkeypatch, languages=languages, model=model)
    _speak(cfg, tmp_path, raw_pcm=raw_pcm)
    assert [body["language"] for body in sent] == [expected]


def test_an_agent_without_its_own_languages_speaks_the_hosts_default(monkeypatch, tmp_path):
    cfg, sent = _setup(monkeypatch, host="no")
    _speak(cfg, tmp_path)
    cfg, sent_unset = _setup(monkeypatch)
    _speak(cfg, tmp_path)
    assert [b["language"] for b in sent] == ["no"] and [b["language"] for b in sent_unset] == ["en"]


def test_a_one_off_clip_carries_the_agents_language(monkeypatch, tmp_path):
    from lib import agents as agents_db
    from lib.tts_engine import ElevenLabsEngine
    cfg, sent = _setup(monkeypatch, languages="no")
    agents_db.create_agent(persona="Ingrid", voice_id="", cwd="/tmp", session="ingrid-1")
    monkeypatch.setattr(config, "load", lambda path=None: cfg)
    monkeypatch.setattr(config.Config, "cartesia_voice_for", lambda self, persona: "voice-1")
    engine = ElevenLabsEngine(tmp_path, api_key="")
    assert engine._synthesize_cartesia("Hei", "", tmp_path / "one.mp3", "ingrid-1")
    assert [b["language"] for b in sent] == ["no"]


@pytest.mark.parametrize("model", ["sonic-3.6", "sonic-2"])
def test_only_cartesia_cells_change_with_the_agents_language(monkeypatch, tmp_path, model):
    """The speech cells: provider x agent language x Cartesia model. What each other
    provider is handed is the same whatever the agent's language; Cartesia's language
    follows the agent where the model speaks it."""
    from lib import deepgram_tts, google_tts
    handed = {}

    def stand_in(provider):
        def run(**kw):
            handed.setdefault(provider, []).append(
                {k: v for k, v in kw.items() if k not in ("out_path", "on_chunk")})
            return 1
        return run
    monkeypatch.setattr(tts_registry, "synthesize_streaming", stand_in("elevenlabs"))
    monkeypatch.setattr(deepgram_tts, "synthesize", stand_in("deepgram"))
    monkeypatch.setattr(google_tts, "synthesize", stand_in("google"))
    monkeypatch.setattr(google_tts, "refuse_if_collision", lambda cfg: None)
    monkeypatch.setattr(google_tts, "credentials", lambda cfg: "creds")
    cells = {}
    for languages in ("", "en", "no", "no,en", "pt"):
        cfg, sent = _setup(monkeypatch, languages=languages, model=model)
        for provider in ("cartesia", "elevenlabs", "deepgram", "google"):
            before = len(handed.get(provider, []))
            _speak(cfg, tmp_path, provider=provider)
            if provider == "cartesia":
                cells[(provider, languages)] = sent[-1]["language"]
            else:
                (call,) = handed[provider][before:]
                assert "language" not in call
                cells[(provider, languages)] = json.dumps(call, sort_keys=True, default=str)
    for provider in ("elevenlabs", "deepgram", "google"):
        assert len({cells[(provider, lang)] for lang in ("", "en", "no", "no,en", "pt")}) == 1
    speaks = cartesia_tts.model_languages(model)
    assert {lang: cells[("cartesia", lang)] for lang in ("", "en", "no", "no,en", "pt")} == {
        "": "en", "en": "en", "no": "no" if "no" in speaks else "en",
        "no,en": "no" if "no" in speaks else "en", "pt": "pt"}
