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

Beside the names, the file keeps "_catalogue" (the LAST successful catalogue fetch, replaced
each time: what the provider offers today, each voice with its language and whether it is public
and whether it is the account's own, as the row said) and "_gone" (voices in use that the provider
answered 404 for at the last lookup: deleted voices, logged once each as voiceGone). The voice
pool hands out only today's catalogue; the profile reads a gone voice as voice_unavailable.
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


def catalogue(provider: str, path: pathlib.Path | None = None) -> dict[str, dict] | None:
    """The provider's catalogue as last fetched (today's): voice id -> {"language", "public",
    "owner"} as each row said, or None when it never was; never a fetch."""
    loaded = _loaded(path or names_path())
    rows = (loaded[0].get("_catalogue") or {}).get(provider) if loaded else None
    return {k: dict(v) for k, v in rows.items()} if rows is not None else None


def gone(provider: str, path: pathlib.Path | None = None) -> frozenset[str]:
    """Voices in use the provider answered 404 for at the last lookup (deleted)."""
    loaded = _loaded(path or names_path())
    return frozenset((loaded[0].get("_gone") or {}).get(provider) or ()) if loaded else frozenset()


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


_META = ("_catalogue", "_gone")       # provider -> sorted voice ids


def _read_bytes(data: bytes, path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        from .log import log_exception
        log_exception("voiceNamesReadFail", exc, detail=str(path))
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {
        str(p): {str(k): str(v) for k, v in m.items() if k and isinstance(v, str) and v.strip()}
        for p, m in raw.items() if isinstance(m, dict) and not str(p).startswith("_")}
    held = raw.get("_gone")
    if isinstance(held, dict):
        out["_gone"] = {str(p): sorted({str(v) for v in ids if v})
                        for p, ids in held.items() if isinstance(ids, list)}
    held = raw.get("_catalogue")
    if isinstance(held, dict):
        # provider -> voice id -> {"language", "public", "owner"} as the catalogue row said
        out["_catalogue"] = {str(p): {str(k): {"language": str((m or {}).get("language") or ""),
                                               "public": bool((m or {}).get("public")),
                                               "owner": bool((m or {}).get("owner"))}
                                      for k, m in rows.items() if k and isinstance(m, dict)}
                             for p, rows in held.items() if isinstance(rows, dict)}
    return out


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
    # A per-agent override ([tts.agents.<name>] cartesia_voice / elevenlabs_voice) is spoken too.
    for override in (getattr(cfg, "tts_agent_overrides", {}) or {}).values():
        for provider in found:
            voice = str((override or {}).get(f"{provider}_voice") or "").strip()
            if voice:
                found[provider].add(voice)
    return found


def holders(cfg, provider: str, voice_id: str) -> list[str]:
    """The names that speak with ``voice_id`` for ``provider`` (agents, contacts, persona
    lists, overrides): for the voiceGone line."""
    from . import agents as agents_db, personas
    names_ = [str(r.get("persona") or r.get("name") or "") for r in
              [*agents_db.list_agents(), *personas.list_all()]
              if voice_map(str(r.get("voice_id") or "")).get(provider) == voice_id]
    if provider == CARTESIA:
        names_ += [n for n, v in (getattr(cfg, "cartesia_voices", {}) or {}).items() if v == voice_id]
    names_ += [n for n, o in (getattr(cfg, "tts_agent_overrides", {}) or {}).items()
               if str((o or {}).get(f"{provider}_voice") or "").strip() == voice_id]
    return sorted(dict.fromkeys(n for n in names_ if n))


def _not_found(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 404 or getattr(exc, "status", None) == 404


def _lookup(key: str, ids: set[str], fetch_one: Callable[[str, str], dict],
            provider: str) -> tuple[dict[str, str], set[str]]:
    """(names found, ids the provider answered 404 for)."""
    from .log import log_exception
    out, missing = {}, set()
    for voice_id in sorted(ids)[:MAX_LOOKUPS]:
        try:
            row = fetch_one(key, voice_id) or {}
        except Exception as exc:  # noqa: BLE001 - an unknown or removed voice stays nameless
            if _not_found(exc):
                missing.add(voice_id)
            else:
                log_exception("voiceNamesLookupFail", exc, detail=f"{provider} {voice_id}")
            continue
        if str(row.get("name") or "").strip():
            out[voice_id] = str(row["name"]).strip()
    return out, missing


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
    from .voice_profile import elevenlabs_id_usable
    cfg = cfg or config_module.load()
    cartesia_key, eleven_key = cfg.cartesia_key(), cfg.eleven_key()
    if not cartesia_key and not eleven_key:
        return []
    used = (voices_in_use or in_use)(cfg)
    fresh: dict[str, dict[str, str]] = {}
    gone_now: dict[str, set[str]] = {}
    today: dict[str, dict[str, dict]] = {}
    if cartesia_key:
        try:
            rows = (cartesia_all or cartesia_voices.all_voices)(cartesia_key)
        except Exception as exc:  # noqa: BLE001 - the file keeps Cartesia's old names
            log_exception("voiceNamesFetchFail", exc, detail=CARTESIA)
        else:
            today[CARTESIA] = {str(r.get("id")): {"language": str(r.get("language") or ""),
                                                   "public": bool(r.get("is_public")),
                                                   "owner": bool(r.get("is_owner"))}
                               for r in rows if r.get("id")}
            found = {str(r.get("id")): str(r.get("name")).strip()
                     for r in rows if r.get("id") and str(r.get("name") or "").strip()}
            named, gone_now[CARTESIA] = _lookup(cartesia_key, used.get(CARTESIA, set()) - set(today[CARTESIA]),
                                                cartesia_one or cartesia_voices.fetch_one, CARTESIA)
            fresh[CARTESIA] = {**found, **named}
    # ElevenLabs is asked only for ids it can hold: a bare Cartesia uuid stored before the
    # one stored shape reads as an ElevenLabs id, and ElevenLabs answers it 400.
    eleven_ids = {v for v in used.get(ELEVENLABS, set()) if elevenlabs_id_usable(v)}
    if eleven_key and eleven_ids:
        fresh[ELEVENLABS], gone_now[ELEVENLABS] = _lookup(
            eleven_key, eleven_ids, eleven_one or elevenlabs_voices.fetch_one, ELEVENLABS)
    if not fresh:
        return []
    current = _read(path) if path.exists() else {}
    before = json.dumps(current, sort_keys=True, separators=(",", ":"))
    for provider, found in fresh.items():
        current[provider] = {k: v for k, v in {**current.get(provider, {}), **found}.items()
                             if k not in gone_now.get(provider, ())}
    for provider, ids in today.items():
        current.setdefault("_catalogue", {})[provider] = ids
    for provider, ids in gone_now.items():
        current.setdefault("_gone", {})[provider] = sorted(ids)
        for voice_id in sorted(ids - set(((json.loads(before) if before else {}).get("_gone") or {})
                                          .get(provider) or ())):
            log("voiceGone", f"provider={provider} id={voice_id} agents={','.join(holders(cfg, provider, voice_id))}")
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
