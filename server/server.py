#!/usr/bin/env python3
"""Companion HTTP server for Claude Code PWA.

Endpoints (see Handler.do_GET / do_POST for the dispatch tables):
  GET  /              → index.html (PWA shell)
  GET  /static/<f>    → static assets
  GET  /manifest.json, /sw.js → root-level static files
  POST /send          → {"text": "..."}, route to an app session
  GET  /events        → SSE stream announcing new audio clips
  GET  /audio/<id>    → MP3 audio file
"""
from __future__ import annotations

import gzip
import json
import mimetypes
import os
import pathlib
import queue
import secrets
import shutil
import sqlite3
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib.agent_store import (  # noqa: E402
    load_agents,
    save_agents,
)
from lib.agent_lifecycle import AgentLifecycleError, AgentLifecycleService  # noqa: E402
from lib.config import persona_personality  # noqa: E402
from lib.context import ServerContext  # noqa: E402
from lib import db  # noqa: E402
from lib import eventlog  # noqa: E402
from lib import health  # noqa: E402
from lib import agents as agents_db  # noqa: E402
from lib import backends  # noqa: E402
from lib import backend_usage  # noqa: E402
from lib import origins  # noqa: E402
from lib import media_store  # noqa: E402
from lib.calendar_request import CalendarRequestError, build_calendar_request  # noqa: E402
from lib import trace as _trace  # noqa: E402
from lib.http_utils import redact_query_secrets  # noqa: E402
from lib.log import log, log_exception  # noqa: E402
from lib.paths import RuntimePaths, _safe_session  # noqa: E402
from lib.protocol import AgentState, ClientAction, SSEType  # noqa: E402
from lib.orchestrator import (  # noqa: E402
    FINAL_FALLBACK,
    OrchestratorService,
    get_settings as get_orchestrator_settings,
    recent_ignored_decisions as recent_orchestrator_ignored_decisions,
    recent_decisions as recent_orchestrator_decisions,
    update_settings as update_orchestrator_settings,
)
from lib.herald import (  # noqa: E402
    DEFAULT_SPEAK_IF_SHORT_CHARS,
    MAX_SPEAK_IF_SHORT_CHARS,
    get_settings as get_herald_settings,
    update_settings as update_herald_settings,
)
from lib.personalities import (  # noqa: E402
    get_settings as get_personality_settings,
    update_settings as update_personality_settings,
)
from lib.conversation import load_conversation, session_cwd  # noqa: E402
from lib.resume import resume_missing_sessions  # noqa: E402
from lib.snapshot import build_agent_snapshot  # noqa: E402
from lib.stt import (STTBusyError, STTModelLoadingError,
                     STTUnknownModelError)  # noqa: E402
from lib.timing import SERVER_TIMING  # noqa: E402
from lib.transcript_log import find_latest_jsonl, parse_turns  # noqa: E402
from lib.turn_dispatch import DispatchError, TurnDispatchService  # noqa: E402
from lib.voices import voices_with_availability  # noqa: E402


from lib.config import load as load_config  # noqa: E402

_CFG = load_config()
PORT = int(os.environ.get("CLAUDE_PWA_PORT", str(_CFG.port)))
BIND_ADDR = os.environ.get("CLAUDE_PWA_BIND", _CFG.bind_addr)


