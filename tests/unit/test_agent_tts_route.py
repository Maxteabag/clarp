"""Direct TTS (ring openings, herald, announcements, preview) takes the agent's
own `[tts.agents.*]` route, as the queued worker does.

The real path, end to end: agent_rings.ring / HeraldManager / the preview
handler -> ElevenLabsEngine.synthesize -> tts_worker._synthesize -> an
installed custom adapter that records what it was asked to speak. Built-in
providers are recording stubs at their module boundary; nothing reaches a
real provider.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import threading
from types import SimpleNamespace

import pytest

from lib import agent_rings as rings
from lib import cartesia_tts, config, custom_tts_adapters, db, tts_engine, tts_mode, tts_worker

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_backend(monkeypatch):
    """No test here dispatches a turn (rings, herald and preview only speak),
    and none may ever launch a real provider CLI if one starts to."""
    from lib import backends

    def refuse(*args, **kwargs):
        raise AssertionError("a real backend launch from a TTS route test")
    monkeypatch.setattr(backends, "spawn_turn", refuse)

ADAPTER = """#!/usr/bin/env python3
import json, pathlib, sys, wave
request = json.load(sys.stdin)
here = pathlib.Path(__file__).parent
with (here / "requests.jsonl").open("a") as log:
    log.write(json.dumps({"provider": here.name, "operation": request.get("operation"),
                          "voice": request.get("voice_id"), "text": request.get("text")}) + "\\n")
if request.get("operation") == "voices":
    print(json.dumps({"ok": True, "voices": [{"id": "rec-default", "name": "Default"}]}))
elif (here / "fail").exists():
    print(json.dumps({"ok": False, "error": "recording adapter told to fail"}))
    raise SystemExit(2)
