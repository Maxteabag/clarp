"""S7 step 6, G0: how transcription_pipeline.transcribe picks and tries STT
engines outside a call, as a golden table.

The inputs that decide the engine are varied (the focused agent's engines and
fallbacks, the phone's X-Transcription-Model, the cloud engine chosen in
settings, long-form escalation, whether the engine takes a model name, whether
the local model is loaded) together with how the first engine fails. The
engines actually called, the status and the error are the golden. The pure
engine plan (stt_router) and the transcribe split must reproduce it exactly.
Calls keep their own suites (test_call_transcription).

Regenerate (only on purpose, after a reviewed behaviour change such as VP-09):
    CLARP_REGEN_STT_GOLDENS=1 python -m pytest -q -n 0 tests/unit/test_stt_engine_plan_goldens.py
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import threading
from types import SimpleNamespace

import pytest

from lib import audio_duration, stt_language, stt_providers, transcription_results
from lib import transcription_pipeline as tp
from lib.stt import STTBusyError, STTModelLoadingError, STTUnknownModelError

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "stt_engine_plan_goldens.json"
AGENT = {"none": [], "one": ["deepgram:nova-3"], "two": ["faster-whisper:medium", "deepgram:nova-3"]}
HEADER = ("", "faster-whisper:small", "server-default")
SELECTED = ("server-default", "deepgram:nova-3")
LONG = ("", "faster-whisper:large-v3")
ENGINE = ("model-capable", "bytes-only")
READY = (True, False)
FIRST_FAILS = ("no", "error", "busy", "loading", "unknown")
ERRORS = {"error": RuntimeError("provider down"), "busy": STTBusyError("busy"),
          "loading": STTModelLoadingError("loading"), "unknown": STTUnknownModelError("unknown model")}


class _Engine:
    def __init__(self, fails, ready):
        self.fails = fails
        self.calls = []
        self.ready = threading.Event()
        if ready:
            self.ready.set()

    def _answer(self, label):
        first = not self.calls
        self.calls.append(label)
        if first and self.fails != "no":
            raise ERRORS[self.fails]
        return "hello", True, 1.0

    def transcribe_bytes(self, audio, ctype, prompt, *, wait=0.0):
        return self._answer("bytes")


class _ModelEngine(_Engine):
    def transcribe_model_bytes(self, model, audio, ctype, prompt, *, wait=0.0):
        return self._answer(model)


def _scenarios():
    for agent, header, selected, long_model, engine, ready, fails in itertools.product(
            AGENT, HEADER, SELECTED, LONG, ENGINE, READY, FIRST_FAILS):
        yield {"agent": agent, "header": header, "selected": selected, "long": long_model,
               "engine": engine, "ready": ready, "first_fails": fails}


def _name(s):
    return "|".join(f"{k}={s[k]}" for k in sorted(s))


def _run(s, monkeypatch, tmp_path):
    monkeypatch.setattr(stt_language, "engines_for", lambda session: list(AGENT[s["agent"]]))
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: s["selected"])
    monkeypatch.setattr(stt_providers, "is_cloud_model", lambda engine: engine.startswith("deepgram:"))
    monkeypatch.setattr(stt_providers, "long_form_model_for", lambda seconds: s["long"])
    monkeypatch.setattr(audio_duration, "seconds", lambda audio: 600.0)
    monkeypatch.setattr(tp, "_cache_dir", lambda: tmp_path / "cache")
    engine = (_ModelEngine if s["engine"] == "model-capable" else _Engine)(s["first_fails"], s["ready"])
    ctx = SimpleNamespace(stt=engine, default_session="claude", vocab_prompt=lambda *, delegated: "")
    job = "job-" + str(abs(hash(_name(s))))
    fp = transcription_results.request_fingerprint(b"audio", "audio/webm", s["header"], False)
    out = tp.transcribe(
        ctx, audio_bytes=b"audio", ctype="audio/webm", hands_free=False,
        requested_model=s["header"], transcription_id=job, fingerprint=fp,
        record_voice=lambda event, **fields: None,
        spawn_voice_metrics=lambda *a, **kw: None,
        focus_session=lambda: "mike")
    payload = out.payload if isinstance(out.payload, dict) else {}
    return {"calls": engine.calls, "status": out.status,
            "error": payload.get("error", ""), "text": payload.get("text", "")}


def test_the_engine_plan_matches_its_golden_table(monkeypatch, tmp_path):
    table = {}
    for s in _scenarios():
        with monkeypatch.context() as m:
            table[_name(s)] = _run(s, m, tmp_path)
    if os.environ.get("CLARP_REGEN_STT_GOLDENS") == "1":
        FIXTURE.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"regenerated {FIXTURE.name} ({len(table)} rows)")
    if not FIXTURE.exists():
        pytest.skip("no golden yet: generated after VP-09 reaches integration (S7 plan)")
    golden = json.loads(FIXTURE.read_text())
    assert sorted(table) == sorted(golden)
    different = [name for name in table if table[name] != golden[name]]
    assert not different, f"{len(different)} rows differ, first: {different[0]}: {table[different[0]]} != {golden[different[0]]}"
