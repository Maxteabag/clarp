"""R152 pin on the REAL engine plan: a recording whose engine cannot take the agent's
language set, over a matrix of what the owner chose.

Nothing that decides the engine is stubbed: the agent's engines and languages, the
Host's selected engine and the long-form escalation are real settings, and
`is_cloud_model`, `selected_engine` and `long_form_model_for` run as they do on a
Host. Audio is stubbed only where it leaves: each provider module's `transcribe`
(what stt_providers.transcribe imports at call time) and the Host's own engine. The
clip's duration is fixed at ten minutes, because the stand-in bytes cannot be probed.

Pinned for every cell:
  (a) audio reaches no provider outside the device's engine, the agent's engines, the
      Host's selected engine and the long-form model (the Host's own engine is local);
  (b) a language set is handed only to an engine that can take one;
  (c) a recording that was heard before R152 is heard exactly as before: the golden
      (tests/fixtures/stt_language_set_pin.json) was recorded on the code before R152
      and keeps, per cell that answered 200, what each provider received and the text;
  and no recording is sent twice to the same provider, and no answer is the old 500
  "agent language-set recognition requires ...".

Left out to run in seconds: the Host's language, cartesia:ink-2 and google as an
agent engine on the device side, an agent engine list starting with google, the
Host's own engine off or missing (unit tests in test_transcription_pipeline), calls
(their own plan) and custom adapters as the own engine.

Regenerate the golden only from the code before R152, on purpose:
    CLARP_REGEN_LANGSET_PIN=1 python -m pytest -q -n 0 tests/unit/test_transcription_language_set_pin.py
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import threading

from lib import agents as agents_db
from lib import audio_duration, config, settings_store, stt_providers, transcription_results
from lib import transcription_pipeline as tp

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "stt_language_set_pin.json"
DEVICE = ("", "deepgram:nova-3", "cartesia:ink-whisper", "elevenlabs:scribe_v2",
          "google:chirp_3", "faster-whisper:small")
AGENT_ENGINES = ("", "deepgram:nova-3", "elevenlabs:scribe_v2,deepgram:nova-3")
AGENT_LANG = ("no", "no,en")
HOST_ENGINE = ("local", "deepgram:nova-3", "google:chirp_3", "elevenlabs:scribe_v2")
OWN = ("small", "small.en")
FAIL = ("none", "set-engines-down")
LONG = ("", "deepgram:nova-3")
SET_PROVIDERS = {"google", "elevenlabs"}
OLD_ERROR = "agent language-set recognition requires"


class _OwnEngine:
    """The Host's own engine, routing as stt.WhisperSTT.transcribe_model_bytes does: its
    default model is heard locally, a cloud model goes to stt_providers.transcribe,
    anything else is a local variant."""

    def __init__(self, own, received):
        self.default_model_id = f"faster-whisper:{own}"
        self.received = received
        self.ready = threading.Event()
        self.ready.set()
        self.available = True

    def transcribe_model_bytes(self, model, audio, ctype, prompt, *, wait=0.0):
        selected = model.strip() or self.default_model_id
        if selected == self.default_model_id:
            return self.transcribe_bytes(audio, ctype, prompt, wait=wait)
        if stt_providers.is_cloud_model(selected):
            return stt_providers.transcribe(selected, audio, ctype, prompt)
        self.received.append(["local-variant", selected])
        return f"local:{selected}", True, 1.0

    def transcribe_bytes(self, audio, ctype, prompt, *, wait=0.0):
        self.received.append(["local", None])
        return "local", True, 1.0


class _Recorder:
    def record_voice(self, event, **fields):
        pass

    def spawn_voice_metrics(self, audio, ctype, **fields):
        pass


def _ctx(stt):
    from types import SimpleNamespace
    return SimpleNamespace(stt=stt, default_session="claude",
                           vocab_prompt=lambda *, delegated: "")


def _cells():
    return itertools.product(DEVICE, AGENT_ENGINES, AGENT_LANG, HOST_ENGINE, OWN, FAIL, LONG)


def _run_matrix(monkeypatch, tmp_path) -> dict:
    import lib.cartesia_stt, lib.cartesia_stt_ws, lib.deepgram_stt, lib.eleven_stt, lib.google_stt
    monkeypatch.setattr(tp, "_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "k")   # every key held
    monkeypatch.setattr(audio_duration, "seconds", lambda audio: 600.0)
    agents_db.create_agent(persona="Ada", voice_id="", cwd="/tmp", session="ada-1")
    state = {"fail": "none"}
    received: list = []

    def edge(provider):
        def run(**kw):
            received.append([provider, kw.get("language")])
            if state["fail"] == "set-engines-down" and provider in SET_PROVIDERS:
                raise RuntimeError(f"{provider} 503")
            return f"heard-{provider}", 1.0
        return run
    for provider, module in (("deepgram", lib.deepgram_stt), ("elevenlabs", lib.eleven_stt),
                             ("google", lib.google_stt), ("cartesia", lib.cartesia_stt),
                             ("cartesia", lib.cartesia_stt_ws)):
        monkeypatch.setattr(module, "transcribe", edge(provider))
    table = {}
    for n, (device, engines, langs, host, own, fail, long_) in enumerate(_cells()):
        state["fail"] = fail
        settings_store.set_text("transcription.agent_engines",
                                json.dumps({"ada": engines} if engines else {}))
        settings_store.set_text("transcription.agent_languages", json.dumps({"ada": langs}))
        settings_store.set_text("transcription.engine", host)
        settings_store.set_text("transcription.language", "")
        settings_store.set_text(stt_providers.LONG_FORM_MODEL_KEY, long_)
        cfg = config.Config(whisper_model=own, google_stt_project="p")
        received.clear()
        with monkeypatch.context() as m:
            m.setattr(config, "load", lambda path=None: cfg)
            fp = transcription_results.request_fingerprint(b"audio", "audio/webm", device, False)
            rec = _Recorder()
            try:
                out = tp.transcribe(
                    _ctx(_OwnEngine(own, received)), audio_bytes=b"audio", ctype="audio/webm",
                    requested_model=device, transcription_id=f"job{n}", fingerprint=fp,
                    record_voice=rec.record_voice, spawn_voice_metrics=rec.spawn_voice_metrics,
                    focus_session=lambda: "ada-1", hands_free=False, utterance_id=f"u{n}",
                    client_ts=1)
                status, payload = out.status, dict(out.payload or {})
            except Exception as e:  # noqa: BLE001 - recorded, then asserted absent
                status, payload = "raised", {"error": f"{type(e).__name__}: {e}"}
        table["|".join((device, engines, langs, host, own, fail, long_))] = {
            "status": status, "text": payload.get("text"), "error": payload.get("error"),
            "engine_fallback": payload.get("engine_fallback"),
            "received": [list(r) for r in received]}
    return table


def _allowed(cell: str) -> set[str]:
    device, engines, _langs, host, _own, _fail, long_ = cell.split("|")
    chosen = [device, *engines.split(","), host, long_]
    return {stt_providers.provider_of(e) for e in chosen if e} | {"local", "local-variant"}


def test_language_set_fallback_on_the_real_plan(monkeypatch, tmp_path):
    table = _run_matrix(monkeypatch, tmp_path)
    if os.environ.get("CLARP_REGEN_LANGSET_PIN"):
        golden = {cell: {"received": r["received"], "text": r["text"]}
                  for cell, r in table.items() if r["status"] == 200}
        FIXTURE.write_text(json.dumps(golden, indent=0, sort_keys=True) + "\n")
        return
    golden = json.loads(FIXTURE.read_text())
    problems = []
    for cell, r in table.items():
        sent_to = [provider for provider, _language in r["received"]]
        if r["status"] == "raised":
            problems.append((cell, "raised", r["error"]))
        if not set(sent_to) <= _allowed(cell):                                          # (a)
            problems.append((cell, "provider not chosen", sent_to))
        handed_set = [p for p, language in r["received"] if "," in str(language or "")]
        if any(p not in SET_PROVIDERS for p in handed_set):                             # (b)
            problems.append((cell, "set to an engine that cannot take one", r["received"]))
        cloud = [p for p in sent_to if p not in ("local", "local-variant")]
        if len(cloud) != len(set(cloud)):
            problems.append((cell, "sent twice to one provider", r["received"]))
        if r["status"] == 500 and OLD_ERROR in str(r["error"]):
            problems.append((cell, "the old 500", r["error"]))
        if cell in golden:                                                              # (c)
            before = golden[cell]
            if (r["status"], r["received"], r["text"], r["engine_fallback"]) != (
                    200, before["received"], before["text"], None):
                problems.append((cell, "changed from before R152", r, before))
    assert not problems, f"{len(problems)} of {len(table)} cells: {problems[:5]}"
    assert len(golden) > 100                      # the golden really covers heard recordings