else:
    with wave.open(request["output_path"], "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(8000))
    print(json.dumps({"ok": True}))
"""


def _install(root: pathlib.Path, adapter_id: str) -> pathlib.Path:
    package = root / adapter_id
    package.mkdir(parents=True)
    (package / "adapter").write_text(ADAPTER)
    (package / "adapter").chmod(0o755)
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "id": adapter_id, "name": adapter_id, "description": "recording adapter",
        "executable": "./adapter", "operations": ["voices", "preview", "synthesize"],
        "audio_format": "audio/wav", "default_voice": "rec-default", "can_fallback": True}))
    return package


def _agent(agent_id, persona, session, voice="stored-voice", muted=False):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, role, muted)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (agent_id, persona, voice, "/tmp", session, db.now_ms(), "agent", 1 if muted else 0))
    db.conn().execute(
        "INSERT INTO runtimes(agent_id, session, backend_session_id, started_at) VALUES (?,?,?,?)",
        (agent_id, session, "bs-" + agent_id, db.now_ms()))


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.adapters = tmp_path / "adapters"
        monkeypatch.setattr(custom_tts_adapters, "ROOT", self.adapters)
        self.rec = _install(self.adapters, "custom.rec")
        self.alt = _install(self.adapters, "custom.alt")
        assert custom_tts_adapters.ROOT == self.adapters
        self.builtin: list[dict] = []
        self.fail: set[str] = set()
        self.engine = tts_engine.ElevenLabsEngine(tmp_path / "audio", api_key="engine-key",
                                                  model="engine-model", speed=1.3)

        def recorder(provider):
            def record(*args, **kwargs):
                text = kwargs.get("text", args[0] if args else "")
                voice = kwargs.get("voice_id", args[1] if len(args) > 1 else None)
                out = kwargs.get("out_path", args[2] if len(args) > 2 else None)
                self.builtin.append({"provider": provider, "voice": voice, "text": text,
                                     **{k: kwargs[k] for k in ("api_key", "model", "speed") if k in kwargs}})
                if provider in self.fail:
                    from lib.cartesia_tts import CartesiaError
                    raise CartesiaError(f"{provider} failed (recorded)")
                pathlib.Path(out).write_bytes(b"ID3mp3")
                return 6
            return record
        # Built-in providers, recorded at their module boundary (no network).
        monkeypatch.setattr(tts_engine, "synthesize_to_file", recorder("elevenlabs-direct"))
        monkeypatch.setattr(cartesia_tts, "synthesize", recorder("cartesia-direct"))
        monkeypatch.setattr(tts_worker, "cartesia_synthesize", recorder("cartesia"))
        monkeypatch.setattr(tts_worker, "synthesize_streaming", recorder("elevenlabs"))
        _agent("a-sander", "Sander", "sander-1")
        _agent("a-theo", "Theo", "theo-97e5")

    def configure(self, provider, overrides=None, fallback="none"):
        self.monkeypatch.setattr(config, "_CACHED", config.Config(
            tts_provider=provider, tts_fallback=fallback, tts_agent_overrides=overrides or {},
            cartesia_api_key="cartesia-key", eleven_api_key="eleven-key"))
        return config.load()

    def spoken(self):
        """Every synthesis, in order: custom adapters and built-in stubs."""
        out = list(self.builtin)
        for package in (self.rec, self.alt):
            log = package / "requests.jsonl"
            for line in (log.read_text().splitlines() if log.is_file() else []):
                row = json.loads(line)
                if row["operation"] == "synthesize":
                    out.append({"provider": row["provider"], "voice": row["voice"], "text": row["text"]})
        return [(r["provider"], r["voice"]) for r in out]

    def reset(self):
        self.builtin.clear()
        for package in (self.rec, self.alt):
            (package / "requests.jsonl").unlink(missing_ok=True)


SANDER = {"sander": {"provider": "custom.rec", "custom.rec_voice": "sander-voice"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _ring(env, agent="a-sander"):
    monkeypatch = env.monkeypatch
    monkeypatch.setattr(rings, "_push", lambda ring, kind: {"sent": 1})
    monkeypatch.setattr(rings, "_schedule_expiry", lambda ring: None)
    monkeypatch.setattr(rings, "_push_voip", lambda ring: {"sent": 0})
    rings.bind(SimpleNamespace(stream=None, tts=env.engine))
    return rings.ring(caller_agent_id=agent, reason="Leak at pump 01",
                      opening_line="Camera 2 shows a possible leak at pump 01.")["ring"]


def _herald(env, session="sander-1", persona="Sander", voice="stored-voice"):
    from lib.herald import HeraldManager
    manager = HeraldManager(stream=None, tts=env.engine, agents=lambda: {})
    return manager._synthesize_herald(session, {"name": persona, "voice_id": voice})


def _queue(env, cfg, session="sander-1", voice="stored-voice", text="queued reply"):
    """What the queued worker selects: its own route into _synthesize (tts_worker.py:108/:132)."""
    from lib import agents
    agent = agents.get_by_session(session)
    tts_worker._synthesize(cfg=cfg, row={"text": text, "voice_id": voice, "session": session},
                           agent=agent, out_path=env.engine.audio_dir.parent / "queued.mp3",
                           on_chunk=None, trace_id="", delivery_fields={"delivery": "chunked-file"},
                           clip_route=tts_mode.route(cfg, agent))


# ---- the agent's own route on the direct path ------------------------------------------------

@pytest.mark.parametrize("global_provider", ["cartesia", "elevenlabs", "custom.alt"])
def test_a_ring_opening_speaks_with_the_agents_own_provider_and_voice(env, global_provider):
    env.configure(global_provider, SANDER)
    ring = _ring(env)
    assert ring["opening_audio_url"].startswith("/audio/")
    assert env.spoken() == [("custom.rec", "sander-voice")]


def test_the_herald_speaks_with_the_agents_own_provider_and_voice(env):
    env.configure("cartesia", SANDER)
    reason, url = _herald(env)
    assert reason == "synthesized" and url.startswith("/audio/herald-internal-")
    assert env.spoken() == [("custom.rec", "sander-voice")]


def test_an_announcement_speaks_with_the_agents_own_provider_and_voice(env):
    from lib.context import ServerContext
    env.configure("cartesia", SANDER)
    ServerContext.speak_announcement(SimpleNamespace(tts=env.engine), "Sander is ready", "stored-voice",
                                     session="sander-1")
    assert env.spoken() == [("custom.rec", "sander-voice")]


@pytest.mark.parametrize("overrides,global_provider", [
    (SANDER, "cartesia"),
    ({"sander": {"provider": "cartesia", "cartesia_voice": "sander-cartesia"}}, "custom.alt"),
    ({"sander": {"custom.alt_voice": "only-a-voice"}}, "custom.alt"),           # voice only, same provider
])
def test_the_direct_path_selects_what_the_queue_selects(env, overrides, global_provider):
    cfg = env.configure(global_provider, overrides)
    _queue(env, cfg)
    queued = env.spoken()
    env.reset()
    _ring(env)
    assert env.spoken() == queued and queued


def test_a_failing_override_provider_uses_the_configured_fallback_as_the_queue_does(env):
    cfg = env.configure("cartesia", SANDER, fallback="custom.alt")
    (env.rec / "fail").touch()
    _queue(env, cfg)
    queued = env.spoken()
    env.reset()
    _ring(env)
    assert env.spoken() == queued == [("custom.rec", "sander-voice"), ("custom.alt", "rec-default")]


def test_a_failing_override_without_a_fallback_is_silent_and_the_ring_still_rings(env):
    env.configure("cartesia", SANDER)
    (env.rec / "fail").touch()
    ring = _ring(env)
    assert ring["state"] == "ringing" and ring["opening_audio_url"] == ""
    assert env.spoken() == [("custom.rec", "sander-voice")]


# ---- unchanged: no override, no agent, disabled, preview, muted ------------------------------

@pytest.mark.parametrize("global_provider,expected", [
    ("cartesia", [("cartesia-direct", "<theo's cartesia voice>")]),  # the persona's Cartesia voice
    ("elevenlabs", [("elevenlabs-direct", "stored-voice")]),
    ("custom.alt", [("custom.alt", "rec-default")]),                # the adapter's default voice
])
def test_without_an_override_the_direct_path_is_unchanged(env, global_provider, expected):
    cfg = env.configure(global_provider, SANDER)                    # Theo has no override
    _ring(env, agent="a-theo")
    theo = cfg.cartesia_voice_for("Theo")
    assert theo
    assert env.spoken() == [(p, theo if v == "<theo's cartesia voice>" else v) for p, v in expected]
    if global_provider == "elevenlabs":                             # the engine's own key, model and speed
        assert env.builtin[0]["api_key"] == "engine-key"
        assert (env.builtin[0]["model"], env.builtin[0]["speed"]) == ("engine-model", 1.3)


@pytest.mark.parametrize("session", [None, "", "nobody-0000"])
def test_without_an_agent_the_direct_path_is_unchanged(env, session):
    env.configure("cartesia", {**SANDER, "nobody-0000": SANDER["sander"]})
    env.engine.synthesize("Hello", "stored-voice", session=session)
    assert env.spoken() == [("elevenlabs-direct", "stored-voice")]   # no Cartesia voice: today's EL fallback


def test_a_legacy_voice_map_still_resolves_per_provider(env):
    env.configure("elevenlabs", SANDER)
    env.engine.synthesize("Hello", json.dumps({"elevenlabs": "el-id", "cartesia": "ca-id"}), session="theo-97e5")
    assert env.spoken() == [("elevenlabs-direct", "el-id")]


def test_server_audio_disabled_stays_disabled_for_an_agent_with_an_override(env):
    env.configure("none", SANDER)
    with pytest.raises(RuntimeError, match="server audio is disabled"):
        env.engine.synthesize("Hello", "stored-voice", session="sander-1")
    assert env.spoken() == []


def _preview(env, body):
    spec = importlib.util.spec_from_file_location("clarp_server_preview", ROOT / "server/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    answer = {}
    handler = SimpleNamespace(
        _read_json=lambda: body,
        ctx=SimpleNamespace(tts=env.engine, default_session="sander-1",
                            stream=SimpleNamespace(broadcast=lambda event: None)),
        _json_ok=lambda payload: answer.update(status=200, body=payload),
        _json_error=lambda status, message: answer.update(status=status, body=message))
    server.Handler._handle_preview(handler)
    return answer


def test_a_voice_preview_speaks_the_explicit_voice_even_for_an_agent_with_an_override(env):
    env.configure("cartesia", SANDER)
    answer = _preview(env, {"voice_id": "candidate-voice", "text": "Hi there"})   # default session: Sander
    assert answer["status"] == 200
    assert env.spoken() == [("elevenlabs-direct", "candidate-voice")]   # today's global path, the explicit voice


def test_a_muted_agent_is_still_refused_before_any_voice(env):
    from lib.agent_rings import CallError
    env.configure("cartesia", SANDER)
    _agent("a-quiet", "Quiet", "quiet-0000", muted=True)
    with pytest.raises(CallError):
        _ring(env, agent="a-quiet")
    assert env.spoken() == []


def test_the_sidecar_keeps_its_fields(env):
    env.configure("cartesia", SANDER)
    ring = _ring(env)
    clip = env.engine.audio_dir / pathlib.Path(ring["opening_audio_url"]).name
    sidecar = json.loads(clip.with_suffix(".json").read_text()) if clip.with_suffix(".json").exists() else None
    assert sidecar is None or {"voice_id", "session"} <= set(sidecar)


# ---- F2: only an effective override changes the route ------------------------------------------

def test_a_same_provider_voice_override_takes_the_agent_route_with_that_voice(env):
    env.configure("cartesia", {"sander": {"cartesia_voice": "sander-cartesia"}})
    _ring(env)
    assert env.spoken() == [("cartesia", "sander-cartesia")]


@pytest.mark.parametrize("override", [
    {"gemini_voice": "Charon"},                       # a voice for a provider not in use
    {"cartesia_voice": ""},                           # an empty voice
    {"provider": "cartesia"},                         # the global provider, named again
])
def test_an_override_that_changes_nothing_keeps_the_legacy_path(env, override):
    env.configure("cartesia", {"sander": override})
    ring = _ring(env)
    # Today's direct path: Sander has no Cartesia voice, so the built-in ElevenLabs backup.
    assert ring["opening_audio_url"].startswith("/audio/")
    assert env.spoken() == [("elevenlabs-direct", "stored-voice")]
    assert env.builtin[0]["api_key"] == "engine-key"


# ---- F1(a): the agent route falls back exactly as the queue does ------------------------------

def test_an_effective_override_without_a_fallback_never_tries_another_provider(env):
    from lib.context import ServerContext
    env.configure("cartesia", {"sander": {"cartesia_voice": "sander-cartesia"}})
    env.fail.add("cartesia")
    ring = _ring(env)
    assert ring["opening_audio_url"] == ""                           # a silent opening, truthfully
    assert _herald(env) == ("synthesis_failed", "")
    ServerContext.speak_announcement(SimpleNamespace(tts=env.engine), "Sander is ready", "stored-voice",
                                     session="sander-1")                     # logged, never raised
    assert env.spoken() == [("cartesia", "sander-cartesia")] * 3        # no second provider, ever


def test_an_effective_override_with_a_configured_fallback_uses_exactly_it(env):
    env.configure("cartesia", {"sander": {"cartesia_voice": "sander-cartesia"}}, fallback="custom.alt")
    env.fail.add("cartesia")
    _ring(env)
    assert env.spoken() == [("cartesia", "sander-cartesia"), ("custom.alt", "rec-default")]


def test_without_an_effective_override_the_elevenlabs_backup_is_unchanged(env):
    cfg = env.configure("cartesia", {"sander": {"gemini_voice": "Charon"}})
    env.fail.add("cartesia-direct")
    _ring(env, agent="a-theo")
    assert env.spoken() == [("cartesia-direct", cfg.cartesia_voice_for("Theo")),
                            ("elevenlabs-direct", "stored-voice")]


def test_the_adapter_dir_is_the_sessions_own_empty_dir():
    """tests/conftest.py (the backend guard's pytest_configure) points
    CLARP_TTS_ADAPTERS_DIR at an empty directory of the test session before any
    lib import, in every directory and xdist worker, so neither this module nor
    any other ever sees an installed adapter unless a test installs one."""
    from lib import tts_providers
    root = pathlib.Path(os.environ.get("CLARP_TTS_ADAPTERS_DIR", ""))
    assert custom_tts_adapters.ROOT == root
    assert root.is_relative_to(pathlib.Path(os.environ["CLARP_TEST_GUARD_DIR"]))
    assert custom_tts_adapters.discover() == []
    assert not [row["id"] for row in tts_providers.status()["providers"] if row["id"].startswith("custom.")]


def _record_google(env):
    from lib import google_tts

    def google(*, text, voice, out_path, creds, on_chunk=None, **_kwargs):
        if voice not in google_tts.VOICES:                  # the core's own voice check
            raise google_tts.GoogleTTSError("voice_unavailable")
        env.builtin.append({"provider": "google", "voice": voice, "text": text})
        pathlib.Path(out_path).write_bytes(b"ID3google")
        return 9
    env.monkeypatch.setattr(google_tts, "synthesize", google)


def test_a_google_voice_preview_speaks_the_requested_voice_not_the_agents_override(env):
    """Preview opts out of the agent route (FINAL r2): with google selected, previewing a google
    voice for an agent whose [tts.agents] entry says cartesia speaks exactly that google voice."""
    _record_google(env)
    env.configure("google", {"sander": {"provider": "cartesia", "cartesia_voice": "sander-c"}})
    answer = _preview(env, {"voice_id": "nb-NO-Chirp3-HD-Aoede", "text": "Hello"})   # session: Sander
    assert answer["status"] == 200
    assert env.spoken() == [("google", "nb-NO-Chirp3-HD-Aoede")]


def test_an_agents_google_override_never_hijacks_another_providers_preview(env):
    _record_google(env)
    env.configure("cartesia", {"sander": {"provider": "google",
                                          "google_voice": "nb-NO-Chirp3-HD-Kore"}})
    answer = _preview(env, {"voice_id": "previewed-voice", "text": "Hello"})   # session: Sander
    assert answer["status"] == 200
    assert env.spoken() == [("elevenlabs-direct", "previewed-voice")]   # today's global path, no google


@pytest.mark.parametrize("requested", ["nb-NO-Chirp3-HD-Nobody", "21m00Tcm4TlvDq8ikWAM"],
                         ids=["off-list-google-id", "elevenlabs-id"])
def test_a_google_preview_of_a_voice_off_the_list_fails_and_never_plays_the_default(env, requested):
    """A preview auditions the REQUESTED voice (FINAL r2): with google selected, an id that is
    not a Google voice is refused, never replaced by Kore."""
    _record_google(env)
    env.configure("google")
    answer = _preview(env, {"voice_id": requested, "text": "Hello"})
    assert answer["status"] == 500
    assert env.spoken() == []
