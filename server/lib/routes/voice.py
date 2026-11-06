"""VoiceRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "voice"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import os
import pathlib
import threading
from lib.agent_store import load_agents, save_agent
from lib.config import persona_personality
from lib import eventlog
from lib import agents as agents_db
from lib import events
from lib import identity
from lib.log import log_exception
from lib.voices import voices_with_availability
from lib.routes.helpers import _runtime_paths, _raw_query_value
from lib.cartesia_preview_locks import _cartesia_preview_lock


class VoiceRoutes:
    def _handle_agent_mute(self):
        """Mute/unmute APNs pushes for a single live agent.

        Badges/unread remain enabled for user-facing speak turns; this flag
        only removes the interruption.
        """
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        if "muted" not in data:
            return self._json_error(400, "muted required")
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        muted = data.get("muted") is True
        agents_db.update_agent(agent["agent_id"], muted=muted)
        fresh = identity.lookup(session) or {}
        return self._json_ok({
            "ok": True,
            "session": session,
            "muted": bool(fresh.get("muted")),
        })

    def _handle_agent_voice(self):
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        voice_id = (data.get("voice_id") or "").strip()
        provider = (data.get("provider") or "").strip().lower()
        if not session or not voice_id:
            return self._json_error(400, "session and voice_id required")
        if provider == "google":
            # Only the built-in google provider is validated: its voices are a fixed allowlist.
            # A legacy custom "google" adapter refuses it first, before the allowlist.
            from lib import google_tts
            code = google_tts.collision_code()
            if code:
                return self._json_error(409, code)
            if voice_id not in google_tts.VOICES:
                return self._json_error(400, "unknown voice")
        agents = load_agents(self.ctx.agents_path)
        if session not in agents:
            return self._json_error(404, "no such agent")
        from lib.voice import merge_voice, resolve_voice
        for sid, info in agents.items():
            if sid == session:
                continue
            occupied = ((resolve_voice((info or {}).get("voice_id"), provider)
                         if provider else (info or {}).get("voice_id")))
            if occupied == voice_id:
                return self._json(409, {"error": "voice taken",
                                        "by": (info or {}).get("name") or sid})
        agents[session]["voice_id"] = (
            merge_voice(agents[session].get("voice_id"), provider, voice_id)
            if provider else voice_id)
        # This agent only: the roster read above may be stale for others (TP-03).
        save_agent(session, agents[session])
        return self._json_ok({"ok": True})

    def _handle_agent_voice_verbosity(self):
        """Set how much one agent narrates aloud while it is still working.

        Read fresh on the next turn dispatch, so moving the slider re-tunes a
        running agent without a relaunch.
        """
        from lib import voice_verbosity
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        if "voice_verbosity" not in data:
            return self._json_error(400, "voice_verbosity required")
        raw = data.get("voice_verbosity")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return self._json_error(400, "voice_verbosity must be an integer")
        if not voice_verbosity.MIN_LEVEL <= raw <= voice_verbosity.MAX_LEVEL:
            return self._json(400, {
     "error": "voice_verbosity out of range",
     "min": voice_verbosity.MIN_LEVEL,
     "max": voice_verbosity.MAX_LEVEL,
 })
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "no such agent")
        agents_db.update_agent(agent["agent_id"], voice_verbosity=raw)
        fresh = identity.lookup(session) or {}
        return self._json_ok({
            "ok": True,
            "session": session,
            "voice_verbosity": int(fresh.get("voice_verbosity") or 0),
            "options": voice_verbosity.options(),
        })

    def _handle_cartesia_voice_preview(self):
        import hashlib
        from lib.cartesia_voices import (
            cached_english_voice, english_voices, has_cached_catalog)
        from lib.cartesia_tts import CartesiaError
        from lib import config as app_config
        query = self._query()
        voice_id = (query.get("id", [""])[0] or "").strip()
        # The picker must load the catalog first, so validate against that same
        # in-memory snapshot. Never turn a preview tap into a slow catalog crawl.
        row = cached_english_voice(voice_id)
        if row is None and not has_cached_catalog():
            row = next((item for item in english_voices()
                        if str(item.get("id") or "") == voice_id), None)
        if not row:
            return self._send(404, b"not found")
        name = str(row.get("name") or "this voice")[:80]
        cfg = app_config.load()
        cache = (_runtime_paths().cache_dir
                 / "voice-previews")
        if cache.is_symlink():
            eventlog.emit("server", "cartesiaPreviewCacheUnsafe",
                          level="error", detail=str(cache))
            return self._send(500, b"preview cache unavailable")
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        prompt = f"Hello, I'm {name}. This is how I'll sound in Clarp."
        # Every input that can alter the bytes belongs in the key. The explicit
        # version makes future changes to synthesis behavior easy to invalidate.
        cache_input = "\0".join(("v1", voice_id, name, cfg.cartesia_model, prompt))
        cache_key = hashlib.sha256(cache_input.encode()).hexdigest()
        path = cache / (cache_key + ".mp3")
        lock = _cartesia_preview_lock(cache_key)
        with lock:
            cached = False
            try:
                stat = path.stat(follow_symlinks=False)
                cached = (path.is_file() and not path.is_symlink()
                          and stat.st_uid == os.geteuid())
            except FileNotFoundError:
                pass
            if not cached:
                temporary = path.with_name(
                    f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    from lib.tts_service import synth_preview
                    cache_primary = synth_preview(prompt, "cartesia", voice_id, temporary, cfg=cfg)
                    if not cache_primary:
                        try:
                            return self._send_file(temporary, content_type="audio/mpeg")
                        finally:
                            try:
                                temporary.unlink(missing_ok=True)
                            except OSError as cleanup_error:
                                log_exception("cartesiaPreviewCleanupFail", cleanup_error, detail=voice_id)
                    temporary.replace(path)
                except (CartesiaError, OSError) as exc:
                    try: temporary.unlink()
                    except OSError: pass
                    log_exception("cartesiaPreviewFail", exc, detail=voice_id)
                    return self._send(502, b"preview unavailable")
        return self._send_file(path)

    def _handle_cartesia_voices(self):
        from lib.cartesia_voices import catalog
        query = self._query()
        force = query.get("force", ["0"])[0] in {"1", "true"}
        try:
            body = catalog(self.ctx.agents_path, force=force)
        except Exception as exc:
            log_exception("cartesiaVoicesFail", exc)
            return self._json_error(502, "Cartesia voice library unavailable")
        self._json_ok(body)

    def _handle_controller_narration(self):
        """One spoken line for the iOS Flic tutorial, in the Host's voice.

        Primary voice is cached per text. A fallback is served temporarily
        so a different voice cannot enter the primary voice cache. Feature `controller_narration`.
        """
        from lib import config as app_config
        from lib import controller_narration
        from lib.cartesia_tts import CartesiaError
        text = controller_narration.normalize_text(
            _raw_query_value(self.path, "text"))
        if not text:
            return self._send(400, b"text required (at most 240 characters)")
        cfg = app_config.load()
        voice = controller_narration.voice_id(cfg)
        if not voice or not cfg.cartesia_key():
            return self._send(503, b"narration voice unavailable")
        cache = (_runtime_paths().cache_dir
                 / "controller-narration")
        if cache.is_symlink():
            eventlog.emit("server", "controllerNarrationCacheUnsafe",
                          level="error", detail=str(cache))
            return self._send(500, b"narration cache unavailable")
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = controller_narration.cache_key(
            text=text, voice=voice, model=cfg.cartesia_model)
        path = cache / (key + ".mp3")
        with _cartesia_preview_lock("narration:" + key):
            cached = False
            try:
                stat = path.stat(follow_symlinks=False)
                cached = (path.is_file() and not path.is_symlink()
                          and stat.st_uid == os.geteuid())
            except FileNotFoundError:
                pass
            if not cached:
                temporary = path.with_name(
                    f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    from lib.tts_service import synth_preview
                    cache_primary = synth_preview(text, "cartesia", voice, temporary, cfg=cfg, purpose="narration", agent={"persona": controller_narration.NARRATION_PERSONA})
                    if not cache_primary:
                        try:
                            return self._send_file(temporary, content_type="audio/mpeg")
                        finally:
                            try:
                                temporary.unlink(missing_ok=True)
                            except OSError as cleanup_error:
                                log_exception("controllerNarrationCleanupFail", cleanup_error, detail=voice)
                    temporary.replace(path)
                except (CartesiaError, OSError) as exc:
                    try: temporary.unlink()
                    except OSError: pass
                    log_exception("controllerNarrationFail", exc, detail=text[:60])
                    return self._send(502, b"narration unavailable")
        return self._send_file(path)

    def _handle_preview(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        voice_id = (data.get("voice_id") or "").strip()
        text = (data.get("text") or "Hi there, this is what I sound like.").strip()
        session = (data.get("session") or self.ctx.default_session).strip() or self.ctx.default_session
        if not voice_id:
            return self._json_error(400, "voice_id required")
        from lib.google_tts import GoogleIdCollision
        try:
            path = self.ctx.tts.synthesize_preview(text[:200], voice_id, session=session)
        except GoogleIdCollision as refused:
            # Built-in Google refused by a legacy custom "google" adapter: only these two guard
            # codes become 409 (raised before any voice, credential or clip); the strict preview
            # route has no fallback, and every other failure keeps its 500.
            return self._json_error(409, str(refused))
        except Exception as e:
            log_exception("previewSynthFail", e, detail=voice_id)
            return self._json_error(500, str(e))
        # A preview plays on request, so it bypasses herald arbitration and
        # goes straight to every connected client.
        name = pathlib.Path(str(path)).name
        events.broadcast(self.ctx.stream, events.audio(
            url=f"/audio/{name}", name=name, session=session, preview=True))
        return self._json_ok({"ok": True, "url": f"/audio/{name}"})

    def _handle_voice_catalog(self):
        from lib.voice_catalog import catalog
        query = self._query()
        session = (query.get("for", [""])[0] or "").strip()
        agents = load_agents(self.ctx.agents_path)
        self._json_ok(catalog(agents, session))

    def _handle_voice_humanness_get(self):
        from lib import voice_humanness
        self._json_ok(voice_humanness.get())

    def _handle_voice_humanness_post(self):
        """Set the Host default and per-agent humanness (0-10) of spoken
        replies. Read on the next spoken turn, so no relaunch is needed."""
        from lib import voice_humanness
        try:
            result = voice_humanness.update(self._read_json())
        except ValueError as error:
            return self._json_error(400, str(error))
        self._json_ok(result)

    def _handle_voice_preview(self):
        import hashlib
        from lib import config as app_config
        from lib.voice_catalog import catalog
        query = self._query()
        provider = (query.get("provider", [""])[0] or "").strip().lower()
        voice_id = (query.get("id", [""])[0] or "").strip()
        if provider == "google":
            # First, before the catalog, any cached preview, credentials or voice interpretation.
            from lib.google_tts import collision_code
            code = collision_code()
            if code:
                return self._json_error(409, code)
        body = catalog(load_agents(self.ctx.agents_path))
        provider_row = next((row for row in body["providers"]
                             if row["id"] == provider), None)
        voice = next((row for row in (provider_row or {}).get("voices", [])
                      if row["id"] == voice_id), None)
        if not provider_row or not provider_row["available"] or not voice:
            return self._send(404, b"voice unavailable")
        cfg = app_config.load()
        from lib.custom_tts_adapters import get as custom_adapter
        from lib.tts_providers import VALID_IDS
        custom_manifest = custom_adapter(provider, reserved_ids=VALID_IDS)
        adapter_revision = ""
        if custom_manifest is not None:
            adapter_revision = ":".join((
                str(custom_manifest.path.stat().st_mtime_ns),
                str(custom_manifest.executable.stat().st_mtime_ns),
            ))
        cache = (_runtime_paths().cache_dir
                 / "voice-previews")
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        prompt = f"Hello, I'm {voice['name']}. This is how I'll sound in Clarp."
        key = hashlib.sha256(
            f"v3\0{provider}\0{voice_id}\0{adapter_revision}\0{prompt}".encode()
        ).hexdigest()
        path = cache / f"{key}.mp3"
        if not path.is_file():
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                from lib.tts_service import synth_preview
                cache_primary = synth_preview(prompt, provider, voice_id, temporary, cfg=cfg, manifest=custom_manifest)
                if not cache_primary:
                    try:
                        return self._send_file(temporary, content_type="audio/mpeg")
                    finally:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError as cleanup_error:
                            log_exception("voicePreviewCleanupFail", cleanup_error, detail=voice_id)
                temporary.replace(path)
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                log_exception("voicePreviewFail", exc,
                              detail=f"{provider}:{voice_id}")
                return self._send(502, b"preview unavailable")
        return self._send_file(path)

    def _handle_voices(self):
        qs = self._query()
        include_sid = (qs.get("for", [""])[0] or "").strip()
        agents_map = load_agents(self.ctx.agents_path)
        out = voices_with_availability(agents_map, include_sid)
        info = agents_map.get(include_sid) or {}
        persona = info.get("name") or info.get("persona") or ""
        bio = persona_personality(persona)
        prefix = "Personality: "
        if bio.startswith(prefix):
            bio = bio[len(prefix):]
        self._json_ok({"voices": out, "bio": bio,
                       "persona": persona})
