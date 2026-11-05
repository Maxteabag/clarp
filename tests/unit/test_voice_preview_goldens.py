"""S7 3c entry characterization of real preview/narration handler outcomes.

Invoke the owning handlers without sockets. Provider/catalog boundaries are
synthetic; status, bytes, call order/settings and cache cleanup are real.
The fixture is captured before replacing the handlers' synthesis ladders.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import pathlib
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from lib import config

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "voice_preview_goldens.json"
PROVIDERS = ("cartesia", "elevenlabs", "gemini", "deepgram", "google", "custom.rec", "openai")
VOICES = {"cartesia": "cart-voice", "elevenlabs": "eleven-voice", "gemini": "Kore",
          "deepgram": "aura-asteria-en", "google": "nb-NO-Chirp3-HD-Aoede",
          "custom.rec": "custom-voice", "openai": "openai-voice"}


@pytest.fixture(scope="module")
def host_module():
    path = pathlib.Path(__file__).resolve().parents[2] / "server" / "server.py"
    spec = importlib.util.spec_from_file_location("s7_preview_goldens_host", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scenarios():
    for kind in ("voice-preview", "cartesia-voice-preview", "controller-narration"):
        providers = PROVIDERS if kind == "voice-preview" else ("cartesia",)
        for provider, fallback, failure, cached in itertools.product(
                providers, ("none", "elevenlabs", "deepgram"),
                ("ok", "primary-fails", "both-fail"), (False, True)):
            yield dict(kind=kind, provider=provider, fallback=fallback, failure=failure, cached=cached, edge="none")
    for kind, edges in (
            ("voice-preview", ("unknown-voice", "provider-unavailable", "id-collision", "id-unverifiable")),
            ("cartesia-voice-preview", ("unknown-voice", "cold-catalog", "unsafe-cache")),
            ("controller-narration", ("empty-text", "long-text", "missing-voice", "missing-key", "unsafe-cache"))):
        for edge in edges:
            yield dict(kind=kind, provider="google" if edge.startswith("id-") else "cartesia",
                       fallback="elevenlabs", failure="ok", cached=False, edge=edge)


def _name(case):
    return "|".join(f"{key}={case[key]}" for key in sorted(case))


def _run(case, monkeypatch, directory, host_module):
    from lib import (cartesia_tts, cartesia_voices, controller_narration, custom_tts_adapters,
                     deepgram_tts, eleven_ws, gemini_tts, google_tts, tts_providers, voice_catalog)
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / "cache"
    cache.mkdir()
    cfg = config.Config(
        tts_provider=case["provider"], tts_fallback=case["fallback"],
        cartesia_api_key="" if case["edge"] == "missing-key" else "cart-key",
        eleven_api_key="eleven-key", gemini_api_key="gemini-key", deepgram_api_key="deepgram-key",
        cartesia_voices={"Rachel": "narrator-voice"}, gemini_voices={"Rachel": "Kore"})
    monkeypatch.setattr(config, "load", lambda *a, **kw: cfg)
    calls, events = [], []
    # The module owning the method may be server.py or an S3 mixin. Bind the
    # filesystem boundary where the real method resolves it, without touching HOME.
    methods = {name: getattr(host_module.Handler, "_handle_" + name.replace("-", "_"))
               for name in ("voice-preview", "cartesia-voice-preview", "controller-narration")}
    for method in methods.values():
        monkeypatch.setitem(method.__globals__, "_runtime_paths", lambda: SimpleNamespace(cache_dir=cache))
        monkeypatch.setitem(method.__globals__, "load_agents", lambda *a, **kw: {})
        monkeypatch.setitem(method.__globals__, "log_exception", lambda *a, **kw: None)
    from lib import eventlog
    monkeypatch.setattr(eventlog, "emit", lambda source, event, **kw: events.append(event))
    voice = VOICES[case["provider"]]
    catalog = {"providers": [{"id": case["provider"], "available": case["edge"] != "provider-unavailable",
                              "voices": [] if case["edge"] == "unknown-voice" else [{"id": voice, "name": "Ada"}]}]}
    monkeypatch.setattr(voice_catalog, "catalog", lambda *a, **kw: catalog)
    monkeypatch.setattr(cartesia_voices, "cached_english_voice", lambda *a: (
        None if case["edge"] in {"unknown-voice", "cold-catalog"} else {"id": voice, "name": "Ada"}))
    monkeypatch.setattr(cartesia_voices, "has_cached_catalog", lambda: case["edge"] != "cold-catalog")
    monkeypatch.setattr(cartesia_voices, "english_voices", lambda: [{"id": voice, "name": "Ada"}])
    monkeypatch.setattr(controller_narration, "voice_id", lambda cfg: (
        "" if case["edge"] == "missing-voice" else "narrator-voice"))
    guard = {"id-collision": google_tts.ID_COLLISION, "id-unverifiable": google_tts.ID_UNVERIFIABLE}.get(case["edge"])
    monkeypatch.setattr(google_tts, "collision_code", lambda *a: guard)
    monkeypatch.setattr(google_tts, "credentials", lambda cfg: "synthetic-credentials")
    package = directory / "adapter"
    package.mkdir()
    executable = package / "speak"
    executable.write_text("not executed")
    manifest = SimpleNamespace(path=package, executable=executable, default_voice="custom-default")
    monkeypatch.setattr(custom_tts_adapters, "get", lambda selected, **kw: (
        manifest if selected == "custom.rec" else None))
    warmup = [case["cached"]]

    def fake(provider, operation="synthesize"):
        def speak(*args, **kw):
            text = kw.get("text", args[0] if args else "")
            selected_voice = kw.get("voice_id") or kw.get("voice")
            calls.append({"provider": provider, "operation": operation, "voice": selected_voice,
                          "text": text, "settings": {k: kw[k] for k in ("api_key", "model", "speed") if k in kw}})
            failing = not warmup[0] and (case["failure"] == "both-fail" or (
                case["failure"] == "primary-fails" and provider == case["provider"]))
            if failing:
                if provider == "cartesia":
                    raise cartesia_tts.CartesiaError("cartesia outage")
                if provider == "google":
                    raise google_tts.GoogleTTSError("google_quota")
                raise RuntimeError(f"{provider} outage")
            body = f"audio:{provider}".encode()
            pathlib.Path(kw["out_path"]).write_bytes(body)
            return len(body)
        return speak
    cartesia, eleven = fake("cartesia"), fake("elevenlabs")
    monkeypatch.setattr(cartesia_tts, "synthesize", cartesia)
    monkeypatch.setattr(eleven_ws, "synthesize_streaming", eleven)
    monkeypatch.setattr(gemini_tts, "synthesize", fake("gemini"))
    monkeypatch.setattr(deepgram_tts, "synthesize", fake("deepgram"))
    monkeypatch.setattr(google_tts, "synthesize", fake("google"))
    monkeypatch.setattr(custom_tts_adapters, "preview", lambda manifest, **kw: fake("custom.rec", "preview")(**kw))
    monkeypatch.setattr(tts_providers, "synthesize", lambda provider, **kw: fake(provider)(**kw))
    # The shared registry retains these two imported callables. Adapt only the
    # provider stub binding if that production module exists on the tested tree.
    registry = sys.modules.get("lib.tts_registry")
    if registry is not None:
        monkeypatch.setattr(registry, "cartesia_synthesize", cartesia)
        monkeypatch.setattr(registry, "synthesize_streaming", eleven)
    text = {"empty-text": "", "long-text": "x" * 241}.get(case["edge"], "Press once")
    query = {"provider": case["provider"], "id": voice, "text": text}
    handler = object.__new__(host_module.Handler)
    handler.path = "/" + case["kind"] + "?" + urlencode(query)
    handler.ctx = SimpleNamespace(agents_path=directory / "agents.json")
    handler._query = lambda: parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    answer = {}
    handler._send = lambda status, body: answer.update(status=status, body=body.decode())
    handler._json_error = lambda status, message: answer.update(status=status, body={"error": message})
    handler._send_file = lambda path: answer.update(status=200, body=pathlib.Path(path).read_bytes().decode())
    if case["edge"] == "unsafe-cache":
        target = directory / "symlink-target"
        target.mkdir()
        (cache / ("controller-narration" if case["kind"] == "controller-narration" else "voice-previews")).symlink_to(target)
    method = methods[case["kind"]]
    if case["cached"]:
        method(handler)
        assert answer.get("status") == 200, "warm the real cache before simulating provider failure"
        calls.clear()
        events.clear()
        answer.clear()
        warmup[0] = False
    method(handler)
    files = [{"directory": str(path.parent.relative_to(cache)), "suffix": path.suffix,
              "body": path.read_bytes().decode()}
             for path in sorted(cache.rglob("*")) if path.is_file()]
    return {"response": answer, "calls": calls, "events": events, "files": files}


def test_preview_and_narration_match_their_golden_table(monkeypatch, tmp_path, host_module):
    table = {}
    for index, case in enumerate(_scenarios()):
        with monkeypatch.context() as m:
            table[_name(case)] = _run(case, m, tmp_path / str(index), host_module)
    if os.environ.get("CLARP_REGEN_PREVIEW_GOLDENS") == "1":
        FIXTURE.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"regenerated {len(table)} preview/narration rows")
    golden = json.loads(FIXTURE.read_text())
    assert sorted(table) == sorted(golden)
    different = [name for name in table if table[name] != golden[name]]
    assert not different, f"{len(different)} rows differ, first: {different[0]}"
