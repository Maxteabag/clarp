"""Names of provider voices for voice_profile's voice_label, kept on disk.

A snapshot never fetches: ``names(provider)`` reads a small JSON file (one stat per call; the
file is parsed again only when it changed) and answers {} when there is none. The file is
filled off every read path by ``VoiceNamesWorker``, a minute after start and every 24 hours:
Cartesia's catalogue (every language, paged) and then, by id, any Cartesia voice in use that
the pages missed; ElevenLabs by id, for the voices in use only. Each provider only with its
key; a failed fetch keeps what the file had; a voice found nowhere stays without a name
(voice_label null). One refresh at a time; a test Host never starts the worker
(CLARP_VOICE_NAMES_WORKER=0). A refresh that finds what the file already holds does not
rewrite it (it only marks the check), and the start-up refresh is skipped while the file is
younger than REFRESH_SECONDS, so a restart with nothing changed moves no profile version.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import pathlib
import threading
from typing import Any, Callable

from .deployment import LAYOUT
from .revisioned_cache import RevisionedCache
from .voice import CARTESIA, ELEVENLABS, voice_map

def names_path() -> pathlib.Path:
    """Resolved on every call (CLARP_VOICE_NAMES, else the Host's cache dir), so a test
    session's own file is used, never the live Host's."""
    return pathlib.Path(os.environ.get("CLARP_VOICE_NAMES") or (LAYOUT.cache_dir / "voice-names.json"))


REFRESH_SECONDS = 24 * 3600
FIRST_AFTER_SECONDS = 60.0
MAX_LOOKUPS = 100          # voices looked up one by one, at most, per provider and refresh

_FILE = RevisionedCache("voice-names-file", max_entries=4)
_REFRESHING = threading.Lock()


def worker_enabled() -> bool:
    return os.environ.get("CLARP_VOICE_NAMES_WORKER", "1") != "0"


def _loaded(path: pathlib.Path) -> tuple[dict[str, dict[str, str]], str] | None:
    """(names, content digest) of the file, parsed again only when it changed; None: no file."""
    try:
        st = path.stat()
    except OSError:
        return None
    return _FILE.get_or_compute(str(path), (st.st_mtime_ns, st.st_size), lambda: _parse(path))


def _parse(path: pathlib.Path) -> tuple[dict[str, dict[str, str]], str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return {}, ""
    return _read_bytes(raw, path), hashlib.sha256(raw).hexdigest()[:16]


def names(provider: str, path: pathlib.Path | None = None) -> dict[str, str]:
    """voice id -> name for ``provider`` from the file; never a fetch, never a query."""
    loaded = _loaded(path or names_path())
    return dict(loaded[0].get(provider) or {}) if loaded else {}


def signature(path: pathlib.Path | None = None) -> str | None:
    """A digest of the file's content, or None: what voice_profile's version follows (the
    same names rewritten leave it unchanged)."""
    loaded = _loaded(path or names_path())
    return loaded[1] if loaded else None


def start_due(path: pathlib.Path | None = None, now: float | None = None) -> bool:
    """Whether the start-up refresh is due: no file, or one older than REFRESH_SECONDS."""
    try:
        age = (time.time() if now is None else now) - (path or names_path()).stat().st_mtime
    except OSError:
        return True
    return age >= REFRESH_SECONDS


def _read(path: pathlib.Path) -> dict[str, dict[str, str]]:
    try:
        return _read_bytes(path.read_bytes(), path)
    except OSError:
        return {}


def _read_bytes(data: bytes, path: pathlib.Path) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
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
        return _refresh(cfg, path=path or names_path(), cartesia_all=cartesia_all,
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
    before = json.dumps(current, sort_keys=True, separators=(",", ":"))
    for provider, found in fresh.items():
        current[provider] = {**current.get(provider, {}), **found}
    body = json.dumps(current, sort_keys=True, separators=(",", ":"))
    if path.exists() and body == before:
        os.utime(path)                 # checked today; same names: the version stays
        return sorted(fresh)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    log("voiceNamesRefreshed", " ".join(f"{p}={len(current[p])}" for p in sorted(fresh)))
    return sorted(fresh)


class VoiceNamesWorker:
    """Refreshes the names file FIRST_AFTER_SECONDS after start (out of the start's busiest
    seconds) when it is due (start_due), and then every REFRESH_SECONDS, on its own thread."""

    def __init__(self, interval: float = REFRESH_SECONDS, first_after: float = FIRST_AFTER_SECONDS,
                 refresh_fn: Callable[[], Any] | None = None):
        self.interval = interval
        self.first_after = first_after
        self._refresh = refresh_fn or refresh
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "VoiceNamesWorker":
        self._thread = threading.Thread(target=self._run, name="voice-names", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def start_pass(self) -> bool:
        """The start-up refresh, only when due; True when it ran."""
        if not start_due():
            return False
        self._once()
        return True

    def _once(self) -> None:
        from . import db
        from .log import log_exception
        try:
            self._refresh()
        except Exception as exc:  # noqa: BLE001 - never take the Host down for names
            log_exception("voiceNamesWorkerFail", exc, detail="")
        finally:
            db.close_local()

    def _run(self) -> None:
        if self._stop.wait(self.first_after):
            return
        self.start_pass()
        while not self._stop.wait(self.interval):
            self._once()
