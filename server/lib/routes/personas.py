"""PersonaRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "personas"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import pathlib
from urllib.parse import unquote
from lib import events
from lib.routes.helpers import _runtime_paths


class PersonaRoutes:
    def _handle_agent_portrait_content(self, portrait_id: str):
        from lib import agent_portraits
        if not portrait_id or "/" in portrait_id:
            return self._send(400, b"bad portrait id")
        row = agent_portraits.get_content(unquote(portrait_id))
        path = pathlib.Path(str((row or {}).get("storage_path") or ""))
        if not row or not path.is_file():
            return self._send(404, b"not found")
        return self._send_file(path)

    def _handle_agent_portrait_generation_start(self):
        from lib import portrait_generation
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None or not isinstance(data, dict):
            return self._json_error(400, "bad json")
        session = str(data.get("session") or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or _runtime_paths().media_dir
        try:
            result = portrait_generation.start(session, media_dir=portrait_dir)
        except portrait_generation.GenerationError as exc:
            return self._json_error(409, str(exc))
        self._json(202, result)

    def _handle_agent_portrait_generation_status(self):
        from lib import portrait_generation
        query = self._query()
        session = (query.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or _runtime_paths().media_dir
        result = portrait_generation.capability(
            session, media_dir=portrait_dir)
        self._json_ok(result)

    def _handle_agent_portraits_list(self):
        from lib import agent_portraits
        query = self._query()
        session = (query.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or _runtime_paths().media_dir
        try:
            result = agent_portraits.list_for_session(
                session, portrait_dir=portrait_dir)
        except agent_portraits.PortraitError as exc:
            return self._json_error(exc.status, str(exc))
        self._json_ok(result)

    def _handle_agent_portraits_update(self):
        from lib import agent_portraits
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        action = str(data.get("action") or "").strip()
        session = str(data.get("session") or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or _runtime_paths().media_dir
        try:
            if action == "add_media_asset":
                result = agent_portraits.add_media_asset(
                    session=session, asset_id=str(data.get("asset_id") or ""),
                    portrait_dir=portrait_dir)
                changed = False
            elif action == "select_primary":
                result = agent_portraits.select_primary(
                    session=session,
                    portrait_id=str(data.get("portrait_id") or ""),
                    portrait_dir=portrait_dir)
                changed = True
            else:
                raise agent_portraits.PortraitError("unsupported portrait action")
        except agent_portraits.PortraitError as exc:
            return self._json_error(exc.status, str(exc))
        if changed:
            events.broadcast(self.ctx.stream, events.agent_roster(
                "portrait-selected", session=session))
        self._json_ok(result)

    def _handle_create_persona(self):
        from lib import persona_avatar_generation, personas
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        raw_voice = str(data.get("voice_id") or "").strip()
        if raw_voice == "{}":
            raw_voice = ""
        try:
            row = personas.create(
                name=str(data.get("name") or ""),
                voice_id=raw_voice,
                avatar_symbol=str(data.get("avatar_symbol") or ""),
                personality=str(data.get("personality") or ""),
                avatar_base64=str(data.get("avatar_base64") or ""),
            )
        except (ValueError, OSError) as exc:
            return self._json_error(409, str(exc))
        if (
            not row.get("avatar_path")
            and not row.get("avatar_symbol")
            and data.get("generate_avatar", True) is not False
        ):
            persona_avatar_generation.start_best_effort(
                row["name"],
                tier=str(data.get("tier") or ""),
                backend=str(data.get("backend") or ""),
            )
        events.broadcast(self.ctx.stream, events.agent_roster("persona-created"))
        return self._json(201, {"persona": personas.public(row)})

    def _handle_delete_persona(self, name: str):
        from lib import personas
        try:
            removed = personas.delete(unquote(name))
        except ValueError as exc:
            return self._json_error(409, str(exc))
        if not removed:
            return self._json_error(404, "personality not removable")
        events.broadcast(self.ctx.stream, events.agent_roster("persona-deleted"))
        return self._json_ok({"ok": True})

    def _handle_persona_avatar(self, persona_id: str):
        from lib import personas
        row = next((p for p in personas.list_all() if p["persona_id"] == persona_id), None)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        return self._send_file(path) if row and path.is_file() else self._send(404, b"not found")

    def _handle_update_persona(self):
        from lib import personas
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            row = personas.update(
                original_name=str(data.get("original_name") or ""),
                name=str(data.get("name") or ""),
                voice_id=str(data.get("voice_id") or ""),
                avatar_symbol=str(data.get("avatar_symbol") or ""),
                personality=str(data.get("personality") or ""),
                avatar_base64=str(data.get("avatar_base64") or ""),
            )
        except (ValueError, OSError) as exc:
            return self._json_error(409, str(exc))
        events.broadcast(self.ctx.stream, events.agent_roster("persona-updated"))
        return self._json_ok({"persona": personas.public(row)})
