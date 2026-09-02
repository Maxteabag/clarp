"""TTSEngine interface and the ElevenLabs implementation.

The real engine speaks directly to api.elevenlabs.io over HTTPS (see
`eleven_http.py`). The interface keeps tests free of network calls — they
substitute `FakeTTSEngine` and get deterministic dummy MP3 bytes.

Bugs this module pins (see TESTS.md):
- B8: cross-filesystem move falls back to copy+delete (shutil.move), never
  os.replace.
- Filename protocol property test — round-trip session ↔ filename.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod

from .eleven_http import synthesize_to_file


def make_clip_filename(session: str | None = None, *, now_ms: int | None = None) -> str:
    """Build the canonical MP3 filename for a clip.

    Format: '<epoch-ms>__<session>.mp3', or '<epoch-ms>.mp3' when no
    session is given.
    """
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    if session:
        # Same allowed-chars rule as the agent session id sanitiser.
        safe = "".join(c for c in session if c.isalnum() or c in "._-") or "anon"
        return f"{ts}__{safe}.mp3"
    return f"{ts}.mp3"


_FN_RE = re.compile(r"^(?P<ts>\d{10,})(?:__(?P<sess>[A-Za-z0-9._-]+))?\.mp3$")


def parse_clip_filename(name: str) -> tuple[int | None, str | None]:
    """Inverse of `make_clip_filename`. Returns (ts_ms, session_or_None)."""
    m = _FN_RE.match(name)
    if not m:
        return None, None
    ts = int(m.group("ts"))
    sess = m.group("sess") or None
    return ts, sess


class TTSEngine(ABC):
    """Generate an MP3 from text and place it in the audio cache."""

    @abstractmethod
    def synthesize_herald(self, text: str, voice_id: str, *,
                          session: str | None = None) -> pathlib.Path:
        """Write a herald announcement clip to the audio cache."""

    @abstractmethod
    def synthesize(self, text: str, voice_id: str, *,
                   session: str | None = None) -> pathlib.Path:
        """Write an MP3 to the audio cache directory.

        Returns the destination path.
        """


class ElevenLabsEngine(TTSEngine):
    def __init__(self, audio_dir: pathlib.Path, *,
                 api_key: str,
                 model: str = "eleven_flash_v2_5",
                 speed: float = 1.2,
                 timeout: float = 20.0):
        self.audio_dir = audio_dir
        self.api_key = api_key
        self.model = model
        self.speed = speed
        self.timeout = timeout

    def _synthesize_cartesia(self, text, voice_id, out_path, session) -> bool:
        """Try Cartesia for a one-off clip. Returns True on success, False if
        Cartesia isn't the active provider, has no voice for this persona, no
        key, or errors — in which case the caller falls back to ElevenLabs."""
        from .config import load as _load
        from . import voice as _voice
        cfg = _load()
        if cfg.tts_provider != _voice.CARTESIA or not cfg.cartesia_key():
            return False
        persona = ""
        if session:
            from . import agents as _agents
            ag = _agents.get_by_session(session)
            persona = (ag or {}).get("persona", "")
        cart_voice = (_voice.resolve_voice(voice_id, _voice.CARTESIA)
                      or cfg.cartesia_voice_for(persona))
        if not cart_voice:
            return False
        from .cartesia_tts import CartesiaError
        from .cartesia_tts import synthesize as _cart
        try:
            _cart(text=text, voice_id=cart_voice, out_path=out_path,
                  api_key=cfg.cartesia_key(), model=cfg.cartesia_model)
            return True
        except CartesiaError:
            return False

    def synthesize_herald(self, text, voice_id, *, session=None) -> pathlib.Path:
        return self.synthesize(
            text, voice_id, session=session,
            _filename_prefix="herald-internal-",
        )

    def synthesize(self, text, voice_id, *, session=None,
                   _filename_prefix: str = "") -> pathlib.Path:
        import time as _t
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".mp3", prefix="claude-pwa-tts-")
        os.close(fd)
        tmp_path = pathlib.Path(tmp)
        started = _t.time()
        try:
            # Provider dispatch (mirrors the PWA worker): Cartesia primary,
            # ElevenLabs fallback. Announcements ("X is ready") were silently
            # failing once the EL quota ran out — Cartesia keeps them audible.
            from .config import load as _load
            cfg = _load()
            if cfg.tts_provider == "none":
                raise RuntimeError("server audio is disabled: no voice provider")
            if cfg.tts_provider not in {"cartesia", "elevenlabs"}:
                from . import agents as _agents
                from .tts_worker import _synthesize as _provider_synthesize
                agent = _agents.get_by_session(session) if session else {}
                _provider_synthesize(
                    cfg=cfg,
                    row={"text": text, "voice_id": voice_id,
                         "session": session or ""},
                    agent=agent or {}, out_path=tmp_path,
                    on_chunk=None, trace_id="",
                    delivery_fields={"delivery": "chunked-file"})
            elif not self._synthesize_cartesia(text, voice_id, tmp_path, session):
                synthesize_to_file(
                    text, voice_id, tmp_path,
                    api_key=self.api_key,
                    model=self.model,
                    speed=self.speed,
                    timeout=self.timeout,
                )
            target = self.audio_dir / (
                _filename_prefix + make_clip_filename(session))
            # B8: must use shutil.move here — tmp and audio_dir may live on
            # different filesystems, in which case an atomic rename would
            # raise EXDEV. shutil falls back to copy+delete.
            shutil.move(str(tmp_path), target)
            # Sidecar metadata so consumers don't have to parse filenames.
            try:
                from . import clips as _clips, agents as _agents
                ag = _agents.get_by_session(session) if session else None
                _clips.write_sidecar(
                    target,
                    agent_id=(ag or {}).get("agent_id"),
                    persona=(ag or {}).get("persona"),
                    voice_id=voice_id,
                    session=session,
                    bytes_=target.stat().st_size,
                    text_len=len(text),
                    extra={"model": self.model, "speed": self.speed},
                )
            except Exception:
                pass
            try:
                from . import eventlog as _el
                _el.emit(
                    "tts", "synthOk",
                    duration_ms=int((_t.time() - started) * 1000),
                    clip_url=f"/audio/{target.name}",
                    detail={
                        "session": session, "voice_id": voice_id,
                        "model": self.model, "speed": self.speed,
                        "bytes": target.stat().st_size,
                        "text_len": len(text),
                    },
                )
            except Exception:
                pass
            return target
        except Exception as e:
            try:
                from . import eventlog as _el
                _el.emit_exception("tts", "synthFail", e,
                                   duration_ms=int((_t.time() - started) * 1000),
                                   detail={"session": session, "voice_id": voice_id})
            except Exception:
                pass
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as cleanup_err:
                from .log import log_exception
                log_exception("ttsCleanupFail", cleanup_err, detail=str(tmp_path))
            raise


class FakeTTSEngine(TTSEngine):
    """Test double. Writes a deterministic byte string to the audio dir."""

    SILENT_MP3 = (
        b"ID3\x04\x00\x00\x00\x00\x00\x00"  # tiny ID3 header — readable as mp3 by most decoders
    )

    def __init__(self, audio_dir: pathlib.Path):
        self.audio_dir = audio_dir
        self.calls: list[dict] = []

    def synthesize_herald(self, text, voice_id, *, session=None) -> pathlib.Path:
        return self._synthesize(
            text, voice_id, session=session,
            filename_prefix="herald-internal-",
        )

    def synthesize(self, text, voice_id, *, session=None) -> pathlib.Path:
        return self._synthesize(text, voice_id, session=session)

    def _synthesize(self, text, voice_id, *, session=None,
                    filename_prefix: str = "") -> pathlib.Path:
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.calls.append({"text": text, "voice_id": voice_id, "session": session})
        target = self.audio_dir / (filename_prefix + make_clip_filename(session))
        target.write_bytes(self.SILENT_MP3)
        return target
