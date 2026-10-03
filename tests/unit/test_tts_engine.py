"""B8 + filename protocol: TTS engine interface and clip filename helpers."""

from __future__ import annotations

import os
import pathlib
import pytest

from lib.tts_engine import (
    FakeTTSEngine,
    make_clip_filename,
    parse_clip_filename,
)
from lib.roster import AGENT_ROSTER


@pytest.mark.parametrize("session", list(AGENT_ROSTER.keys()) + [None])
def test_filename_round_trip(session):
    """Property: every persona's session id round-trips through the filename
    protocol cleanly, so the SSE event can derive it."""
    name = make_clip_filename(session, now_ms=1700_000_000_000)
    ts, sess = parse_clip_filename(name)
    assert ts == 1700_000_000_000
    if session is None:
        assert sess is None
    else:
        # Session id is lowercased / sanitised when used as a session name; the
        # filename should preserve whatever was passed in (after sanitising).
        expected = "".join(c for c in session if c.isalnum() or c in "._-")
        assert sess == expected


def test_filename_strips_unsafe_chars():
    ts_ms = 1_700_000_000_000
    name = make_clip_filename("evil; rm -rf", now_ms=ts_ms)
    assert name == f"{ts_ms}__evilrm-rf.mp3"
    ts, sess = parse_clip_filename(name)
    assert ts == ts_ms and sess == "evilrm-rf"


def test_anonymous_clip_has_no_session_section():
    ts_ms = 1_700_000_000_000
    name = make_clip_filename(None, now_ms=ts_ms)
    assert name == f"{ts_ms}.mp3"
    ts, sess = parse_clip_filename(name)
    assert ts == ts_ms and sess is None


def test_fake_tts_writes_file(tmp_path):
    audio = tmp_path / "audio"
    eng = FakeTTSEngine(audio)
    out = eng.synthesize("hello", "voice-1", session="claude")
    assert out.exists()
    assert out.read_bytes() == FakeTTSEngine.SILENT_MP3
    assert "__claude.mp3" in out.name
    assert eng.calls[0] == {"text": "hello", "voice_id": "voice-1",
                             "session": "claude"}


def test_herald_tts_uses_internal_watcher_safe_filename(tmp_path):
    eng = FakeTTSEngine(tmp_path / "audio")

    out = eng.synthesize_herald("ready", "voice-1", session="rachel")

    assert out.name.startswith("herald-internal-")
    assert out.name.endswith("__rachel.mp3")


def test_real_engine_lands_the_clip_across_filesystems(tmp_path, monkeypatch):
    """B8: tmp and audio_dir may be on different filesystems, where a rename
    raises EXDEV; the clip must still land (copy+delete) and tmp be removed."""
    import errno
    from lib import config, tts_engine
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="elevenlabs"))
    temporary = tmp_path / "tmpfs" / "clip.mp3"
    temporary.parent.mkdir()

    def fixed_mkstemp(**_kwargs):
        return os.open(temporary, os.O_CREAT | os.O_RDWR, 0o600), str(temporary)

    def cross_device(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(tts_engine.tempfile, "mkstemp", fixed_mkstemp)
    monkeypatch.setattr(tts_engine, "synthesize_to_file",
                        lambda _text, _voice, out_path, **_kw: pathlib.Path(out_path).write_bytes(b"mp3"))
    monkeypatch.setattr(os, "rename", cross_device)
    monkeypatch.setattr(os, "replace", cross_device)
    engine = tts_engine.ElevenLabsEngine(tmp_path / "audio", api_key="configured")

    out = engine.synthesize("hello", "voice", session="claude")

    assert out.parent == tmp_path / "audio" and out.read_bytes() == b"mp3"
    assert not temporary.exists()


@pytest.mark.parametrize("provider", ["none"])
def test_client_or_disabled_provider_never_generates_paid_server_audio(
        provider, tmp_path, monkeypatch):
    from lib import config, tts_engine
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider=provider))
    temporary = tmp_path / "provider-disabled.mp3"

    def fixed_mkstemp(**_kwargs):
        return os.open(temporary, os.O_CREAT | os.O_RDWR, 0o600), str(temporary)

    monkeypatch.setattr(tts_engine.tempfile, "mkstemp", fixed_mkstemp)
    monkeypatch.setattr(
        tts_engine, "synthesize_to_file",
        lambda *_args, **_kwargs: pytest.fail("paid server TTS must not run"))
    engine = tts_engine.ElevenLabsEngine(tmp_path / "audio", api_key="configured")

    with pytest.raises(RuntimeError, match="server audio is disabled"):
        engine.synthesize("hello", "voice", session="agent")

    assert not temporary.exists()


def test_elevenlabs_fallback_gets_its_own_id_from_a_provider_map(tmp_path, monkeypatch):
    """A {provider: id} voice must reach ElevenLabs as the bare ElevenLabs id."""
    from lib import config, tts_engine
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="elevenlabs"))
    sent = []

    def fake_synthesize(text, voice_id, out_path, **_kwargs):
        sent.append(voice_id)
        pathlib.Path(out_path).write_bytes(b"mp3")

    monkeypatch.setattr(tts_engine, "synthesize_to_file", fake_synthesize)
    engine = tts_engine.ElevenLabsEngine(tmp_path / "audio", api_key="configured")
    engine.synthesize("hello", '{"cartesia":"c-uuid","elevenlabs":"elevenId123"}')
    engine.synthesize("hello", "plainElevenId")

    assert sent == ["elevenId123", "plainElevenId"]


def test_emotion_tags_reach_gemini_but_not_its_cartesia_fallback(tmp_path, monkeypatch):
    """Gemini acts [laughing] out; Cartesia, the fallback, would read it aloud."""
    from lib import config, gemini_tts, tts_mode, tts_worker
    cfg = config.Config(tts_provider="gemini", tts_fallback="cartesia",
                        cartesia_api_key="k", gemini_api_key="g")
    sent = []

    def failing_gemini(*, text, **_kwargs):
        sent.append(("gemini", text))
        raise gemini_tts.GeminiTTSError("down")

    def cartesia(*, text, **_kwargs):
        sent.append(("cartesia", text))
        return 3

    monkeypatch.setattr(gemini_tts, "synthesize", failing_gemini)
    monkeypatch.setattr(tts_worker, "cartesia_synthesize", cartesia)
    monkeypatch.setattr(tts_worker, "_gemini_voice", lambda *a, **k: "Kore")
    tts_worker._synthesize(
        cfg=cfg, row={"text": "Ha, [laughing] it works.", "voice_id": "c-uuid"},
        agent={"persona": "Rachel"}, out_path=tmp_path / "x.mp3", on_chunk=None,
        trace_id="", clip_route=tts_mode.Route("gemini", None, "cartesia"))

    assert sent == [("gemini", "Ha, [laughing] it works."), ("cartesia", "Ha, it works.")]
