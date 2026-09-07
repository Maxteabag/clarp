"""Keep what the transcriber heard.

Until now /transcribe discarded the clip the moment Whisper answered, and the
phone deleted its copy right after upload. When a transcript comes back
garbled there is nothing to listen to, so nobody can say whether the model
misheard or the microphone never captured it. This retains each clip next to
its trace id, behind a setting, with a bounded footprint.

Retention is opt-in (`transcription.retain_audio`) because it is diagnostic
material, not product data; the sweep keeps at most `MAX_CLIPS` files or
`MAX_AGE_DAYS` days, whichever prunes more.
"""
from __future__ import annotations

import json
import pathlib
import re
import time

from . import settings_store
from .log import log_exception

RETAIN_KEY = "transcription.retain_audio"
MAX_CLIPS = 500
MAX_AGE_DAYS = 14

_EXT = {
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".m4a",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/webm": ".webm",
}
_TRACE_RE = re.compile(r"^[0-9a-f]{8,64}$")


def enabled() -> bool:
    return settings_store.get_bool(RETAIN_KEY, default=False)


def set_enabled(value: bool) -> None:
    settings_store.set_bool(RETAIN_KEY, bool(value))


def directory(cache_dir: pathlib.Path) -> pathlib.Path:
    return cache_dir / "heard"


def _ext(content_type: str) -> str:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return _EXT.get(base, ".bin")


def valid_trace(trace_id: str) -> bool:
    return bool(_TRACE_RE.match(trace_id or ""))


def retain(cache_dir: pathlib.Path, *, trace_id: str, audio_bytes: bytes,
           content_type: str, session: str = "", run_id: int = 0,
           model: str = "") -> pathlib.Path | None:
    """Write the clip and a sidecar. Returns the path, or None when disabled.

    Never raises: retention is a diagnostic, and a full disk must not fail a
    user's turn.
    """
    if not enabled() or not valid_trace(trace_id) or not audio_bytes:
        return None
    try:
        root = directory(cache_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{trace_id}{_ext(content_type)}"
        path.write_bytes(audio_bytes)
        (root / f"{trace_id}.json").write_text(json.dumps({
            "trace_id": trace_id, "session": session, "run_id": run_id,
            "model": model, "content_type": content_type,
            "bytes": len(audio_bytes), "created_at": int(time.time() * 1000),
        }))
        sweep(root)
        return path
    except OSError as e:
        log_exception("heardAudioRetainFail", e, detail=trace_id)
        return None


def lookup(cache_dir: pathlib.Path, trace_id: str
           ) -> tuple[pathlib.Path, dict] | None:
    """The retained clip and its sidecar for one trace, if kept."""
    if not valid_trace(trace_id):
        return None
    root = directory(cache_dir)
    sidecar = root / f"{trace_id}.json"
    if not sidecar.is_file():
        return None
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
    for candidate in root.glob(f"{trace_id}.*"):
        if candidate.suffix != ".json" and candidate.is_file():
            return candidate, meta
    return None


def sweep(root: pathlib.Path, *, now: float | None = None) -> int:
    """Drop clips past the age or count limit. Returns how many were removed."""
    t = time.time() if now is None else now
    try:
        sidecars = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
    except OSError:
        return 0
    removed = 0
    cutoff = t - MAX_AGE_DAYS * 86400
    for index, sidecar in enumerate(sidecars):
        try:
            too_old = sidecar.stat().st_mtime < cutoff
        except OSError:
            too_old = True
        if index < MAX_CLIPS and not too_old:
            continue
        stem = sidecar.stem
        for victim in root.glob(f"{stem}.*"):
            try:
                victim.unlink()
                removed += 1
            except OSError:
                pass
    return removed
