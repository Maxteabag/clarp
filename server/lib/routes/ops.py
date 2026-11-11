"""OpsRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "ops"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import time
import json
from dataclasses import asdict
import os
import pathlib
import secrets
from lib.controller_events import ControllerEventError, build_controller_event
from lib import db
from lib import eventlog
from lib import agents as agents_db
from lib.calendar_request import CalendarRequestError, build_calendar_request
from lib import events
from lib import identity
from lib.log import log, log_exception
from lib.protocol import ClientAction
from lib.routes.helpers import _runtime_paths


class OpsRoutes:
    def _handle_clog(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        from lib import diagnostics_settings
        if not diagnostics_settings.accepts_client_uploads():
            return self._json_ok({"ok": True, "captured": False})
        # Accept either a single event {event, detail, ...} or a batch
        # {events: [...]}. Batched form keeps the PWA from spamming one
        # request per state transition.
        items = data.get("events") if isinstance(data.get("events"), list) else [data]

        # Legacy line-based file (humans still read it) — single combined append.
        log_path = pathlib.Path(os.environ.get(
            "CLAUDE_PWA_CLIENT_LOG",
            str(_runtime_paths().hook_log),
        ))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                for it in items:
                    line_detail = it.get('detail', '')
                    if isinstance(line_detail, dict):
                        line_detail = json.dumps(line_detail, separators=(",", ":"))
                    f.write(f"{time.strftime('%H:%M:%S')} client   "
                            f"{it.get('event','?')} {line_detail}\n")
        except OSError as e:
            log_exception("clogWriteFail", e, detail=str(log_path))

        # Structured eventlog rows — one per event.
        request_trace_ids = {
            (it.get("trace_id") or "").strip()
            for it in items
            if isinstance(it, dict) and (it.get("trace_id") or "").strip()
        }
        if len(request_trace_ids) == 1:
            self._trace_id = next(iter(request_trace_ids))
        for it in items:
            try:
                ev_name = (it.get("event") or "log")[:64]
                detail = it.get("detail")
                if isinstance(detail, str) and detail:
                    detail = {"msg": detail}
                clip_id = it.get("clip_id")
                try:
                    clip_id = int(clip_id) if clip_id not in (None, "") else None
                except (TypeError, ValueError):
                    clip_id = None
                eventlog.emit(
                    "client", ev_name,
                    session=(it.get("session") or None),
                    agent_id=(it.get("agent_id") or None),
                    backend_session_id=(it.get("backend_session_id") or None),
                    persona=(it.get("persona") or None),
                    clip_id=clip_id,
                    clip_url=(it.get("clip_url") or None),
                    client_id=(it.get("client_id") or None),
                    duration_ms=it.get("duration_ms"),
                    trace_id=(it.get("trace_id") or None),
                    detail=detail if isinstance(detail, dict) else None,
                )
            except Exception as e:
                log_exception("clogEmitFail", e)
        self._json_ok({"ok": True})

    def _handle_crash(self):
        """Receive a MetricKit diagnostic payload from the iOS app.

        Payloads carry crash, hang, CPU-exception, disk-write, and slow-launch
        call stacks.
        """
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        try:
            from lib.ios_diagnostics import diagnostic_counts
            diagnostic_dir = pathlib.Path(db.DB_PATH).parent / "crashes"
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            out = diagnostic_dir / f"ios-diagnostic-{ts}-{secrets.token_hex(4)}.json"
            blob = json.dumps(data, indent=2)
            out.write_text(blob)
            counts = diagnostic_counts(data)
            eventlog.emit(
                "server",
                "iosDiagnosticReport",
                level="warn" if counts["hangs"] or counts["crashes"] else "info",
                detail={"file": out.name, "bytes": len(blob), **counts},
            )
            log(
                "iosDiagnosticReport",
                f"{out.name} crashes={counts['crashes']} hangs={counts['hangs']} "
                f"cpu={counts['cpu_exceptions']} disk={counts['disk_write_exceptions']} "
                f"launches={counts['slow_launches']} bytes={len(blob)}",
            )
        except Exception as e:  # noqa: BLE001
            log_exception("crashStoreFail", e)
            return self._json_error(500, "store failed")
        return self._json_ok({"ok": True})

    def _handle_focus(self):
        """Client tells us which agent view the user is looking at. Drives the
        herald system's decision of whether to suppress an off-focus agent."""
        data = self._read_json()
        if self._reject_janitor_control(data):
            return
        if data is None:
            return self._json_error(400, "bad json")
        sid = (data.get("session") or "").strip()
        if not sid:
            return self._json_error(400, "session required")
        agent = identity.lookup(sid)
        if agent:
            from lib import client_chat
            client_chat.record(self.principal().principal, sid)
        herald = getattr(self.ctx, "herald", None)
        if agent and herald is not None:
            try:
                with agents_db.focus_guard():
                    agents_db.set_focus(agent["agent_id"])
                    herald.set_focus(sid)
            except Exception as e:
                log_exception("focusDbWriteFail", e, detail=sid)
        elif agent:
            try:
                agents_db.set_focus(agent["agent_id"])
            except Exception as e:
                log_exception("focusDbWriteFail", e, detail=sid)
        elif herald is not None:
            try:
                herald.set_focus(sid)
            except Exception as e:
                log_exception("focusSetFail", e, detail=sid)
        return self._json_ok({"ok": True})

    def _handle_get_location(self):
        """Latest stored fix for ?session=<id>. {} when none shared yet —
        read by the request-location skill."""
        qs = self._query()
        session = (qs.get("session", [""])[0] or "").strip()
        from lib import location
        loc = location.get_location(session)
        return self._json_ok(loc or {})

    def _handle_register_device(self):
        """Register an iOS APNs device token for push notifications. The device
        POSTs its token (optionally the session it's focused on); when any agent
        finishes its turn the server pushes a "your turn" alert to it. Body:
        {"token": "<hex>", "session": "...", "environment": "production",
         "base_url": "http://private-overlay-address:7682",
         "push_grant": "pg1.…"}. The optional push grant is issued by the
        phone through Audio Central and lets this Host push to it without an
        APNs key or Audio Central account of its own.

        An Android phone registers its FCM registration token the same way,
        with {"platform": "android", "provider": "fcm"} (provider defaults to
        "fcm" for android, "apns" otherwise); see docs/push-android.md."""
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        token = (data.get("token") or "").strip()
        if not token:
            return self._json_error(400, "token required")
        from lib import apns
        from lib.agent_ring_live import device_ok
        from lib.http_utils import principal_of
        # Provenance: only the AUTHENTICATED paired, full-scope, non-revoked device owns what it registers
        # (never a body field or the client-supplied session); every other registrant leaves it unbound.
        who = principal_of(self)
        owner = who.principal if (who.full_scope and who.principal.startswith("device_")
                                  and device_ok(who.principal)) else ""
        try:
            apns.register_token(
                token,
                session=(data.get("session") or "").strip() or None,
                environment=(data.get("environment") or "").strip() or None,
                platform=(data.get("platform") or "ios").strip() or "ios",
                base_url=(data.get("base_url") or "").strip(),
                push_grant=str(data.get("push_grant") or ""),
                owner=owner,
                provider=str(data.get("provider") or ""),
            )
        except apns.InvalidDeviceToken:
            return self._json(400, {"ok": False, "error": "device_token_invalid"})
        except apns.TokenOwnedElsewhere:
            log("deviceTokenOwnerConflict", f"registrant={owner or who.principal or '-'} token={token[:12]}…")
            return self._json(409, {"ok": False, "error": "device_token_owned_elsewhere"})
        except Exception as e:  # noqa: BLE001
            log_exception("deviceRegisterFail", e)
            return self._json_error(500, "register failed")
        return self._json_ok({"ok": True})

    def _handle_remote_action(self):
        """Receive a fire-and-forget remote action (typically from an iOS
        Shortcut driving the Action Button). Broadcasts it over SSE so the
        already-running PWA can act without a page reload."""
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        action = (data.get("action") or "").strip().lower()
        if action not in ClientAction.valid():
            return self._json_error(400, "unknown action")
        if action == ClientAction.CONTROLLER_EVENT:
            try:
                event = events.remote_action_from(build_controller_event(data))
            except ControllerEventError as exc:
                return self._json_error(400, str(exc))
        else:
            event = events.remote_action(action, int(time.time() * 1000))
        events.broadcast_ephemeral(self.ctx.stream, event)
        log("remoteAction", action)
        body = {"ok": True}
        if action == ClientAction.CONTROLLER_EVENT:
            body["controller_event_id"] = event["controller_event_id"]
        return self._json_ok(body)

    def _handle_remote_action_get(self):
        """GET variant for iOS Shortcuts that use 'Open URL' rather than
        'Get Contents of URL' with an explicit POST. Accepts ?action=...
        and broadcasts the same SSE event."""
        qs = self._query()
        action = (qs.get("action", [""])[0] or "").strip().lower()
        if (action not in ClientAction.valid()
                or action == ClientAction.CONTROLLER_EVENT):
            return self._json_error(400, "unknown action")
        events.broadcast_ephemeral(
            self.ctx.stream, events.remote_action(action, int(time.time() * 1000)))
        log("remoteAction", f"GET {action}")
        # Tiny no-cache HTML so Safari shows something blank instead of raw JSON.
        body = b"<!doctype html><meta charset=utf-8><title>ok</title>"
        self._send(200, body, "text/html")

    def _handle_request_calendar(self):
        """An agent asks the app to add an Apple Calendar event. Body:
        {"session":"...", "title":"...", "start":"ISO8601", "end":"ISO8601",
         "time_zone":"Europe/Oslo", "location":"...", "notes":"...",
         "url":"...", "all_day":false, "calendar":"Work"}."""
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        request_id = f"cal-{secrets.token_hex(8)}"
        try:
            request = build_calendar_request(data, request_id=request_id)
        except CalendarRequestError as exc:
            return self._json_error(400, str(exc))
        events.broadcast(self.ctx.stream, events.calendar_request(**asdict(request)))
        return self._json_ok({"ok": True, "request_id": request_id})

    def _handle_request_location(self):
        """An agent asks the app for the user's location: broadcast a request the
        focused client surfaces as a one-tap Share-location prompt. Body:
        {"session": "..."}."""
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        events.broadcast(self.ctx.stream, events.location_request(session=session))
        return self._json_ok({"ok": True})

    def _handle_set_location(self):
        """The app POSTs the user's current GPS fix for a session (one-shot
        CoreLocation, When-In-Use). Body: {"session","lat","lng","accuracy"?}."""
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._json_error(400, "session required")
        try:
            lat = float(data["lat"])
            lng = float(data["lng"])
        except (KeyError, TypeError, ValueError):
            return self._json_error(400, "lat/lng required")
        acc = data.get("accuracy")
        from lib import location
        try:
            row = location.set_location(
                session, lat, lng, float(acc) if acc is not None else None)
        except ValueError as e:
            return self._json_error(400, str(e))
        except Exception as e:  # noqa: BLE001
            log_exception("setLocationFail", e)
            return self._json_error(500, "store failed")
        return self._json_ok({"ok": True, **row})
