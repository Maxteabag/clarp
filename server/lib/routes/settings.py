"""SettingsRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "settings"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import threading
from lib import health
from lib import agents as agents_db
from lib import backends
from lib import identity
from lib.log import log_exception
from lib.herald import DEFAULT_SPEAK_IF_SHORT_CHARS, MAX_SPEAK_IF_SHORT_CHARS, get_settings as get_herald_settings, update_settings as update_herald_settings
from lib.personalities import get_settings as get_personality_settings, update_settings as update_personality_settings


class SettingsRoutes:
    def _connection_server_info(self):
        from lib.server_identity import get_server_info
        info = get_server_info()
        transport = self.server.local_transport
        info["local_connection"] = transport.description() if transport else {"enabled": False}
        if self.server.local_transport_error:
            info["local_connection"]["error"] = self.server.local_transport_error
        return info

    def _handle_application_activity(self):
        if not getattr(self, "_request_auth_validated", False):
            return self._json_error(401, "authentication required")
        if getattr(self, "_request_device_scope", "") != "full":
            return self._json_error(403, "full device access required")
        from lib import application_activity
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "object required")
        try:
            result = application_activity.report(self._request_principal,
                data.get("instance_id"), data.get("sequence"), data.get("foreground"),
                data.get("input_age_ms"), data.get("sent_at_ms"))
        except ValueError as error:
            return self._json_error(400, str(error))
        self._json_ok(result)

    def _handle_automation_settings_get(self):
        from lib.automation_settings import get
        self._json_ok(get())

    def _handle_automation_settings_post(self):
        from lib.automation_settings import update
        data = self._read_json()
        if data is None or not isinstance(data.get("special_treatment"), bool):
            return self._json_error(400, "special_treatment required")
        self._json_ok(update(data["special_treatment"]))

    def _handle_avatar_settings_get(self):
        from lib.avatar_settings import get
        self._json_ok(get())

    def _handle_avatar_settings_post(self):
        from lib.avatar_settings import update
        data = self._read_json()
        if data is None or not isinstance(data.get("model_avatars"), bool):
            return self._json_error(400, "model_avatars required")
        self._json_ok(update(data["model_avatars"]))

    def _handle_backend_auth(self):
        from lib.backend_auth import status, task
        every = (self._query().get("harnesses") or [""])[0] == "all"
        rows = status(every_harness=every)
        listed = {row["id"] for row in rows}
        body = {"backends": rows, "tasks": {
            adapter.id: task(adapter.id) for adapter in backends.auth_adapters()
            if adapter.id in listed}}
        self._json_ok(body)

    def _handle_backend_auth_login(self):
        from lib.backend_auth import start_login
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        backend = str(data.get("backend") or "").strip().lower()
        try:
            result = start_login(backend)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        self._json(202, result)

    def _handle_backend_auth_login_code(self):
        from lib.backend_auth import submit_login_code
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        backend = str(data.get("backend") or "").strip().lower()
        try:
            result = submit_login_code(backend, str(data.get("code") or ""))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except RuntimeError as exc:
            return self._json_error(409, str(exc))
        self._json_ok(result)

    def _handle_backend_auth_logout(self):
        from lib.backend_auth import logout
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        backend = str(data.get("backend") or "").strip().lower()
        try:
            result = logout(backend)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except RuntimeError as exc:
            return self._json_error(409, str(exc))
        self._json_ok(result)

    def _handle_desktop_presence(self):
        if not getattr(self, "_request_auth_validated", False):
            return self._json_error(401, "authenticated desktop required")
        if getattr(self, "_request_device_scope", "") != "full":
            return self._json_error(403, "full device access required")
        from lib import desktop_presence
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "object required")
        try:
            result = desktop_presence.update(principal=self._request_principal,
                instance_id=data.get("instance_id"), sequence=data.get("sequence"), active=data.get("active"),
                sent_at_ms=data.get("sent_at_ms"))
        except ValueError as error:
            return self._json_error(400, str(error))
        self._json_ok(result)

    def _handle_diagnostics_health(self):
        from lib import tts_queue
        stt_ready = bool(self.ctx.stt.ready.is_set())
        ffmpeg_ready = self.server.boot_config.delivery != "hls" or shutil.which("ffmpeg") is not None
        body = json.dumps({
            "ready": stt_ready and ffmpeg_ready,
            "checks": {
                "stt_ready": stt_ready,
                "ffmpeg_ready": ffmpeg_ready,
            },
            "subsystems": health.snapshot(),
            "tts_queue": {
                "pending": tts_queue.pending_count(),
                "in_flight": tts_queue.in_flight_count(),
            },
            "deployed_version": self.ctx.deployed_version(),
        }).encode()
        self._send(200, body, "application/json")

    def _handle_diagnostics_settings_get(self):
        """Return opt-in developer diagnostics owned by this Computer."""
        from lib import diagnostics_settings
        return self._json_ok({
            "ok": True, "settings": diagnostics_settings.get().public(),
        })

    def _handle_diagnostics_settings_post(self):
        from lib import diagnostics_settings
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            settings = diagnostics_settings.update(data)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        return self._json_ok({
            "ok": True, "settings": settings.public(),
        })

    def _handle_dirs(self):
        from lib.launch_paths import recover_user_path
        qs = self._query()
        raw = (qs.get("path", [""])[0] or "").strip()
        raw = str(recover_user_path(raw))
        if raw.endswith("/"):
            base, frag = raw[:-1] or "/", ""
        else:
            base = os.path.dirname(raw) or "/"
            frag = os.path.basename(raw).lower()
        matches: list[str] = []
        try:
            for entry in sorted(os.listdir(base)):
                if entry.startswith("."):
                    continue
                full = os.path.join(base, entry)
                if not os.path.isdir(full):
                    continue
                if frag and not entry.lower().startswith(frag):
                    continue
                matches.append(full)
                if len(matches) >= 20:
                    break
        except OSError as e:
            log_exception("dirsListFail", e, detail=base)
        body = json.dumps({"base": base, "matches": matches}).encode()
        self._send(200, body, "application/json")

    def _handle_favorite_paths(self):
        qs = self._query()
        try:
            limit = int(qs.get("limit", ["5"])[0])
        except ValueError:
            limit = 5
        self._json_ok({"paths": agents_db.favorite_paths(limit)})

    def _handle_harness_provider_login(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        harness, provider = data.get("harness"), data.get("provider")
        if "key" in data:
            result = self._harness_provider_call(
                lambda hp: hp.save_api_key(harness, provider, data.get("key")))
            if result is not None:
                self._json_ok({"ok": True, "provider": result})
            return
        result = self._harness_provider_call(
            lambda hp: hp.start_oauth(harness, provider, data.get("method"),
                                      data.get("inputs")))
        if result is not None:
            self._json(202, result)

    def _handle_harness_provider_login_code(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        result = self._harness_provider_call(
            lambda hp: hp.submit_code(data.get("harness"), data.get("provider"),
                                      data.get("code")))
        if result is not None:
            self._json_ok(result)

    def _handle_harness_provider_logout(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        result = self._harness_provider_call(
            lambda hp: hp.logout(data.get("harness"), data.get("provider")))
        if result is not None:
            self._json_ok({"ok": True, "provider": result})

    def _handle_harness_provider_models(self):
        qs = self._query()
        harness = (qs.get("harness") or [""])[0]
        provider = (qs.get("provider") or [""])[0]
        result = self._harness_provider_call(lambda hp: hp.models(harness, provider))
        if result is not None:
            self._json_ok(result)

    def _handle_harness_providers(self):
        harness = (self._query().get("harness") or [""])[0]
        result = self._harness_provider_call(lambda hp: hp.providers(harness))
        if result is not None:
            self._json_ok(result)

    def _handle_herald_settings_get(self):
        body = json.dumps({
            "settings": get_herald_settings().as_dict(),
            "defaults": {
                "speak_if_short_chars": DEFAULT_SPEAK_IF_SHORT_CHARS,
                "short_reply_bypass_enabled": True,
            },
            "limits": {
                "speak_if_short_chars": [0, MAX_SPEAK_IF_SHORT_CHARS],
            },
        }).encode()
        self._send(200, body, "application/json")

    def _handle_herald_settings_post(self):
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            settings = update_herald_settings(data)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        return self._json_ok({"ok": True, "settings": settings.as_dict()})

    def _handle_judgments_decisions_get(self):
        from lib import judgments
        query = self._query()
        site = (query.get("site") or [""])[0]
        try:
            limit = int((query.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        if site and site not in judgments.SITES:
            return self._json_error(400, "unknown site")
        body = json.dumps({"decisions": judgments.recent(limit=limit, site=site)}).encode()
        self._send(200, body, "application/json")

    def _handle_judgments_settings_get(self):
        from lib import judgments
        body = json.dumps({
            "settings": judgments.status(),
            "sites": list(judgments.SITES),
            "defaults": {"enabled": False, "timeout_ms": judgments.DEFAULT_TIMEOUT_MS},
            "limits": {"timeout_ms": [100, judgments.MAX_TIMEOUT_MS]},
        }).encode()
        self._send(200, body, "application/json")

    def _handle_judgments_settings_post(self):
        from lib import judgments
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            settings = judgments.update_settings(data)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        return self._json_ok({"ok": True, "settings": settings})

    def _handle_launch_directories(self):
        from lib.launch_directories import lookup
        query = self._query().get("q", [""])[0]
        return self._json_ok(lookup(query))

    def _handle_managed_skills(self):
        from lib.managed_skills import status
        try:
            skills = status()
            if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
                from lib.personal_skills import status as personal_status
                skills += personal_status()
            body = {"skills": skills}
        except (RuntimeError, OSError) as exc:
            log_exception("managedSkillsStatusFail", exc)
            return self._json_error(503, str(exc))
        self._json_ok(body)

    def _handle_managed_skills_update(self):
        from lib.managed_skills import set_enabled
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        skill_id = str(data.get("skill_id") or "").strip()
        enabled = data.get("enabled")
        if not skill_id or not isinstance(enabled, bool):
            return self._json_error(400, "skill_id and boolean enabled are required")
        try:
            skill = set_enabled(skill_id, enabled)
        except ValueError as exc:
            return self._json_error(409, str(exc))
        except OSError as exc:
            log_exception("managedSkillsUpdateFail", exc, detail=skill_id)
            return self._json_error(500, str(exc))
        self._json_ok({"ok": True, "skill": skill})

    def _handle_paired_device_revoke(self):
        from lib.device_pairing import revoke
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            return self._json_error(400, "device_id required")
        if not revoke(device_id):
            return self._json_error(404, "device not found")
        self._json_ok({"ok": True})
        self.server.disconnect_device(device_id)

    def _handle_paired_devices(self):
        from lib.device_pairing import list_devices
        self._json_ok({"devices": list_devices()})

    def _handle_pairing_exchange(self):
        from lib.device_pairing import PairingError, exchange
        from lib.server_identity import get_server_info
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            device = exchange(
                str(data.get("code") or ""),
                device_name=str(data.get("device_name") or ""),
            )
        except PairingError as exc:
            retry = self._auth_failure_retry()
            if retry:
                return self._send(429, b'{"error":"too many pairing failures"}',
                                  "application/json", extra_headers={"Retry-After": str(retry)})
            return self._json_error(409, str(exc))
        payload = {"device": device, "server": self._connection_server_info()}
        self._json(201, payload)

    def _handle_past_sessions(self):
        from lib.launch_paths import recover_user_path
        qs = self._query()
        raw = (qs.get("cwd", [""])[0] or "").strip()
        raw = str(recover_user_path(raw))
        backend = backends.normalize(qs.get("backend", [""])[0])
        all_projects = (qs.get("scope", [""])[0] or "").strip().lower() == "all"
        limit = 100 if all_projects else 20
        sessions = backends.list_sessions(
            backend, raw, limit=limit, all_projects=all_projects)
        body = json.dumps({
            "cwd": raw, "scope": "all" if all_projects else "workspace",
            "sessions": sessions,
        }).encode()
        self._send(200, body, "application/json")

    def _handle_personalities_settings_get(self):
        body = json.dumps({
            "settings": get_personality_settings().as_dict(),
        }).encode()
        self._send(200, body, "application/json")

    def _handle_personalities_settings_post(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        settings = update_personality_settings(data)
        return self._json_ok({"ok": True, "settings": settings.as_dict()})

    def _handle_relay_status(self):
        relay = self.server.relay
        status = relay.status() if relay else {"enabled": False, "state": "disabled"}
        self._json_ok(status)

    def _handle_server_update(self):
        from lib.server_update import request_update
        status, payload = request_update(self.ctx.default_session)
        self._json(status, payload)

    def _handle_server_update_status(self):
        from lib.server_update import get_update_status
        query = self._query()
        force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
        self._json_ok(get_update_status(force=force))

    def _handle_tool_explanation_settings_get(self):
        from lib import tool_explanation_settings
        self._json_ok(tool_explanation_settings.get())

    def _handle_tool_explanation_settings_post(self):
        from lib import tool_explanation_settings
        data = self._read_json()
        try:
            result = tool_explanation_settings.update(data)
        except ValueError as error:
            return self._json_error(400, str(error))
        self._json_ok(result)

    def _handle_tool_explanations(self):
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "object required")
        session = data.get("session")
        if not isinstance(session, str) or not (agent := identity.lookup(session)):
            return self._json_error(404, "agent not found")
        from lib import tool_explanation_settings
        if not tool_explanation_settings.enabled():
            # Turned off on this Host: the explainer is not asked at all.
            items = data.get("items") if isinstance(data.get("items"), list) else []
            return self._json_ok({
                "detail_level": data.get("detail_level"), "model": "",
                "items": [{"id": item.get("id"), "status": "disabled"}
                          for item in items[:8] if isinstance(item, dict)]})
        try:
            result = self.ctx.tool_explanations.request(
                data.get("detail_level"), data.get("items"), cwd=agent.get("cwd"),
                release=data.get("release"), target_agent_id=agent["agent_id"],
                include_provenance=data.get("include_provenance") is True)
        except ValueError as error:
            return self._json_error(400, str(error))
        self._json_ok(result)

    def _handle_tool_explanations_stats(self):
        from lib import tool_explanation_learning, tool_explanation_templates
        from lib.tool_explanations import PROMPT_VERSION
        query = self._query()
        try:
            result = tool_explanation_learning.stats(
                (query.get("window") or ["24h"])[0], (query.get("bucket") or ["hour"])[0],
                prompt_version=PROMPT_VERSION, templates_version=tool_explanation_templates.VERSION)
        except ValueError as error:
            return self._json_error(400, str(error))
        self._json_ok(result)

    def _handle_tts_providers_get(self):
        from lib.tts_providers import status
        self._json_ok(status())

    def _handle_tts_providers_post(self):
        from lib import config as config_module
        from lib.config_writer import set_toml_value
        from lib.tts_providers import status, valid_ids
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        provider = str(data.get("provider") or "").strip().lower()
        fallback = str(data.get("fallback") or "none").strip().lower()
        voice = str(data.get("voice") or "").strip()
        if "google" in (provider, fallback):
            from lib.google_tts import collision_code
            code = collision_code()
            if code:
                return self._json_error(409, code)
        allowed = valid_ids()
        rows = {row["id"]: row for row in status()["providers"]}
        if (provider not in allowed or fallback not in allowed
                or (fallback != "none" and not rows.get(
                    fallback, {}).get("can_fallback", False))):
            return self._json_error(400, "unsupported TTS provider or fallback")
        current = status()
        rows = {row["id"]: row for row in current["providers"]}
        for selected in {provider, fallback} - {"none"}:
            if rows[selected]["kind"] == "local" and not rows[selected]["installed"]:
                return self._json_error(409, f"local TTS provider is not installed: {selected}")
            if rows[selected].get("custom") and not rows[selected]["available"]:
                return self._json_error(409, f"custom TTS adapter is unavailable: {selected}")
        config_path = pathlib.Path(os.environ.get(
            "CLAUDE_PWA_CONFIG", str(config_module.CONFIG_PATH)))
        set_toml_value(config_path, "tts", "provider", provider)
        set_toml_value(config_path, "tts", "fallback", fallback)
        if voice:
            set_toml_value(config_path, "local_tts", "voice", voice)
        restart_required = provider != "cartesia"
        if restart_required:
            set_toml_value(config_path, "audio", "delivery", "chunked-file")
        config_module.reset_cache()
        payload = status() | {"restart_scheduled": restart_required}
        self._json_ok(payload)
        if restart_required:
            from lib import service_manager
            timer = threading.Timer(
                0.5, lambda: service_manager.restart(check=False))
            timer.daemon = True
            timer.start()

    # Model providers inside a harness (OpenCode). Keys travel only in POST
    # bodies, are written to OpenCode's own store and are never returned.
    def _harness_provider_call(self, action):
        from lib import harness_providers
        try:
            return action(harness_providers)
        except harness_providers.ProviderError as exc:
            self._json_error(400, str(exc))
        except harness_providers.ProviderConflict as exc:
            self._json_error(409, str(exc))
        except OSError as exc:
            log_exception("harnessProviderStoreFail", exc)
            self._json_error(500, "OpenCode's credential store could not be written")
        return None
