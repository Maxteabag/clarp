"""Cartesia Realtime STT over the websocket, which is the only place it
accepts vocabulary.

The batch endpoint takes no biasing at all, so Ink-Whisper has always
transcribed blind while every other provider was handed the glossary. These
tests pin the streaming contract that fixes that: keyterms ride on the
connection URL, and only Ink-2 honours them.
"""
from __future__ import annotations

import json

import pytest

from lib import cartesia_stt_ws as ws


class FakeSocket:
    """Records what was sent and replays a scripted server transcript."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.closed = False

    def send_binary(self, payload: bytes) -> None:
        self.binary.append(payload)

    def send(self, payload: str) -> None:
        self.text.append(payload)

    def recv(self) -> str:
        if not self.script:
            return json.dumps({"type": "done", "request_id": "r"})
        return json.dumps(self.script.pop(0))

    def close(self) -> None:
        self.closed = True


# --- connection URL -------------------------------------------------------

def test_url_carries_the_contract_parameters():
    url = ws.connection_url(model="ink-2", sample_rate=16_000, keyterms=[])
    assert url.startswith("wss://api.cartesia.ai/stt/websocket?")
    assert "model=ink-2" in url
    assert "encoding=pcm_s16le" in url
    assert "sample_rate=16000" in url
    assert f"cartesia_version={ws.API_VERSION}" in url


def test_each_keyterm_is_its_own_repeated_parameter():
    url = ws.connection_url(
        model="ink-2", sample_rate=16_000, keyterms=["Tripletex", "Omarchy"])
    assert url.count("keyterm=") == 2
    assert "keyterm=Tripletex" in url
    assert "keyterm=Omarchy" in url


def test_ink_whisper_is_never_sent_keyterms():
    """Cartesia ignores them with a warning; sending them is a lie to the user."""
    url = ws.connection_url(
        model="ink-whisper", sample_rate=16_000, keyterms=["Tripletex"])
    assert "keyterm=" not in url


@pytest.mark.parametrize("model", ["ink-2", "ink-preview"])
def test_biasing_models_accept_keyterms(model):
    url = ws.connection_url(model=model, sample_rate=16_000, keyterms=["ECIT"])
    assert "keyterm=ECIT" in url


# --- the documented limits ------------------------------------------------

def test_keyterms_are_capped_at_one_hundred():
    kept = ws.fit_keyterms([f"term{i}" for i in range(250)])
    assert len(kept) == ws.MAX_KEYTERMS == 100


def test_keyterms_are_capped_at_twelve_hundred_characters():
    kept = ws.fit_keyterms(["x" * 100] * 50)
    assert sum(len(t) for t in kept) <= ws.MAX_KEYTERM_CHARS == 1200


def test_fitting_keeps_earlier_terms_first():
    """Ranking upstream already put the most valuable terms first."""
    kept = ws.fit_keyterms(["first", "second", *[f"pad{i}" for i in range(200)]])
    assert kept[0] == "first"
    assert kept[1] == "second"


def test_blank_terms_never_reach_the_wire():
    assert ws.fit_keyterms(["  ", "", "real"]) == ["real"]


# --- the exchange ---------------------------------------------------------

def test_audio_is_sent_as_binary_chunks_then_finalized_and_closed():
    sock = FakeSocket([
        {"type": "transcript", "is_final": True, "text": "hello there"},
        {"type": "flush_done", "request_id": "r"},
        {"type": "done", "request_id": "r"},
    ])
    text = ws.transcribe_pcm(sock, pcm=b"\x00\x01" * 8000, chunk_bytes=4000)
    assert text == "hello there"
    assert sock.binary, "audio must be sent as binary frames"
    assert all(len(c) <= 4000 for c in sock.binary)
    assert b"".join(sock.binary) == b"\x00\x01" * 8000
    assert "finalize" in sock.text
    assert "close" in sock.text
    assert sock.closed


def test_final_transcripts_are_joined_and_partials_ignored():
    """`text` is a delta from the last final chunk, so only finals accumulate."""
    sock = FakeSocket([
        {"type": "transcript", "is_final": False, "text": "Trip"},
        {"type": "transcript", "is_final": True, "text": "Tripletex"},
        {"type": "transcript", "is_final": False, "text": "and Omar"},
        {"type": "transcript", "is_final": True, "text": " and Omarchy"},
        {"type": "done", "request_id": "r"},
    ])
    assert ws.transcribe_pcm(sock, pcm=b"\x00" * 100) == "Tripletex and Omarchy"


def test_a_server_error_is_raised_with_its_message():
    sock = FakeSocket([
        {"type": "error", "status_code": 402, "message": "quota exhausted"},
    ])
    with pytest.raises(ws.CartesiaSTTStreamError, match="quota exhausted"):
        ws.transcribe_pcm(sock, pcm=b"\x00" * 100)


def test_silence_yields_empty_text_rather_than_an_error():
    sock = FakeSocket([{"type": "done", "request_id": "r"}])
    assert ws.transcribe_pcm(sock, pcm=b"\x00" * 100) == ""


# --- wiring ---------------------------------------------------------------

def test_ink_2_is_offered_and_declares_keyterm_biasing():
    from lib import stt_providers
    row = next(m for d in stt_providers.CATALOG if d["id"] == "cartesia"
               for m in d["models"] if m["id"] == "cartesia:ink-2")
    assert row["biasing"] == "keyterms"


def test_ink_whisper_is_kept_as_the_latency_option():
    from lib import stt_providers
    ids = {m["id"] for d in stt_providers.CATALOG if d["id"] == "cartesia"
           for m in d["models"]}
    assert {"cartesia:ink-2", "cartesia:ink-whisper"} <= ids


def test_cartesia_budget_now_matches_what_ink_2_accepts():
    from lib.vocab_compile import budget_for
    b = budget_for("cartesia", "ink-2")
    assert b.capacity == 100
    assert "term" in str(b.unit).lower()


def test_only_the_biasing_models_take_the_socket_route():
    from lib import stt_providers
    assert "ink-2" in stt_providers._CARTESIA_SOCKET_MODELS
    assert "ink-whisper" not in stt_providers._CARTESIA_SOCKET_MODELS
