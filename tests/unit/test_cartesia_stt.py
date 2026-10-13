"""Cartesia batch STT sends the language Clarp chose and never assumes English.

Cartesia's POST /stt (API 2026-08-14) defaults an omitted language to English
and documents no automatic detection, so an unnamed language would quietly
transcribe Norwegian speech as English.
"""
from __future__ import annotations

import io
import re

import pytest

from lib import cartesia_stt


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    bodies: list[bytes] = []

    def fake_urlopen(request, timeout=None):
        bodies.append(request.data)
        return _Response(b'{"text": "ok", "duration": 1.5}')

    monkeypatch.setattr(cartesia_stt.urllib.request, "urlopen", fake_urlopen)
    return bodies


def _field(body: bytes, name: str) -> str | None:
    match = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n', body)
    return match.group(1).decode() if match else None


def _call(**kwargs):
    return cartesia_stt.transcribe(audio_bytes=b"RIFF", content_type="audio/wav",
                                   api_key="k", **kwargs)


def test_the_language_has_to_be_named(sent):
    with pytest.raises(TypeError):
        _call()
    assert sent == []


@pytest.mark.parametrize("language", ["no", "en"])
def test_the_named_language_is_what_is_sent(sent, language):
    assert _call(language=language) == ("ok", 1.5)
    assert _field(sent[0], "language") == language
    assert _field(sent[0], "model") == "ink-whisper"


@pytest.mark.parametrize("language", ["", "  ", "no,en", "auto", "english"])
def test_anything_but_one_language_code_is_refused_before_sending(sent, language):
    with pytest.raises(cartesia_stt.CartesiaSTTError, match="one language"):
        _call(language=language)
    assert sent == []
