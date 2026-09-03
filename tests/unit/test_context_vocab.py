"""ServerContext resolves provider/model for the vocabulary budget."""
from __future__ import annotations

from types import SimpleNamespace

from lib.context import ServerContext, TranscriptionVocab


def _resolve(requested, stt):
    fn = ServerContext.__dict__["_transcription_provider_model"]
    return fn(SimpleNamespace(stt=stt), requested)


def test_server_default_uses_the_loaded_whisper_model():
    stt = SimpleNamespace(provider="faster-whisper", model_name="small.en")
    assert _resolve("", stt) == ("faster-whisper", "small.en")
    assert _resolve("server-default", stt) == ("faster-whisper", "small.en")


def test_requested_model_id_splits_into_provider_and_model():
    stt = SimpleNamespace(provider="faster-whisper", model_name="small.en")
    assert _resolve("faster-whisper:large-v3-turbo", stt) == (
        "faster-whisper", "large-v3-turbo")
    assert _resolve("assemblyai", stt) == ("assemblyai", "")


def test_stub_stt_without_attributes_falls_back_to_whisper():
    assert _resolve("", object()) == ("faster-whisper", "")


def test_transcription_vocab_is_a_plain_value():
    v = TranscriptionVocab(payload="Clarp", run_id=3, provider="p", model="m")
    assert (v.payload, v.run_id) == ("Clarp", 3)