def _truthy_header(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class ContextHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with an attached ServerContext.

    Handler reads `self.server.ctx` to access all injected dependencies
    (tts, audio stream, stt, filesystem paths). This is the
    .NET-AddScoped-style DI seam: production builds a real ctx; tests build
    one with FakeTTSEngine + StubSTT and exercise the same
    handler class against it.
    """

    # Listen backlog. The stdlib default of 5 is a BSD-era value (Linux allows
    # 4096) and is a defect on its own; keep it raised. It stopped being a
    # band-aid once the handler spoke HTTP/1.1: reconnect bursts used to pay
    # a fresh TCP connection per request (2600+ kernel ListenOverflows) and
    # now reuse pooled connections through the tailscale-serve proxy.
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """A client that hangs up mid-response is routine (the phone cancels a
        superseded transcript poll, the proxy drops an idle keep-alive
        connection). The stdlib default printed a 15-line traceback for each;
        log one line instead and keep the traceback for anything else."""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            try:
                log("clientGone", f"{client_address[0]} {type(exc).__name__}")
            except Exception:  # noqa: BLE001
                pass
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, handler_cls, ctx: ServerContext):
        self.ctx = ctx
        self._close_callbacks = []
        super().__init__(addr, handler_cls)

    def on_close(self, callback) -> None:
        self._close_callbacks.append(callback)

    def server_close(self):
        for callback in self._close_callbacks:
            try:
                callback()
            except Exception as e:  # noqa: BLE001
                log_exception("serverCloseCallbackFail", e)
        self._close_callbacks.clear()
        super().server_close()


def resume_persisted_agents(ctx: ServerContext) -> None:
    """Restore persisted backend-session bindings after a server restart."""
    agents = load_agents(ctx.agents_path)
    if not agents:
        return
    results = resume_missing_sessions(
        agents, pathlib.Path.home(),
        backend_sessions_by_session=agents_db.backend_sessions_by_session())
    for r in results:
        if r.get("ok"):
            agent = agents_db.get_by_session(r["sid"])
            if (agent and r.get("action") == "fresh"
                    and agents_db.live_backend_session(agent["agent_id"])):
                # Validation rejected a stale on-disk session. Open a blank
                # runtime so the next dispatch cannot accidentally resume it.
                agents_db.start_runtime(agent["agent_id"], r["sid"])
            if agent and agents_db.current_runtime_id(agent["agent_id"]) is None:
                agents_db.start_runtime(agent["agent_id"], r["sid"])
            if agent and r.get("backend_session_id"):
                try:
                    agents_db.bind_backend_session(agent["agent_id"], r["backend_session_id"])
                except agents_db.SessionAlreadyBound as e:
                    log("startupSessionConflict",
                        f"{r['sid']} wants {e.backend_session_id} "
                        f"owned by {e.owner_agent_id}; leaving fresh")
        if r["action"] != "already-running":
            log("startupAgentRestore", f"{r['sid']} {r['action']} ok={r['ok']}")


def broadcast_boot_version(ctx: ServerContext) -> None:
    """Push the current static-file version through SSE so already-open PWAs
    can detect a redeploy and reload immediately."""
    ctx.stream.broadcast({"type": SSEType.SERVER_VERSION,
                          "version": ctx.sw_version()})


_CARTESIA_PREVIEW_LOCKS: dict[str, threading.Lock] = {}
_CARTESIA_PREVIEW_LOCKS_GUARD = threading.Lock()


def _cartesia_preview_lock(voice_id: str) -> threading.Lock:
    with _CARTESIA_PREVIEW_LOCKS_GUARD:
        return _CARTESIA_PREVIEW_LOCKS.setdefault(voice_id, threading.Lock())


def _raw_query_value(path: str, key: str) -> str:
    """Decode one query value without form-style `+` to space rewriting.

    Filesystem paths legitimately contain plus signs. Native URLQueryItem may
    leave them literal, whereas parse_qs assumes HTML form encoding.
    """
    from urllib.parse import unquote, urlparse
    for field in urlparse(path).query.split("&"):
        raw_key, separator, raw_value = field.partition("=")
        if separator and unquote(raw_key) == key:
            return unquote(raw_value)
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudePWA/1.0"
    # HTTP/1.1 so the tailscale-serve reverse proxy (and any direct client)
    # can keep connections alive: at the 1.0 default every request paid a
    # fresh TCP connection + handler thread + sqlite open, and the iOS app's
    # ~20-request foreground bursts overflowed the listen queue (the incident
    # behind request_queue_size=128 above). Requires every response to carry
    # Content-Length (all _send paths do) or to close explicitly (streams).
    protocol_version = "HTTP/1.1"
    # Reap dead/idle connections instead of pinning a thread forever. Applies
    # per blocking socket op (recv/send). Long-lived WebSocket upgrades opt
    # out after their handshake (settimeout(None) at each upgrade site); the
    # SSE stream writes a ping every 10s so a healthy client never trips this.
    timeout = 120
    # Nagle + delayed-ACK stalls small-write streams (SSE events, streaming
    # audio) by ~40ms per exchange; every event/chunk is already assembled
    # into few writes, so tinygrams are not a concern.
    disable_nagle_algorithm = True

    def parse_request(self):
        # Per-request state: whether the request body has been read. Under
        # keep-alive an unread body would desync the next request on the
        # connection, so _send() drains or closes before responding early
        # (e.g. 401 before the handler ever touched rfile).
        self._body_consumed = False
        return super().parse_request()

    def _drain_request_body(self):
        """Consume an unread request body before writing a response.

        Bounded: bodies over 1 MiB aren't worth reading just to save the
        connection — close it instead. No-op when the body was already read
        or there is none.
        """
        if getattr(self, "_body_consumed", True):
            return
        self._body_consumed = True
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            remaining = 0
        if remaining <= 0:
            return
        if remaining > 1024 * 1024:
            self.close_connection = True
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            self.close_connection = True

    def finish(self):
        # Each request runs on its own thread with its own sqlite connection
        # (see lib.db.conn). Close it here so short-lived request threads
        # release their FDs instead of leaking them.
        try:
            super().finish()
        finally:
            db.close_local()
            try:
                from lib import telemetry
                telemetry.close_local()
            except Exception:
                pass

    _GET_ROUTES = {
        "/": "_send_index",
        "/sw.js": "_send_sw",
        "/events": "_sse",
        "/log": "_handle_log",
        "/message-tool-details": "_handle_message_tool_details",
        "/status": "_handle_status",
        "/diagnostics/health": "_handle_diagnostics_health",
        "/backend-usage": "_handle_backend_usage",
        "/backend-auth": "_handle_backend_auth",
        "/managed-skills": "_handle_managed_skills",
        "/transcription-capabilities": "_handle_transcription_capabilities",
        "/transcription-guidance": "_handle_transcription_guidance_get",
        "/transcription-providers": "_handle_transcription_providers_get",
        "/vocab/packs": "_handle_vocab_packs_get",
        "/vocab/terms": "_handle_vocab_terms_get",
        "/vocab/profiles": "_handle_vocab_profiles_get",
        "/vocab/profile": "_handle_vocab_profile_get",
        "/vocab/assignments": "_handle_vocab_assignments_get",
        "/vocab/budgets": "_handle_vocab_budgets_get",
        "/vocab/preview": "_handle_vocab_preview_get",
        "/vocab/runs": "_handle_vocab_runs_get",
        "/vocab/run": "_handle_vocab_run_get",
        "/transcription-audio": "_handle_transcription_audio_get",
        "/clips/recoverable": "_handle_recoverable_clips",
        "/server-info": "_handle_server_info",
        "/paired-devices": "_handle_paired_devices",
        "/server-update": "_handle_server_update_status",
        "/turn-queue": "_handle_turn_queue",
        "/agents/snapshot": "_handle_snapshot",
        "/identity/prompt-history": "_handle_prompt_history",
        "/background-jobs": "_handle_background_jobs",
        "/task-plan": "_handle_task_plan",
        "/artifacts": "_handle_artifacts_list",
        "/attention": "_handle_attention",
        "/voices": "_handle_voices",
        "/voice-catalog": "_handle_voice_catalog",
        "/voice-preview": "_handle_voice_preview",
        "/cartesia-voices": "_handle_cartesia_voices",
        "/cartesia-voice-preview": "_handle_cartesia_voice_preview",
        "/tts/providers": "_handle_tts_providers_get",
        "/agent-files": "_handle_agent_files",
        "/agent-file": "_handle_agent_file",
        "/orchestrator/settings": "_handle_orchestrator_settings_get",
        "/herald/settings": "_handle_herald_settings_get",
        "/personalities/settings": "_handle_personalities_settings_get",
        "/automation-settings": "_handle_automation_settings_get",
        "/agent-model-options": "_handle_agent_model_options",
        "/favorite-paths": "_handle_favorite_paths",
        "/orchestrator/decisions": "_handle_orchestrator_decisions",
        "/dirs": "_handle_dirs",
        "/past-sessions": "_handle_past_sessions",
        "/remote-action": "_handle_remote_action_get",
        "/location": "_handle_get_location",
        "/media": "_handle_media_list",
        "/agent-portraits": "_handle_agent_portraits_list",
        "/agent-portrait-generation": "_handle_agent_portrait_generation_status",
        "/teams": "_handle_teams_list",
        "/dreaming/runs": "_handle_dreaming_runs",
        "/dreaming/settings": "_handle_dreaming_settings_get",
        "/heartbeat/settings": "_handle_heartbeat_settings_get",
        "/diagnostics/settings": "_handle_diagnostics_settings_get",
        "/agent-heartbeat/status": "_handle_agent_heartbeat_status",
        "/oracle/status": "_handle_oracle_status",
        "/oracle/delegations": "_handle_oracle_delegations_get",
        "/oracle/realtime": "_handle_oracle_realtime",
        "/agent-schedules": "_handle_agent_schedules_get",
    }
    _ROOT_STATIC = {"/manifest.json", "/styles.css", "/icon.png"}
    _POST_ROUTES = {
        "/send": "_handle_send",
        "/dreaming/run": "_handle_dreaming_run_post",
        "/orchestrator/route-delegation": "_handle_orchestrator_route_delegation",
        "/transcribe": "_handle_transcribe",
        "/transcription-models/install": "_handle_transcription_model_install",
        "/transcription-models/remove": "_handle_transcription_model_remove",
        "/transcription-guidance": "_handle_transcription_guidance_post",
        "/transcription-providers": "_handle_transcription_providers_post",
        "/vocab/packs": "_handle_vocab_packs_post",
        "/vocab/packs/update": "_handle_vocab_packs_update",
        "/vocab/packs/delete": "_handle_vocab_packs_delete",
        "/vocab/terms": "_handle_vocab_terms_post",
        "/vocab/terms/update": "_handle_vocab_terms_update",
        "/vocab/terms/delete": "_handle_vocab_terms_delete",
        "/vocab/profiles": "_handle_vocab_profiles_post",
        "/vocab/profiles/delete": "_handle_vocab_profiles_delete",
        "/vocab/profiles/packs": "_handle_vocab_profile_packs_post",
        "/vocab/profiles/packs/remove": "_handle_vocab_profile_packs_remove",
        "/vocab/assign": "_handle_vocab_assign_post",
        "/vocab/unassign": "_handle_vocab_unassign_post",
        "/pairing/exchange": "_handle_pairing_exchange",
        "/paired-devices/revoke": "_handle_paired_device_revoke",
        "/tts/providers": "_handle_tts_providers_post",
        "/backend-auth/login": "_handle_backend_auth_login",
        "/backend-auth/login-code": "_handle_backend_auth_login_code",
        "/backend-auth/logout": "_handle_backend_auth_logout",
        "/server-update": "_handle_server_update",
        "/managed-skills": "_handle_managed_skills_update",
        "/upload": "_handle_upload",
        "/media": "_handle_media_publish",
        "/agent-portraits": "_handle_agent_portraits_update",
        "/agent-portrait-generation": "_handle_agent_portrait_generation_start",
        "/select": "_handle_select",
        "/clog": "_handle_clog",
        "/agents": "_handle_create_agent",
        "/personas": "_handle_create_persona",
        "/personas/update": "_handle_update_persona",
        "/agent-voice": "_handle_agent_voice",
        "/agent-llm": "_handle_agent_llm",
        "/agent-mcp": "_handle_agent_mcp",
        "/agent-heartbeat": "_handle_agent_heartbeat",
        "/agent-archive": "_handle_agent_archive",
        "/heartbeat/settings": "_handle_heartbeat_settings_post",
        "/diagnostics/settings": "_handle_diagnostics_settings_post",
        "/agent-dreaming": "_handle_agent_dreaming",
        "/agent-mute": "_handle_agent_mute",
        "/team-nudging": "_handle_team_nudging",
        "/compact": "_handle_compact",
        "/orchestrator/settings": "_handle_orchestrator_settings_post",
        "/herald/settings": "_handle_herald_settings_post",
        "/personalities/settings": "_handle_personalities_settings_post",
        "/automation-settings": "_handle_automation_settings_post",
        "/preview": "_handle_preview",
        "/stop": "_handle_stop",
        "/remote-action": "_handle_remote_action",
        "/focus": "_handle_focus",
        "/clips/ack": "_handle_clip_ack",
        "/devices": "_handle_register_device",
        "/location": "_handle_set_location",
        "/location/request": "_handle_request_location",
        "/calendar/request": "_handle_request_calendar",
        "/crash": "_handle_crash",
        "/teams": "_handle_team_create",
        "/artifacts": "_handle_artifact_create",
        "/decisions": "_handle_decision_create",
        "/oracle/delegations": "_handle_oracle_delegation_create",
        "/oracle/delegations/ack": "_handle_oracle_delegation_ack",
        "/oracle/delegations/cancel": "_handle_oracle_delegation_cancel",
        "/agent-schedules": "_handle_agent_schedules_post",
        "/agent-schedules/toggle": "_handle_agent_schedules_toggle",
        "/agent-schedules/delete": "_handle_agent_schedules_delete",
    }
    _PUT_ROUTES = {
        "/dreaming/settings": "_handle_dreaming_settings_put",
    }

    # ---- DI plumbing ---------------------------------------------------

    @property
    def ctx(self) -> ServerContext:
        return self.server.ctx  # type: ignore[attr-defined]

    # ---- request-level eventlog ----------------------------------------

    def _log_http(self, code: int, started: float) -> None:
        """Called after every request to emit one eventlog row."""
        path = redact_query_secrets(self.path)
        status = int(code)
        try:
            database = db.finish_request_metrics()
            phases = dict(getattr(self, "_response_phases", {}) or {})
            if database:
                phases["database"] = {
                    **database,
                    "sqlite_ms": round(float(database.get("sqlite_ms") or 0), 3),
                    "max_query_ms": round(float(database.get("max_query_ms") or 0), 3),
                }
            eventlog.emit(
                "server", "httpRequest",
                level="error" if status >= 500 else ("warn" if status >= 400 else "info"),
                trace_id=getattr(self, "_trace_id", None),
                request_id=getattr(self, "_request_id", None),
                path=path,
                status=status,
                duration_ms=int((time.time() - started) * 1000),
                detail={
                    "method": self.command,
                    "path": path,
                    "status": status,
                    "client": self.address_string(),
                    "phases": phases,
                    "interaction_id": self._interaction_id(),
                },
            )
        except Exception:
            pass

    def handle_one_request(self):
        started = time.time()
        self._request_started_monotonic = time.perf_counter()
        self._response_phases = {}
        from lib import diagnostics_settings
        db.begin_request_metrics(
            enabled=diagnostics_settings.allows("database"))
        self._request_id = _trace.new_id()
        self._trace_id = None
        try:
            super().handle_one_request()
        finally:
            # `self.command` and a status set by send_response are only set
            # after super() parses the request line. If the connection dies
            # or runs out of FDs before that, neither attribute exists yet,
            # so use getattr to keep the finally-block from raising.
            code = getattr(self, "_status_code", 0) or 0
            if getattr(self, "command", None):
                self._log_http(code, started)

    def send_response(self, code, message=None):
        self._status_code = code
        self._drain_request_body()
        super().send_response(code, message)

    # --- HTTP helpers -----------------------------------------------------

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler's stock logger; forward to journal.
        try:
            print(f"{self.address_string()} {format % args}", flush=True)
        except OSError as e:
            log_exception("httpAccessLogFail", e)

    def _send(self, code, body=b"", content_type="text/plain", extra_headers=None):
        response_started = time.perf_counter()
        request_started = getattr(self, "_request_started_monotonic", response_started)
        headers = dict(extra_headers or {})
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
        compressible = (
            content_type.startswith("application/json")
            or content_type.startswith("text/")
            or content_type.startswith("application/javascript")
        )
        if (body and len(body) >= 1024 and accepts_gzip and compressible
                and "Content-Encoding" not in headers):
            compression_started = time.perf_counter()
            body = gzip.compress(body, compresslevel=5)
            compression_ms = (time.perf_counter() - compression_started) * 1000
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        else:
            compression_ms = 0.0
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        write_started = time.perf_counter()
        if body:
            self.wfile.write(body)
        finished = time.perf_counter()
        self._response_phases = {
            "handler_ms": round((response_started - request_started) * 1000, 3),
            "compression_ms": round(compression_ms, 3),
            "socket_write_ms": round((finished - write_started) * 1000, 3),
            "response_bytes": len(body),
        }

    def _send_sw(self):
        """Serve sw.js with a version string derived from the newest static
        file mtime. The static file declares VERSION = 'claude-pwa-v1' as a
        placeholder; we rewrite that to a dynamic value so the browser sees
        fresh bytes whenever any static asset changes → SW updates → page
        auto-reloads via the controllerchange listener in app.js."""
        version = self.ctx.sw_version()
        src = (self.ctx.static / "sw.js").read_text()
        body = src.replace(
            "'claude-pwa-v1'",
            f"'claude-pwa-{version}'",
            1,
        ).encode()
        self._send(200, body, "application/javascript")

    def _send_file(self, path: pathlib.Path, content_type: str = "", secure: bool = False):
        if not path.is_file():
            return self._send(404, b"not found")
        ctype = content_type
        if not ctype:
            ctype, _ = mimetypes.guess_type(str(path))
        if not ctype:
            ctype = "application/octet-stream"
        data = path.read_bytes()
        return self._send_ranged_data(data, ctype, secure=secure)

    def _send_ranged_data(self, data: bytes, ctype: str, *, secure: bool = False):
        total = len(data)
        security_headers = ({"X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "sandbox; default-src 'none'"}
                            if secure else {})
        # Honor Range requests. AVPlayer (iOS) will NOT stream a remote audio
        # file unless the server speaks byte ranges — without this it fails to
        # play plain mp3s with CoreMediaErrorDomain -12640 (the "X here, ready
        # for an update" herald clips were silent for exactly this reason).
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start, end = 0, total - 1
            try:
                spec = rng.split("=", 1)[1].split(",", 1)[0]
                lo, _, hi = spec.partition("-")
                if lo:
                    start = int(lo)
                    if hi:
                        end = int(hi)
                elif hi:                       # suffix range: bytes=-N (last N)
                    start = max(0, total - int(hi))
                start = max(0, start)
                end = min(end, total - 1)
            except ValueError:
                start, end = 0, total - 1
            if start > end or start >= total:
                start, end = 0, total - 1
            return self._send(206, data[start:end + 1], ctype, extra_headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                **security_headers,
            })
        self._send(200, data, ctype, extra_headers={
            "Accept-Ranges": "bytes", **security_headers,
        })

    def _send_index(self):
        return self._send_file(self.ctx.static / "index.html")

    def _send_root_static(self, path: str):
        return self._send_file(self.ctx.static / path.lstrip("/"))

    def _send_complete_clip(self, clip_id: int) -> None:
        """Wait for a chunked-file clip to finalize, then serve it statically."""
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                row = db.conn().execute(
                    "SELECT path, producer_status FROM clips WHERE clip_id = ?",
                    (clip_id,),
                ).fetchone()
            except Exception as e:  # noqa: BLE001
                log_exception("completeClipLookupFail", e, detail=str(clip_id))
                return self._send(500, b"clip lookup failed")
            if row is None:
                return self._send(404, b"not found")
            target = pathlib.Path(row["path"] or "").resolve()
            status = str(row["producer_status"] or "").lower()
            if status == "complete" and target.is_file():
                audio_root = self.ctx.audio_dir.resolve()
                if target == audio_root or audio_root not in target.parents:
                    return self._send(403, b"forbidden")
                return self._send_file(target)
            time.sleep(0.05)
        return self._send(504, b"clip not complete")

    def _dispatch_exact(self, routes: dict[str, str], path: str) -> bool:
        name = routes.get(path)
        if not name:
            return False
        getattr(self, name)()
        return True

    def _read_json(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            self._body_consumed = True
            data = json.loads(self.rfile.read(n)) if n > 0 else {}
        except (ValueError, json.JSONDecodeError) as e:
            log_exception("requestJsonParseFail", e, detail=self.path)
            return None
        if not isinstance(data, dict):
            # Every handler does data.get(...): a JSON string, array, or
            # number body must read as "bad json", not kill the connection.
            log("requestJsonNotObject", f"{self.path} {type(data).__name__}")
            return None
        return data

    # --- Auth gate -------------------------------------------------------
    #
    # When ctx.auth_token is non-empty, every request must carry a matching
    # bearer token via Authorization header OR ?token= query parameter. The
    # PWA shell itself (HTML / JS / CSS / icon / service worker / manifest)
    # is intentionally public so the client can bootstrap and pick up the
    # token from the URL on first visit.
    _PUBLIC_EXACT = {"/", "/sw.js", "/manifest.json",
                     "/styles.css", "/icon.png", "/pairing/exchange"}
    _PUBLIC_PREFIXES = ("/static/", "/notification-avatars/")

    def _authorized(self) -> bool:
        self._request_auth_validated = False
        self._request_device_scope = ""
        self._request_principal = ""
        token = getattr(self.ctx, "auth_token", "") or ""
        if not token:
            return True  # auth disabled
        bare = self.path.split("?", 1)[0]
        if bare in self._PUBLIC_EXACT:
            return True
        for p in self._PUBLIC_PREFIXES:
            if bare.startswith(p):
                return True
        # Header: Authorization: Bearer <token>
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            supplied = auth.split(None, 1)[1].strip()
            if self._accept_credential(supplied, token):
                return True
        # Cookie: claude_pwa_token=<token>. Used by EventSource/iframe where
        # custom Authorization headers are not available.
        from http.cookies import SimpleCookie
        try:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            supplied = (cookie.get("claude_pwa_token").value
                        if cookie.get("claude_pwa_token") else "")
            if supplied and self._accept_credential(supplied.strip(), token):
                return True
        except Exception as e:  # noqa: BLE001
            log_exception("authCookieParseFail", e)
        # Query string: ?token=<token>
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        for v in qs.get("token", []):
            if self._accept_credential((v or "").strip(), token):
                return True
        return False

    def _accept_credential(self, supplied: str, admin_token: str) -> bool:
        if secrets.compare_digest(supplied, admin_token):
            self._request_auth_validated = True
            self._request_device_scope = "full"
            self._request_principal = "administrator"
            return True
        from lib.device_pairing import authenticate
        device = authenticate(supplied)
        if device is None:
            return False
        self._request_auth_validated = True
        self._request_device_scope = str(device["scope"])
        self._request_principal = str(device["device_id"])
        return True

    _DEVICE_FULL_ONLY_PREFIXES = (
        "/backend-auth", "/server-update", "/managed-skills",
        "/orchestrator/", "/herald/", "/personalities/",
        "/automation-settings", "/paired-devices", "/tts/providers",
        "/oracle/",
    )
    _LIMITED_DEVICE_POST_EXACT = frozenset({
        "/send", "/transcribe", "/upload", "/select", "/focus",
        "/clips/ack", "/clog", "/location", "/calendar/response",
    })

    def _device_forbidden(self, path: str, method: str) -> bool:
        if getattr(self, "_request_device_scope", "") != "limited":
            return False
        if any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in self._DEVICE_FULL_ONLY_PREFIXES
        ):
            return True
        if method == "GET":
            return False
        if method == "POST":
            return path not in self._LIMITED_DEVICE_POST_EXACT
        return True

    def _oracle_auth_missing(self, path: str) -> bool:
        return (path == "/oracle" or path.startswith("/oracle/")) and not bool(
            getattr(self, "_request_auth_validated", False))

    def _reject_unauthorized(self) -> None:
        self._send(401, b'{"error":"unauthorized"}', "application/json")

    # --- GET dispatch ----------------------------------------------------

    def do_GET(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._oracle_auth_missing(path):
            return self._send(
                401, b'{"error":"Oracle requires authenticated full-device access"}',
                "application/json")
        if self._device_forbidden(path, "GET"):
            return self._send(403, b'{"error":"full device access required"}',
                              "application/json")
        if path in self._ROOT_STATIC:
            return self._send_root_static(path)
        if self._dispatch_exact(self._GET_ROUTES, path):
            return
        if path.startswith("/teams/") and path.endswith("/messages"):
            team_id = path[len("/teams/"):-len("/messages")].strip("/")
            return self._handle_team_messages(team_id)
        if path.startswith("/artifacts/"):
            from urllib.parse import unquote
            return self._handle_artifact_get(unquote(path[len("/artifacts/"):].strip("/")))
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (self.ctx.static / rel).resolve()
            if self.ctx.static.resolve() in target.parents or target == self.ctx.static.resolve():
                return self._send_file(target)
            return self._send(403, b"forbidden")
        if path.startswith("/media/"):
            asset_id = path[len("/media/"):].strip("/")
            return self._handle_media_content(asset_id)
        if path.startswith("/agent-portraits/") and path.endswith("/content"):
            portrait_id = path[len("/agent-portraits/"):-len("/content")].strip("/")
            return self._handle_agent_portrait_content(portrait_id)
        if path.startswith("/notification-avatars/"):
            return self._handle_notification_avatar(
                path[len("/notification-avatars/"):].strip("/"))
        if path.startswith("/avatars/"):
            return self._handle_agent_avatar(path[len("/avatars/"):].strip("/"))
        if path.startswith("/persona-avatars/"):
            return self._handle_persona_avatar(path[len("/persona-avatars/"):].strip("/"))
        if path.startswith("/audio/"):
            name = path[len("/audio/"):]
            target = (self.ctx.audio_dir / name).resolve()
            if self.ctx.audio_dir.resolve() in target.parents:
                # If the worker is still streaming bytes to this clip
                # (sidecar present, streamable=true, no `bytes` field yet),
                # serve with Transfer-Encoding: chunked so we don't send a
                # truncating Content-Length. iOS Safari falls back to
                # plain HTTP for audio/mpeg (MSE unsupported), and the
                # static handler's Content-Length=<partial> would stop
                # playback after the first few words.
                from lib.audio_growing import is_in_progress, serve_growing
                if target.is_file() and is_in_progress(target):
                    return serve_growing(self, target)
                return self._send_file(target)
            return self._send(403, b"forbidden")
        if path.startswith("/stt/stream"):
            # Provider-owned turn taking: the phone streams PCM, the relay
            # forwards it to the chosen recogniser and returns its turn events.
            from urllib.parse import parse_qs, urlparse
            from lib.stt_stream import serve_stt_stream
            parsed = parse_qs(urlparse(self.path).query)
            return serve_stt_stream(
                self, {k: (v[0] if v else "") for k, v in parsed.items()})
        if path.startswith("/terminal/"):
            # Interactive terminal WS: spawns the agent's CLI in a PTY resumed
            # on the same session id and bridges raw bytes both ways.
            from lib.terminal_ws import serve_terminal
            session = path[len("/terminal/"):].strip("/")
            return serve_terminal(self, session)
        if path.startswith("/clips/") and path.endswith("/stream"):
            raw = path[len("/clips/"):-len("/stream")].strip("/")
            try:
                clip_id = int(raw)
            except ValueError:
                return self._send(400, b"bad clip id")
            from lib.clip_stream import serve_clip_stream
            return serve_clip_stream(
                self, self.ctx.clip_broker, clip_id, self.ctx.audio_dir
            )
        if path.startswith("/clips/") and path.endswith("/complete.mp3"):
            raw = path[len("/clips/"):-len("/complete.mp3")].strip("/")
            try:
                clip_id = int(raw)
            except ValueError:
                return self._send(400, b"bad clip id")
            return self._send_complete_clip(clip_id)
        # HLS artifact routes: /clips/<id>/playlist.m3u8 and
        # /clips/<id>/segment-N.aac. Only present when HlsDelivery is the
        # active delivery; otherwise the dir just doesn't exist and we 404.
        if path.startswith("/clips/") and "/" in path[len("/clips/"):]:
            rest = path[len("/clips/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                try:
                    clip_id = int(parts[0])
                except ValueError:
                    return self._send(400, b"bad clip id")
                filename = parts[1]
                from lib.clip_delivery.hls import serve_hls_artifact
                return serve_hls_artifact(
                    self, self.ctx.audio_dir, clip_id, filename,
                )
        return self._send(404, b"not found")

    def _handle_remote_action_get(self):
        """GET variant for iOS Shortcuts that use 'Open URL' rather than
        'Get Contents of URL' with an explicit POST. Accepts ?action=...
        and broadcasts the same SSE event."""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        action = (qs.get("action", [""])[0] or "").strip().lower()
        if action not in ClientAction.valid():
            return self._send(400, b'{"error":"unknown action"}', "application/json")
        self.ctx.stream.broadcast({"type": SSEType.REMOTE_ACTION, "action": action,
                          "ts": int(time.time() * 1000)})
        log("remoteAction", f"GET {action}")
        # Tiny no-cache HTML so Safari shows something blank instead of raw JSON.
        body = b"<!doctype html><meta charset=utf-8><title>ok</title>"
        self._send(200, body, "text/html")

    def _handle_agent_avatar(self, agent_id: str):
        from lib import agents as agents_db
        row = agents_db.get_by_agent_id(agent_id)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        if not row or not path.is_file():
            return self._send(404, b"not found")
        self._send_file(path)

    def _handle_notification_avatar(self, agent_id: str):
        """Serve one short-lived APNs avatar capability without app auth."""
        from urllib.parse import parse_qs, unquote, urlparse
        from lib import agents as agents_db
        from lib.avatar_urls import (
            avatar_content_version,
            notification_avatar_authorized,
        )

        identity = unquote(agent_id)
        row = agents_db.get_by_agent_id(identity)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        if not row or not path.is_file():
            return self._send(404, b"not found")
        query = parse_qs(urlparse(self.path).query)
        try:
            expires_at = int((query.get("exp") or [""])[0])
        except ValueError:
            return self._send(403, b"forbidden")
        content_version = avatar_content_version(path)
        supplied_version = str((query.get("v") or [""])[0])
        signature = str((query.get("sig") or [""])[0])
        if supplied_version != content_version or not notification_avatar_authorized(
            secret=str(getattr(self.ctx, "auth_token", "") or ""),
            agent_id=identity,
            content_version=content_version,
            expires_at=expires_at,
            signature=signature,
            now=int(time.time()),
        ):
            return self._send(403, b"forbidden")
        self._send_file(path)

    # --- GET handlers ----------------------------------------------------

    def _handle_voices(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        include_sid = (qs.get("for", [""])[0] or "").strip()
        agents_map = load_agents(self.ctx.agents_path)
        out = voices_with_availability(agents_map, include_sid)
        info = agents_map.get(include_sid) or {}
        persona = info.get("name") or info.get("persona") or ""
        bio = persona_personality(persona)
        prefix = "Personality: "
        if bio.startswith(prefix):
            bio = bio[len(prefix):]
        self._send(200, json.dumps({"voices": out, "bio": bio,
                                    "persona": persona}).encode(),
                   "application/json")

    def _handle_cartesia_voices(self):
        from urllib.parse import parse_qs, urlparse
        from lib.cartesia_voices import catalog
        query = parse_qs(urlparse(self.path).query)
        force = query.get("force", ["0"])[0] in {"1", "true"}
        try:
            body = catalog(self.ctx.agents_path, force=force)
        except Exception as exc:
            log_exception("cartesiaVoicesFail", exc)
            return self._send(502, json.dumps({
                "error": "Cartesia voice library unavailable"
            }).encode(), "application/json")
        self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_voice_catalog(self):
        from urllib.parse import parse_qs, urlparse
        from lib.voice_catalog import catalog
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("for", [""])[0] or "").strip()
        agents = load_agents(self.ctx.agents_path)
        self._send(200, json.dumps(catalog(agents, session)).encode(),
                   "application/json")

    def _handle_voice_preview(self):
        from urllib.parse import parse_qs, urlparse
        import hashlib
        from lib import config as app_config
        from lib.voice_catalog import catalog
        query = parse_qs(urlparse(self.path).query)
        provider = (query.get("provider", [""])[0] or "").strip().lower()
        voice_id = (query.get("id", [""])[0] or "").strip()
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
        cache = (RuntimePaths.from_home(pathlib.Path.home()).cache_dir
                 / "voice-previews")
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        prompt = f"Hello, I'm {voice['name']}. This is how I'll sound in Clarp."
        key = hashlib.sha256(
            f"v3\0{provider}\0{voice_id}\0{adapter_revision}\0{prompt}".encode()
        ).hexdigest()
        path = cache / f"{key}.mp3"
        if not path.is_file():
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                if provider == "cartesia":
                    from lib.cartesia_tts import synthesize
                    synthesize(text=prompt, voice_id=voice_id,
                               out_path=temporary, api_key=cfg.cartesia_key(),
                               model=cfg.cartesia_model)
                elif provider == "elevenlabs":
                    from lib.eleven_ws import synthesize_streaming
                    from lib.voice_catalog import (
                        ELEVEN_PREVIEW_MODEL, ELEVEN_PREVIEW_SPEED,
                    )
                    synthesize_streaming(
                        text=prompt, voice_id=voice_id, out_path=temporary,
                        api_key=cfg.eleven_key(),
                        model=ELEVEN_PREVIEW_MODEL,
                        speed=ELEVEN_PREVIEW_SPEED)
                elif provider == "deepgram":
                    from lib.deepgram_tts import synthesize
                    synthesize(text=prompt, voice_id=voice_id,
                               out_path=temporary, api_key=cfg.deepgram_key())

                else:
                    from lib.custom_tts_adapters import preview as custom_preview
                    from lib.tts_providers import synthesize
                    if custom_manifest is not None:
                        custom_preview(
                            custom_manifest, text=prompt, voice=voice_id,
                            out_path=temporary)
                    else:
                        synthesize(provider, text=prompt, voice=voice_id,
                                   out_path=temporary)
                temporary.replace(path)
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                log_exception("voicePreviewFail", exc,
                              detail=f"{provider}:{voice_id}")
                return self._send(502, b"preview unavailable")
        return self._send_file(path)

    def _handle_cartesia_voice_preview(self):
        from urllib.parse import parse_qs, urlparse
        import hashlib
        from lib.cartesia_voices import (
            cached_english_voice, english_voices, has_cached_catalog)
        from lib.cartesia_tts import synthesize, CartesiaError
        from lib import config as app_config
        query = parse_qs(urlparse(self.path).query)
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
        cache = (RuntimePaths.from_home(pathlib.Path.home()).cache_dir
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
                    synthesize(
                        text=prompt,
                        voice_id=voice_id, out_path=temporary,
                        api_key=cfg.cartesia_key(), model=cfg.cartesia_model)
                    temporary.replace(path)
                except (CartesiaError, OSError) as exc:
                    try: temporary.unlink()
                    except OSError: pass
                    log_exception("cartesiaPreviewFail", exc, detail=voice_id)
                    return self._send(502, b"preview unavailable")
        return self._send_file(path)

    def _handle_orchestrator_settings_get(self):
        from lib.orchestrator import provider_options
        body = json.dumps({
            "settings": get_orchestrator_settings().__dict__,
            "providers": provider_options(),
            "recent_decisions": recent_orchestrator_decisions(5),
            "ignored_decisions": recent_orchestrator_ignored_decisions(20),
        }).encode()
        self._send(200, body, "application/json")

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

    def _handle_personalities_settings_get(self):
        body = json.dumps({
            "settings": get_personality_settings().as_dict(),
        }).encode()
        self._send(200, body, "application/json")

    def _handle_agent_model_options(self):
        from lib import provider_capabilities
        self._send(
            200,
            json.dumps(provider_capabilities.endpoint_response()).encode(),
            "application/json",
        )

    def _handle_automation_settings_get(self):
        from lib.automation_settings import get
        self._send(200, json.dumps(get()).encode(), "application/json")

    def _handle_automation_settings_post(self):
        from lib.automation_settings import update
        data = self._read_json()
        if data is None or not isinstance(data.get("special_treatment"), bool):
            return self._send(400, b'{"error":"special_treatment required"}',
                              "application/json")
        self._send(200, json.dumps(update(data["special_treatment"])).encode(),
                   "application/json")

    def _handle_favorite_paths(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int(qs.get("limit", ["5"])[0])
        except ValueError:
            limit = 5
        self._send(
            200,
            json.dumps({"paths": agents_db.favorite_paths(limit)}).encode(),
            "application/json",
        )

    def _handle_orchestrator_decisions(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int(qs.get("limit", ["30"])[0])
        except ValueError:
            limit = 30
        self._send(
            200,
            json.dumps({"decisions": recent_orchestrator_decisions(limit)}).encode(),
            "application/json",
        )

    def _handle_snapshot(self):
        """Unified per-agent read model for the dashboard."""
        body = json.dumps(build_agent_snapshot(self.ctx)).encode()
        self._send(200, body, "application/json")

    def _handle_prompt_history(self):
        """Authenticated history of prospectively evidenced user prompts."""
        if not (getattr(self.ctx, "auth_token", "") or ""):
            return self._send(
                503,
                b'{"error":"prompt history requires configured authentication"}',
                "application/json",
            )
        from urllib.parse import parse_qs, urlparse
        from lib.prompt_history import build_prompt_history

        qs = parse_qs(urlparse(self.path).query)
        session_id = (qs.get("session_id", [""])[0] or "").strip()
        compatibility_session = (qs.get("session", [""])[0] or "").strip()
        if not session_id and not compatibility_session:
            return self._send(
                400, b'{"error":"session_id required"}', "application/json",
            )
        try:
            limit = int(qs.get("limit", ["50"])[0] or 50)
        except ValueError:
            return self._send(
                400, b'{"error":"invalid limit"}', "application/json",
            )
        before = (qs.get("before", [""])[0] or "").strip()
        try:
            history = build_prompt_history(
                session_id=session_id,
                compatibility_session_slug=compatibility_session,
                limit=limit,
                before=before,
            )
        except ValueError as exc:
            return self._send(
                400,
                json.dumps({"error": str(exc)}).encode(),
                "application/json",
            )
        if history is None:
            return self._send(
                404, b'{"error":"session not found"}', "application/json",
            )
        self._send(
            200,
            json.dumps(history, ensure_ascii=False).encode(),
            "application/json",
        )

    def _handle_dirs(self):
        from urllib.parse import parse_qs, urlparse
        from lib.launch_paths import recover_user_path
        qs = parse_qs(urlparse(self.path).query)
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

    def _handle_past_sessions(self):
        from urllib.parse import parse_qs, urlparse
        from lib import backends
        from lib.launch_paths import recover_user_path
        qs = parse_qs(urlparse(self.path).query)
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

    @staticmethod
    def _first_user_message(jsonl: pathlib.Path) -> str:
        try:
            with jsonl.open(encoding="utf-8") as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError as e:
                        log_exception("pastSessionsJsonSkip", e, detail=str(jsonl))
                        continue
                    if d.get("type") != "user":
                        continue
                    msg = d.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content.strip()
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                return (c.get("text") or "").strip()
        except OSError as e:
            log_exception("pastSessionsReadFail", e, detail=str(jsonl))
        return ""

    def _handle_status(self):
        """Is the named agent busy? Primary signal: a marker file written by the
        UserPromptSubmit hook and cleared by Stop."""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        session = (qs.get("session", [self.ctx.default_session])[0] or self.ctx.default_session).strip()
        from lib import agents as agents_db
        agent = agents_db.get_by_session(session)
        # busy state is purely DB-driven now (hooks + clarp_runner write
        # to state_log). The old terminal-scrape fallback is gone.
        busy = bool(agent and agents_db.is_busy(agent["agent_id"]))
        body = json.dumps({
            "session": session, "busy": busy,
            "deployed_version": self.ctx.deployed_version(),
            "release_id": getattr(
                self.ctx, "deployed_release_id", lambda: "development")(),
        }).encode()
        self._send(200, body, "application/json")

    def _handle_diagnostics_health(self):
        from lib import tts_queue
        stt_ready = bool(self.ctx.stt.ready.is_set())
        ffmpeg_ready = _CFG.delivery != "hls" or shutil.which("ffmpeg") is not None
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

    def _activate_transcription_if_default(self, installed_id: str) -> None:
        from lib.config import load as load_config
        from lib.stt import (DisabledSTT, UnavailableSTT, WhisperSTT,
                             WhisperCppSTT,
                             _installed_model_records)
        cfg = load_config()
        provider = getattr(cfg, "whisper_provider", "faster-whisper")
        expected = f"{provider}:{cfg.whisper_model}"
        if not cfg.whisper_enabled:
            if isinstance(self.ctx.stt, DisabledSTT):
                self.ctx.stt.available = True
            return
        if installed_id != expected:
            return
        current = self.ctx.stt
        if (
            isinstance(current, WhisperSTT)
            and not isinstance(current, UnavailableSTT)
            and current.default_model_id == installed_id
        ):
            if not current.load_done.wait(timeout=330.0):
                raise RuntimeError(
                    f"installed model still loading: {installed_id}")
            if current.load_error is None:
                return
            if not getattr(current, "_clarp_activation_failure_reported", False):
                current._clarp_activation_failure_reported = True
                raise RuntimeError(str(current.load_error))
        record = next(item for item in _installed_model_records()
                      if item["id"] == installed_id)
        if provider == "whisper.cpp":
            replacement = WhisperCppSTT(
                cfg.whisper_model, model_source=record["_local_path"],
                runtime_source=record["_runtime_path"])
        else:
            from lib.stt import SubprocessWhisperSTT
            stt_cls = (SubprocessWhisperSTT
                       if getattr(cfg, "whisper_isolate", True) else WhisperSTT)
            replacement = stt_cls(
                cfg.whisper_model, cfg.whisper_compute,
                model_source=record["_local_path"])
        # Publish loader ownership before starting it. A timed-out recovery
        # retry must wait on this exact loader rather than create a duplicate.
        self.ctx.stt = replacement
        replacement.start_loading()
        if not replacement.load_done.wait(timeout=330.0):
            raise RuntimeError(f"installed model did not load: {installed_id}")
        if replacement.load_error is not None:
            replacement._clarp_activation_failure_reported = True
            raise RuntimeError(str(replacement.load_error))

    def _handle_transcription_capabilities(self):
        from lib.transcription_models import recover_completed_installs
        recover_completed_installs(self._activate_transcription_if_default)
        capabilities = getattr(self.ctx.stt, "capabilities", None)
        if callable(capabilities):
            payload = capabilities()
        else:
            payload = {
                "available": bool(self.ctx.stt.ready.is_set()),
                "default_model": "server-default",
                "models": [{
                    "id": "server-default", "name": "Server default",
                    "provider": "server", "model": "default", "weight": "unknown",
                }],
                "adapters": [],
            }
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _transcription_guidance_payload(self):
        from lib.vocab import settings_payload
        return settings_payload(self.ctx.active_agent_names())

    def _handle_transcription_guidance_get(self):
        self._send(
            200,
            json.dumps(self._transcription_guidance_payload()).encode(),
            "application/json",
        )

    def _handle_transcription_guidance_post(self):
        from lib.vocab import update_guidance
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            update_guidance(data)
        except ValueError as exc:
            return self._send(
                400, json.dumps({"error": str(exc)}).encode(), "application/json")
        payload = self._transcription_guidance_payload()
        payload["ok"] = True
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _handle_transcription_providers_get(self):
        from lib import stt_providers
        self._send(200, json.dumps(stt_providers.status()).encode(),
                   "application/json")

    def _handle_transcription_providers_post(self):
        from lib import stt_providers
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            payload = stt_providers.update_settings(data)
        except ValueError as exc:
            return self._send(
                400, json.dumps({"error": str(exc)}).encode(), "application/json")
        payload["ok"] = True
        self._send(200, json.dumps(payload).encode(), "application/json")

    # ---- vocabulary: packs, terms, profiles, assignments, preview, runs ----
    #
    # The transparency contract behind the iOS vocabulary screens: everything
    # the compiler will do is previewable, and everything it did is readable.

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlparse
        parsed = parse_qs(urlparse(self.path).query)
        return {k: (v[0] if v else "") for k, v in parsed.items()}

    def _json_ok(self, payload) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _json_error(self, code: int, message: str) -> None:
        self._send(code, json.dumps({"error": message}).encode(), "application/json")

    def _vocab_body(self) -> dict | None:
        data = self._read_json()
        if not isinstance(data, dict):
            self._json_error(400, "bad json")
            return None
        return data

    def _handle_vocab_packs_get(self):
        from lib import vocab_store
        counts = vocab_store.pack_term_counts()
        packs = vocab_store.list_packs()
        for pack in packs:
            pack["terms"] = counts.get(pack["pack_id"], 0)
        self._json_ok({"packs": packs})

    def _handle_vocab_packs_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        name = str(data.get("name") or "").strip()
        if not name:
            return self._json_error(400, "name is required")
        kind = str(data.get("kind") or "static")
        if kind not in ("static", "dynamic"):
            return self._json_error(400, "kind must be static or dynamic")
        try:
            pack_id = vocab_store.create_pack(
                name, kind=kind, generator=str(data.get("generator") or ""),
                priority=float(data.get("priority", 1.0)),
                floor=int(data.get("floor", 0)),
                enabled=bool(data.get("enabled", True)))
        except sqlite3.IntegrityError:
            return self._json_error(409, "a pack with that name exists")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        for term in data.get("terms") or []:
            if isinstance(term, str):
                vocab_store.add_term(pack_id, term)
            elif isinstance(term, dict):
                vocab_store.add_term(
                    pack_id, str(term.get("text") or ""),
                    rarity=float(term.get("rarity", 0.5)),
                    say_as=str(term.get("say_as") or ""),
                    often_heard_as=str(term.get("often_heard_as") or ""))
        self._json_ok({"ok": True, "pack_id": pack_id})

    def _handle_vocab_packs_update(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        pack_id = str(data.get("pack_id") or "")
        if not pack_id:
            return self._json_error(400, "pack_id is required")
        try:
            if "enabled" in data:
                vocab_store.set_pack_enabled(pack_id, bool(data["enabled"]))
            vocab_store.update_pack(
                pack_id, name=data.get("name"),
                priority=data.get("priority"), floor=data.get("floor"))
        except sqlite3.IntegrityError:
            return self._json_error(409, "a pack with that name exists")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True})

    def _handle_vocab_packs_delete(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        pack_id = str(data.get("pack_id") or "")
        if not pack_id:
            return self._json_error(400, "pack_id is required")
        vocab_store.delete_pack(pack_id)
        self._json_ok({"ok": True})

    def _handle_vocab_terms_get(self):
        from lib import vocab_store
        pack_id = self._query().get("pack_id", "")
        if not pack_id:
            return self._json_error(400, "pack_id is required")
        self._json_ok({"pack_id": pack_id, "terms": vocab_store.list_terms(pack_id)})

    def _handle_vocab_terms_post(self):
        from lib import vocab_store
        from lib.vocab_generators import estimate_rarity
        data = self._vocab_body()
        if data is None:
            return
        pack_id = str(data.get("pack_id") or "")
        text = str(data.get("text") or "").strip()
        if not pack_id or not text:
            return self._json_error(400, "pack_id and text are required")
        rarity = data.get("rarity")
        try:
            added = vocab_store.add_term(
                pack_id, text,
                rarity=float(rarity) if rarity is not None else estimate_rarity(text),
                say_as=str(data.get("say_as") or ""),
                often_heard_as=str(data.get("often_heard_as") or ""),
                source=str(data.get("source") or "manual"))
        except sqlite3.IntegrityError:
            return self._json_error(404, "no such pack")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True, "added": added})

    def _handle_vocab_terms_update(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        try:
            term_id = int(data.get("term_id"))
        except (TypeError, ValueError):
            return self._json_error(400, "term_id is required")
        try:
            updated = vocab_store.update_term(
                term_id, text=data.get("text"), say_as=data.get("say_as"),
                often_heard_as=data.get("often_heard_as"), rarity=data.get("rarity"))
        except sqlite3.IntegrityError:
            return self._json_error(409, "that term already exists in the pack")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True, "updated": updated})

    def _handle_vocab_terms_delete(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        try:
            term_id = int(data.get("term_id"))
        except (TypeError, ValueError):
            return self._json_error(400, "term_id is required")
        vocab_store.delete_term(term_id)
        self._json_ok({"ok": True})

    def _handle_vocab_profiles_get(self):
        from lib import vocab_store
        self._json_ok({"profiles": vocab_store.list_profiles()})

    def _handle_vocab_profile_get(self):
        from lib import vocab_store
        profile_id = self._query().get("profile_id", "")
        detail = vocab_store.profile_detail(profile_id) if profile_id else None
        if detail is None:
            return self._json_error(404, "no such profile")
        self._json_ok(detail)

    def _handle_vocab_profiles_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        name = str(data.get("name") or "").strip()
        if not name:
            return self._json_error(400, "name is required")
        try:
            profile_id = vocab_store.create_profile(name)
        except sqlite3.IntegrityError:
            return self._json_error(409, "a profile with that name exists")
        for position, pack_id in enumerate(data.get("pack_ids") or []):
            vocab_store.add_pack_to_profile(profile_id, str(pack_id), position)
        self._json_ok({"ok": True, "profile_id": profile_id})

    def _handle_vocab_profiles_delete(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        if not profile_id:
            return self._json_error(400, "profile_id is required")
        vocab_store.delete_profile(profile_id)
        self._json_ok({"ok": True})

    def _handle_vocab_profile_packs_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        pack_id = str(data.get("pack_id") or "")
        if not profile_id or not pack_id:
            return self._json_error(400, "profile_id and pack_id are required")
        try:
            vocab_store.add_pack_to_profile(
                profile_id, pack_id, int(data.get("position", 0)))
        except sqlite3.IntegrityError:
            return self._json_error(404, "no such profile or pack")
        except (TypeError, ValueError) as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True})

    def _handle_vocab_profile_packs_remove(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        pack_id = str(data.get("pack_id") or "")
        if not profile_id or not pack_id:
            return self._json_error(400, "profile_id and pack_id are required")
        vocab_store.remove_pack_from_profile(profile_id, pack_id)
        self._json_ok({"ok": True})

    def _handle_vocab_assignments_get(self):
        from lib import vocab_store
        self._json_ok({"assignments": vocab_store.assignments()})

    def _handle_vocab_assign_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        profile_id = str(data.get("profile_id") or "")
        if not profile_id:
            return self._json_error(400, "profile_id is required")
        try:
            vocab_store.assign_profile(
                profile_id, agent_id=str(data.get("agent_id") or ""),
                team_id=str(data.get("team_id") or ""))
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except sqlite3.IntegrityError:
            return self._json_error(404, "no such profile")
        self._json_ok({"ok": True})

    def _handle_vocab_unassign_post(self):
        from lib import vocab_store
        data = self._vocab_body()
        if data is None:
            return
        agent_id = str(data.get("agent_id") or "")
        team_id = str(data.get("team_id") or "")
        if not agent_id and not team_id:
            return self._json_error(400, "agent_id or team_id is required")
        vocab_store.unassign(agent_id=agent_id, team_id=team_id)
        self._json_ok({"ok": True})

    def _handle_vocab_budgets_get(self):
        from lib import stt_providers
        from lib.vocab_compile import describe_budgets
        self._json_ok({"budgets": describe_budgets(),
                       "engine": stt_providers.selected_engine(),
                       "models": stt_providers.cloud_models()})

    def _handle_vocab_preview_get(self):
        query = self._query()
        preview = getattr(self.ctx, "vocab_preview", None)
        if not callable(preview):
            return self._json_error(503, "vocabulary preview unavailable")
        try:
            payload = preview(
                session=query.get("session", ""),
                requested_model=query.get("model", ""),
                delegated=_truthy_header(query.get("delegated", "")))
        except Exception as exc:  # noqa: BLE001
            log_exception("vocabPreviewFail", exc)
            return self._json_error(500, "vocabulary preview failed")
        self._json_ok(payload)

    def _handle_vocab_runs_get(self):
        from lib import vocab_store
        query = self._query()
        try:
            limit = max(1, min(200, int(query.get("limit") or 20)))
        except ValueError:
            limit = 20
        self._json_ok({"runs": vocab_store.recent_runs(
            limit, session=query.get("session", ""))})

    def _handle_vocab_run_get(self):
        from lib import heard_audio, vocab_store
        trace_id = self._query().get("trace_id", "")
        run = vocab_store.run_for_trace(trace_id) if trace_id else None
        if run is None:
            return self._json_error(404, "no run for that trace")
        kept = heard_audio.lookup(
            RuntimePaths.from_home(pathlib.Path.home()).cache_dir, trace_id)
        run["audio_url"] = f"/transcription-audio?trace_id={trace_id}" if kept else None
        self._json_ok(run)

    def _handle_transcription_audio_get(self):
        """The clip the transcriber was given for one trace, if retained."""
        from lib import heard_audio
        trace_id = self._query().get("trace_id", "")
        kept = heard_audio.lookup(
            RuntimePaths.from_home(pathlib.Path.home()).cache_dir, trace_id)
        if kept is None:
            return self._json_error(404, "no retained audio for that trace")
        path, meta = kept
        self._send_file(path, meta.get("content_type") or "application/octet-stream")

    def _handle_transcription_model_install(self):
        from lib.transcription_models import start_install
        from lib.server_identity import get_server_info
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        model_id = str(data.get("model_id") or "").strip()
        try:
            result = start_install(
                model_id, computer_id=str(get_server_info()["server_id"]),
                on_complete=self._activate_transcription_if_default)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(202, json.dumps(result).encode(), "application/json")

    def _handle_transcription_model_remove(self):
        from lib.transcription_models import remove
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        model_id = str(data.get("model_id") or "").strip()
        try:
            remove(model_id)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps({"ok": True, "model_id": model_id}).encode(),
                   "application/json")

    def _handle_backend_usage(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        force = any(str(v).lower() in {"1", "true", "yes"} for v in qs.get("refresh", []))
        payload = backend_usage.get_backend_usage(force_codex=force)
        for event in payload.get("limit_events") or []:
            self.ctx.stream.broadcast(event)
        body = json.dumps(payload).encode()
        self._send(200, body, "application/json")

    def _handle_backend_auth(self):
        from lib.backend_auth import status, task
        from lib import backends
        body = {"backends": status(), "tasks": {
            adapter.id: task(adapter.id) for adapter in backends.auth_adapters()}}
        self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_backend_auth_login(self):
        from lib.backend_auth import start_login
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        backend = str(data.get("backend") or "").strip().lower()
        try:
            result = start_login(backend)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(202, json.dumps(result).encode(), "application/json")

    def _handle_backend_auth_login_code(self):
        from lib.backend_auth import submit_login_code
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        backend = str(data.get("backend") or "").strip().lower()
        try:
            result = submit_login_code(backend, str(data.get("code") or ""))
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except RuntimeError as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps(result).encode(), "application/json")

    def _handle_backend_auth_logout(self):
        from lib.backend_auth import logout
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        backend = str(data.get("backend") or "").strip().lower()
        try:
            result = logout(backend)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except RuntimeError as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps(result).encode(), "application/json")

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
            return self._send(503, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_managed_skills_update(self):
        from lib.managed_skills import set_enabled
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        skill_id = str(data.get("skill_id") or "").strip()
        enabled = data.get("enabled")
        if not skill_id or not isinstance(enabled, bool):
            return self._send(
                400, b'{"error":"skill_id and boolean enabled are required"}',
                "application/json")
        try:
            skill = set_enabled(skill_id, enabled)
        except ValueError as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except OSError as exc:
            log_exception("managedSkillsUpdateFail", exc, detail=skill_id)
            return self._send(500, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps({"ok": True, "skill": skill}).encode(),
                   "application/json")

    def _handle_server_info(self):
        from lib.server_identity import get_server_info
        self._send(200, json.dumps(get_server_info()).encode(), "application/json")

    def _handle_pairing_exchange(self):
        from lib.device_pairing import PairingError, exchange
        from lib.server_identity import get_server_info
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            device = exchange(
                str(data.get("code") or ""),
                device_name=str(data.get("device_name") or ""),
            )
        except PairingError as exc:
            return self._send(
                409, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        payload = {"device": device, "server": get_server_info()}
        self._send(201, json.dumps(payload).encode(), "application/json")

    def _handle_paired_devices(self):
        from lib.device_pairing import list_devices
        self._send(
            200, json.dumps({"devices": list_devices()}).encode(),
            "application/json")

    def _handle_paired_device_revoke(self):
        from lib.device_pairing import revoke
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            return self._send(
                400, b'{"error":"device_id required"}', "application/json")
        if not revoke(device_id):
            return self._send(404, b'{"error":"device not found"}',
                              "application/json")
        self._send(200, b'{"ok":true}', "application/json")

    def _handle_tts_providers_get(self):
        from lib.tts_providers import status
        self._send(200, json.dumps(status()).encode(), "application/json")

    def _handle_tts_providers_post(self):
        from lib import config as config_module
        from lib.config_writer import set_toml_value
        from lib.tts_providers import status, valid_ids
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        provider = str(data.get("provider") or "").strip().lower()
        fallback = str(data.get("fallback") or "none").strip().lower()
        voice = str(data.get("voice") or "").strip()
        allowed = valid_ids()
        rows = {row["id"]: row for row in status()["providers"]}
        if (provider not in allowed or fallback not in allowed
                or (fallback != "none" and not rows.get(
                    fallback, {}).get("can_fallback", False))):
            return self._send(
                400, b'{"error":"unsupported TTS provider or fallback"}',
                "application/json")
        current = status()
        rows = {row["id"]: row for row in current["providers"]}
        for selected in {provider, fallback} - {"none"}:
            if rows[selected]["kind"] == "local" and not rows[selected]["installed"]:
                return self._send(
                    409, json.dumps({
                        "error": f"local TTS provider is not installed: {selected}",
                    }).encode(), "application/json")
            if rows[selected].get("custom") and not rows[selected]["available"]:
                return self._send(
                    409, json.dumps({
                        "error": f"custom TTS adapter is unavailable: {selected}",
                    }).encode(), "application/json")
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
        self._send(200, json.dumps(payload).encode(), "application/json")
        if restart_required:
            from lib import service_manager
            timer = threading.Timer(
                0.5, lambda: service_manager.restart(check=False))
            timer.daemon = True
            timer.start()




    def _handle_server_update_status(self):
        from lib.server_update import get_update_status
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)
        force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
        self._send(200, json.dumps(get_update_status(force=force)).encode(), "application/json")

    def _handle_server_update(self):
        from lib.server_update import request_update
        status, payload = request_update(self.ctx.default_session)
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _session_cwd(self, session: str) -> pathlib.Path:
        """cwd for an agent is whatever its DB row says — there's no
        live terminal process to query any more."""
        return session_cwd(session)

    def _handle_agent_files(self):
        from urllib.parse import parse_qs, urlparse
        from lib.agent_files import AgentFileError, list_directory
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [""])[0] or "").strip()
        relative = _raw_query_value(self.path, "path")
        requested_root = _raw_query_value(self.path, "root")
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"agent not found"}', "application/json")
        cwd = requested_root or str(agent.get("cwd") or "")
        if requested_root.startswith("@"):
            alias, _, suffix = requested_root.partition("/")
            if alias not in {"@workspace", "@home"}:
                return self._send(400, b'{"error":"unknown directory root"}', "application/json")
            base = pathlib.Path(str(agent.get("cwd") or "")) if alias == "@workspace" else pathlib.Path.home()
            candidate = (base / suffix).resolve()
            try: candidate.relative_to(base.resolve())
            except ValueError:
                return self._send(403, b'{"error":"directory escapes root"}', "application/json")
            cwd = str(candidate)
        if not cwd:
            return self._send(404, b'{"error":"agent workspace unavailable"}',
                              "application/json")
        try:
            root_candidate = pathlib.Path(cwd).expanduser()
            if requested_root and not root_candidate.is_absolute():
                return self._send(400, b'{"error":"root path must be absolute"}',
                                  "application/json")
            # Explorer intentionally follows the authenticated operator trust
            # model of the terminal/agent CLI: any path readable by the server
            # user is browseable. It remains read-only and size-bounded.
            root = root_candidate
            if not root.is_dir():
                return self._send(404, b'{"error":"agent workspace unavailable"}',
                                  "application/json")
            payload = list_directory(root, relative)
        except AgentFileError as exc:
            return self._send(exc.status, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except ValueError as exc:
            return self._send(403, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except (OSError, RuntimeError) as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _handle_task_plan(self):
        from urllib.parse import parse_qs, urlparse
        from lib import task_plans
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [""])[0] or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        self._send(200, json.dumps({
            "plan": task_plans.active_for_session(session)
        }).encode(), "application/json")

    def _handle_agent_file(self):
        from urllib.parse import parse_qs, urlparse
        from lib.agent_files import AgentFileError, read_text_file
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [""])[0] or "").strip()
        relative = _raw_query_value(self.path, "path")
        requested_root = _raw_query_value(self.path, "root")
        if not session or not relative:
            return self._send(400, b'{"error":"session and path required"}',
                              "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"agent not found"}', "application/json")
        cwd = requested_root or str(agent.get("cwd") or "")
        if not cwd:
            return self._send(404, b'{"error":"agent workspace unavailable"}',
                              "application/json")
        try:
            root_candidate = pathlib.Path(cwd).expanduser()
            if requested_root and not root_candidate.is_absolute():
                return self._send(400, b'{"error":"root path must be absolute"}',
                                  "application/json")
            # See `_handle_agent_files`: this is an operator capability, not a
            # sandbox boundary. Descendant traversal still uses no-follow FDs.
            root = root_candidate
            if not root.is_dir():
                return self._send(404, b'{"error":"agent workspace unavailable"}',
                                  "application/json")
            payload = read_text_file(root, relative)
        except AgentFileError as exc:
            return self._send(exc.status, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except ValueError as exc:
            return self._send(403, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        except (OSError, RuntimeError) as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _handle_log(self):
        """Return the conversation turns for an agent's transcript.

        Query params:
          session=NAME   app session name (defaults to the focused agent)
          limit=N        cap the returned turns to the last N (default 100)
          after_revision=N  return only messages changed after SQLite revision N.
          before=MESSAGE_ID page older history ending before this message.
        """
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        session = (qs.get("session", [self.ctx.default_session])[0] or self.ctx.default_session).strip()
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 5000))
        try:
            after_revision = int(qs.get("after_revision", ["0"])[0] or 0)
        except ValueError:
            after_revision = 0
        before_message_id = (qs.get("before", [""])[0] or "").strip()
        include_automated = str(
            qs.get("include_automated", ["1"])[0]
        ).lower() not in {"0", "false", "no"}
        include_tool_details = str(
            qs.get("include_tool_details", ["1"])[0]
        ).lower() not in {"0", "false", "no"}
        try:
            body_obj = load_conversation(
                session=session,
                after_revision=after_revision,
                before_message_id=before_message_id,
                limit=limit,
                include_automated=include_automated,
                include_tool_details=include_tool_details,
                interaction_id=self._interaction_id(),
                claude_finder=find_latest_jsonl,
                claude_parser=parse_turns,
            )
        except OSError as e:
            log_exception("logParseFail", e, detail=session)
            return self._send(500, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        body = json.dumps(body_obj).encode()
        self._send(200, body, "application/json")

    def _interaction_id(self) -> str:
        value = str(self.headers.get("X-Clarp-Interaction-ID") or "").strip()
        if len(value) > 80 or any(ch not in "-0123456789abcdefABCDEF" for ch in value):
            return ""
        return value

    def _handle_message_tool_details(self):
        from urllib.parse import parse_qs, urlparse
        from lib import message_store
        qs = parse_qs(urlparse(self.path).query)
        session = (qs.get("session", [""])[0] or "").strip()
        message_id = (qs.get("message_id", [""])[0] or "").strip()
        if not session or not message_id:
            return self._send(400, b'{"error":"session and message_id required"}',
                              "application/json")
        details = message_store.message_tool_details(
            session=session, message_id=message_id)
        if details is None:
            return self._send(404, b'{"error":"message not found"}',
                              "application/json")
        self._send(200, json.dumps(details).encode(), "application/json")

    def _sse(self):
        last_event_id = ""
        try:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            last_event_id = (
                self.headers.get("Last-Event-ID")
                or qs.get("last_event_id", [""])[0]
                or ""
            ).strip()
        except Exception:
            last_event_id = ""
        try:
            replay_after = int(last_event_id) if last_event_id else None
        except ValueError:
            replay_after = None
        # The stream is unbounded (no Content-Length), so this connection can
        # never be reused — say so explicitly and let the base class close it
        # when the handler returns. Under keep-alive an unmarked unbounded
        # response would hang the proxy waiting for a length.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        q = self.ctx.stream.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            cursor = replay_after
            while True:
                page = self.ctx.stream.recent(cursor)
                for ev in page:
                    event_id = ev.get("event_id")
                    if event_id:
                        self.wfile.write(f"id: {event_id}\n".encode())
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                # Fresh replay is already a compact time-window snapshot. A
                # cursor replay pages until caught up so >500 offline events
                # cannot disappear between reconnect and live subscription.
                if cursor is None or len(page) < 500:
                    break
                next_cursor = max(
                    (int(ev.get("event_id") or 0) for ev in page),
                    default=cursor,
                )
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
            # Roster nudge: a freshly (re)connected client's agent list may
            # predate this connection, and a live stream alone never restores
            # per-agent availability on the client. The client treats this
            # event as "refetch the roster snapshot now".
            self.wfile.write(b'data: {"type": "agent-roster"}\n\n')
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=SERVER_TIMING.sse_queue_timeout_sec)
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        ev = {}
                    event_id = ev.get("event_id")
                    if event_id:
                        self.wfile.write(f"id: {event_id}\n".encode())
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Backpressure eviction: broadcast() marks a full queue
                    # evicted and stops feeding it. Deliver what was drained,
                    # then CLOSE — never ghost-ping a stream that receives
                    # nothing (the client would see a healthy, silent stream
                    # forever). The client reconnects and resumes loss-free
                    # via Last-Event-ID.
                    if getattr(q, "evicted", False):
                        try:
                            eventlog.emit("server", "sseEvictedClose", level="warning")
                        except Exception:
                            pass
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            try:
                eventlog.emit("server", "sseClientGone", level="info")
            except Exception:
                pass
        finally:
            self.ctx.stream.unsubscribe(q)

    # --- POST dispatch ---------------------------------------------------

    def do_POST(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._oracle_auth_missing(path):
            return self._send(
                401, b'{"error":"Oracle requires authenticated full-device access"}',
                "application/json")
        if self._device_forbidden(path, "POST"):
            return self._send(403, b'{"error":"full device access required"}',
                              "application/json")
        if self._dispatch_exact(self._POST_ROUTES, path):
            return
        if path.startswith("/turn-queue/") and path.endswith("/send"):
            queue_id = path[len("/turn-queue/"):-len("/send")].strip("/")
            return self._handle_turn_queue_send(queue_id)
        if path.startswith("/teams/"):
            rest = path[len("/teams/"):].strip("/")
            if rest.endswith("/members"):
                return self._handle_team_add_member(rest[:-len("/members")].strip("/"))
            return self._handle_team_update(rest)
        if path.startswith("/decisions/") and path.endswith("/resolve"):
            decision_id = path[len("/decisions/"):-len("/resolve")].strip("/")
            return self._handle_decision_resolve(decision_id)
        if path.startswith("/artifacts/"):
            from urllib.parse import unquote
            return self._handle_artifact_update(unquote(path[len("/artifacts/"):].strip("/")))
        return self._send(404, b"not found")

    def do_PUT(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._device_forbidden(path, "PUT"):
            return self._send(403, b'{"error":"full device access required"}',
                              "application/json")
        if self._dispatch_exact(self._PUT_ROUTES, path):
            return
        if path.startswith("/turn-queue/"):
            return self._handle_turn_queue_update(path[len("/turn-queue/"):].strip("/"))
        return self._send(404, b"not found")

    def _handle_focus(self):
        """Client tells us which agent view the user is looking at. Drives the
        herald system's decision of whether to suppress an off-focus agent."""
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        sid = (data.get("session") or "").strip()
        if not sid:
            return self._send(400, b'{"error":"session required"}', "application/json")
        from lib import agents as agents_db
        agent = agents_db.get_by_session(sid)
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
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_register_device(self):
        """Register an iOS APNs device token for push notifications. The device
        POSTs its token (optionally the session it's focused on); when any agent
        finishes its turn the server pushes a "your turn" alert to it. Body:
        {"token": "<hex>", "session": "...", "environment": "production",
         "base_url": "http://private-overlay-address:7682"}."""
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        token = (data.get("token") or "").strip()
        if not token:
            return self._send(400, b'{"error":"token required"}', "application/json")
        from lib import apns
        try:
            apns.register_token(
                token,
                session=(data.get("session") or "").strip() or None,
                environment=(data.get("environment") or "").strip() or None,
                platform=(data.get("platform") or "ios").strip() or "ios",
                base_url=(data.get("base_url") or "").strip(),
            )
        except Exception as e:  # noqa: BLE001
            log_exception("deviceRegisterFail", e)
            return self._send(500, b'{"error":"register failed"}', "application/json")
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_set_location(self):
        """The app POSTs the user's current GPS fix for a session (one-shot
        CoreLocation, When-In-Use). Body: {"session","lat","lng","accuracy"?}."""
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        try:
            lat = float(data["lat"])
            lng = float(data["lng"])
        except (KeyError, TypeError, ValueError):
            return self._send(400, b'{"error":"lat/lng required"}', "application/json")
        acc = data.get("accuracy")
        from lib import location
        try:
            row = location.set_location(
                session, lat, lng, float(acc) if acc is not None else None)
        except ValueError as e:
            return self._send(
                400,
                json.dumps({"error": str(e)}).encode(),
                "application/json",
            )
        except Exception as e:  # noqa: BLE001
            log_exception("setLocationFail", e)
            return self._send(500, b'{"error":"store failed"}', "application/json")
        return self._send(200, json.dumps({"ok": True, **row}).encode(),
                          "application/json")

    def _handle_get_location(self):
        """Latest stored fix for ?session=<id>. {} when none shared yet —
        read by the request-location skill."""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        session = (qs.get("session", [""])[0] or "").strip()
        from lib import location
        loc = location.get_location(session)
        return self._send(200, json.dumps(loc or {}).encode(), "application/json")

    def _handle_teams_list(self):
        from lib import team_store
        body = {"teams": team_store.list_teams()}
        return self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_team_create(self):
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            team = team_store.create_team(
                str(data.get("name") or ""),
                color=str(data.get("color") or ""),
            )
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        return self._send(200, json.dumps({"ok": True, "team": team}).encode(),
                          "application/json")

    def _handle_team_update(self, team_id: str):
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            if "leader" in data:
                team_store.set_leader(team_id, (data.get("leader") or "") or None)
            if any(k in data for k in ("name", "color", "archived")):
                team = team_store.update_team(
                    team_id,
                    name=data.get("name") if "name" in data else None,
                    color=data.get("color") if "color" in data else None,
                    archived=data.get("archived") if "archived" in data else None,
                )
            else:
                team = team_store.get_team(team_id)
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        if team is None:
            return self._send(404, b'{"error":"team not found"}', "application/json")
        return self._send(200, json.dumps({"ok": True, "team": team}).encode(),
                          "application/json")

    def _handle_team_nudging(self):
        """Enable/disable autonomous leader nudging for a team."""
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        team_id = str(data.get("team_id") or "").strip()
        if not team_id:
            return self._send(400, b'{"error":"team_id required"}',
                              "application/json")
        if "nudge_enabled" not in data:
            return self._send(400, b'{"error":"nudge_enabled required"}',
                              "application/json")
        team = team_store.set_nudge_enabled(team_id, data.get("nudge_enabled") is True)
        if team is None:
            return self._send(404, b'{"error":"team not found"}', "application/json")
        return self._send(200, json.dumps({"ok": True, "team": team}).encode(),
                          "application/json")

    def _handle_team_add_member(self, team_id: str):
        from lib import team_store
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        agent_id = str(data.get("agent_id") or "").strip()
        if not agent_id:
            return self._send(400, b'{"error":"agent_id required"}',
                              "application/json")
        if not team_store.add_member(team_id, agent_id):
            return self._send(404, b'{"error":"team or agent not found"}',
                              "application/json")
        return self._send(200, json.dumps({
            "ok": True,
            "team": team_store.get_team(team_id),
        }).encode(), "application/json")

    def _handle_team_remove_member(self, team_id: str, agent_id: str):
        from lib import team_store
        if not team_id or not agent_id:
            return self._send(404, b'{"error":"team or agent not found"}',
                              "application/json")
        team_store.remove_member(team_id, agent_id)
        return self._send(200, json.dumps({
            "ok": True,
            "team": team_store.get_team(team_id),
        }).encode(), "application/json")

    def _handle_team_delete(self, team_id: str):
        from lib import team_store
        if not team_id:
            return self._send(404, b'{"error":"team not found"}', "application/json")
        if not team_store.delete_team(team_id):
            return self._send(404, b'{"error":"team not found"}', "application/json")
        return self._send(200, json.dumps({"ok": True}).encode(),
                          "application/json")

    def _handle_team_messages(self, team_id: str):
        from urllib.parse import parse_qs, urlparse
        from lib import team_store
        if not team_id:
            return self._send(404, b'{"error":"team not found"}', "application/json")
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
        except ValueError:
            limit = 100
        body = {
            "team_id": team_id,
            "messages": team_store.list_team_messages(team_id, limit=limit),
        }
        return self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_request_location(self):
        """An agent asks the app for the user's location: broadcast a request the
        focused client surfaces as a one-tap Share-location prompt. Body:
        {"session": "..."}."""
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        self.ctx.stream.broadcast(
            {"type": SSEType.LOCATION_REQUEST, "session": session})
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_request_calendar(self):
        """An agent asks the app to add an Apple Calendar event. Body:
        {"session":"...", "title":"...", "start":"ISO8601", "end":"ISO8601",
         "time_zone":"Europe/Oslo", "location":"...", "notes":"...",
         "url":"...", "all_day":false, "calendar":"Work"}."""
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        request_id = f"cal-{secrets.token_hex(8)}"
        try:
            request = build_calendar_request(data, request_id=request_id)
        except CalendarRequestError as exc:
            return self._send(
                400,
                json.dumps({"error": str(exc)}).encode(),
                "application/json",
            )
        self.ctx.stream.broadcast(request.as_event(SSEType.CALENDAR_REQUEST))
        return self._send(
            200,
            json.dumps({"ok": True, "request_id": request_id}).encode(),
            "application/json",
        )

    def _handle_crash(self):
        """Receive a MetricKit diagnostic payload from the iOS app.

        Payloads carry crash, hang, CPU-exception, disk-write, and slow-launch
        call stacks.
        """
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            from lib import db
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
            return self._send(500, b'{"error":"store failed"}', "application/json")
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_remote_action(self):
        """Receive a fire-and-forget remote action (typically from an iOS
        Shortcut driving the Action Button). Broadcasts it over SSE so the
        already-running PWA can act without a page reload."""
        data = self._read_json() or {}
        action = (data.get("action") or "").strip().lower()
        if action not in ClientAction.valid():
            return self._send(400, b'{"error":"unknown action"}', "application/json")
        self.ctx.stream.broadcast({"type": SSEType.REMOTE_ACTION, "action": action,
                          "ts": int(time.time() * 1000)})
        log("remoteAction", action)
        return self._send(200, b'{"ok":true}', "application/json")

    # --- POST handlers ---------------------------------------------------

    def _handle_agent_voice(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        voice_id = (data.get("voice_id") or "").strip()
        provider = (data.get("provider") or "").strip().lower()
        if not session or not voice_id:
            return self._send(400, b'{"error":"session and voice_id required"}',
                              "application/json")
        agents = load_agents(self.ctx.agents_path)
        if session not in agents:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        from lib.voice import merge_voice, resolve_voice
        for sid, info in agents.items():
            if sid == session:
                continue
            occupied = ((resolve_voice((info or {}).get("voice_id"), provider)
                         if provider else (info or {}).get("voice_id")))
            if occupied == voice_id:
                return self._send(409,
                    json.dumps({"error": "voice taken",
                                "by": (info or {}).get("name") or sid}).encode(),
                    "application/json")
        agents[session]["voice_id"] = (
            merge_voice(agents[session].get("voice_id"), provider, voice_id)
            if provider else voice_id)
        save_agents(agents, self.ctx.agents_path)
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_agent_llm(self):
        """Set a running agent's model and/or reasoning effort. Read fresh on
        the agent's next turn, so this re-tunes a live agent without relaunch.
        Send "" for a field to clear the override (fall back to config default).
        Only fields present in the body are changed."""
        from lib import agents as agents_db
        from lib import backends
        from lib import config
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        backend = backends.normalize(agent.get("backend"))
        valid = list(backends.valid_efforts(backend))
        update: dict[str, str] = {}
        if "model" in data:
            if data.get("model") is not None and not isinstance(data.get("model"), str):
                return self._send(400, b'{"error":"model must be string or null"}',
                                  "application/json")
            model = (data.get("model") or "").strip()
            if not backends.is_valid_model(backend, model):
                return self._send(400, json.dumps({
                    "error": "invalid model for backend", "backend": backend,
                }).encode(), "application/json")
            update["model"] = model
        if "effort" in data:
            if data.get("effort") is not None and not isinstance(data.get("effort"), str):
                return self._send(400, b'{"error":"effort must be string or null"}',
                                  "application/json")
            effort = (data.get("effort") or "").strip().lower()
            if effort and effort not in valid:
                return self._send(400, json.dumps(
                    {"error": "invalid effort for backend",
                     "backend": backend, "valid_efforts": valid}).encode(),
                    "application/json")
            update["effort"] = effort
        next_model = update.get("model", str(agent.get("model") or "").strip())
        if backend == backends.AGY and not next_model:
            next_model = config.load().agy_model.strip()
        next_effort = update.get("effort", str(agent.get("effort") or "").strip())
        if backend == backends.AGY and next_model and next_effort:
            return self._send(400, json.dumps({
                "error": "AGY model-specific effort compatibility is unknown",
                "backend": backend,
            }).encode(), "application/json")
        if update:
            agents_db.update_agent(agent["agent_id"], **update)
        fresh = agents_db.get_by_session(session) or {}
        return self._send(200, json.dumps({
            "ok": True, "backend": backend,
            "model": fresh.get("model", ""), "effort": fresh.get("effort", ""),
            "valid_efforts": valid,
        }).encode(), "application/json")

    def _handle_agent_mcp(self):
        """Set which MCP servers an agent loads (user-driven, from the app).
        Body: {"session": "...", "mcp_servers": ["name", ...]}. Names are
        validated against the global ~/.claude.json catalog; unknown names are
        dropped. Read fresh on the agent's next turn — no relaunch."""
        from lib import agents as agents_db
        from lib import config
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        catalog = config.read_global_mcp_servers()
        requested = data.get("mcp_servers")
        if not isinstance(requested, list):
            return self._send(400, b'{"error":"mcp_servers must be a list"}',
                              "application/json")
        # Keep only known servers, de-duplicated, in request order.
        chosen: list[str] = []
        for name in requested:
            n = str(name).strip()
            if n in catalog and n not in chosen:
                chosen.append(n)
        from lib.mcp_selection import encode
        agents_db.update_agent(agent["agent_id"], mcp_servers=encode(chosen))
        return self._send(200, json.dumps({
            "ok": True,
            "session": session,
            "mcp_servers": chosen,
            "available_mcp_servers": sorted(catalog.keys()),
        }).encode(), "application/json")

    def _handle_agent_heartbeat(self):
        """Enable/disable autonomous heartbeat for a single live agent."""
        from lib import agents as agents_db
        from lib import heartbeat
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        if "heartbeat_enabled" not in data:
            return self._send(400, b'{"error":"heartbeat_enabled required"}',
                              "application/json")
        if not isinstance(data.get("heartbeat_enabled"), bool):
            return self._send(400, b'{"error":"heartbeat_enabled must be boolean"}',
                              "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        enabled = bool(data["heartbeat_enabled"])
        agents_db.update_agent(
            agent["agent_id"],
            heartbeat_enabled=enabled,
        )
        heartbeat.record_heartbeat_activity(agent["agent_id"])
        fresh = agents_db.get_by_session(session) or {}
        return self._send(200, json.dumps({
            "ok": True,
            "session": session,
            "heartbeat_enabled": bool(fresh.get("heartbeat_enabled")),
        }).encode(), "application/json")

    def _handle_agent_schedules_get(self):
        from urllib.parse import parse_qs, urlparse
        from lib import scheduler
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [""])[0] or "").strip()
        agent_id = (query.get("agent_id", [""])[0] or "").strip()
        schedules = scheduler.list_schedules(
            session=session if session else None,
            agent_id=agent_id if agent_id else None,
        )
        return self._send(200, json.dumps({"schedules": schedules}).encode(), "application/json")

    def _handle_agent_schedules_post(self):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = str(data.get("session") or "").strip()
        name = str(data.get("name") or "").strip()
        cron = str(data.get("cron_expression") or data.get("cron") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        enabled = bool(data.get("enabled", True))
        if not session or not name or not cron or not prompt:
            return self._send(400, b'{"error":"session, name, cron_expression, and prompt are required"}', "application/json")
        try:
            item = scheduler.create_schedule(session, name, cron, prompt, enabled=enabled)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        return self._send(201, json.dumps({"ok": True, "schedule": item}).encode(), "application/json")

    def _handle_agent_schedules_toggle(self):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        schedule_id = str(data.get("schedule_id") or "").strip()
        enabled = data.get("enabled")
        if not schedule_id or not isinstance(enabled, bool):
            return self._send(400, b'{"error":"schedule_id and enabled boolean required"}', "application/json")
        item = scheduler.update_schedule(schedule_id, enabled=enabled)
        if not item:
            return self._send(404, b'{"error":"schedule not found"}', "application/json")
        return self._send(200, json.dumps({"ok": True, "schedule": item}).encode(), "application/json")

    def _handle_agent_schedules_delete(self):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        schedule_id = str(data.get("schedule_id") or "").strip()
        if not schedule_id:
            return self._send(400, b'{"error":"schedule_id required"}', "application/json")
        ok = scheduler.delete_schedule(schedule_id)
        return self._send(200, json.dumps({"ok": ok}).encode(), "application/json")

    def _handle_schedule_patch(self, schedule_id: str):
        from lib import scheduler
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
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
            return self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        if not item:
            return self._send(404, b'{"error":"schedule not found"}', "application/json")
        return self._send(200, json.dumps({"ok": True, "schedule": item}).encode(), "application/json")

    def _handle_schedule_delete(self, schedule_id: str):
        from lib import scheduler
        ok = scheduler.delete_schedule(schedule_id)
        if not ok:
            return self._send(404, b'{"error":"schedule not found"}', "application/json")
        return self._send(200, json.dumps({"ok": True}).encode(), "application/json")

    def _handle_agent_archive(self):
        from lib import agents as agents_db
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = str(data.get("session") or "").strip()
        archived = data.get("archived")
        if not session or not isinstance(archived, bool):
            return self._send(400, b'{"error":"session and archived required"}', "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        agents_db.set_archived(agent["agent_id"], archived)
        return self._send(200, json.dumps({"ok": True, "session": session,
                                           "archived": archived}).encode(),
                          "application/json")

    def _handle_agent_heartbeat_status(self):
        """Return one Agent's scheduler projection and recent heartbeat outcomes."""
        from urllib.parse import parse_qs, urlparse
        from lib import agents as agents_db
        from lib import db, heartbeat
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [""])[0] or "").strip()
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        rows = db.conn().execute(
            """SELECT message_id, text, timestamp, updated_at
                 FROM messages
                WHERE agent_id=? AND role='assistant' AND origin='heartbeat'
                ORDER BY updated_at DESC LIMIT 20""",
            (agent["agent_id"],),
        ).fetchall()
        history = [{
            "id": row["message_id"], "text": row["text"],
            "timestamp": row["timestamp"], "updated_at": row["updated_at"],
        } for row in rows]
        return self._send(200, json.dumps({
            "ok": True, "session": session,
            "schedule": heartbeat.agent_schedule(agent), "history": history,
        }).encode(), "application/json")

    def _handle_heartbeat_settings_get(self):
        """Return the heartbeat policy owned by this Computer."""
        from lib import heartbeat
        return self._send(200, json.dumps({
            "ok": True,
            "settings": heartbeat.get_settings().as_dict(),
            "heartbeat_prompt": heartbeat.HEARTBEAT_PROMPT,
        }).encode(), "application/json")

    def _handle_heartbeat_settings_post(self):
        """Atomically update the heartbeat policy for this Computer."""
        from lib import heartbeat
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            settings = heartbeat.update_settings(data)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        return self._send(200, json.dumps({
            "ok": True,
            "settings": settings.as_dict(),
            "heartbeat_prompt": heartbeat.HEARTBEAT_PROMPT,
        }).encode(), "application/json")

    def _handle_diagnostics_settings_get(self):
        """Return opt-in developer diagnostics owned by this Computer."""
        from lib import diagnostics_settings
        return self._send(200, json.dumps({
            "ok": True, "settings": diagnostics_settings.get().public(),
        }).encode(), "application/json")

    def _handle_diagnostics_settings_post(self):
        from lib import diagnostics_settings
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            settings = diagnostics_settings.update(data)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        return self._send(200, json.dumps({
            "ok": True, "settings": settings.public(),
        }).encode(), "application/json")

    def _handle_agent_dreaming(self):
        """Enable/disable nightly creative dreaming for a single live agent."""
        from lib import agents as agents_db
        from lib import dreaming
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        if "dreaming_enabled" not in data:
            return self._send(400, b'{"error":"dreaming_enabled required"}',
                              "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        enabled = data.get("dreaming_enabled") is True
        agents_db.update_agent(agent["agent_id"], dreaming_enabled=enabled)
        fresh = agents_db.get_by_session(session) or {}
        settings = dreaming.get_settings()
        return self._send(200, json.dumps({
            "ok": True,
            "session": session,
            "dreaming_enabled": bool(fresh.get("dreaming_enabled")),
            "dream_target_hour": dreaming.DREAM_TARGET_HOUR,
            "dream_planned_rounds": settings.planned_rounds,
            "dream_target_tokens": settings.target_token_budget,
            "dream_min_directions": settings.min_directions,
            "dream_settings": settings.as_dict(),
            "dream_prompt": dreaming.dreaming_prompt_text(),
        }).encode(), "application/json")

    def _handle_agent_mute(self):
        """Mute/unmute APNs pushes for a single live agent.

        Badges/unread remain enabled for user-facing speak turns; this flag
        only removes the interruption.
        """
        from lib import agents as agents_db
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        if "muted" not in data:
            return self._send(400, b'{"error":"muted required"}',
                              "application/json")
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"no such agent"}', "application/json")
        muted = data.get("muted") is True
        agents_db.update_agent(agent["agent_id"], muted=muted)
        fresh = agents_db.get_by_session(session) or {}
        return self._send(200, json.dumps({
            "ok": True,
            "session": session,
            "muted": bool(fresh.get("muted")),
        }).encode(), "application/json")

    def _handle_dreaming_runs(self):
        """Inspect recent deep dreaming run/thread/round ledger entries."""
        from urllib.parse import parse_qs, urlparse
        from lib import dreaming
        qs = parse_qs(urlparse(self.path).query)
        session = (qs.get("session", [""])[0] or "").strip()
        try:
            limit = int(qs.get("limit", ["10"])[0] or "10")
        except ValueError:
            limit = 10
        runs = dreaming.list_dream_runs(session=session, limit=limit)
        settings = dreaming.get_settings()
        return self._send(200, json.dumps({
            "ok": True,
            "runs": runs,
            "contract": settings.as_dict(),
        }).encode(), "application/json")

    def _dreaming_backend_options(self):
        """Backends the dreaming picker may offer, with their effort levels.

        Model lists are deliberately not duplicated here — a client already
        fetches `/agent-model-options` for the full catalogue and can filter it
        by the chosen backend id.
        """
        from lib import backends
        options = [{
            "id": "",
            "label": "Same as the agent",
            "efforts": [],
        }]
        for backend_id in backends.ids():
            fields = backends.catalogue_fields(backend_id)
            options.append({
                "id": backend_id,
                "label": fields.get("label") or backend_id,
                "efforts": list(backends.valid_efforts(backend_id)),
            })
        return options

    def _handle_dreaming_run_post(self):
        """Start one dream immediately, with an optional pinned recipe."""
        from lib import dreaming
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = str(data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}',
                              "application/json")
        try:
            run = dreaming.start_manual_run(
                session,
                seed_strategy=str(data.get("seed_strategy") or "").strip(),
                context_dose=str(data.get("context_dose") or "").strip(),
            )
        except ValueError as e:
            return self._send(
                409, json.dumps({"error": str(e)}).encode(), "application/json")
        return self._send(200, json.dumps({
            "ok": True,
            "run_id": run.get("run_id"),
            "session": run.get("session"),
            "seed_strategy": run.get("seed_strategy"),
            "context_dose": run.get("context_dose"),
            "seed_material": run.get("seed_material"),
        }).encode(), "application/json")

    def _handle_dreaming_settings_get(self):
        from lib import dream_seeds, dreaming
        return self._send(200, json.dumps({
            "ok": True,
            "settings": dreaming.get_settings().as_dict(),
            "limits": {
                "dreams_per_night": [
                    dreaming.DREAMS_PER_NIGHT_MIN,
                    dreaming.DREAMS_PER_NIGHT_MAX,
                ],
                "direction_count": [
                    dreaming.DREAM_DIRECTION_MIN,
                    dreaming.DREAM_DIRECTION_MAX,
                ],
                "target_token_budget": [
                    dreaming.DREAM_TOKEN_BUDGET_MIN,
                    dreaming.DREAM_TOKEN_BUDGET_MAX,
                ],
            },
            # Everything a client needs to draw the dreaming backend/model/
            # effort pickers without shipping its own catalogue. An empty
            # choice means "inherit the agent's own backend".
            "options": {
                "backends": self._dreaming_backend_options(),
                "seed_strategies": list(dream_seeds.STRATEGIES),
                "context_doses": list(dream_seeds.CONTEXT_DOSES),
            },
        }).encode(), "application/json")

    def _handle_dreaming_settings_put(self):  # noqa: D401
        from lib import dreaming
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        settings = dreaming.update_settings(data)
        return self._send(200, json.dumps({
            "ok": True,
            "settings": settings.as_dict(),
        }).encode(), "application/json")

    def _handle_compact(self):
        """Compact a conversation on demand (from the app). Drives the backend
        CLI's interactive /compact (claude/codex) or /compress (agy) in a
        throwaway tmux session resumed on the same session id, keeping the id.
        Body: {"session": "..."}. Returns immediately; the app watches the
        snapshot's `compacting` flag + `context_tokens` for progress."""
        from lib import compaction
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session:
            return self._send(400, b'{"error":"session required"}', "application/json")
        result = compaction.compact_session(session)
        status = 200 if result.get("ok") else 409
        return self._send(status, json.dumps(result).encode(), "application/json")

    def _handle_orchestrator_settings_post(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            settings = update_orchestrator_settings(data)
        except ValueError as exc:
            return self._send(
                400, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        return self._send(
            200,
            json.dumps({"ok": True, "settings": settings.__dict__}).encode(),
            "application/json",
        )

    def _handle_herald_settings_post(self):
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            settings = update_herald_settings(data)
        except ValueError as exc:
            return self._send(
                400,
                json.dumps({"error": str(exc)}).encode(),
                "application/json",
            )
        return self._send(
            200,
            json.dumps({"ok": True, "settings": settings.as_dict()}).encode(),
            "application/json",
        )

    def _handle_personalities_settings_post(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        settings = update_personality_settings(data)
        return self._send(
            200,
            json.dumps({"ok": True, "settings": settings.as_dict()}).encode(),
            "application/json",
        )

    def _handle_preview(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        voice_id = (data.get("voice_id") or "").strip()
        text = (data.get("text") or "Hi there, this is what I sound like.").strip()
        session = (data.get("session") or self.ctx.default_session).strip() or self.ctx.default_session
        if not voice_id:
            return self._send(400, b'{"error":"voice_id required"}', "application/json")
        try:
            path = self.ctx.tts.synthesize(text[:200], voice_id, session=session)
        except Exception as e:
            log_exception("previewSynthFail", e, detail=voice_id)
            return self._send(500, f'{{"error":"{e}"}}'.encode(), "application/json")
        # A preview plays on request, so it bypasses herald arbitration and
        # goes straight to every connected client.
        name = pathlib.Path(str(path)).name
        self.ctx.stream.broadcast({
            "type": SSEType.AUDIO, "url": f"/audio/{name}", "name": name,
            "session": session, "preview": True,
        })
        return self._send(200, json.dumps({"ok": True, "url": f"/audio/{name}"}).encode(),
                          "application/json")

    def _handle_oracle_status(self):
        from lib.oracle_realtime import capability
        return self._send(
            200, json.dumps(capability()).encode(), "application/json")

    def _handle_oracle_realtime(self):
        from lib.oracle_realtime import serve
        return serve(self)

    def _handle_oracle_delegations_get(self):
        from urllib.parse import parse_qs, urlparse
        from lib import oracle_delegations, turn_dispatch
        oracle_delegations.reconcile_orphans(
            is_live=turn_dispatch.owns_inflight_trace)
        query = parse_qs(urlparse(self.path).query)
        try:
            limit = int(query.get("limit", ["50"])[0] or "50")
        except ValueError:
            limit = 50
        include_all = str(query.get("all", ["0"])[0]).lower() in {
            "1", "true", "yes",
        }
        principal = str(self._request_principal or "administrator")
        rows = (oracle_delegations.recent(
                    owner_principal=principal, limit=limit) if include_all
                else oracle_delegations.undelivered(
                    owner_principal=principal, limit=limit))
        return self._send(
            200, json.dumps({"delegations": rows}).encode(),
            "application/json")

    def _handle_oracle_delegation_create(self):
        from lib import oracle_delegations
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            row = oracle_delegations.dispatch(
                ctx=self.ctx,
                delegation_id=str(data.get("delegation_id") or ""),
                session=str(data.get("session") or ""),
                request_text=str(data.get("request") or ""),
                authenticated_at_admission=bool(
                    getattr(self, "_request_auth_validated", False)
                    and (getattr(self.ctx, "auth_token", "") or "")
                ),
                owner_principal=str(self._request_principal or "administrator"),
            )
        except oracle_delegations.DelegationCollision as exc:
            return self._send(
                409, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        except (ValueError, LookupError) as exc:
            return self._send(
                400 if isinstance(exc, ValueError) else 404,
                json.dumps({"error": str(exc)}).encode(),
                "application/json")
        except Exception as exc:  # noqa: BLE001
            status = int(getattr(exc, "status", 500))
            log_exception("oracleDelegationFail", exc)
            return self._send(
                status, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        return self._send(
            200, json.dumps({"delegation": row}).encode(),
            "application/json")

    def _handle_oracle_delegation_ack(self):
        from lib import oracle_delegations
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            delegation_id = oracle_delegations.normalize_id(
                str(data.get("delegation_id") or ""))
        except ValueError as exc:
            return self._send(
                400, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        if not oracle_delegations.acknowledge(
                delegation_id,
                owner_principal=str(self._request_principal or "administrator")):
            return self._send(
                404, b'{"error":"terminal delegation not found"}',
                "application/json")
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_oracle_delegation_cancel(self):
        from lib import oracle_delegations
        data = self._read_json()
        if not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        principal = str(self._request_principal or "administrator")
        session = str(data.get("session") or "").strip()
        if session:
            try:
                rows = oracle_delegations.cancel_for_session(
                    session,
                    stop=lambda: self._stop_agent_session(
                        session, strict=True, defer_finish=True)[1],
                    owner_principal=principal)
            except ValueError as exc:
                return self._send(
                    400, json.dumps({"error": str(exc)}).encode(),
                    "application/json")
            except Exception as exc:  # noqa: BLE001
                log_exception("oracleAgentCancelFail", exc, detail=session)
                return self._send(
                    502, b'{"error":"agent cancellation failed"}',
                    "application/json")
            return self._send(200, json.dumps({
                "cancelled_count": len(rows), "delegations": rows,
            }).encode(), "application/json")
        try:
            delegation_id = oracle_delegations.normalize_id(
                str(data.get("delegation_id") or ""))
        except ValueError as exc:
            return self._send(
                400, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        try:
            row = oracle_delegations.cancel(
                delegation_id, owner_principal=principal)
        except oracle_delegations.DelegationNotCancellable as exc:
            return self._send(
                409, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        if row is None:
            return self._send(
                404, b'{"error":"delegation not found"}', "application/json")
        return self._send(
            200, json.dumps({"delegation": row}).encode(),
            "application/json")

    def _handle_stop(self):
        """Terminate any in-flight clarp turn for the given agent. With
        there is no long-lived subprocess to send Escape to; instead the per-turn
        clarp subprocess is registered in clarp_runner._ACTIVE and we
        SIGTERM the lot."""
        data = self._read_json() or {}
        session = (data.get("session") or self.ctx.default_session).strip() or self.ctx.default_session
        n = self._stop_agent_session(session, strict=False)
        return self._send(200, json.dumps({"ok": True, "terminated": n}).encode(),
                          "application/json")

    def _stop_agent_session(
        self, session: str, *, strict: bool, defer_finish: bool = False
    ):
        """Authoritative local stop shared by /stop and Oracle cancellation."""
        from lib import agents as agents_db
        from lib import backends
        from lib import turn_dispatch
        from lib import turn_queue
        agent = agents_db.get_by_session(session)
        n = 0
        dropped = 0
        if agent:
            agent_id = agent["agent_id"]
            stop_snapshot = turn_dispatch.snapshot_stop_state(agent_id)
            queue_was_paused = bool(turn_queue.state(agent_id)["paused"])
            dropped = turn_dispatch.clear_for_agent(
                agent_id, preserve_queue=True, pause_queue=True)
            try:
                n = backends.interrupt(
                    backends.normalize(agent.get("backend")), agent_id)
            except Exception as exc:  # barrier still must be released below
                log_exception("turnStopInterruptFail", exc, detail=agent_id)
                if strict:
                    turn_dispatch.restore_stop_state(agent_id, stop_snapshot)
                    turn_queue.set_paused(agent_id, queue_was_paused)
                    raise
            if strict and stop_snapshot.get("trace_id") and n <= 0:
                turn_dispatch.restore_stop_state(agent_id, stop_snapshot)
                turn_queue.set_paused(agent_id, queue_was_paused)
                raise RuntimeError("backend did not confirm interruption")
            # A SIGTERM'd turn often dies without firing its terminal callback,
            # so the agent is left stuck on a busy state ("thinking") and the
            # in-flight dispatch slot leaks. Record a terminal INTERRUPTED state
            # (non-busy → clears the badge), free the slot + queue, and push the
            # state so clients update immediately instead of on the next poll.
            # Durable user-requested queue entries remain visible and paused;
            # stopping work must never silently delete acknowledged messages.
            try:
                agents_db.record_state(agent_id, AgentState.INTERRUPTED,
                                       {"source": "user_stop", "message": "Turn stopped"})
                if getattr(self.ctx, "stream", None) is not None:
                    self.ctx.stream.broadcast({
                        "type": SSEType.AGENT_STATE,
                        "session": session,
                        "agent_id": agent_id,
                        "kind": AgentState.INTERRUPTED,
                    })
                    queue_state = turn_queue.state(agent_id)
                    self.ctx.stream.broadcast({
                        "type": SSEType.QUEUE_UPDATED,
                        "session": session,
                        "agent_id": agent_id,
                        "queue_depth": queue_state["count"],
                        "queue_paused": queue_state["paused"],
                        "queue_started": False,
                        "queue_revision": queue_state["revision"],
                    })
            except Exception as exc:  # stop already succeeded
                log_exception("turnStopBookkeepingFail", exc, detail=agent_id)
            if defer_finish:
                def release(cancelled_trace_ids: set[str]) -> None:
                    turn_dispatch.prepare_queued_for_finish(
                        agent_id, stop_snapshot, cancelled_trace_ids)
                    turn_dispatch.finish_stop(
                        self.ctx, agent_id, backend_registry=backends)
                log("turnStop", f"session={session} terminated={n} barrier=held")
                return n, release
            else:
                turn_dispatch.finish_stop(
                    self.ctx, agent_id, backend_registry=backends)
        log("turnStop", f"session={session} terminated={n} queued_preserved={turn_queue.pending_count(agent_id) if agent else 0}")
        if defer_finish:
            return n, (lambda _trace_ids: None)
        return n

    def do_DELETE(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._device_forbidden(path, "DELETE"):
            return self._send(403, b'{"error":"full device access required"}',
                              "application/json")
        if path.startswith("/teams/") and "/members/" in path:
            rest = path[len("/teams/"):]
            team_id, agent_id = rest.split("/members/", 1)
            return self._handle_team_remove_member(team_id.strip("/"), agent_id.strip("/"))
        if path.startswith("/teams/"):
            return self._handle_team_delete(path[len("/teams/"):].strip("/"))
        if path.startswith("/background-jobs/"):
            return self._handle_background_job_cancel(
                path[len("/background-jobs/"):].strip("/"))
        if path.startswith("/turn-queue/"):
            return self._handle_turn_queue_delete(path[len("/turn-queue/"):].strip("/"))
        if path.startswith("/transcription-results/"):
            return self._handle_transcription_result_delete(
                path[len("/transcription-results/"):].strip("/"))
        if path.startswith("/schedules/"):
            return self._handle_schedule_delete(path[len("/schedules/"):].strip("/"))
        if self.path.startswith("/agents/"):
            return self._handle_delete_agent(self.path[len("/agents/"):])
        if path.startswith("/personas/"):
            return self._handle_delete_persona(path[len("/personas/"):].strip("/"))
        return self._send(404, b"not found")

    def do_PATCH(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if path.startswith("/schedules/"):
            return self._handle_schedule_patch(path[len("/schedules/"):].strip("/"))
        return self._send(404, b"not found")

    def _handle_transcription_result_delete(self, job_id: str):
        from urllib.parse import unquote
        from lib import transcription_results
        try:
            job_id = transcription_results.normalize_job_id(unquote(job_id))
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        transcription_results.delete(job_id)
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_background_jobs(self):
        from lib import background_jobs
        return self._send(
            200,
            json.dumps(background_jobs.snapshot()).encode(),
            "application/json",
        )

    def _broadcast_artifact(self, artifact: dict) -> None:
        if getattr(self.ctx, "stream", None) is None:
            return
        self.ctx.stream.broadcast({
            "type": SSEType.ARTIFACT_UPDATED,
            "session": artifact.get("session", ""),
            "agent_id": artifact.get("agent_id", ""),
            "artifact_id": artifact.get("artifact_id", ""),
        })
        from lib import artifacts
        self.ctx.stream.broadcast({
            "type": SSEType.ATTENTION_UPDATED,
            "attention_count": len(artifacts.attention()),
        })

    def _handle_artifacts_list(self):
        from urllib.parse import parse_qs, urlparse
        from lib import artifacts
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
            offset = int(qs.get("offset", ["0"])[0] or 0)
            created_from = int(qs["created_from"][0]) if qs.get("created_from") else None
            created_to = int(qs["created_to"][0]) if qs.get("created_to") else None
        except (TypeError, ValueError):
            return self._send(400, b'{"error":"invalid limit"}', "application/json")
        rows = artifacts.list_artifacts(
            session=(qs.get("session", [""])[0] or "").strip(),
            agent_id=(qs.get("agent_id", [""])[0] or "").strip(),
            type=(qs.get("type", [""])[0] or "").strip(),
            search=(qs.get("search", [""])[0] or "").strip(),
            created_from=created_from, created_to=created_to,
            limit=limit, offset=offset,
            order=(qs.get("order", ["updated"])[0] or "updated").strip(),
        )
        return self._send(200, json.dumps({"artifacts": rows}).encode(), "application/json")

    def _handle_artifact_get(self, artifact_id: str):
        from lib import artifacts
        row = artifacts.get(artifact_id)
        if not row:
            return self._send(404, b'{"error":"artifact not found"}', "application/json")
        return self._send(200, json.dumps({"artifact": row}).encode(), "application/json")

    def _handle_artifact_create(self):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict): return self._send(400, b'{"error":"json object required"}', "application/json")
        if str(data.get("type") or "").strip().lower() in {"decision", "plan"}:
            return self._send(409, b'{"error":"reserved artifact type"}', "application/json")
        try:
            row = artifacts.create(
                session=str(data.get("session") or ""), type=str(data.get("type") or ""),
                title=str(data.get("title") or ""), summary=str(data.get("summary") or ""),
                status=str(data.get("status") or "ready"),
                reference_id=str(data.get("reference_id") or ""), payload=data.get("payload"),
                artifact_id=str(data.get("artifact_id") or ""))
        except (ValueError, sqlite3.IntegrityError) as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._broadcast_artifact(row)
        return self._send(201, json.dumps({"artifact": row}).encode(), "application/json")

    def _handle_artifact_update(self, artifact_id: str):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict): return self._send(400, b'{"error":"json object required"}', "application/json")
        try: row = artifacts.update(artifact_id, data)
        except ValueError as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._broadcast_artifact(row)
        return self._send(200, json.dumps({"artifact": row}).encode(), "application/json")

    def _handle_decision_create(self):
        from lib import artifacts
        data = self._read_json()
        if not isinstance(data, dict): return self._send(400, b'{"error":"json object required"}', "application/json")
        try:
            row = artifacts.create_decision(
                session=str(data.get("session") or ""), title=str(data.get("title") or "Decision"),
                question=str(data.get("question") or ""), context=str(data.get("context") or ""),
                yes_label=str(data.get("yes_label") or "Yes"), no_label=str(data.get("no_label") or "No"),
                payload=data.get("payload"), reference_id=str(data.get("reference_id") or ""),
                expires_at=data.get("expires_at"))
        except (ValueError, sqlite3.IntegrityError) as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._broadcast_artifact(row)
        return self._send(201, json.dumps({"artifact": row}).encode(), "application/json")

    def _handle_decision_resolve(self, decision_id: str):
        from lib import artifacts
        # This is a cooperative consent workflow, not a hostile-code sandbox:
        # local agents already share the host and SQLite file. `resolved_by`
        # records the user-facing API action; skills are required to stop and
        # await it rather than self-resolve.
        data = self._read_json()
        if not isinstance(data, dict): return self._send(400, b'{"error":"json object required"}', "application/json")
        choice = str(data.get("choice") or "")
        choice = choice.strip().lower()
        if choice not in {"accepted", "rejected"}:
            return self._send(400, b'{"error":"invalid decision choice"}', "application/json")
        raw_revision = data.get("expected_revision")
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
            return self._send(400, b'{"error":"expected_revision must be an integer"}',
                              "application/json")
        revision = raw_revision
        try:
            row, changed = artifacts.resolve(
                decision_id, choice=choice, expected_revision=revision)
        except ValueError as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._broadcast_artifact(row)
        self._deliver_pending_decisions()
        return self._send(200, json.dumps({"artifact": row, "changed": changed,
                                           "delivery_pending": artifacts.delivery_pending(decision_id)}).encode(),
                          "application/json")

    def _deliver_pending_decisions(self) -> set[str]:
        from lib import artifacts
        delivered: set[str] = set()
        for pending in artifacts.pending_deliveries():
            decision_id = pending["decision_id"]
            outcome = ("The decision expired without approval. Do not perform the protected action."
                       if pending["choice"] == "expired"
                       else f"the user chose: {pending['choice']}. Continue accordingly and revalidate the action before acting.")
            text = ("[Clarp decision resolved]\n"
                    f"Decision ID: {decision_id}\n"
                    f"Artifact ID: {pending['artifact_id']}\n"
                    f"Question: {pending['question']}\n"
                    f"Context: {pending['context']}\n"
                    f"Reference: {pending['reference_id']}\n"
                    f"Payload: {pending['payload_json']}\n"
                    f"{outcome}")
            try:
                TurnDispatchService(self.ctx).dispatch(
                    text=text, requested_session=pending["session"], trace_id=_trace.new_id(),
                    forced_session=pending["session"],
                    client_msg_id=f"decision-{decision_id}", synthesize_audio=False,
                    origin="automation", queue_if_busy=False)
                artifacts.mark_delivered(decision_id)
                delivered.add(decision_id)
            except Exception as exc:
                log_exception("decisionWakeFail", exc, detail=decision_id)
        return delivered

    def _handle_attention(self):
        from lib import artifacts
        items = artifacts.attention()
        self._deliver_pending_decisions()
        return self._send(200, json.dumps({"items": items, "count": len(items)}).encode(),
                          "application/json")

    def _handle_turn_queue(self):
        from urllib.parse import parse_qs, urlparse
        from lib import agents as agents_db
        from lib import turn_queue
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        agent = agents_db.get_by_session(session)
        if not agent:
            return self._send(404, b'{"error":"agent not found"}', "application/json")
        state = turn_queue.state(agent["agent_id"])
        items = [{
            "id": row["queue_id"],
            "text": row["text"],
            "enqueued_at": row["enqueued_at"],
            "synthesize_audio": bool(row["synthesize_audio"]),
        } for row in turn_queue.pending(agent["agent_id"])]
        return self._send(200, json.dumps({
            "items": items,
            "paused": state["paused"],
            "revision": state["revision"],
        }).encode(), "application/json")

    def _broadcast_turn_queue(self, agent_id: str, session: str) -> None:
        from lib import turn_queue
        if getattr(self.ctx, "stream", None) is None:
            return
        state = turn_queue.state(agent_id)
        self.ctx.stream.broadcast({
            "type": SSEType.QUEUE_UPDATED,
            "session": session,
            "agent_id": agent_id,
            "queue_depth": state["count"],
            "queue_paused": state["paused"],
            "queue_started": False,
            "queue_revision": state["revision"],
        })

    def _handle_turn_queue_update(self, queue_id: str):
        from urllib.parse import unquote
        from lib import turn_queue
        queue_id = unquote(queue_id)
        row = turn_queue.get(queue_id)
        data = self._read_json()
        text = str((data or {}).get("text") or "").strip()
        if data is None or not text:
            return self._send(400, b'{"error":"text required"}', "application/json")
        if not row or not turn_queue.update_text(queue_id, text):
            return self._send(404, b'{"error":"queued message not found"}', "application/json")
        self._broadcast_turn_queue(str(row["agent_id"]), str(row["session"]))
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_turn_queue_delete(self, queue_id: str):
        from urllib.parse import unquote
        from lib import turn_queue
        queue_id = unquote(queue_id)
        row = turn_queue.get(queue_id)
        if not row or not turn_queue.remove(queue_id):
            return self._send(404, b'{"error":"queued message not found"}', "application/json")
        self._broadcast_turn_queue(str(row["agent_id"]), str(row["session"]))
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_turn_queue_send(self, queue_id: str):
        from urllib.parse import unquote
        try:
            result = TurnDispatchService(self.ctx).dispatch_queued(unquote(queue_id))
        except DispatchError as exc:
            return self._send(exc.status, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        return self._send(202, json.dumps({
            "ok": True,
            "session": result.session,
            "queued": result.queued,
            "queue_depth": result.queue_depth,
            "queue_revision": result.queue_revision,
        }).encode(), "application/json")

    def _handle_background_job_cancel(self, job_id: str):
        from urllib.parse import unquote
        from lib import background_jobs
        job, cancellation_changed = background_jobs.cancel_with_result(unquote(job_id))
        if not job:
            return self._send(404, b'{"error":"job not found"}', "application/json")
        if job["status"] != "cancelled":
            return self._send(
                409,
                json.dumps({
                    "error": f"job already {job['status']}", "job": job,
                }).encode(),
                "application/json",
            )
        # Cancellation immediately closes the durable gate checked by workers
        # before each delivery/action. The prompt is cleanup: it lets the owner
        # tear down or reconfigure a shared process, but correctness does not
        # depend on that best-effort dispatch succeeding.
        if cancellation_changed:
            try:
                import shlex
                cancelled_handle = background_jobs.job_handle(job)
                gate_command = (
                    "clarp-agent-bg "
                    f"{shlex.quote(job['session'])} job-cancelled "
                    f"{shlex.quote(cancelled_handle)}"
                )
                worker_identity = (
                    f"pid={job.get('worker_pid') or 'unknown'} "
                    f"start_token={job.get('worker_start_token') or 'unknown'}"
                )
                TurnDispatchService(self.ctx).dispatch(
                    text=(f"the user cancelled background job {job['job_id']}: "
                          f"{job['title']} (handle {cancelled_handle}; "
                          f"{worker_identity}). Before stopping anything, run "
                          f"`{gate_command}`. Proceed only if it returns 0 and "
                          "the target process still matches that exact worker identity; "
                          "otherwise this cleanup was superseded by a newer generation. "
                          "Then clear the short status if no jobs remain."),
                    requested_session=job["session"],
                    forced_session=job["session"],
                    trace_id=_trace.new_id(),
                    synthesize_audio=False,
                    origin="automation",
                )
            except Exception as exc:
                log_exception("backgroundJobCancelPromptFail", exc, detail=job_id)
        return self._send(200, json.dumps({
            "ok": True, "changed": cancellation_changed, "job": job,
        }).encode(),
                          "application/json")

    def _handle_create_agent(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            result = AgentLifecycleService(self.ctx).create(data)
        except AgentLifecycleError as e:
            # Log the rejection reason so a failed start is never invisible —
            # greppable as `agentCreateRejected` alongside the POST line.
            log("agentCreateRejected",
                f"status={e.status} code={e.code} :: {e.message}")
            return self._send(e.status, json.dumps(e.response()).encode(),
                              "application/json")
        return self._send(200, json.dumps({
            "ok": True, "session": result.session, "name": result.persona,
        }).encode(), "application/json")

    def _handle_create_persona(self):
        from lib import personas
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        try:
            row = personas.create(
                name=str(data.get("name") or ""),
                voice_id=str(data.get("voice_id") or ""),
                avatar_symbol=str(data.get("avatar_symbol") or ""),
                personality=str(data.get("personality") or ""),
                avatar_base64=str(data.get("avatar_base64") or ""),
            )
        except (ValueError, OSError) as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self.ctx.stream.broadcast({"type": SSEType.AGENT_ROSTER, "kind": "persona-created"})
        return self._send(201, json.dumps({"persona": personas.public(row)}).encode(),
                          "application/json")

    def _handle_update_persona(self):
        from lib import personas
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
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
            return self._send(409, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        self.ctx.stream.broadcast({"type": SSEType.AGENT_ROSTER, "kind": "persona-updated"})
        return self._send(200, json.dumps({"persona": personas.public(row)}).encode(),
                          "application/json")

    def _handle_delete_persona(self, name: str):
        from urllib.parse import unquote
        from lib import personas
        try:
            removed = personas.delete(unquote(name))
        except ValueError as exc:
            return self._send(409, json.dumps({"error": str(exc)}).encode(),
                              "application/json")
        if not removed:
            return self._send(404, b'{"error":"personality not removable"}',
                              "application/json")
        self.ctx.stream.broadcast({"type": SSEType.AGENT_ROSTER, "kind": "persona-deleted"})
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_persona_avatar(self, persona_id: str):
        from lib import personas
        row = next((p for p in personas.list_all() if p["persona_id"] == persona_id), None)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        return self._send_file(path) if row and path.is_file() else self._send(404, b"not found")


    def _handle_delete_agent(self, session: str):
        from lib import turn_dispatch
        agent = agents_db.get_by_session(session.strip("/"))
        if agent:
            # Invalidate dispatch state before delete interrupts the backend;
            # its terminal callback must not drain queued work after deletion.
            turn_dispatch.clear_for_agent(agent["agent_id"])
        try:
            AgentLifecycleService(self.ctx).delete(session)
        except AgentLifecycleError as e:
            return self._send(e.status, json.dumps(e.response()).encode(),
                              "application/json")
        return self._send(200, b'{"ok":true}', "application/json")

    def _handle_clog(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        from lib import diagnostics_settings
        if not diagnostics_settings.accepts_client_uploads():
            return self._send(
                200, b'{"ok":true,"captured":false}', "application/json")
        # Accept either a single event {event, detail, ...} or a batch
        # {events: [...]}. Batched form keeps the PWA from spamming one
        # request per state transition.
        items = data.get("events") if isinstance(data.get("events"), list) else [data]

        # Legacy line-based file (humans still read it) — single combined append.
        log_path = pathlib.Path(os.environ.get(
            "CLAUDE_PWA_CLIENT_LOG",
            str(RuntimePaths.from_home(pathlib.Path.home()).hook_log),
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
        self._send(200, b'{"ok":true}', "application/json")

    def _handle_select(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = (data.get("session") or "").strip()
        if not session or not all(c.isalnum() or c in "._-" for c in session):
            return self._send(400, b'{"error":"bad session name"}', "application/json")
        from lib import agents as agents_db
        if not agents_db.get_by_session(session):
            return self._send(404, b'{"error":"no such session"}', "application/json")
        # Persist focus in the DB and in the small focus-state file read by
        # audio routing.
        from lib import agents as agents_db
        agent = agents_db.get_by_session(session)
        herald = getattr(self.ctx, "herald", None)
        if agent and herald is not None:
            try:
                with agents_db.focus_guard():
                    agents_db.set_focus(agent["agent_id"])
                    herald.set_focus(session)
            except Exception as e:  # noqa: BLE001
                log_exception("focusDbWriteFail", e, detail=session)
        elif agent:
            try:
                agents_db.set_focus(agent["agent_id"])
            except Exception as e:  # noqa: BLE001
                log_exception("focusDbWriteFail", e, detail=session)
        state_path = RuntimePaths.from_home(pathlib.Path.home()).app_session
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(session + "\n")
        except OSError as e:
            log_exception("selectStateWriteFail", e, detail=str(state_path))
            return self._send(500, b'{"error":"could not persist selection"}',
                              "application/json")
        # Tell the herald manager the user's focus changed — if that agent
        # had a pending herald, their held buffer drains now.
        # Push focus change so the client doesn't have to poll for it.
        self.ctx.stream.broadcast({
            "type": SSEType.AGENT_FOCUS,
            "session": session,
            "agent_id": (agent or {}).get("agent_id"),
        })
        self._send(200, json.dumps({"ok": True, "session": session}).encode(),
                   "application/json")

    def _handle_orchestrator_route_delegation(self):
        self._orchestrator_fallback_request = True
        return self._handle_send()

    def _handle_send(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b"bad json")
        text = (data.get("text") or "").strip()
        session = (data.get("session") or self.ctx.default_session).strip() or self.ctx.default_session
        trace_id = (data.get("trace_id") or "").strip() or _trace.new_id()
        self._trace_id = trace_id
        # Stable client-authored message id (idempotency key). The client keys
        # its optimistic bubble by it; we store the durable user row under it so
        # the two match by identity. A client that sends none gets the trace id.
        client_msg_id = (data.get("client_msg_id") or "").strip() or trace_id
        transcription_id = (data.get("transcription_id") or "").strip()
        if transcription_id:
            from lib import transcription_results
            try:
                transcription_id = transcription_results.normalize_job_id(
                    transcription_id)
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(),
                                  "application/json")
        synthesize_audio_raw = data.get("synthesize_audio", None)
        hands_free = data.get("hands_free", False) is True
        unheard_audio_sessions_raw = data.get("unheard_audio_sessions") or []
        unheard_audio_sessions = tuple(dict.fromkeys(
            str(value).strip()
            for value in unheard_audio_sessions_raw[:64]
            if isinstance(value, str) and str(value).strip()
        )) if isinstance(unheard_audio_sessions_raw, list) else ()
        orchestrator_fallback = (
            getattr(self, "_orchestrator_fallback_request", False)
            or data.get("orchestrator_fallback", False) is True
        )
        queue_if_busy = data.get("queue_if_busy", False) is True
        # Only always-on hands-free dictation gets routed (orchestrator / spoken
        # name). Tap-to-record and typed text (hands_free=false) always go
        # straight to the agent the client has open — no routing, no exceptions.
        force_session = (data.get("force_session", False) is True) or (not hands_free)
        # Sender identity: when another agent prompts this one, `sender` names
        # it (session or agent_id). The message is then stamped origin=agent so
        # the client renders it with the sender's avatar instead of as the user.
        sender_raw = (data.get("sender") or "").strip()
        sender_agent_id = ""
        if sender_raw:
            sender = (agents_db.get_by_session(sender_raw)
                      or agents_db.get_by_agent_id(sender_raw))
            if sender:
                sender_agent_id = sender["agent_id"]
        origin = (data.get("origin") or "").strip().lower()
        if origin not in origins.CLIENT_SETTABLE_ORIGINS:
            origin = "agent" if sender_agent_id else "user"
        if synthesize_audio_raw is None:
            synthesize_audio = origin == "user"
        else:
            synthesize_audio = (
                synthesize_audio_raw is not False
                if origin == "user"
                else synthesize_audio_raw is True
            )
        from lib import prompt_admissions
        prompt_admission = prompt_admissions.create(
            authenticated_at_admission=bool(
                getattr(self, "_request_auth_validated", False)
                and (getattr(self.ctx, "auth_token", "") or "")
            ),
            origin=origin,
            sender_agent_id=sender_agent_id,
            channel="voice" if transcription_id else "chat",
            observed_at=db.now_ms(),
            client_admission_id=client_msg_id,
            trace_id=trace_id,
            original_text=text,
        )
        if not text:
            return self._send(400, b"empty text")
        if not force_session:
            orchestrated = OrchestratorService(self.ctx).handle_send(
                text=text,
                requested_session=session,
                trace_id=trace_id,
                prompt_admission=prompt_admission,
                hands_free=hands_free,
                synthesize_audio=synthesize_audio,
                unheard_audio_sessions=unheard_audio_sessions,
                dispatch=TurnDispatchService(self.ctx).dispatch,
                fallback_request=orchestrator_fallback,
            )
            if orchestrated is None and orchestrator_fallback:
                body = {
                    "ok": True,
                    "session": "",
                    "dispatch": "",
                    "trace_id": trace_id,
                    "orchestrator": {
                        "action": "disabled",
                        "decision_id": None,
                        "decision": None,
                    },
                }
                if transcription_id:
                    transcription_results.delete(transcription_id)
                return self._send(200, json.dumps(body).encode(), "application/json")
            if orchestrated is not None and (
                orchestrated.action != FINAL_FALLBACK or orchestrator_fallback
            ):
                status = orchestrated.status
                body = {
                    "ok": orchestrated.ok,
                    "session": orchestrated.session,
                    "dispatch": orchestrated.dispatch,
                    "trace_id": trace_id,
                    "orchestrator": {
                        "action": orchestrated.action,
                        "decision_id": orchestrated.decision_id,
                        "decision": orchestrated.decision,
                    },
                }
                if orchestrated.error:
                    body["error"] = orchestrated.error
                if orchestrated.ok and transcription_id:
                    transcription_results.delete(transcription_id)
                return self._send(status, json.dumps(body).encode(), "application/json")
        try:
            result = TurnDispatchService(self.ctx).dispatch(
                text=text, requested_session=session, trace_id=trace_id,
                synthesize_audio=synthesize_audio,
                forced_session=session if force_session else "",
                client_msg_id=client_msg_id,
                origin=origin, sender_agent_id=sender_agent_id,
                prompt_admission=prompt_admission,
                queue_if_busy=queue_if_busy,
                unheard_audio_sessions=unheard_audio_sessions,
            )
        except DispatchError as e:
            return self._send(e.status, str(e).encode(), "text/plain")
        if transcription_id:
            transcription_results.delete(transcription_id)
        self._send(200, json.dumps({"ok": True, "session": result.session,
                                    "dispatch": result.backend,
                                    "queued": result.queued,
                                    "queue_depth": result.queue_depth,
                                    "queue_revision": result.queue_revision,
                                    "trace_id": trace_id}).encode(),
                   "application/json")

    def _handle_clip_ack(self):
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        status = (data.get("status") or "").strip()
        clip_id = data.get("clip_id")
        try:
            clip_id = int(clip_id) if clip_id not in (None, "") else None
        except (TypeError, ValueError):
            return self._send(400, b'{"error":"bad clip_id"}', "application/json")
        url = (data.get("url") or "").strip()
        error = (data.get("error") or "").strip() or None
        trace_id = (data.get("trace_id") or None)
        self._trace_id = trace_id
        try:
            ok = agents_db.mark_clip_status(
                clip_id=clip_id,
                url=url,
                status=status,
                error=error,
            )
        except ValueError:
            return self._send(400, b'{"error":"bad status"}', "application/json")
        eventlog.emit("client", "clipAck",
                      trace_id=trace_id,
                      clip_id=clip_id,
                      clip_url=url or None,
                      detail={"clip_id": clip_id, "status": status,
                              "updated": ok, "error": error})
        self._send(200, json.dumps({"ok": True, "updated": ok}).encode(),
                   "application/json")

    def _handle_recoverable_clips(self):
        from urllib.parse import parse_qs, urlparse
        from lib import clip_store
        query = parse_qs(urlparse(self.path).query)
        session = str(query.get("session", [""])[0] or "").strip()
        events = clip_store.recoverable_events(session=session)
        self._send(
            200,
            json.dumps({"events": events}).encode(),
            "application/json",
        )

    def _handle_transcribe(self):
        from lib import transcription_results

        requested_model = (self.headers.get("X-Transcription-Model") or "").strip()
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError as e:
            log_exception("transcribeReadFail", e)
            return self._send(400, f'{{"error":"{e}"}}'.encode(), "application/json")
        if n <= 0 or n > 25 * 1024 * 1024:  # cap at 25 MB per clip
            return self._send(400, b'{"error":"bad size"}', "application/json")
        # Once the body read starts, a disconnect means there is nothing
        # useful left to drain before responding.  BufferedReader.read(n)
        # returns fewer than n bytes at EOF, so validate the count before
        # fingerprinting: caching a truncated upload under the durable job ID
        # poisons every later retry with a false collision.
        self._body_consumed = True
        try:
            audio_bytes = self.rfile.read(n)
        except OSError as e:
            self.close_connection = True
            log_exception("transcribeReadFail", e)
            return self._send(
                408, b'{"error":"incomplete audio upload"}',
                "application/json")
        if len(audio_bytes) != n:
            log("transcribeReadIncomplete",
                f"expected={n} received={len(audio_bytes)}")
            self.close_connection = True
            return self._send(
                408, b'{"error":"incomplete audio upload"}',
                "application/json")

        ctype = self.headers.get("Content-Type", "audio/webm")
        hands_free = _truthy_header(self.headers.get("X-Hands-Free"))
        try:
            transcription_id = transcription_results.normalize_job_id(
                self.headers.get("X-Transcription-ID"))
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        fingerprint = transcription_results.request_fingerprint(
            audio_bytes, ctype, requested_model, hands_free)

        # A retry can arrive while the original request is still finishing.
        # Serialize equal IDs, then consult the durable result before invoking
        # Whisper so a lost HTTP response never causes duplicate inference.
        with transcription_results.serialize(transcription_id):
            try:
                cached = transcription_results.load(transcription_id, fingerprint)
            except transcription_results.JobIDCollisionError as e:
                return self._send(409, json.dumps({"error": str(e)}).encode(),
                                  "application/json")
            if cached is not None:
                cached["cached"] = True
                return self._send(200, json.dumps(cached).encode(),
                                  "application/json")
            return self._transcribe_uncached(
                audio_bytes, ctype, hands_free, requested_model,
                transcription_id, fingerprint)

    def _transcribe_uncached(self, audio_bytes, ctype, hands_free,
                             requested_model, transcription_id, fingerprint):
        from lib import transcription_results

        if getattr(self.ctx.stt, "available", True) is False:
            return self._send(503, b'{"error":"server transcription disabled"}',
                              "application/json")
        if not requested_model:
            # A cloud engine chosen in settings stands in for the server
            # default; an explicit header from the client still wins.
            try:
                from lib import stt_providers
                engine = stt_providers.selected_engine()
                if stt_providers.is_cloud_model(engine):
                    requested_model = engine
            except Exception as e:  # noqa: BLE001
                log_exception("sttEngineSettingFail", e)
        if not requested_model and not self.ctx.stt.ready.is_set():
            return self._send(503, b'{"error":"whisper model loading"}',
                              "application/json")
        # The trace is minted before compiling so the vocab run, the
        # transcribe event and everything downstream share one id.
        trace_id = _trace.new_id()
        self._trace_id = trace_id
        try:
            focus = RuntimePaths.from_home(pathlib.Path.home()).app_session.read_text().strip()
        except OSError:
            focus = ""
        vocab_run_id = 0
        vocab_fn = getattr(self.ctx, "vocab_for_transcription", None)
        if callable(vocab_fn):
            try:
                vocab = vocab_fn(delegated=hands_free, session=focus,
                                 trace_id=trace_id,
                                 requested_model=requested_model)
                prompt, vocab_run_id = vocab.payload, vocab.run_id
            except Exception as e:  # noqa: BLE001 - biasing never blocks STT
                log_exception("vocabForTranscribeFail", e)
                prompt = self.ctx.vocab_prompt(delegated=hands_free)
        else:
            prompt = self.ctx.vocab_prompt(delegated=hands_free)
        started = time.monotonic()
        try:
            # Authoritative transcript: wait for the whisper lock rather than
            # 429 — best-effort live-transcription partials must yield to it.
            model_transcribe = getattr(self.ctx.stt, "transcribe_model_bytes", None)
            if requested_model and callable(model_transcribe):
                text, ends_terminal, _dur = model_transcribe(
                    requested_model, audio_bytes, ctype, prompt, wait=10.0)
            elif requested_model and requested_model != "server-default":
                raise STTUnknownModelError(
                    f"transcription model not installed: {requested_model}")
            else:
                text, ends_terminal, _dur = self.ctx.stt.transcribe_bytes(
                    audio_bytes, ctype, prompt, wait=10.0)
            health.mark_success("stt")
        except STTUnknownModelError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        except STTModelLoadingError as e:
            return self._send(503, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        except STTBusyError:
            health.mark_error("stt", "busy")
            return self._send(429, b'{"error":"whisper busy"}',
                              "application/json")
        except Exception as e:
            health.mark_error("stt", e)
            log_exception("transcribeFail", e)
            return self._send(500, f'{{"error":"{e}"}}'.encode(), "application/json")
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            from lib import heard_audio
            heard_audio.retain(
                RuntimePaths.from_home(pathlib.Path.home()).cache_dir,
                trace_id=trace_id, audio_bytes=audio_bytes, content_type=ctype,
                session=focus, run_id=vocab_run_id, model=requested_model or "")
        except Exception as e:  # noqa: BLE001 - diagnostics never block a turn
            log_exception("heardAudioFail", e)
        if vocab_run_id:
            try:
                from lib import vocab_store
                vocab_store.update_run_result(
                    vocab_run_id, transcript=text, latency_ms=latency_ms)
            except Exception as e:  # noqa: BLE001
                log_exception("vocabRunUpdateFail", e)

        # Run the user's utterance against any pending heralds. Affirmatives
        # with a name release that agent's held buffer; mentions / declines
        # leave the buffer intact.
        herald = getattr(self.ctx, "herald", None)
        herald_consumed = False
        # Deterministic herald grants/declines must run before LLM routing.
        # If the orchestrator is unavailable, this preserves the old
        # regex/fuzzy "Yes, Bella" fallback instead of dispatching the grant
        # phrase as an agent message.
        skip_herald = False
        if herald is not None and text:
            try:
                decision = herald.on_user_text(text)
                # A grant/decline ("Yes Domi?") is a COMMAND, not a prompt —
                # it releases (or holds) the agent's buffer. Mark it consumed so
                # we don't also dispatch it to the agent (which made Domi reply
                # to "Yes Domi?" itself).
                if decision and (decision.granted or decision.declined):
                    herald_consumed = True
            except Exception as e:
                log_exception("heraldIntentFail", e)

        # The client echoes the trace id back in subsequent /send + /clog
        # calls so the whole turn — STT, send, hook, Claude reply, TTS,
        # broadcast, play — carries one queryable id.
        if focus:
            agents_db.set_trace_for_session(focus, trace_id)
        eventlog.emit("server", "transcribe", trace_id=trace_id, session=focus or None,
                      duration_ms=int(_dur * 1000) if _dur else None,
                      detail={"text": text, "ends_terminal": ends_terminal,
                              "herald_consumed": herald_consumed,
                              "hands_free": hands_free,
                              "orchestrator_skip_herald": skip_herald,
                              "vocab_run_id": vocab_run_id or None,
                              "stt_latency_ms": latency_ms})

        # Blank the text when the utterance was a herald grant/decline so the
        # client's empty-text guard skips dispatch — it released the buffer,
        # it isn't a message for the agent.
        reply_text = "" if herald_consumed else text
        response = {"text": reply_text, "ends_terminal": ends_terminal,
                    "trace_id": trace_id,
                    "herald_consumed": herald_consumed,
                    "hands_free": hands_free,
                    "orchestrator_skip_herald": skip_herald,
                    "vocab_run_id": vocab_run_id or None,
                    "cached": False}
        try:
            transcription_results.store(
                transcription_id, fingerprint, response)
        except transcription_results.JobIDCollisionError as e:
            return self._send(409, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        except Exception as e:
            log_exception("transcriptionResultStoreFail", e)
            return self._send(500, b'{"error":"transcription result persistence failed"}',
                              "application/json")
        body = json.dumps(response).encode()
        self._send(200, body, "application/json")

    def _handle_upload(self):
        """Receive a raw file body from a client (e.g. an image picked on the
        phone) and save it under this server's per-session uploads dir. Returns
        the absolute path so the client can drop it into the prompt — agents
        run with --dangerously-skip-permissions, so they read it directly."""
        from urllib.parse import unquote
        from lib import upload_results
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 50 * 1024 * 1024:  # cap at 50 MB per upload
                return self._send(400, b'{"error":"bad size"}', "application/json")
            blob = self.rfile.read(n)
            self._body_consumed = True
        except (ValueError, OSError) as e:
            log_exception("uploadReadFail", e)
            return self._send(400, f'{{"error":"{e}"}}'.encode(), "application/json")
        if len(blob) != n:
            self.close_connection = True
            return self._send(
                408, b'{"error":"incomplete upload"}', "application/json")

        # Which agent/session this attaches to: explicit header, then the
        # focused session, then the default — same precedence the rest of the
        # API uses.
        session = (self.headers.get("X-Session") or "").strip()
        if not session:
            try:
                session = RuntimePaths.from_home(
                    pathlib.Path.home()).app_session.read_text().strip()
            except OSError:
                session = ""
        if not session:
            session = self.ctx.default_session

        name = _safe_upload_name(
            unquote((self.headers.get("X-File-Name") or "").strip()),
            self.headers.get("Content-Type", ""),
        )
        base = self.ctx.uploads_dir or RuntimePaths.from_home(
            pathlib.Path.home()).uploads_dir
        dest_dir = base / _safe_session(session)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            upload_id = (self.headers.get("X-Upload-ID") or "").strip()
            if upload_id:
                dest = upload_results.store(
                    session_dir=dest_dir, upload_id=upload_id, name=name,
                    content_type=self.headers.get("Content-Type", ""), blob=blob,
                    record_root=base)
            else:
                # No idempotency key: every request stores a new file.
                dest = dest_dir / f"{secrets.token_hex(4)}-{name}"
                dest.write_bytes(blob)
        except upload_results.UploadIDCollisionError as e:
            return self._send(409, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        except OSError as e:
            log_exception("uploadWriteFail", e)
            return self._send(500, f'{{"error":"{e}"}}'.encode(), "application/json")

        eventlog.emit("server", "upload", session=session or None,
                      detail={"name": name, "size": len(blob), "path": str(dest)})
        body = json.dumps({"ok": True, "path": str(dest), "name": name,
                           "size": len(blob), "session": session}).encode()
        self._send(200, body, "application/json")

    def _handle_media_publish(self):
        """Publish an agent-produced image or allowlisted artifact file.

        Agents call this via the clarp-media helper. The server performs the
        two-phase work (blob copy + SQLite row) so agents never have to update
        both a directory and the DB themselves.
        """
        from urllib.parse import unquote
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > media_store.MAX_MEDIA_BYTES:
                return self._send(400, b'{"error":"bad size"}', "application/json")
            blob = self.rfile.read(n)
            self._body_consumed = True
            if len(blob) != n:
                return self._send(400, b'{"error":"incomplete body"}', "application/json")
        except (ValueError, OSError) as e:
            log_exception("mediaReadFail", e)
            return self._send(400, json.dumps({"error": str(e)}).encode(),
                              "application/json")

        session = (self.headers.get("X-Session") or "").strip()
        if not session:
            session = self.ctx.default_session
        name = unquote((self.headers.get("X-File-Name") or "").strip())
        caption = unquote((self.headers.get("X-Caption") or "").strip())
        created_by = (self.headers.get("X-Created-By") or "agent").strip()
        try:
            asset = media_store.publish(
                session=session,
                blob=blob,
                source_name=name,
                content_type=self.headers.get("Content-Type", ""),
                caption=caption,
                created_by=created_by,
                media_dir=self.ctx.media_dir or RuntimePaths.from_home(
                    pathlib.Path.home()).media_dir,
            )
        except media_store.MediaError as e:
            return self._send(e.status, json.dumps({"error": str(e)}).encode(),
                              "application/json")
        except OSError as e:
            log_exception("mediaWriteFail", e)
            return self._send(500, json.dumps({"error": str(e)}).encode(),
                              "application/json")

        eventlog.emit("server", "mediaPublish", session=session or None,
                      detail={"asset_id": asset["asset_id"],
                              "name": asset["source_name"],
                              "size": asset["bytes"]})
        body = json.dumps({"ok": True, "asset": asset, **asset}).encode()
        self._send(200, body, "application/json")

    def _handle_media_list(self):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        session = (qs.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        try:
            limit = int(qs.get("limit", ["100"])[0] or 100)
        except ValueError:
            limit = 100
        assets = media_store.list_for_session(session, limit=limit)
        self._send(200, json.dumps({"session": session, "assets": assets}).encode(),
                   "application/json")

    def _handle_media_content(self, asset_id: str):
        if not asset_id or "/" in asset_id or "." in pathlib.PurePath(asset_id).parts:
            return self._send(400, b"bad asset id")
        row = media_store.get(asset_id)
        if not row:
            return self._send(404, b"not found")
        path = pathlib.Path(row["storage_path"]).resolve()
        root = (self.ctx.media_dir or RuntimePaths.from_home(
            pathlib.Path.home()).media_dir).resolve()
        if not path.is_file() or (path != root and root not in path.parents):
            return self._send(404, b"not found")
        return self._send_file(path, str(row["mime_type"]), secure=True)

    def _handle_agent_portraits_list(self):
        from urllib.parse import parse_qs, urlparse
        from lib import agent_portraits
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or RuntimePaths.from_home(
            pathlib.Path.home()).media_dir
        try:
            result = agent_portraits.list_for_session(
                session, portrait_dir=portrait_dir)
        except agent_portraits.PortraitError as exc:
            return self._send(
                exc.status, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        self._send(200, json.dumps(result).encode(), "application/json")

    def _handle_agent_portraits_update(self):
        from lib import agent_portraits
        data = self._read_json()
        if data is None:
            return self._send(400, b'{"error":"bad json"}', "application/json")
        action = str(data.get("action") or "").strip()
        session = str(data.get("session") or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or RuntimePaths.from_home(
            pathlib.Path.home()).media_dir
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
            return self._send(
                exc.status, json.dumps({"error": str(exc)}).encode(),
                "application/json")
        if changed:
            self.ctx.stream.broadcast({
                "type": SSEType.AGENT_ROSTER,
                "kind": "portrait-selected",
                "session": session,
            })
        self._send(200, json.dumps(result).encode(), "application/json")

    def _handle_agent_portrait_generation_status(self):
        from urllib.parse import parse_qs, urlparse
        from lib import portrait_generation
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or RuntimePaths.from_home(
            pathlib.Path.home()).media_dir
        result = portrait_generation.capability(
            session, media_dir=portrait_dir)
        self._send(200, json.dumps(result).encode(), "application/json")

    def _handle_agent_portrait_generation_start(self):
        from lib import portrait_generation
        data = self._read_json()
        if data is None or not isinstance(data, dict):
            return self._send(400, b'{"error":"bad json"}', "application/json")
        session = str(data.get("session") or self.ctx.default_session).strip()
        portrait_dir = self.ctx.media_dir or RuntimePaths.from_home(
            pathlib.Path.home()).media_dir
        try:
            result = portrait_generation.start(session, media_dir=portrait_dir)
        except portrait_generation.GenerationError as exc:
            return self._send(
                409, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._send(202, json.dumps(result).encode(), "application/json")

    def _handle_agent_portrait_content(self, portrait_id: str):
        from urllib.parse import unquote
        from lib import agent_portraits
        if not portrait_id or "/" in portrait_id:
            return self._send(400, b"bad portrait id")
        row = agent_portraits.get_content(unquote(portrait_id))
        path = pathlib.Path(str((row or {}).get("storage_path") or ""))
        if not row or not path.is_file():
            return self._send(404, b"not found")
        return self._send_file(path)


def _safe_upload_name(raw: str, content_type: str = "") -> str:
    """Reduce a client-supplied filename to a safe basename, preserving the
    extension so the agent's Read tool recognises images. Blocks path
    traversal (no separators / leading dots survive); falls back to a
    type-derived name when nothing usable is sent."""
    base = os.path.basename((raw or "").replace("\\", "/")).strip()
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ()")
    cleaned = cleaned.strip().replace(" ", "_").lstrip(".")
    primary_type = content_type.split(";", 1)[0].strip()
    if not cleaned:
        ext = mimetypes.guess_extension(primary_type) or ".bin"
        cleaned = "upload" + ext
    elif "." not in cleaned:
        cleaned += mimetypes.guess_extension(primary_type) or ""
    return cleaned[:128]


def _deliver_decision_rows(ctx: ServerContext) -> None:
    from lib import artifacts
    for pending in artifacts.pending_deliveries():
        decision_id = pending["decision_id"]
        outcome = ("The decision expired without approval. Do not perform the protected action."
                   if pending["choice"] == "expired"
                   else f"the user chose: {pending['choice']}. Continue accordingly and revalidate the action before acting.")
        text = ("[Clarp decision resolved]\n"
                f"Decision ID: {decision_id}\nArtifact ID: {pending['artifact_id']}\n"
                f"Question: {pending['question']}\nContext: {pending['context']}\n"
                f"Reference: {pending['reference_id']}\nPayload: {pending['payload_json']}\n{outcome}")
        try:
            TurnDispatchService(ctx).dispatch(
                text=text, requested_session=pending["session"],
                forced_session=pending["session"], trace_id=_trace.new_id(),
                client_msg_id=f"decision-{decision_id}", synthesize_audio=False,
                origin="automation", queue_if_busy=False)
            artifacts.mark_delivered(decision_id)
        except Exception as exc:
            log_exception("decisionWakeFail", exc, detail=decision_id)


def build_server(ctx: ServerContext, port: int,
                 bind_addr: str | None = None, *,
                 restart_recovery: bool = False) -> ContextHTTPServer:
    """Wire a fully-injected HTTP server. Tests use this with a fake ctx."""
    # Persona definitions are a startup-owned invariant.  Historically they
    # were materialized lazily by the first persona/snapshot read, which made
    # zero-session definitions depend on endpoint call order.  Initialize them
    # before request threads start so every read model remains read-only.
    from lib import personas
    personas.ensure_builtins()

    listener_addr = bind_addr or BIND_ADDR
    if listener_addr not in {"127.0.0.1", "::1", "localhost"} and not ctx.auth_token:
        log("unsafeNetworkConfig",
            f"bind={listener_addr} has no auth token; restrict the listener or enable auth")
    herald = getattr(ctx, "herald", None)
    ctx.stream.start()
    srv = ContextHTTPServer((listener_addr, port), Handler, ctx)
    srv.on_close(ctx.stream.stop)
    if getattr(_CFG, "network_advertise_lan", False):
        from lib.bonjour import BonjourAdvertiser
        from lib.server_identity import get_server_info
        server_info = get_server_info()
        bonjour = BonjourAdvertiser(
            name=str(server_info["name"]),
            server_id=str(server_info["server_id"]),
            port=port, auth_required=bool(ctx.auth_token))
        if bonjour.start():
            srv.on_close(bonjour.stop)
    # Tail state_log + push every new row as an `agent-state` SSE event.
    from lib.state_watcher import StateLogWatcher
    watcher = StateLogWatcher(ctx.stream)
    watcher.start()
    srv.on_close(watcher.stop)
    from lib.background_job_watcher import BackgroundJobWatcher
    background_job_watcher = BackgroundJobWatcher(ctx.stream)
    background_job_watcher.start()
    srv.on_close(background_job_watcher.stop)
    # TTS worker: drains tts_queue, does the ElevenLabs call from the
    # server process (not from short-lived hook subprocesses). The hooks
    # only write queue rows; this is the executor side.
    from lib.tts_worker import TTSWorker
    from lib.paths import RuntimePaths as _RP
    from lib.clip_delivery import build_from_config, DeliveryDeps
    _paths = _RP.from_home(pathlib.Path.home())
    # The delivery is what decides how clip bytes reach the client
    # (chunked-file + broker today; hls tomorrow). Config knob:
    # [audio] delivery = "hls" (or CLAUDE_PWA_DELIVERY=hls).
    delivery = build_from_config(
        _CFG, deps=DeliveryDeps(broker=ctx.clip_broker),
    )
    tts_worker = TTSWorker(
        audio_dir=_paths.audio_dir,
        stream=ctx.stream,
        herald=herald,
        broker=ctx.clip_broker,
        delivery=delivery,
    )
    tts_worker.start()
    srv.on_close(tts_worker.stop)
    from lib.maintenance import MaintenanceWorker
    maintenance_worker = MaintenanceWorker(audio_dir=_paths.audio_dir)
    maintenance_worker.start()
    srv.on_close(maintenance_worker.stop)
    from lib.resource_telemetry import ResourceTelemetryWorker
    resource_telemetry = ResourceTelemetryWorker()
    resource_telemetry.start()
    srv.on_close(resource_telemetry.stop)
    decision_stop = threading.Event()
    def _decision_loop() -> None:
        from lib import artifacts
        while not decision_stop.wait(5):
            try:
                artifacts.attention()  # materializes expirations + deliveries
                _deliver_decision_rows(ctx)
            except Exception as exc:
                log_exception("decisionDeliveryWorkerFail", exc)
    decision_thread = threading.Thread(
        target=_decision_loop, daemon=True, name="decision-delivery")
    decision_thread.start()
    srv.on_close(decision_stop.set)
    # Transcript streamer: tails ~/.claude/projects/.../<uuid>.jsonl via
    # inotify for every live agent. As Claude finishes each text block,
    # the streamer enqueues it in tts_queue — the same queue the Stop
    # hook uses — so audio starts synthesizing before the turn ends.
    # Lifecycle is auto-managed: it reconciles with the agents/runtimes
    # tables every second and binds/unbinds inotify watches accordingly.
    from lib.transcript_streamer import TranscriptStreamer
    transcript_streamer = TranscriptStreamer(stream=ctx.stream)
    transcript_streamer.start()
    srv.on_close(transcript_streamer.stop)
    # Autonomous team-leader loop: periodically nudges idle team leaders to
    # review and unstick stalled teammates. Only fires when there's something to
    # do (a stalled member or unread team activity) — an idle team costs nothing.
    from lib.team_leader import TeamLeaderScheduler

    def _send_leader_tick(session: str, text: str) -> None:
        TurnDispatchService(ctx).dispatch(
            text=text, requested_session=session, trace_id=_trace.new_id(),
            synthesize_audio=False, forced_session=session, origin="leader_tick",
        )

    leader_scheduler = TeamLeaderScheduler(send_tick=_send_leader_tick)
    leader_scheduler.start()
    srv.on_close(leader_scheduler.stop)
    # Per-agent heartbeat loop: opt-in and idle-only. It sends a hidden
    # heartbeat prompt; HEARTBEAT_OK replies are suppressed by message_store.
    from lib.heartbeat import HeartbeatScheduler

    def _send_agent_heartbeat(session: str, text: str) -> None:
        agent = agents_db.get_by_session(session)
        if not agent or agents_db.is_busy(agent["agent_id"]):
            return
        TurnDispatchService(ctx).dispatch(
            text=text, requested_session=session, trace_id=_trace.new_id(),
            synthesize_audio=False, forced_session=session, origin="heartbeat",
        )

    heartbeat_scheduler = HeartbeatScheduler(send_heartbeat=_send_agent_heartbeat)
    heartbeat_scheduler.start()
    srv.on_close(heartbeat_scheduler.stop)
    from lib.dreaming import DreamingScheduler

    def _send_agent_dream(session: str, text: str) -> bool:
        from lib import compaction
        from lib import dreaming
        agent = agents_db.get_by_session(session)
        if (not agent or agents_db.is_busy(agent["agent_id"])
                or backends.active_handles(agent.get("backend"), agent["agent_id"])
                or compaction.is_compacting(session)):
            return False
        return dreaming.dispatch_isolated_dream(agent, text)

    dreaming_scheduler = DreamingScheduler(send_dream=_send_agent_dream)
    dreaming_scheduler.start()
    srv.on_close(dreaming_scheduler.stop)

    from lib.scheduler import AgentScheduleRunner

    def _send_scheduled_job(session: str, text: str) -> None:
        agent = agents_db.get_by_session(session)
        if not agent:
            return
        TurnDispatchService(ctx).dispatch(
            text=text, requested_session=session, trace_id=_trace.new_id(),
            synthesize_audio=False, forced_session=session, origin="automation",
        )

    schedule_runner = AgentScheduleRunner(dispatch_turn=_send_scheduled_job)
    schedule_runner.start()
    srv.on_close(schedule_runner.stop)

    broadcast_boot_version(ctx)
    resume_persisted_agents(ctx)
    if restart_recovery:
        # Mark the turns the previous process took down with it before the
        # restart heartbeat asks the agents to carry on (issue #11).
        from lib import interrupted_turns
        interrupted = interrupted_turns.recover_after_restart(
            stream=getattr(ctx, "stream", None))
        if interrupted:
            log("turnRestartRecovery", f"marked={len(interrupted)}")
        restart_heartbeats = heartbeat_scheduler.run_restart_recovery_once()
        if restart_heartbeats:
            log("heartbeatRestartRecovery", f"sent={restart_heartbeats}")
    recovered_queues = TurnDispatchService(ctx).recover_queued()
    if recovered_queues:
        log("queuedRecovery", f"recovered={recovered_queues}")
    return srv


if __name__ == "__main__":
    from lib.herald import HeraldManager
    from lib import reconcile
    # Boot-time reconciliation (INV1-INV3, lib.reconcile): a server killed
    # mid-turn leaves busy state rows, ghost session bindings and phantom
    # in-flight slots behind. Re-derive from reality (no process survives a
    # restart) so the app never renders phantom "working" badges and no agent
    # resumes a session that doesn't exist. The same pass runs per agent on
    # every snapshot read.
    repaired = reconcile.reconcile_all()
    if repaired:
        log("bootReconcile", f"repaired {repaired} agent(s)")
    prod_ctx = ServerContext.production()
    # Wire the herald manager so off-focus agents raise their hand.
    prod_ctx.herald = HeraldManager(  # type: ignore[attr-defined]
        stream=prod_ctx.stream, tts=prod_ctx.tts,
        agents=lambda: load_agents(prod_ctx.agents_path),
        # Single source of truth: the herald reads focus live from the DB, so it
        # can never drift from the real focus (no set_focus to keep in sync).
        focus_session=agents_db.get_focus_session,
        focus_guard=agents_db.focus_guard,
    )
    # Whisper takes ~5 s to load — kick it off before binding the port so
    # the first /transcribe request doesn't time out.
    prod_ctx.stt.start_loading()  # type: ignore[attr-defined]
    srv = build_server(prod_ctx, PORT, restart_recovery=True)
    log("serverStart", f"port={PORT}")
    print(f"claude-pwa listening on :{PORT}", flush=True)
    srv.serve_forever()
