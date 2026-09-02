"""Raw-PCM wire format is configuration, threaded end to end (P4).

The native app configures its decoder from the `audio_format` the server
advertises per clip, so the advertised format, the format requested from
Cartesia, and the config knob must all be the same value — a mismatch plays
garbage. These tests pin that plumbing so the s16le@24k flip is a config
change, not a code change.
"""
import json
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import cartesia_ws, config  # noqa: E402
from lib.clip_delivery import build_from_config  # noqa: E402
from lib.clip_delivery.raw_pcm import RAW_PCM_FORMAT, RawPcmDelivery  # noqa: E402


def test_config_defaults_keep_current_wire_format(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[audio]\n")
    config.reset_cache_for_tests()
    loaded = config.load(path)
    assert loaded.raw_pcm_encoding == "pcm_f32le"
    assert loaded.raw_pcm_sample_rate == 44100


def test_config_reads_raw_pcm_format_knobs(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[audio]\nraw_pcm_encoding = "PCM_S16LE"\nraw_pcm_sample_rate = 24000\n')
    config.reset_cache_for_tests()
    loaded = config.load(path)
    assert loaded.raw_pcm_encoding == "pcm_s16le"
    assert loaded.raw_pcm_sample_rate == 24000


def test_config_env_overrides_raw_pcm_format(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[audio]\nraw_pcm_encoding = "pcm_f32le"\n')
    monkeypatch.setenv("CLAUDE_PWA_RAW_PCM_ENCODING", "pcm_s16le")
    monkeypatch.setenv("CLAUDE_PWA_RAW_PCM_SAMPLE_RATE", "24000")
    config.reset_cache_for_tests()
    loaded = config.load(path)
    assert loaded.raw_pcm_encoding == "pcm_s16le"
    assert loaded.raw_pcm_sample_rate == 24000


def test_delivery_advertises_configured_format():
    default = RawPcmDelivery()
    assert default.format == RAW_PCM_FORMAT
    tuned = RawPcmDelivery(encoding="pcm_s16le", sample_rate=24000)
    assert tuned.format == {
        "container": "raw", "encoding": "pcm_s16le",
        "sample_rate": 24000, "channels": 1,
    }
    # The module default is never mutated by an instance override.
    assert RAW_PCM_FORMAT["encoding"] == "pcm_f32le"


def test_factory_threads_config_into_raw_pcm_delivery():
    cfg = types.SimpleNamespace(
        delivery="raw-pcm", raw_pcm_encoding="pcm_s16le", raw_pcm_sample_rate=24000)
    delivery = build_from_config(cfg)
    assert isinstance(delivery, RawPcmDelivery)
    assert delivery.format["encoding"] == "pcm_s16le"
    assert delivery.format["sample_rate"] == 24000


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.frames = [json.dumps({
            "type": "chunk", "data": "", "done": True,
            "status_code": 206, "context_id": "ctx",
        })]

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        return self.frames.pop(0)

    def close(self) -> None:
        pass


def test_cartesia_request_uses_configured_format(monkeypatch, tmp_path):
    fake = _FakeWS()
    monkeypatch.setattr(cartesia_ws, "_open_ws", lambda url, **kw: fake)
    with pytest.raises(cartesia_ws.CartesiaError):
        # Empty body raises after the request was sent — the request is what
        # we assert on.
        cartesia_ws.synthesize_raw_pcm(
            text="hi", voice_id="v", out_path=tmp_path / "c.pcm", api_key="k",
            encoding="pcm_s16le", sample_rate=24000)
    assert fake.sent, "no request was sent to Cartesia"
    assert fake.sent[0]["output_format"] == {
        "container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000,
    }


def test_cartesia_request_defaults_match_module_default(monkeypatch, tmp_path):
    fake = _FakeWS()
    monkeypatch.setattr(cartesia_ws, "_open_ws", lambda url, **kw: fake)
    with pytest.raises(cartesia_ws.CartesiaError):
        cartesia_ws.synthesize_raw_pcm(
            text="hi", voice_id="v", out_path=tmp_path / "c.pcm", api_key="k")
    assert fake.sent[0]["output_format"] == {
        "container": "raw",
        "encoding": RAW_PCM_FORMAT["encoding"],
        "sample_rate": RAW_PCM_FORMAT["sample_rate"],
    }
