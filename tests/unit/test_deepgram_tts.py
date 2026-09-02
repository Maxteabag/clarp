from __future__ import annotations

import io

from lib import deepgram_tts


class Response:
    def __init__(self):
        self.body = io.BytesIO(b"mp3")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        return self.body.read(size)


def test_flux_uses_v2_speak(monkeypatch):
    urls = []
    monkeypatch.setattr(
        deepgram_tts.urllib.request,
        "urlopen",
        lambda request, timeout: urls.append(request.full_url) or Response())
    deepgram_tts.synthesize(
        text="hello", voice_id="flux-haley-en", out_path=None,
        api_key="key")
    assert urls[0].startswith("https://api.deepgram.com/v2/speak?")


def test_aura_uses_v1_speak(monkeypatch):
    urls = []
    monkeypatch.setattr(
        deepgram_tts.urllib.request,
        "urlopen",
        lambda request, timeout: urls.append(request.full_url) or Response())
    deepgram_tts.synthesize(
        text="hello", voice_id="aura-2-thalia-en", out_path=None,
        api_key="key")
    assert urls[0].startswith("https://api.deepgram.com/v1/speak?")
