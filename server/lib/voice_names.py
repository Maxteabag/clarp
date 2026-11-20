"""Names of provider voices for voice_profile's voice_label, kept on disk.

A snapshot never fetches: ``names(provider)`` reads a small JSON file (one stat per call; the
file is parsed again only when it changed) and answers {} when there is none. The file is
filled off every read path by ``VoiceNamesWorker``, a minute after start and every 24 hours:
Cartesia's catalogue (every language, paged) and then, by id, any Cartesia voice in use that
the pages missed; ElevenLabs by id, for the voices in use only. Each provider only with its
key; a failed fetch keeps what the file had; a voice found nowhere stays without a name
(voice_label null). One refresh at a time; a test Host never starts the worker
(CLARP_VOICE_NAMES_WORKER=0).
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any, Callable

from .deployment import LAYOUT
from .revisioned_cache import RevisionedCache
from .voice import CARTESIA, ELEVENLABS, voice_map

PATH = pathlib.Path(os.environ.get("CLARP_VOICE_NAMES", LAYOUT.cache_dir / "voice-names.json"))
REFRESH_SECONDS = 24 * 3600
FIRST_AFTER_SECONDS = 60.0
MAX_LOOKUPS = 100          # voices looked up one by one, at most, per provider and refresh

_FILE = RevisionedCache("voice-names-file", max_entries=4)
_REFRESHING = threading.Lock()


def worker_enabled() -> bool:
    return os.environ.get("CLARP_VOICE_NAMES_WORKER", "1") != "0"


def names(provider: str, path: pathlib.Path | None = None) -> dict[str, str]:
    """voice id -> name for ``provider`` from the file; never a fetch, never a query."""
    path = path or PATH
    try:
        st = path.stat()
    except OSError:
        return {}
    data = _FILE.get_or_compute(str(path), (st.st_mtime_ns, st.st_size), lambda: _read(path))
    return dict(data.get(provider) or {})


def signature(path: pathlib.Path | None = None) -> list[int] | None:
    """The names file's (mtime_ns, size), or None: what voice_profile's version follows."""
    try:
        st = (path or PATH).stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _read(path: pathlib.Path) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        from .log import log_exception
        log_exception("voiceNamesReadFail", exc, detail=str(path))
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(p): {str(k): str(v) for k, v in m.items() if k and isinstance(v, str) and v.strip()}
            for p, m in raw.items() if isinstance(m, dict)}


def in_use(cfg) -> dict[str, set[str]]:
    """The Cartesia and ElevenLabs voices an agent, a contact or the persona lists name."""
    from . import agents as agents_db, personas
    found: dict[str, set[str]] = {CARTESIA: set(), ELEVENLABS: set()}
    found[CARTESIA].update(v for v in (getattr(cfg, "cartesia_voices", {}) or {}).values() if v)
    found[CARTESIA].update(v for v in (getattr(cfg, "cartesia_pool", ()) or ()) if v)
    for row in [*agents_db.list_agents(), *personas.list_all()]:
        stored = voice_map(str(row.get("voice_id") or ""))
        for provider in found:
            if stored.get(provider):
                found[provider].add(stored[provider])
    return found


def _lookup(key: str, ids: set[str], fetch_one: Callable[[str, str], dict], provider: str) -> dict[str, str]:
    from .log import log_exception
    out = {}
    for voice_id in sorted(ids)[:MAX_LOOKUPS]:
        try:
            row = fetch_one(key, voice_id) or {}
        except Exception as exc:  # noqa: BLE001 - an unknown or removed voice stays nameless
            log_exception("voiceNamesLookupFail", exc, detail=f"{provider} {voice_id}")
            continue
        if str(row.get("name") or "").strip():
            out[voice_id] = str(row["name"]).strip()
    return out


def refresh(cfg=None, *, path: pathlib.Path | None = None,
            cartesia_all: Callable[[str], list[dict]] | None = None,
            cartesia_one: Callable[[str, str], dict] | None = None,
            eleven_one: Callable[[str, str], dict] | None = None,
            voices_in_use: Callable[[Any], dict[str, set[str]]] | None = None) -> list[str]:
    """Fetch the names and write the file; the providers refreshed (none: file untouched).
    Never two at once: a refresh asked while one runs does nothing."""
    if not _REFRESHING.acquire(blocking=False):
        return []
    try:
        return _refresh(cfg, path=path or PATH, cartesia_all=cartesia_all,
                        cartesia_one=cartesia_one, eleven_one=eleven_one,
                        voices_in_use=voices_in_use)
    finally:
        _REFRESHING.release()


def _refresh(cfg, *, path, cartesia_all, cartesia_one, eleven_one, voices_in_use) -> list[str]:
    from . import cartesia_voices, config as config_module, elevenlabs_voices
    from .log import log, log_exception
    cfg = cfg or config_module.load()
    cartesia_key, eleven_key = cfg.cartesia_key(), cfg.eleven_key()
    if not cartesia_key and not eleven_key:
        return []
    used = (voices_in_use or in_use)(cfg)
    fresh: dict[str, dict[str, str]] = {}
    if cartesia_key:
        try:
            rows = (cartesia_all or cartesia_voices.all_voices)(cartesia_key)
        except Exception as exc:  # noqa: BLE001 - the file keeps Cartesia's old names
            log_exception("voiceNamesFetchFail", exc, detail=CARTESIA)
        else:
            found = {str(r.get("id")): str(r.get("name")).strip()
                     for r in rows if r.get("id") and str(r.get("name") or "").strip()}
            found.update(_lookup(cartesia_key, used.get(CARTESIA, set()) - set(found),
                                 cartesia_one or cartesia_voices.fetch_one, CARTESIA))
            fresh[CARTESIA] = found
    if eleven_key and used.get(ELEVENLABS):
        fresh[ELEVENLABS] = _lookup(eleven_key, used[ELEVENLABS],
                                    eleven_one or elevenlabs_voices.fetch_one, ELEVENLABS)
    if not fresh:
        return []
    current = _read(path) if path.exists() else {}
    for provider, found in fresh.items():
        current[provider] = {**current.get(provider, {}), **found}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    log("voiceNamesRefreshed", " ".join(f"{p}={len(current[p])}" for p in sorted(fresh)))
    return sorted(fresh)


class VoiceNamesWorker:
    """Refreshes the names file FIRST_AFTER_SECONDS after start (out of the start's busiest
    seconds) and then every REFRESH_SECONDS, on its own thread."""

    def __init__(self, interval: float = REFRESH_SECONDS, first_after: float = FIRST_AFTER_SECONDS):
        self.interval = interval
        self.first_after = first_after
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "VoiceNamesWorker":
        self._thread = threading.Thread(target=self._run, name="voice-names", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from . import db
        from .log import log_exception
        if self._stop.wait(self.first_after):
            return
        while not self._stop.is_set():
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 - never take the Host down for names
                log_exception("voiceNamesWorkerFail", exc, detail="")
            finally:
                db.close_local()
            self._stop.wait(self.interval)
