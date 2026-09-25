"""Characterization tests for lib.stt_worker (Whisper subprocess protocol).

The worker is driven in-process with stdin/stdout replaced, using the
built-in fake model (CLAUDE_PWA_STT_FAKE=1). Pins the newline-delimited
JSON contract, the load/transcribe/quit state machine, and the decode
parameters handed to faster-whisper.
"""
from __future__ import annotations

import io
import json

import pytest

from lib import stt_worker


def _run(monkeypatch, lines, fake=True):
    if fake:
        monkeypatch.setenv("CLAUDE_PWA_STT_FAKE", "1")
    else:
        monkeypatch.delenv("CLAUDE_PWA_STT_FAKE", raising=False)
    monkeypatch.setattr(stt_worker.sys, "stdin", io.StringIO("".join(l + "\n" for l in lines)))
    out = io.StringIO()
    monkeypatch.setattr(stt_worker.sys, "stdout", out)
    code = stt_worker.main()
    return code, [json.loads(l) for l in out.getvalue().splitlines()]


def test_load_then_transcribe_then_quit(monkeypatch):
    code, out = _run(monkeypatch, [
        json.dumps({"op": "load", "model_source": "tiny"}),
        json.dumps({"op": "transcribe", "id": "r1", "path": "/nope.wav"}),
        json.dumps({"op": "quit"}),
        json.dumps({"op": "transcribe", "id": "after-quit", "path": "/nope.wav"}),
    ])
    assert code == 0
    assert out == [
        {"event": "ready"},
        {"id": "r1", "segments": [{"text": " fake transcript.", "no_speech_prob": 0.0}]},
    ]


def test_transcribe_before_load_reports_error_and_continues(monkeypatch):
    code, out = _run(monkeypatch, [
        json.dumps({"op": "transcribe", "id": "early", "path": "/x.wav"}),
        json.dumps({"op": "load"}),
        json.dumps({"op": "quit"}),
    ])
    assert code == 0
    assert out == [{"id": "early", "error": "model not loaded"}, {"event": "ready"}]


def test_blank_invalid_and_unknown_lines_are_skipped(monkeypatch):
    code, out = _run(monkeypatch, ["", "   ", "not json", json.dumps({"op": "dance"}),
                                   json.dumps({"op": "quit"})])
    assert code == 0 and out == []


def test_eof_without_quit_exits_zero(monkeypatch):
    code, out = _run(monkeypatch, [json.dumps({"op": "load"})])
    assert code == 0 and out == [{"event": "ready"}]


def test_load_error_is_reported_and_exits_one(monkeypatch):
    def boom(req):
        raise RuntimeError("no such model")

    monkeypatch.setattr(stt_worker, "_load", boom)
    code, out = _run(monkeypatch, [json.dumps({"op": "load"}), json.dumps({"op": "quit"})])
    assert code == 1
    assert out[0]["event"] == "load_error"
    assert out[0]["error"].startswith("no such model\n")
    assert "RuntimeError" in out[0]["error"]
    assert len(out) == 1


def test_transcribe_error_is_per_request(monkeypatch):
    class Broken:
        def transcribe(self, path, **kw):
            raise OSError("cannot read " + path)

    monkeypatch.setattr(stt_worker, "_load", lambda req: Broken())
    code, out = _run(monkeypatch, [
        json.dumps({"op": "load"}),
        json.dumps({"op": "transcribe", "id": "a", "path": "/a.wav"}),
        json.dumps({"op": "transcribe", "id": "b", "path": "/b.wav"}),
    ])
    assert code == 0
    assert out[1:] == [{"id": "a", "error": "cannot read /a.wav"},
                       {"id": "b", "error": "cannot read /b.wav"}]


def test_output_is_compact_single_line_json(monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_STT_FAKE", "1")
    out = io.StringIO()
    monkeypatch.setattr(stt_worker.sys, "stdout", out)
    stt_worker._emit({"id": "x", "segments": [{"text": "a b", "no_speech_prob": 0.5}]})
    assert out.getvalue() == '{"id":"x","segments":[{"text":"a b","no_speech_prob":0.5}]}\n'


def test_transcribe_passes_pinned_decode_parameters():
    seen = {}

    class Seg:
        def __init__(self, text, prob=None):
            self.text = text
            if prob is not None:
                self.no_speech_prob = prob

    class Model:
        def transcribe(self, path, **kw):
            seen["path"] = path
            seen.update(kw)
            return iter([Seg(" hello", 0.25), Seg(" world")]), {"lang": "en"}

    segments = stt_worker._transcribe(Model(), {"path": "/in.wav", "prompt": "Clarp, Theo"}, "no")
    assert segments == [{"text": " hello", "no_speech_prob": 0.25},
                        {"text": " world", "no_speech_prob": 0.0}]
    assert seen == {
        "path": "/in.wav", "language": "no", "beam_size": 1,
        "condition_on_previous_text": False, "initial_prompt": "Clarp, Theo",
        "no_speech_threshold": 0.7, "log_prob_threshold": -0.9,
        "compression_ratio_threshold": 2.4, "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 400, "speech_pad_ms": 150},
    }


def test_missing_prompt_becomes_empty_initial_prompt():
    seen = {}

    class Model:
        def transcribe(self, path, **kw):
            seen.update(kw)
            return iter([]), None

    assert stt_worker._transcribe(Model(), {"path": "/in.wav"}, None) == []
    assert seen["initial_prompt"] == "" and seen["language"] is None


def test_language_from_load_is_reused_for_every_transcribe(monkeypatch):
    seen = []

    class Model:
        def transcribe(self, path, **kw):
            seen.append(kw["language"])
            return iter([]), None

    monkeypatch.setattr(stt_worker, "_load", lambda req: Model())
    _run(monkeypatch, [json.dumps({"op": "load", "language": "nb"}),
                       json.dumps({"op": "transcribe", "id": "1", "path": "p"}),
                       json.dumps({"op": "transcribe", "id": "2", "path": "p"})])
    assert seen == ["nb", "nb"]


def test_load_builds_whisper_model_from_request(monkeypatch):
    import sys
    import types

    built = {}

    class WhisperModel:
        def __init__(self, source, **kw):
            built["source"] = source
            built.update(kw)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.delenv("CLAUDE_PWA_STT_FAKE", raising=False)
    stt_worker._load({"model_source": "/models/small", "compute_type": "", "cpu_threads": "4"})
    assert built == {"source": "/models/small", "device": "cpu", "compute_type": "int8", "cpu_threads": 4}
    stt_worker._load({"model_source": "m", "compute_type": "float16"})
    assert built["compute_type"] == "float16" and built["cpu_threads"] == 0
