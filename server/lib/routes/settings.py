"""SettingsRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "settings"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import json
import os
import shutil
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
