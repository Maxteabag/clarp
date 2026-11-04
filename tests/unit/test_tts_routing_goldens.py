"""S7 step 3, G0: the TTS worker's provider ladder as a golden table.

Every provider call in tts_worker._synthesize is replaced by a fake that
records (provider, voice, text) and succeeds, fails before its first chunk, or
fails after it. A matrix of configurations (primary provider, fallback,
whether the primary has a key, delivery, a per-agent override) and failures is
run through the real ladder; the calls made, the chunks seen, the result and
the provenance events are the golden. The TTSProvider/service refactor must
reproduce the table exactly.

Regenerate (only on purpose, after a reviewed behaviour change such as VP-01):
    CLARP_REGEN_TTS_GOLDENS=1 python -m pytest -q -n 0 tests/unit/test_tts_routing_goldens.py
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib

import pytest

from lib import config, deepgram_tts, gemini_tts, google_tts, tts_mode, tts_worker

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "tts_routing_goldens.json"
PROVIDERS = ("cartesia", "elevenlabs", "gemini", "deepgram", "google")
FALLBACKS = ("none", "elevenlabs", "cartesia")
FAILURES = ("ok", "primary-fails-before-audio", "primary-fails-after-audio", "both-fail")
DELIVERIES = ("chunked-file", "raw-pcm")
OVERRIDES = ("none", "gemini:Kore")


def _scenarios():
    for provider, fallback, key, delivery, override, failure in itertools.product(
            PROVIDERS, FALLBACKS, (True, False), DELIVERIES, OVERRIDES, FAILURES):
        yield {"provider": provider, "fallback": fallback, "primary_key": key,
               "delivery": delivery, "override": override, "failure": failure}


def _name(s):
    return "|".join(f"{k}={s[k]}" for k in sorted(s))


def _config(s):
    keys = {"cartesia_api_key": "ck", "eleven_api_key": "ek", "gemini_api_key": "gk",
            "deepgram_api_key": "dk"}
    primary = s["provider"] if s["override"] == "none" else s["override"].split(":")[0]
    if not s["primary_key"]:
        keys[{"cartesia": "cartesia_api_key", "elevenlabs": "eleven_api_key", "gemini": "gemini_api_key",
              "deepgram": "deepgram_api_key"}.get(primary, "unused")] = ""
        keys.pop("unused", None)
    return config.Config(tts_provider=s["provider"], tts_fallback=s["fallback"],
                         cartesia_voices={"Ada": "cart-ada"}, **keys)


def _route(cfg, s):
    if s["override"] == "none":
        return tts_mode.Route(cfg.tts_provider, None, tts_mode.fallback_for(cfg, cfg.tts_provider))
    provider, voice = s["override"].split(":")
    return tts_mode.Route(provider, voice, tts_mode.fallback_for(cfg, provider))


def _run(s, monkeypatch, tmp_path):
    calls, events, chunks = [], [], []
    primary = s["provider"] if s["override"] == "none" else s["override"].split(":")[0]

    def fake(name):
        def synth(*, text, out_path, on_chunk=None, **kw):
            voice = kw.get("voice_id") or kw.get("voice")
            calls.append([name, voice, "spoken" if "[laughing]" in text else "plain"])
            failing = (s["failure"] == "both-fail"
                       or (s["failure"].startswith("primary-fails") and name.split("-")[0] == primary))
            if failing and s["failure"] == "primary-fails-after-audio" and on_chunk:
                on_chunk(0, b"a")
            if failing:
                raise RuntimeError(f"{name} failed")
            if on_chunk:
                on_chunk(0, b"a")
            return 1
        return synth

    monkeypatch.setattr(tts_worker, "cartesia_synthesize", fake("cartesia"))
    monkeypatch.setattr(tts_worker, "cartesia_synthesize_raw_pcm", fake("cartesia-raw"))
    monkeypatch.setattr(tts_worker, "synthesize_streaming", fake("elevenlabs"))
    monkeypatch.setattr(gemini_tts, "synthesize", fake("gemini"))
    monkeypatch.setattr(deepgram_tts, "synthesize", fake("deepgram"))
    monkeypatch.setattr(google_tts, "synthesize", fake("google"))
    monkeypatch.setattr(google_tts, "refuse_if_collision", lambda cfg: None)
    monkeypatch.setattr(google_tts, "credentials", lambda cfg: "creds")
    monkeypatch.setattr(tts_worker, "_emit", lambda source, name, **kw: events.append(
        [name, {k: v for k, v in (kw.get("detail") or {}).items() if k != "error"}]))
    cfg = _config(s)
    row = {"voice_id": "v-raw", "text": "Hello [laughing] there", "agent_id": "a1",
           "session": "ada", "trace_id": "t1", "queue_id": 1, "source": "pwa"}
    agent = {"persona": "Ada", "agent_id": "a1", "session": "ada"}
    try:
        written = tts_worker._synthesize(
            cfg=cfg, row=row, agent=agent, out_path=tmp_path / "clip.mp3",
            on_chunk=lambda i, b: chunks.append(i), trace_id="t1",
            delivery_fields={"delivery": s["delivery"]}, clip_route=_route(cfg, s))
        result = f"ok:{written}"
    except Exception as error:  # noqa: BLE001 - the golden records it
        result = f"{type(error).__name__}: {error}"
    return {"calls": calls, "chunks": len(chunks), "result": result, "events": events}


def test_the_worker_ladder_matches_its_golden_table(monkeypatch, tmp_path):
    table = {}
    for s in _scenarios():
        with monkeypatch.context() as m:
            table[_name(s)] = _run(s, m, tmp_path)
    if os.environ.get("CLARP_REGEN_TTS_GOLDENS") == "1":
        FIXTURE.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"regenerated {FIXTURE.name} ({len(table)} rows)")
    if not FIXTURE.exists():
        pytest.skip("no golden yet: generated after VP-01 reaches integration (S7 plan)")
    golden = json.loads(FIXTURE.read_text())
    assert sorted(table) == sorted(golden)
    different = [name for name in table if table[name] != golden[name]]
    assert not different, f"{len(different)} rows differ, first: {different[0]}: {table[different[0]]} != {golden[different[0]]}"
