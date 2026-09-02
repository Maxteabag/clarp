"""B8 + filename protocol: TTS engine interface and clip filename helpers."""

from __future__ import annotations

import os
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


def test_real_engine_uses_shutil_move(tmp_path):
    """B8: tmp and audio_dir may be on different filesystems, so the engine
    must use shutil.move (which falls back to copy+delete) rather than
    os.replace (which raises EXDEV). Pin via source inspection."""
    import inspect
    from lib.tts_engine import ElevenLabsEngine
    src = inspect.getsource(ElevenLabsEngine.synthesize)
    assert "shutil.move" in src
    assert "os.replace" not in src
    assert "except Exception as e" in src
    assert "tmp_path.unlink(missing_ok=True)" in src
    assert 'cfg.tts_provider == "none"' in src


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
