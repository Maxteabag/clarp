from __future__ import annotations

import importlib.util
import pathlib


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "verify_raw_pcm_stream.py"
_SPEC = importlib.util.spec_from_file_location("verify_raw_pcm_stream", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_word_error_rate_normalizes_punctuation_and_case():
    assert _MODULE.word_error_rate("Hello, world!", "hello world") == 0
    assert _MODULE.word_error_rate("one two three", "one three") == 1 / 3


def test_pcm_metrics_rejects_partial_float_frame():
    assert _MODULE.pcm_metrics(b"abc")["frame_aligned"] is False


def test_playback_buffer_model_detects_starvation_between_reads():
    healthy = _MODULE.playback_buffer_metrics([(0.1, 17640), (0.15, 17640)])
    starved = _MODULE.playback_buffer_metrics([(0.1, 4096), (0.3, 4096)])
    assert healthy["underruns"] == 0
    assert starved["underruns"] == 1
