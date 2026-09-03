"""Local Whisper decoding parameters.

Greedy decoding was the default and is the wrong trade for this workload:
it commits to the first plausible token, which is how a coined product name
becomes an ordinary word. These tests pin the new default and the bounds.
"""
from __future__ import annotations

import pytest

from lib import settings_store, stt


def test_beam_search_is_the_default_not_greedy():
    assert stt.DEFAULT_BEAM_SIZE > 1
    assert stt.inference_beam_size() == stt.DEFAULT_BEAM_SIZE


def test_beam_size_is_configurable():
    settings_store.set_text("transcription.decode.beam_size", "3")
    assert stt.inference_beam_size() == 3


@pytest.mark.parametrize("stored,expected", [
    ("0", 1),        # below the floor
    ("-4", 1),
    ("99", 8),       # above the ceiling: latency cost with no accuracy gain
])
def test_beam_size_is_clamped_to_a_usable_range(stored, expected):
    settings_store.set_text("transcription.decode.beam_size", stored)
    assert stt.inference_beam_size() == expected


def test_a_nonsense_setting_falls_back_to_the_default(monkeypatch):
    """A mistyped value must not stall a turn."""
    settings_store.set_text("transcription.decode.beam_size", "fast please")
    assert stt.inference_beam_size() == stt.DEFAULT_BEAM_SIZE


def test_a_broken_settings_store_falls_back_to_the_default(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_store, "get_int", boom)
    assert stt.inference_beam_size() == stt.DEFAULT_BEAM_SIZE
