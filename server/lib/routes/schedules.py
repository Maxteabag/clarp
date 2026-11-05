"""ScheduleRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "schedules"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

from urllib.parse import unquote


class ScheduleRoutes:
    def _handle_agent_schedules_delete(self):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        schedule_id = str(data.get("schedule_id") or "").strip()
        if not schedule_id:
            return self._json_error(400, "schedule_id required")
        ok = scheduler.delete_schedule(schedule_id)
        return self._json_ok({"ok": ok})

    def _handle_agent_schedules_get(self):
        from lib import scheduler
        query = self._query()
        session = (query.get("session", [""])[0] or "").strip()
        agent_id = (query.get("agent_id", [""])[0] or "").strip()
        schedules = scheduler.list_schedules(
            session=session if session else None,
            agent_id=agent_id if agent_id else None,
        )
        return self._json_ok({"schedules": schedules})

    def _handle_agent_schedules_post(self):
        from lib import scheduler
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        session = str(data.get("session") or "").strip()
        name = str(data.get("name") or "").strip()
        cron = str(data.get("cron_expression") or data.get("cron") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        enabled = bool(data.get("enabled", True))
        if not session or not name or not cron or not prompt:
            return self._json_error(400, "session, name, cron_expression, and prompt are required")
        try:
            item = scheduler.create_schedule(session, name, cron, prompt, enabled=enabled)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        return self._json(201, {"ok": True, "schedule": item})

    def _handle_agent_schedules_toggle(self):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        schedule_id = str(data.get("schedule_id") or "").strip()
        enabled = data.get("enabled")
        if not schedule_id or not isinstance(enabled, bool):
            return self._json_error(400, "schedule_id and enabled boolean required")
        item = scheduler.update_schedule(schedule_id, enabled=enabled)
        if not item:
            return self._json_error(404, "schedule not found")
        return self._json_ok({"ok": True, "schedule": item})

    def _handle_background_job_detail(self, job_id: str):
        from lib import background_jobs
        job_id = unquote(job_id)
        if not job_id or "/" in job_id:
            return self._send(404, b"not found")
        # A log may hold anything the worker printed; a limited device gets
        # the timeline and progress but not the file contents.
        limited = getattr(self, "_request_device_scope", "") == "limited"
        detail = background_jobs.detail(job_id, include_log=not limited)
        if detail is None:
            return self._json_error(404, "job not found")
        return self._json_ok(detail)

    def _handle_background_jobs(self):
        from lib import background_jobs
        return self._json_ok(background_jobs.snapshot())

    def _handle_schedule_delete(self, schedule_id: str):
        from lib import scheduler
        ok = scheduler.delete_schedule(schedule_id)
        if not ok:
            return self._json_error(404, "schedule not found")
        return self._json_ok({"ok": True})

    def _handle_schedule_patch(self, schedule_id: str):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        enabled = data.get("enabled")
        name = data.get("name")
        cron = data.get("cron_expression") or data.get("cron")
        prompt = data.get("prompt")
        try:
            item = scheduler.update_schedule(
                schedule_id,
                enabled=enabled if isinstance(enabled, bool) else None,
                name=str(name).strip() if name else None,
                cron_expression=str(cron).strip() if cron else None,
                prompt=str(prompt).strip() if prompt else None,
            )
        except ValueError as exc:
            return self._json_error(400, str(exc))
        if not item:
            return self._json_error(404, "schedule not found")
        return self._json_ok({"ok": True, "schedule": item})
