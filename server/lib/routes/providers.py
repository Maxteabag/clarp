"""ProviderRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "providers"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import os
import pathlib
import threading
from lib import backends
from lib.log import log_exception


def _restart_own_service():
    from lib import service_manager
    try:
        service_manager.restart(check=False)
    except Exception as exc:  # noqa: BLE001 - a timer thread has no caller to tell
        log_exception("ttsProviderRestartFail", exc)


class ProviderRoutes:
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

    def _handle_server_update(self):
        from lib.server_update import request_update
        status, payload = request_update(self.ctx.default_session)
        self._json(status, payload)

    def _handle_server_update_status(self):
        from lib.server_update import get_update_status
        query = self._query()
        force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
        self._json_ok(get_update_status(force=force))

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
        # Only the installed Host restarts clarp.service: any other Host (a QA
        # or development one) would restart the live one. The choice is saved
        # either way and the answer says which it was (issue 322).
        refusal = None
        if restart_required:
            from lib import service_manager
            refusal = service_manager.host_refusal("restart")
        payload = status() | {"restart_scheduled": restart_required and refusal is None}
        if refusal is not None:
            payload["restart_refusal"] = {"reason": refusal.reason, "message": refusal.detail}
        self._json_ok(payload)
        if restart_required and refusal is None:
            timer = threading.Timer(0.5, _restart_own_service)
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
