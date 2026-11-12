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

import time
_HOST_PROCESS_STARTED_AT = time.monotonic()
from lib import transcript_import_cache as _transcript_import_cache
_transcript_import_cache.note_host_started(_HOST_PROCESS_STARTED_AT)

from lib import clip_store
from lib import viz_normalize

import gzip
import json
from dataclasses import asdict
import mimetypes
import os
import pathlib
import queue
import re
import secrets
import shutil
import sqlite3
import sys
import threading
from urllib.parse import parse_qs, unquote, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib.agent_store import (  # noqa: E402
    load_agents,
    save_agent,
)
from lib.agent_lifecycle import AgentLifecycleError, AgentLifecycleService  # noqa: E402
from lib.config import persona_personality  # noqa: E402
from lib.controller_events import (  # noqa: E402
    ControllerEventError,
    build_controller_event,
)
from lib.context import ServerContext  # noqa: E402
from lib import db  # noqa: E402
from lib import eventlog  # noqa: E402

from lib.outdated_clients import _note_outdated_client  # noqa: E402
from lib.turn_identity import TurnIdentityUnavailable  # noqa: E402
from lib import audio_metrics, voice_events  # noqa: E402
from lib import health  # noqa: E402
from lib import agents as agents_db  # noqa: E402
from lib.routes import registry as route_registry  # noqa: E402
from lib import backends  # noqa: E402
from lib import backend_usage  # noqa: E402
from lib import media_store  # noqa: E402
from lib.calendar_request import CalendarRequestError, build_calendar_request  # noqa: E402
from lib import trace as _trace  # noqa: E402
from lib import events  # noqa: E402
from lib import identity  # noqa: E402
from lib.http_utils import HandlerResponder, Principal, redact_query_secrets  # noqa: E402
from lib.log import log, log_exception  # noqa: E402
from lib.http_diagnostics import _note_http_failure  # noqa: E402
from lib.paths import RuntimePaths, _safe_session  # noqa: E402
from lib.focus import current_focus_session  # noqa: E402
from lib.send_request import SendRequest, SendRequestError  # noqa: E402
from lib.transcription_pipeline import transcribe as run_transcription  # noqa: E402
from lib.protocol import AgentState, ClientAction  # noqa: E402
from lib import turn_lifecycle  # noqa: E402
from lib.turn_lifecycle import TurnEvent  # noqa: E402
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
from lib.snapshot import build_agent_snapshot  # noqa: E402
from lib.timing import SERVER_TIMING  # noqa: E402
from lib.turn_dispatch import DispatchCommand, DispatchError, TurnDispatchService  # noqa: E402
from lib.voices import voices_with_availability  # noqa: E402

# Background workers, imported here so tests can monkeypatch them as
# `server.<Name>`; `_server_workers` looks every one of them up at call time.
from lib.background_job_watcher import BackgroundJobWatcher  # noqa: E402
from lib.decision_delivery import DecisionDeliveryWorker  # noqa: E402
from lib.dispatch_adapters import DispatchAdapters  # noqa: E402
from lib.dreaming import DreamingScheduler  # noqa: E402
from lib.heartbeat import HeartbeatScheduler  # noqa: E402
from lib.janitor_autonomy import AutonomyJanitors  # noqa: E402
from lib.janitor_runner import JanitorRunner  # noqa: E402
from lib.maintenance import MaintenanceWorker  # noqa: E402
from lib.resource_telemetry import ResourceTelemetryWorker  # noqa: E402
from lib.scheduler import AgentScheduleRunner  # noqa: E402
from lib.state_watcher import StateLogWatcher  # noqa: E402
from lib.team_leader import TeamLeaderScheduler  # noqa: E402
from lib.transcript_streamer import TranscriptStreamer  # noqa: E402
from lib.tts_worker import TTSWorker  # noqa: E402
from lib.workers import BOOT, SERVING, Worker, WorkerSet, started  # noqa: E402
from lib.cache_warmup import CacheWarmupWorker  # noqa: E402


from lib.config import load as load_config  # noqa: E402

_CFG = load_config()
PORT = int(os.environ.get("CLAUDE_PWA_PORT", str(_CFG.port)))
BIND_ADDR = os.environ.get("CLAUDE_PWA_BIND", _CFG.bind_addr)


from lib.routes.helpers import _runtime_paths, _truthy_header, _raw_query_value, _PLAY_EVENTS, _clip_session_and_trace, _safe_upload_name  # noqa: E402  (s3_apply)
# Serializes read-decide-swap of ctx.stt in _activate_transcription_if_default.
# Request threads never take it: the transcription pipeline reads ctx.stt once
# per request, so a swap mid-request cannot hand one utterance two engines.
_STT_ACTIVATION_LOCK = threading.Lock()


class StopNotRecorded(RuntimeError):
    """The Stop ran (process interrupted, barrier released) but its terminal
    row could not be written after its bounded retries (TP-N1b)."""


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
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            try:
                log("clientGone", f"{client_address[0]} {type(exc).__name__}")
            except Exception as diagnostic_error:  # noqa: BLE001
                _note_http_failure("H01", diagnostic_error)
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, handler_cls, ctx: ServerContext):
        self.ctx = ctx
        # The import-time config snapshot (_CFG), the same object, for handler
        # code that must not import this module. Set here, not in build_server,
        # so the LAN listener (a subclass) and directly built servers carry it.
        self.boot_config = _CFG
        self._close_callbacks = []
        self.local_tls = False
        self.local_transport = None
        self.local_transport_error = ""
        self.relay = None
        # lib.workers.WorkerSet once build_server has started the workers.
        self.workers = None
        import weakref
        self._device_connections = weakref.WeakKeyDictionary()
        self._device_connections_lock = threading.Lock()
        from lib.request_security import FailureLimiter
        self.auth_failures = FailureLimiter()
        super().__init__(addr, handler_cls)

    def register_device_connection(self, connection, principal: str = "") -> bool:
        with self._device_connections_lock:
            if principal:
                # Authentication checked existence. Re-check revocation while
                # holding the registry lock so revoke cannot miss a connection
                # that was authenticated just before its database update.
                row = db.conn().execute(
                    "SELECT revoked_at FROM paired_devices WHERE device_id = ?", (principal,)
                ).fetchone()
                if row is not None and row["revoked_at"] is not None:
                    return False
                self._device_connections[connection] = principal
            else:
                self._device_connections.pop(connection, None)
            return True

    def disconnect_device(self, device_id: str) -> None:
        import socket
        with self._device_connections_lock:
            connections = [connection for connection, principal in self._device_connections.items()
                           if principal == device_id]
            for connection in connections:
                self._device_connections.pop(connection, None)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

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
    from lib.runtime_startup import restore_persisted_agents
    restore_persisted_agents(ctx)


def broadcast_boot_version(ctx: ServerContext) -> None:
    """Push the current static-file version through SSE so already-open PWAs
    can detect a redeploy and reload immediately."""
    events.broadcast(ctx.stream, events.server_version(ctx.sw_version()))


from lib.cartesia_preview_locks import _cartesia_preview_lock  # noqa: E402


def _header_int(raw: str | None) -> int | None:
    try:
        return int(float(raw)) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _spawn_voice_metrics(audio_bytes: bytes, ctype: str, *, session, trace_id,
                         utterance_id, transcript) -> None:
    """Level and integrity metrics for the capture, off the request thread.

    Decoding a 30 s clip costs tens of milliseconds; the request has already
    waited for STT, so the numbers land a beat later as a `level` row (and a
    `corrupt` row when they look wrong).
    """
    def work() -> None:
        try:
            metrics = audio_metrics.analyze(audio_bytes, ctype)
            reasons = audio_metrics.corruption_reasons(metrics, transcript=transcript)
            voice_events.record(
                "level", source="server", session=session, trace_id=trace_id,
                utterance_id=utterance_id, duration_ms=metrics.get("duration_ms"),
                level_db=metrics.get("rms_db"), peak_db=metrics.get("peak_db"),
                detail=metrics)
            if reasons:
                voice_events.record(
                    "corrupt", source="server", session=session, trace_id=trace_id,
                    utterance_id=utterance_id,
                    detail={"reasons": reasons, "metrics": metrics})
        except Exception as e:  # noqa: BLE001
            log_exception("voiceMetricsFail", e, detail=utterance_id or "")
        finally:
            db.close_local()

    threading.Thread(target=work, name="voice-metrics", daemon=True).start()


def _compressible(content_type: str) -> bool:
    """The types _send gzips for a client that accepts it."""
    return (content_type.startswith("application/json")
            or content_type.startswith("text/")
            or content_type.startswith("application/javascript"))


def _security_headers(secure: bool) -> dict:
    return ({"X-Content-Type-Options": "nosniff",
             "Content-Security-Policy": "sandbox; default-src 'none'"}
            if secure else {})


_BYTE_RANGE = re.compile(r"[ \t]*(?P<lo>[0-9]{0,4000})[ \t]*-[ \t]*(?P<hi>[0-9]{0,4000})[ \t]*",
                         re.ASCII)


def _byte_range(header: str, total: int) -> tuple[str, int, int]:
    """Read a Range header against a representation of ``total`` bytes.

    Returns ``("full", 0, total - 1)`` when there is no usable range (none, a
    unit other than bytes, or a malformed or inverted spec, which RFC 9110
    §14.2 says to ignore), ``("partial", start, end)`` with ``end`` clamped to
    the last byte, or ``("unsatisfiable", 0, -1)`` when the range starts at or
    past the end, or asks for a zero-length suffix (RFC 9110 §15.5.17). Of
    several ranges only the first is served, as before.
    """
    full = ("full", 0, total - 1)
    if not header.startswith("bytes="):
        return full
    # ASCII decimal digits only: str.isdigit() and int() also take
    # superscripts, other scripts' digits and underscores, and int() raises
    # past 4300 digits. Such a range is malformed, so it is ignored.
    match = _BYTE_RANGE.fullmatch(header.split("=", 1)[1].split(",", 1)[0])
    if not match or not (match["lo"] or match["hi"]):
        return full
    try:
        lo = int(match["lo"]) if match["lo"] else None
        hi = int(match["hi"]) if match["hi"] else None
    except ValueError:
        return full
    if lo is None:                               # suffix: bytes=-N, the last N
        if hi == 0 or total == 0:
            return ("unsatisfiable", 0, -1)
        return ("partial", max(0, total - hi), total - 1)
    if hi is not None and hi < lo:
        return full
    if lo >= total:
        return ("unsatisfiable", 0, -1)
    return ("partial", lo, min(total - 1 if hi is None else hi, total - 1))


from lib.routes.vocab import VocabRoutes  # noqa: E402  (s3_apply)


from lib.routes.settings import SettingsRoutes  # noqa: E402  (s3_apply)


from lib.routes.providers import ProviderRoutes  # noqa: E402  (s3_apply)


from lib.routes.ops import OpsRoutes  # noqa: E402  (s3_apply)


from lib.routes.artifacts import ArtifactRoutes  # noqa: E402  (s3_apply)


from lib.routes.media import MediaRoutes  # noqa: E402  (s3_apply)


from lib.routes.goals import GoalRoutes  # noqa: E402  (s3_apply)


from lib.routes.teams import TeamRoutes  # noqa: E402  (s3_apply)


from lib.routes.schedules import ScheduleRoutes  # noqa: E402  (s3_apply)


from lib.routes.heartbeat import HeartbeatRoutes  # noqa: E402  (s3_apply)


from lib.routes.agents import AgentRoutes  # noqa: E402  (s3_apply)


from lib.routes.personas import PersonaRoutes  # noqa: E402  (s3_apply)


from lib.routes.calls import CallRoutes  # noqa: E402  (s3_apply)


from lib.routes.oracle import OracleRoutes  # noqa: E402  (s3_apply)


from lib.routes.voice import VoiceRoutes  # noqa: E402  (s3_apply)


from lib.routes.voice_timeline import VoiceTimelineRoutes  # noqa: E402  (s3_apply)


from lib.routes.send import SendRoutes  # noqa: E402  (s3_apply)


class Handler(SendRoutes, VoiceTimelineRoutes, VoiceRoutes, OracleRoutes, CallRoutes, PersonaRoutes, AgentRoutes, HeartbeatRoutes, ScheduleRoutes, TeamRoutes, GoalRoutes, MediaRoutes, ArtifactRoutes, OpsRoutes, ProviderRoutes, SettingsRoutes, VocabRoutes, BaseHTTPRequestHandler):
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
        # The request clock starts here. handle_one_request() blocks in
        # readline() for the next request on a keep-alive connection, so a
        # clock started there counts idle socket time as handler latency
        # (every path showed a 90 s maximum, the keep-alive timeout).
        self._request_started_wall = time.time()
        self._request_started_monotonic = time.perf_counter()
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
            except Exception as diagnostic_error:
                _note_http_failure("H02", diagnostic_error)

    _GET_ROUTES = {
        "/": "_send_index",
        "/sw.js": "_send_sw",
        "/events": "_sse",
        "/log": "_handle_log",
        "/live": "_handle_live",
        "/message-tool-details": "_handle_message_tool_details",
        "/status": "_handle_status",
        "/network/relay": "_handle_relay_status",
        "/viz/events": "_handle_viz_events",
        "/viz": "_send_viz_page",
        "/diagnostics/health": "_handle_diagnostics_health",
        "/backend-usage": "_handle_backend_usage",
        "/hotseat/status": "_handle_hotseat_status",
        "/hotseat/desk": "_handle_hotseat_desk",
        "/backend-auth": "_handle_backend_auth",
        "/harness-providers": "_handle_harness_providers",
        "/harness-providers/models": "_handle_harness_provider_models",
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
        "/clips/message": "_handle_message_clips",
        "/voice-events": "_handle_voice_events_get",
        "/voice-events/utterances": "_handle_voice_utterances_get",
        "/server-info": "_handle_server_info",
        "/paired-devices": "_handle_paired_devices",
        "/devices/push-tokens": "_handle_push_tokens",
        "/server-update": "_handle_server_update_status",
        "/turn-queue": "_handle_turn_queue",
        "/agents/snapshot": "_handle_snapshot",
        "/agents/archived": "_handle_archived_agents",
        "/agent-conversations": "_handle_agent_conversations",
        "/identity/prompt-history": "_handle_prompt_history",
        "/background-jobs": "_handle_background_jobs",
        "/task-plan": "_handle_task_plan",
        "/task-plans": "_handle_task_plans",
        "/task-plan/document": "_handle_task_goal_document",
        "/goal-ledger": "_handle_goal_ledger",
        "/goal-ledger/delegations": "_handle_goal_delegations",
        "/artifacts": "_handle_artifacts_list",
        "/attention": "_handle_attention",
        "/attention/inbox": "_handle_attention_inbox",
        "/pinned-artifacts": "_handle_pinned_artifacts",
        "/voices": "_handle_voices",
        "/voice-catalog": "_handle_voice_catalog",
        "/voice-preview": "_handle_voice_preview",
        "/cartesia-voices": "_handle_cartesia_voices",
        "/cartesia-voice-preview": "_handle_cartesia_voice_preview",
        "/controller-narration": "_handle_controller_narration",
        "/tts/providers": "_handle_tts_providers_get",
        "/agent-files": "_handle_agent_files",
        "/agent-file": "_handle_agent_file",
        "/orchestrator/settings": "_handle_orchestrator_settings_get",
        "/herald/settings": "_handle_herald_settings_get",
        "/judgments/settings": "_handle_judgments_settings_get",
        "/judgments/decisions": "_handle_judgments_decisions_get",
        "/tool-explanations/stats": "_handle_tool_explanations_stats",
        "/tool-explanations/settings": "_handle_tool_explanation_settings_get",
        "/voice-humanness": "_handle_voice_humanness_get",
        "/personalities/settings": "_handle_personalities_settings_get",
        "/automation-settings": "_handle_automation_settings_get",
        "/avatar-settings": "_handle_avatar_settings_get",
        "/agent-model-options": "_handle_agent_model_options",
        "/favorite-paths": "_handle_favorite_paths",
        "/launch-directories": "_handle_launch_directories",
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
        "/agent-goal": "_handle_agent_goal_get",
        "/agent-helper-state": "_handle_agent_helper_state_get",
        "/oracle/status": "_handle_oracle_status",
        "/oracle/contact": "_handle_oracle_contact_get",
        "/oracle/voice-provider": "_handle_oracle_voice_provider_get",
        "/oracle/handoff": "_handle_oracle_handoff_get",
        "/calls": "_handle_calls_get",
        "/calls/ring": "_handle_calls_ring_get",
        "/calls/ring/settings": "_handle_calls_ring_settings_get",
        "/calls/ring/device": "_handle_calls_ring_device_get",
        "/calls/ring/live": "_handle_calls_ring_live_socket",
        "/oracle/delegations": "_handle_oracle_delegations_get",
        "/oracle/realtime": "_handle_oracle_realtime",
        "/oracle/v2": "_handle_oracle_v2",
        "/oracle/calls/status": "_handle_oracle_call_status",
        "/oracle/v2/calls/status": "_handle_oracle_live_call_status",
        "/oracle/v2/context/image": "_handle_oracle_context_image",
        "/agent-schedules": "_handle_agent_schedules_get",
    }
    _ROOT_STATIC = {"/manifest.json", "/styles.css", "/icon.png"}
    _POST_ROUTES = {
        "/tool-explanations": "_handle_tool_explanations",
        "/tool-explanations/settings": "_handle_tool_explanation_settings_post",
        "/desktop-presence": "_handle_desktop_presence",
        "/application-activity": "_handle_application_activity",
        "/viz/supersede": "_handle_viz_supersede",
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
        "/vocab/profiles/workspace": "_handle_vocab_profile_workspace",
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
        "/hotseat/switch": "_handle_hotseat_switch",
        "/hotseat/resume": "_handle_hotseat_resume",
        "/harness-providers/login": "_handle_harness_provider_login",
        "/harness-providers/login-code": "_handle_harness_provider_login_code",
        "/harness-providers/logout": "_handle_harness_provider_logout",
        "/server-update": "_handle_server_update",
        "/managed-skills": "_handle_managed_skills_update",
        "/upload": "_handle_upload",
        "/media": "_handle_media_publish",
        "/agent-portraits": "_handle_agent_portraits_update",
        "/agent-portrait-generation": "_handle_agent_portrait_generation_start",
        "/select": "_handle_select",
        "/clog": "_handle_clog",
        "/voice-events": "_handle_voice_events_post",
        "/agents": "_handle_create_agent",
        "/personas": "_handle_create_persona",
        "/personas/update": "_handle_update_persona",
        "/agent-voice": "_handle_agent_voice",
        "/agent-llm": "_handle_agent_llm",
        "/agent-mcp": "_handle_agent_mcp",
        "/agent-heartbeat": "_handle_agent_heartbeat",
        "/task-plan/action": "_handle_task_plan_action",
        "/goal-ledger/delegations": "_handle_goal_delegation_enable",
        "/goal-ledger/delegations/stop": "_handle_goal_delegation_stop",
        "/agent-goal": "_handle_agent_goal_start",
        "/agent-goal/pause": "_handle_agent_goal_pause",
        "/agent-goal/resume": "_handle_agent_goal_resume",
        "/agent-goal/clear": "_handle_agent_goal_clear",
        "/agent-archive": "_handle_agent_archive",
        "/agent-helper-state": "_handle_agent_helper_state",
        "/heartbeat/settings": "_handle_heartbeat_settings_post",
        "/diagnostics/settings": "_handle_diagnostics_settings_post",
        "/agent-dreaming": "_handle_agent_dreaming",
        "/agent-mute": "_handle_agent_mute",
        "/agent-rename": "_handle_agent_rename",
        "/agent-assign": "_handle_agent_assign",
        "/agent-voice-verbosity": "_handle_agent_voice_verbosity",
        "/voice-humanness": "_handle_voice_humanness_post",
        "/team-nudging": "_handle_team_nudging",
        "/compact": "_handle_compact",
        "/orchestrator/settings": "_handle_orchestrator_settings_post",
        "/orchestrator/addressing": "_handle_orchestrator_addressing",
        "/herald/settings": "_handle_herald_settings_post",
        "/judgments/settings": "_handle_judgments_settings_post",
        "/personalities/settings": "_handle_personalities_settings_post",
        "/automation-settings": "_handle_automation_settings_post",
        "/avatar-settings": "_handle_avatar_settings_post",
        "/preview": "_handle_preview",
        "/stop": "_handle_stop",
        "/repair-slot": "_handle_repair_slot",
        "/remote-action": "_handle_remote_action",
        "/focus": "_handle_focus",
        "/clips/ack": "_handle_clip_ack",
        "/devices": "_handle_register_device",
        "/devices/live-activity": "_handle_live_activity_register",
        "/location": "_handle_set_location",
        "/location/request": "_handle_request_location",
        "/calendar/request": "_handle_request_calendar",
        "/crash": "_handle_crash",
        "/teams": "_handle_team_create",
        "/artifacts": "_handle_artifact_create",
        "/decisions": "_handle_decision_create",
        "/oracle/calls": "_handle_oracle_call_create",
        "/oracle/calls/close": "_handle_oracle_call_close",
        "/oracle/v2/calls": "_handle_oracle_live_call_create",
        "/oracle/contact": "_handle_oracle_contact_post",
        "/oracle/voice-provider": "_handle_oracle_voice_provider_post",
        "/oracle/connect": "_handle_oracle_connect",
        "/calls": "_handle_calls_start",
        "/calls/add": "_handle_calls_add",
        "/calls/remove": "_handle_calls_remove",
        "/calls/hold": "_handle_calls_hold",
        "/calls/resume": "_handle_calls_resume",
        "/calls/switch": "_handle_calls_switch",
        "/calls/transfer": "_handle_calls_transfer",
        "/calls/end": "_handle_calls_end",
        "/calls/ring": "_handle_calls_ring",
        "/calls/ring/answer": "_handle_calls_ring_answer",
        "/calls/ring/opening": "_handle_calls_ring_opening",
        "/calls/ring/decline": "_handle_calls_ring_decline",
        "/calls/ring/cancel": "_handle_calls_ring_cancel",
        "/calls/ring/settings": "_handle_calls_ring_settings_post",
        "/calls/ring/voip-token": "_handle_calls_ring_voip_token",
        "/calls/ring/device": "_handle_calls_ring_device",
        "/calls/live": "_handle_calls_live",
        "/calls/ring/live/close": "_handle_calls_ring_live_close",
        "/calls/ring/live/fallback": "_handle_calls_ring_live_fallback",
        "/oracle/handoffs/ack": "_handle_oracle_handoff_ack",
        "/oracle/v2/calls/close": "_handle_oracle_live_call_close",
        "/oracle/v2/calls/control": "_handle_oracle_live_call_control",
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
        except Exception as diagnostic_error:
            _note_http_failure("H03", diagnostic_error)

    def handle_one_request(self):
        # One Handler serves every request on a keep-alive connection, so
        # `command`, `_status_code` and the request clock still hold the
        # previous request here. When the idle connection finally closes,
        # super() reads an empty line and never parses a request; logging off
        # those leftovers emitted a phantom copy of the previous request timed
        # across the idle wait (the "90 s maximum" on every path). Only a
        # request that reached parse_request() this iteration is logged.
        self._request_started_wall = None
        self._status_code = 0
        self._request_started_monotonic = time.perf_counter()
        self._response_phases = {}
        from lib import diagnostics_settings
        db.begin_request_metrics(
            enabled=diagnostics_settings.allows("database"))
        self._request_id = _trace.new_trace_id()
        self._trace_id = None
        try:
            super().handle_one_request()
        except TurnIdentityUnavailable as exc:
            # HTTP-N5: the turn token's lookup failed. Every call site asks before
            # any side effect or response, so this is a retryable 503. Should a
            # response already have started, it cannot become one: log and close
            # the connection so the client never reuses a half-answered socket.
            if self._status_code:
                log_exception("turnIdentityAfterResponse", exc)
                self.close_connection = True
            else:
                self._json(503, {"error": "turn identity unavailable", "retryable": True})
                self.wfile.flush()
        finally:
            # `self.command` and a status set by send_response are only set
            # after super() parses the request line. If the connection dies
            # or runs out of FDs before that, neither attribute exists yet,
            # so use getattr to keep the finally-block from raising.
            code = getattr(self, "_status_code", 0) or 0
            started = getattr(self, "_request_started_wall", None)
            if started is not None and getattr(self, "command", None):
                self._log_http(code, started)

    def send_response(self, code, message=None):
        self._status_code = code
        self._drain_request_body()
        super().send_response(code, message)

    # --- HTTP helpers -----------------------------------------------------

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler's stock logger; forward to journal.
        try:
            message = format % args
            # The access logger receives the raw HTTP request line.
            if getattr(self, "path", ""):
                message = message.replace(self.path, redact_query_secrets(self.path))
            print(f"{self.address_string()} {message}", flush=True)
        except OSError as e:
            log_exception("httpAccessLogFail", e)

    def _query(self) -> dict[str, list[str]]:
        """Parsed query string of the current request, cached per request.

        Keyed on self.path because one Handler instance serves every request
        on a keep-alive connection."""
        cached = getattr(self, "_query_cache", None)
        if cached is None or cached[0] != self.path:
            cached = (self.path, parse_qs(urlparse(self.path).query))
            self._query_cache = cached
        return cached[1]

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _json_ok(self, payload) -> None:
        self._json(200, payload)

    def _json_error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

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
        if "Cache-Control" not in headers:
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

    def _send_file(self, path: pathlib.Path, content_type: str = "", secure: bool = False,
                   cache_headers: dict | None = None):
        if not path.is_file():
            return self._send(404, b"not found")
        ctype = content_type
        if not ctype:
            ctype, _ = mimetypes.guess_type(str(path))
        if not ctype:
            ctype = "application/octet-stream"
        if (_compressible(ctype)
                and "gzip" in self.headers.get("Accept-Encoding", "").lower()):
            # App text a client takes gzipped is compressed whole by _send, as
            # before; such files are small.
            data = path.read_bytes()
            return self._send_ranged_data(data, ctype, secure=secure, cache_headers=cache_headers)
        return self._stream_file(path, ctype, secure=secure, cache_headers=cache_headers)

    # One bounded read and one bounded write at a time: memory per response
    # stays at one chunk whatever the file size, each write gets the whole
    # handler timeout (a slow reader that keeps up is never cut, a stalled one
    # still is), and a client that goes away stops the reads at once.
    FILE_CHUNK = 256 * 1024

    def _stream_file(self, path: pathlib.Path, ctype: str, *, secure: bool = False,
                     cache_headers: dict | None = None):
        response_started = time.perf_counter()
        request_started = getattr(self, "_request_started_monotonic", response_started)
        try:
            source = path.open("rb")
        except OSError:
            return self._send(404, b"not found")
        with source:
            total = os.fstat(source.fileno()).st_size
            headers = {"Accept-Ranges": "bytes", **_security_headers(secure)}
            kind, start, end = _byte_range(self.headers.get("Range", ""), total)
            if kind == "unsatisfiable":
                return self._send(416, b"", "text/plain", extra_headers={
                    **headers, "Content-Range": f"bytes */{total}"})
            headers.update(cache_headers or {})
            length = end - start + 1
            self.send_response(206 if kind == "partial" else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            if "Cache-Control" not in headers:
                self.send_header("Cache-Control", "no-store")
            if kind == "partial":
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            write_started = time.perf_counter()
            read = written = 0
            try:
                source.seek(start)
                while written < length:
                    chunk = source.read(min(self.FILE_CHUNK, length - written))
                    if not chunk:
                        # The file shrank under us: the body is short of its
                        # Content-Length, so this connection cannot be reused.
                        log("fileShortRead", f"path={path} expected={length} "
                                             f"sent={written}")
                        self.close_connection = True
                        break
                    read += len(chunk)
                    self.wfile.write(chunk)
                    written += len(chunk)
            finally:
                self._response_phases = {
                    "handler_ms": round((write_started - request_started) * 1000, 3),
                    "compression_ms": 0.0,
                    "socket_write_ms": round((time.perf_counter() - write_started) * 1000, 3),
                    "response_bytes": written,
                    "file_bytes_read": read,
                }

    def _send_not_modified(self, headers: dict) -> None:
        self.send_response(304)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def _send_app_asset(self, path: pathlib.Path):
        """One of the web app's own files: immutable when the URL carries its
        content hash (see lib/static_assets.py), revalidated otherwise."""
        from lib import static_assets
        if not path.is_file():
            return self._send(404, b"not found")
        requested = (self._query().get("v") or [""])[0]
        headers = static_assets.asset_cache_headers(path, requested)
        if (headers["Cache-Control"] != static_assets.IMMUTABLE
                and static_assets.etag_matches(self.headers.get("If-None-Match", ""),
                                               headers["ETag"])):
            return self._send_not_modified(headers)
        return self._send_file(path, cache_headers=headers)

    def _send_ranged_data(self, data: bytes, ctype: str, *, secure: bool = False,
                          cache_headers: dict | None = None):
        total = len(data)
        security_headers = _security_headers(secure)
        # Honor Range requests. AVPlayer (iOS) will NOT stream a remote audio
        # file unless the server speaks byte ranges — without this it fails to
        # play plain mp3s with CoreMediaErrorDomain -12640 (the "X here, ready
        # for an update" herald clips were silent for exactly this reason).
        kind, start, end = _byte_range(self.headers.get("Range", ""), total)
        if kind == "unsatisfiable":
            return self._send(416, b"", "text/plain", extra_headers={
                "Accept-Ranges": "bytes", **security_headers,
                "Content-Range": f"bytes */{total}"})
        if kind == "partial":
            return self._send(206, data[start:end + 1], ctype, extra_headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                **security_headers, **(cache_headers or {}),
            })
        self._send(200, data, ctype, extra_headers={
            "Accept-Ranges": "bytes", **security_headers, **(cache_headers or {}),
        })

    def _send_index(self):
        # The shell is small and names every asset by content hash, so it is
        # always revalidated (304 when unchanged) and the assets never are.
        from lib import static_assets
        body = static_assets.versioned_index(self.ctx.static)
        headers = {"Cache-Control": static_assets.REVALIDATE,
                   "ETag": static_assets.etag_for(body)}
        if static_assets.etag_matches(self.headers.get("If-None-Match", ""), headers["ETag"]):
            return self._send_not_modified(headers)
        return self._send(200, body, "text/html", extra_headers=headers)

    def _send_root_static(self, path: str):
        return self._send_app_asset(self.ctx.static / path.lstrip("/"))

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
            if status == "failed":
                return self._send(410, b"clip failed")
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
            log_exception("requestJsonParseFail", e, detail=redact_query_secrets(self.path))
            return None
        if not isinstance(data, dict):
            # Every handler does data.get(...): a JSON string, array, or
            # number body must read as "bad json", not kill the connection.
            log("requestJsonNotObject", f"{redact_query_secrets(self.path)} {type(data).__name__}")
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

    def principal(self) -> Principal:
        """Who is asking, as `_authorized` decided it for this request. Library
        modules receive this instead of reading the attributes below."""
        return Principal(
            authenticated=bool(getattr(self, "_request_auth_validated", False)),
            device_scope=str(getattr(self, "_request_device_scope", "") or ""),
            principal=str(getattr(self, "_request_principal", "") or ""))

    def responder(self) -> HandlerResponder:
        return HandlerResponder(self)

    def _authorized(self) -> bool:
        self._request_auth_validated = False
        self._request_device_scope = ""
        self._request_principal = ""
        self._request_auth_source = ""
        if hasattr(self.server, "register_device_connection"):
            self.server.register_device_connection(self.connection)
        token = getattr(self.ctx, "auth_token", "") or ""
        bare = self.path.split("?", 1)[0]
        if bare in self._PUBLIC_EXACT:
            return True
        for p in self._PUBLIC_PREFIXES:
            if bare.startswith(p):
                return True
        from lib.request_security import local_request
        self._request_is_local = (not getattr(self.server, "local_tls", False)
                                  and local_request(self.client_address[0], self.headers))
        if not token:
            return self._request_is_local
        # Header: Authorization: Bearer <token>
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            supplied = auth.split(None, 1)[1].strip()
            if self._accept_credential(supplied, token):
                self._request_auth_source = "header"
                return True
        # Cookie: claude_pwa_token=<token>. Used by EventSource/iframe where
        # custom Authorization headers are not available.
        from http.cookies import SimpleCookie
        try:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            supplied = (cookie.get("claude_pwa_token").value
                        if cookie.get("claude_pwa_token") else "")
            if supplied and self._accept_credential(supplied.strip(), token):
                self._request_auth_source = "cookie"
                return True
        except Exception as e:  # noqa: BLE001
            log_exception("authCookieParseFail", e)
        # URL credentials are retained only for local CLI/bootstrap use.
        if not self._request_is_local:
            return False
        # Query string: ?token=<token>
        qs = self._query()
        for v in qs.get("token", []):
            if self._accept_credential((v or "").strip(), token):
                self._request_auth_source = "query"
                return True
        return False

    def _accept_credential(self, supplied: str, admin_token: str) -> bool:
        if (getattr(self, "_request_is_local", False)
                and secrets.compare_digest(supplied.encode(), admin_token.encode())):
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
        if hasattr(self.server, "register_device_connection"):
            if not self.server.register_device_connection(self.connection, self._request_principal):
                self._request_auth_validated = False
                self._request_device_scope = ""
                self._request_principal = ""
                return False
        return True

    _DEVICE_FULL_ONLY_PREFIXES = (
        "/backend-auth", "/harness-providers", "/server-update", "/managed-skills",
        "/hotseat/desk", "/hotseat/switch", "/hotseat/resume",
        "/orchestrator/", "/herald/", "/personalities/",
        "/automation-settings", "/avatar-settings", "/paired-devices",
        "/tts/providers", "/network/", "/server-terminal",
        "/oracle/", "/agent-file", "/janitor-runs/", "/calls",
    )
    _LIMITED_DEVICE_POST_EXACT = frozenset({
        "/send", "/transcribe", "/upload", "/select", "/focus",
        "/clips/ack", "/clog", "/location", "/calendar/response",
        "/remote-action",
        "/voice-events",
    })

    def _device_forbidden(self, path: str, method: str) -> bool:
        if getattr(self, "_request_device_scope", "") != "limited":
            return False
        if any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in self._DEVICE_FULL_ONLY_PREFIXES
        ):
            return True
        # Exactly the routes, so unmatched paths keep their 404 (HTTP-01/03).
        # /past-sessions with any scope: a caller-chosen cwd lists that
        # directory's sessions and their first messages (HTTP-03b).
        if path in ("/dirs", "/past-sessions") or (path.startswith("/terminal/") and method == "GET"):
            return True
        if method == "GET":
            return False
        from lib import controller_actions_http
        if controller_actions_http.handles(path):
            return not controller_actions_http.limited_device_allowed(path, method)
        if method == "POST":
            return path not in self._LIMITED_DEVICE_POST_EXACT
        return True

    def _auth_failure_retry(self) -> int:
        from lib.request_security import failure_source
        return self.server.auth_failures.failure(
            failure_source(self.client_address[0], self.headers))

    def _reject_unauthorized(self) -> None:
        retry = self._auth_failure_retry()
        if retry:
            return self._send(429, b'{"error":"too many authentication failures"}',
                              "application/json", extra_headers={"Retry-After": str(retry)})
        self._json_error(401, "unauthorized")

    # --- GET dispatch ----------------------------------------------------

    def do_GET(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._oracle_auth_missing(path):
            return self._json_error(401, "Oracle requires authenticated full-device access")
        if self._device_forbidden(path, "GET"):
            return self._json_error(403, "full device access required")
        return route_registry.dispatch(self, "GET", path)

    def _handle_notification_avatar(self, agent_id: str):
        """Serve one short-lived APNs avatar capability without app auth."""
        from lib.avatar_urls import (
            avatar_content_version,
            notification_avatar_authorized,
        )

        agent_key = unquote(agent_id)
        row = identity.lookup(agent_key)
        path = pathlib.Path(str((row or {}).get("avatar_path") or ""))
        if not row or not path.is_file():
            return self._send(404, b"not found")
        query = self._query()
        try:
            expires_at = int((query.get("exp") or [""])[0])
        except ValueError:
            return self._send(403, b"forbidden")
        content_version = avatar_content_version(path)
        supplied_version = str((query.get("v") or [""])[0])
        signature = str((query.get("sig") or [""])[0])
        if supplied_version != content_version or not notification_avatar_authorized(
            secret=str(getattr(self.ctx, "auth_token", "") or ""),
            agent_id=agent_key,
            content_version=content_version,
            expires_at=expires_at,
            signature=signature,
            now=int(time.time()),
        ):
            return self._send(403, b"forbidden")
        self._send_file(path)

    # --- GET handlers ----------------------------------------------------

    def _handle_viz_events(self):
        """Normalized fleet activity for the map, over a time window."""
        # Kept on the raw _send/parse_qs pair: tests drive this handler with a
        # duck-typed stub that provides only `path` and `_send`.
        qs = parse_qs(urlparse(self.path).query)

        def _int(name, default):
            try:
                return int((qs.get(name, [""])[0] or "").strip() or default)
            except ValueError:
                return default

        window_s = max(60, min(_int("window", 3600), 90 * 86400))
        until = _int("until", 0) or None
        anchor = until if until else int(time.time() * 1000)
        payload = viz_normalize.build_fleet_map(
            anchor - window_s * 1000, until, limit=_int("limit", 4000))
        from lib import viz_learning
        clusters = payload.pop("_unknown_clusters", [])
        payload["learning"] = viz_learning.status()
        self._send(200, json.dumps(payload).encode(), "application/json")
        if payload.get("world"):
            viz_learning.offer_scene(payload["world"])
        else:
            viz_learning.offer(clusters)

    def _handle_viz_supersede(self):
        from lib import viz_learning
        data = self._read_json()
        if isinstance(data, dict) and data.get("world"):
            world = viz_normalize.build_fleet_map(int(time.time()*1000)-3600000)["world"]
            result = viz_learning.offer_scene(world, force=True, reason=str(data.get("reason", "Improve this detail while preserving the established world")) + " Selected entity: " + str(data.get("entity_id", "whole world")))
            return self._json(202, result)
        if (not isinstance(data, dict) or not isinstance(data.get("entity_id"), str)
                or not isinstance(data.get("revision"), int)):
            return self._json_error(400, "entity_id and revision required")
        try:
            result = viz_learning.supersede(data["entity_id"], data["revision"],
                                             str(data.get("reason", "Rethink this representation")))
        except ValueError as error:
            return self._json_error(409, str(error))
        self._json(202, result)

    def _send_viz_page(self):
        """The fleet map itself; a static page that reads /viz/events."""
        return self._send_file(self.ctx.static / "viz.html")

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
        qs = self._query()
        session = (qs.get("session", [self.ctx.default_session])[0] or self.ctx.default_session).strip()
        agent = identity.lookup(session)
        # busy state is purely DB-driven now (hooks + the backend runners write
        # to state_log). The old terminal-scrape fallback is gone.
        busy = bool(agent and agents_db.is_busy(agent["agent_id"]))
        runtime_payload = {
            "available": True,
            "reason": "",
            "release_id": "embedded",
            "fenced": False,
            "sealed": False,
            "draining": False,
            "drain": {"phase": "idle"},
            "active_turns": int(busy),
            "observed_at": int(time.time() * 1000),
        }
        runtime_client = getattr(self.ctx, "runtime_client", None)
        if runtime_client is not None:
            # Stable and complete, or explicitly unavailable: a failed status
            # call never reads as idle (docs/protocol.md "Runtime status").
            from lib.runtime_release import status_view_for
            runtime_payload = status_view_for(runtime_client).block()
        from lib import runtime_attestation
        body = json.dumps({
            "session": session, "busy": busy,
            "runtime": runtime_payload,
            "delivery_admission": runtime_attestation.status_block(),
            "deployed_version": self.ctx.deployed_version(),
            "release_id": getattr(
                self.ctx, "deployed_release_id", lambda: "development")(),
        }).encode()
        self._send(200, body, "application/json")

    def _activate_transcription_if_default(self, installed_id: str) -> None:
        with _STT_ACTIVATION_LOCK:
            self._activate_transcription_if_default_locked(installed_id)

    def _activate_transcription_if_default_locked(self, installed_id: str) -> None:
        # Deliberately late-bound: tests monkeypatch lib.config.load, so the
        # module-level load_config alias would bypass the patch.
        from lib.config import load as load_config
        from lib.stt import (DisabledSTT, UnavailableSTT, WhisperSTT,
                             WhisperCppSTT, close_engine,
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
        # Load before publishing: the old engine serves meanwhile, and only a
        # loaded replacement takes over; the old engine's worker is then
        # stopped (VP-07). A load past the timeout is still published, so a
        # recovery retry waits on this exact loader rather than a duplicate.
        replacement.start_loading()
        if not replacement.load_done.wait(timeout=330.0):
            close_engine(self.ctx.replace_stt(replacement))
            raise RuntimeError(f"installed model did not load: {installed_id}")
        if replacement.load_error is not None:
            close_engine(replacement)
            raise RuntimeError(str(replacement.load_error))
        close_engine(self.ctx.replace_stt(replacement))

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
        self._json_ok(payload)

    def _transcription_guidance_payload(self):
        from lib.vocab import settings_payload
        return settings_payload(self.ctx.active_agent_names())

    def _handle_transcription_guidance_get(self):
        self._json_ok(self._transcription_guidance_payload())

    def _handle_transcription_guidance_post(self):
        from lib.vocab import update_guidance
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            update_guidance(data)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        payload = self._transcription_guidance_payload()
        payload["ok"] = True
        self._json_ok(payload)

    def _handle_transcription_providers_get(self):
        from lib import stt_providers
        self._json_ok(stt_providers.status())

    def _handle_transcription_providers_post(self):
        from lib import stt_providers
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        try:
            payload = stt_providers.update_settings(data)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        payload["ok"] = True
        self._json_ok(payload)

    # ---- vocabulary: packs, terms, profiles, assignments, preview, runs ----
    #
    # The transparency contract behind the iOS vocabulary screens: everything
    # the compiler will do is previewable, and everything it did is readable.

    def _query_first(self) -> dict[str, str]:
        return {k: (v[0] if v else "") for k, v in self._query().items()}

    def _handle_transcription_audio_get(self):
        """The clip the transcriber was given for one trace, if retained."""
        from lib import heard_audio
        trace_id = self._query_first().get("trace_id", "")
        kept = heard_audio.lookup(
            _runtime_paths().cache_dir, trace_id)
        if kept is None:
            return self._json_error(404, "no retained audio for that trace")
        path, meta = kept
        self._send_file(path, meta.get("content_type") or "application/octet-stream")

    def _handle_transcription_model_install(self):
        from lib.transcription_models import start_install
        from lib.server_identity import get_server_info
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        model_id = str(data.get("model_id") or "").strip()
        try:
            result = start_install(
                model_id, computer_id=str(get_server_info()["server_id"]),
                on_complete=self._activate_transcription_if_default)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        self._json(202, result)

    def _handle_transcription_model_remove(self):
        from lib.transcription_models import remove
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        model_id = str(data.get("model_id") or "").strip()
        try:
            remove(model_id)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        self._json_ok({"ok": True, "model_id": model_id})

    def _handle_hotseat_status(self):
        """The persisted Hotseat snapshot only: no CLI, subprocess or provider read."""
        from lib import hotseat_status
        self._send(200, json.dumps(hotseat_status.payload()).encode(), "application/json")

    def _hotseat_desk(self):
        """The desk object build_server put on the context, or None once this
        answered: 404 like any unknown route while the owner has the desk
        turned off ([hotseat] desk = false), 503 on a Host without the object."""
        from lib import server_identity
        if not server_identity.hotseat_desk_enabled():
            self._send(404, b"not found")
            return None
        desk = getattr(self.ctx, "hotseat_desk", None)
        if desk is None:
            self._json_error(503, "hotseat desk unavailable")
        return desk

    def _handle_hotseat_desk(self):
        """Full Hotseat TUI desk snapshot (`refresh=1` asks for a live CLI read;
        the desk allows one per minimum interval and says when the next may run)."""
        desk = self._hotseat_desk()
        if desk is None:
            return
        qs = self._query()
        force = any(str(v).lower() in {"1", "true", "yes"} for v in qs.get("refresh", []))
        self._send(200, json.dumps(desk.payload(refresh=force)).encode(), "application/json")

    def _handle_hotseat_switch(self):
        """Switch the machine-wide default Claude or Codex account via Hotseat."""
        desk = self._hotseat_desk()
        if desk is None:
            return
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        status, body = desk.switch_default(
            str(data.get("provider") or ""),
            str(data.get("alias") or ""),
        )
        return self._json(status, body)

    def _hotseat_send_prompt(self, session: str, text: str, client_msg_id: str) -> dict:
        """The desk's resume prompt, dispatched in this process the way every
        scheduler dispatches: no call to this Host's own listener, no admin
        token, nothing admitted in the administrator's name. `client_msg_id`
        is the stop's id, so the dispatcher starts at most one turn for it.

        `queue_if_busy`: the desk checked that the agent is idle, but that
        check cannot be atomic with the dispatch. A turn that started in
        between is waited for; without this an automation send preempts it.
        The answer says `queued` when the prompt waits behind such a turn."""
        from lib import turn_queue
        from lib.dispatch_adapters import ORIGIN_AUTOMATION
        from lib.turn_dispatch import UNCONFIRMED_DELIVERY
        try:
            result = TurnDispatchService(self.ctx).submit(DispatchCommand(
                text=text, requested_session=session, forced_session=session,
                trace_id=_trace.new_trace_id(), client_msg_id=client_msg_id,
                synthesize_audio=False, origin=ORIGIN_AUTOMATION, queue_if_busy=True))
        except DispatchError as exc:
            self._hotseat_withdraw_unstarted(session, client_msg_id)
            return {"ok": False, "error": f"{exc.status}: {exc}",
                    "unconfirmed": str(exc) == UNCONFIRMED_DELIVERY}
        return {"ok": True, "queued": bool(result.queued)}

    def _hotseat_withdraw_unstarted(self, session: str, queue_id: str) -> None:
        """A start that failed leaves the desk's prompt as a `queued` row of the
        agent's queue. The dispatcher has no proof that it never reached a
        backend, so nothing would ever run it: it would sit in the chat as a
        waiting message that cannot be sent and hold the agent's automation
        back. Take it out again. Only a row still `queued` is removed; one that
        was claimed or started belongs to a turn."""
        from lib import turn_queue
        try:
            if turn_queue.status(queue_id) != "queued" or not turn_queue.remove(queue_id):
                return
            agent = identity.lookup(session)
            if agent:
                self._broadcast_turn_queue(str(agent["agent_id"]), session)
            log("hotseatDeskPromptWithdrawn", f"session={session}")
        except Exception as exc:  # noqa: BLE001 - the failure itself is already being reported
            log_exception("hotseatDeskWithdrawFail", exc, detail=session)

    def _hotseat_unpause_queue(self, agent_id: str, session: str) -> dict:
        """Lift a Stop's pause; whoever owns turns picks the waiting ones up."""
        from lib import turn_queue
        changed = turn_queue.set_paused(agent_id, False)
        if changed:
            log("queueResumed", f"session={session} source=hotseat_desk")
            runtime_client = getattr(self.ctx, "runtime_client", None)
            try:
                if runtime_client is not None:
                    runtime_client.recover_queued()
                else:
                    TurnDispatchService(self.ctx).recover_queued()
            except Exception as exc:  # noqa: BLE001
                log_exception("queueResumeRecoverFail", exc, detail=session)
        self._broadcast_turn_queue(agent_id, session)
        return turn_queue.state(agent_id)

    def _handle_hotseat_resume(self):
        """Continue a stopped Clarp agent, unpause its queue, or get a native resume command."""
        desk = self._hotseat_desk()
        if desk is None:
            return
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        status, body = desk.resume_work(
            str(data.get("id") or ""),
            kind=str(data.get("kind") or "clarp"),
            backend=str(data.get("backend") or ""),
            action=str(data.get("action") or "continue"),
            send_prompt=self._hotseat_send_prompt,
            unpause_fn=self._hotseat_unpause_queue,
        )
        return self._json(status, body)

    def _handle_backend_usage(self):
        qs = self._query()
        force = any(str(v).lower() in {"1", "true", "yes"} for v in qs.get("refresh", []))
        payload = backend_usage.get_backend_usage(force_codex=force)
        for event in payload.get("limit_events") or []:
            events.broadcast(self.ctx.stream, events.as_event(event))
        body = json.dumps(payload).encode()
        self._send(200, body, "application/json")




    def _session_cwd(self, session: str) -> pathlib.Path:
        """cwd for an agent is whatever its DB row says — there's no
        live terminal process to query any more."""
        return session_cwd(session)

    def _handle_log(self):
        """Return the conversation turns for an agent's transcript.

        Query params:
          session=NAME   app session name (defaults to the focused agent)
          limit=N        cap the returned turns to the last N (default 100)
          after_revision=N  return only messages changed after SQLite revision N.
          before=MESSAGE_ID page older history ending before this message.
        """
        qs = self._query()
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
        from lib import agent_conversations
        from lib.message_previews import UnknownHistoryCursor
        if agent_conversations.is_pair_session(session):
            try:
                body_obj = agent_conversations.load_timeline(
                    session, after_revision=after_revision,
                    before_message_id=before_message_id, limit=limit)
            except UnknownHistoryCursor:
                return self._json_error(409, "unknown history cursor")
            return self._json_ok(body_obj)
        try:
            body_obj = load_conversation(
                session=session,
                after_revision=after_revision,
                before_message_id=before_message_id,
                limit=limit,
                include_automated=include_automated,
                include_tool_details=include_tool_details,
                interaction_id=self._interaction_id(),
                background_import=True,
            )
        except UnknownHistoryCursor:
            return self._json_error(409, "unknown history cursor")
        except OSError as e:
            log_exception("logParseFail", e, detail=session)
            return self._json_error(500, str(e))
        from lib import server_identity
        if server_identity.feature_advertised(server_identity.SEND_DELIVERY_STATE):
            try:
                error = self._add_delivery_state(body_obj, session, qs)
            except Exception as e:  # noqa: BLE001 - no cursor moves on a failed read
                log_exception("deliveryReadFail", e, detail=session)
                return self._json_error(500, "delivery state unavailable")
            if error:
                return self._json_error(400, error)
        body = json.dumps(body_obj).encode()
        self._send(200, body, "application/json")

    def _delivery_observation(self, *, fresh: bool, timeout: float):
        """ONE fresh runtime status RPC bounded as a whole by ``timeout`` (never the
        client's shared default): an OBSERVATION, never "current". None when it
        times out or fails; the caller counts every one, and the log line is
        coalesced (at most one per phase and exception type per 10 s, type name
        only)."""
        from lib import runtime_attestation
        client = getattr(self.ctx, "runtime_client", None)
        try:
            return client.status_within(timeout)
        except Exception as e:  # noqa: BLE001 - unavailable means omit
            due, label = runtime_attestation.log_due("o2_unavailable" if fresh else "o1_unavailable", e)
            if due:
                log("deliveryObservationFail", f"phase={'o2' if fresh else 'o1'} type={label}")
            return None

    def _add_delivery_state(self, body_obj: dict, session: str, qs: dict) -> str:
        """F4 (send_delivery_state): S2 on user rows and the seq feed, in ONE
        read snapshot, bracketed by two runtime observations (E2) and, with an
        external runtime, ONE fresh bounded manager read (B) between them.

        Fields are added only if O1 (before the snapshot) and O2 (a fresh RPC
        after it) both name the SAME capable, non-draining runtime identity
        (release_id, instance) with the SAME attestation, and B bound that
        attestation to the runtime unit's main process; otherwise nothing is
        added (omission is never evidence). The bracket shows that the same
        runtime process answered at both instants, that the manager named it
        main at B, and that no switch COMPLETED in between. It does NOT prove
        that this process was the only writer during the read, and a switch
        right after O2 is caught only by the next SERVED compatible scope (a
        cursor is valid only for the exact scope returned here). Without an
        external runtime the Host owns turns itself and uses its own
        per-process identity. Returns a 400 message, or "" when fine.

        Everything after the parameter checks is the OPTIONAL enrichment of a
        /log that already succeeded: any Exception there omits all delivery
        fields (counted by type, coalesced log), never turning into an error;
        BaseException is not caught."""
        from lib import runtime_attestation
        agent = agents_db.get_by_session(session)
        if not agent:
            return ""
        agent_id = agent["agent_id"]

        def number(name: str) -> int | None | str:
            raw = (qs.get(name, [""])[0] or "").strip()
            if raw == "":
                return None
            # ASCII digits only: str.isdigit() accepts e.g. "²", which int() rejects.
            if not (raw.isascii() and raw.isdigit()):
                return f"bad {name}"
            return int(raw)

        def text(name: str) -> str:
            return (qs.get(name, [""])[0] or "").strip()

        since, until = number("delivery_since"), number("delivery_until")
        generation = number("delivery_stop_generation")
        for value in (since, until, generation):
            if isinstance(value, str):
                return value
        bootstrap = (qs.get("delivery_bootstrap", [""])[0] or "").strip() in {"1", "true"}
        carried = (text("delivery_runtime_release"), text("delivery_runtime_instance"))
        try:
            fields = self._delivery_fields(body_obj, agent_id, since=since, until=until,
                                           generation=generation, bootstrap=bootstrap,
                                           carried=carried)
        except Exception as e:  # noqa: BLE001 - F1: the optional enrichment only; omit
            if runtime_attestation.note_unexpected(e):
                log("deliveryEnrichmentFail", f"type={runtime_attestation.type_label(e)}")
            return ""
        if fields is None:
            return ""
        rows, top = fields
        for turn in body_obj.get("turns") or []:
            if turn.get("role") == "user" and str(turn.get("id")) in rows:
                turn["delivery"] = rows[str(turn.get("id"))]
        body_obj.update(top)
        return ""

    def _delivery_fields(self, body_obj: dict, agent_id: str, *, since, until, generation,
                         bootstrap: bool, carried: tuple[str, str]):
        """The delivery fields of one /log bracket, or None to omit them all. Nothing
        is written into the response here: the caller merges only a complete result."""
        from lib import runtime_attestation
        # ROOT 17e71bf1: ONE monotonic deadline for this whole optional bracket.
        budget = runtime_attestation.Budget()
        try:
            return self._delivery_bracket(budget, body_obj, agent_id, since=since, until=until,
                                          generation=generation, bootstrap=bootstrap, carried=carried)
        finally:
            budget.finish()

    def _delivery_bracket(self, budget, body_obj: dict, agent_id: str, *, since, until, generation,
                          bootstrap: bool, carried: tuple[str, str]):
        from lib import launch_receipts, runtime_attestation, server_identity, settings_store
        client = getattr(self.ctx, "runtime_client", None)
        external = client is not None
        first = first_attestation = None
        if external:
            observed = self._first_observation(budget, client)
            if observed is None:
                budget.phase("o1")
                return None
            observed, reserved = observed
            try:
                budget.phase("o1")
                first = launch_receipts.runtime_identity(observed)
                if first is None:
                    return None                # O1 unusable: omit everything
                first_attestation = runtime_attestation.attestation_key(observed)
                # B: ONE fresh, bounded manager read for this bracket, outside any
                # transaction and before the read snapshot; never cached or reused.
                taken, reserved = reserved, False      # admit() gives a reserved permit back itself
                # No early-disconnect peek (ROOT 8efe6041): a peek on the request socket
                # waits under its 120 s timeout. A client that left may let this bounded
                # optional work finish (<= 2.0 s, 1 in flight, 2 starts/s); the response
                # write's clientGone handling covers the rest. No cancellation is claimed.
                admitted = runtime_attestation.admit(observed, in_transaction=db.conn().in_transaction,
                                                     budget=budget, reserved=taken)
            finally:
                if reserved:
                    runtime_attestation.release_reservation()
            budget.phase("b")
            if not admitted:
                return None
        # server_id() may mint and store the id on first use: read it before
        # the read snapshot, which must stay write-free.
        server_id = server_identity.server_id()
        users = [dict(turn) for turn in body_obj.get("turns") or []
                 if turn.get("role") == "user"]
        page = None
        if since is not None:
            # A plain since=0 is the first bootstrap page (packet v4 §1.3d).
            bootstrap = bootstrap or since == 0
        if budget.expired():
            runtime_attestation.note("deadline_exhausted")
            return None
        connection = db.conn()
        try:
            with settings_store.read_snapshot():
                # The deadline also bounds the SQL: past it, SQLite interrupts the
                # running statement. Cleared before the snapshot's COMMIT runs.
                connection.set_progress_handler(lambda: 1 if budget.expired() else 0, 1000)
                try:
                    epoch = launch_receipts.annotate_rows(agent_id, users)
                    if since is not None:
                        page = launch_receipts.delivery_page(
                            agent_id, since=since, until=until, stop_generation=generation,
                            bootstrap=bootstrap)
                finally:
                    connection.set_progress_handler(None, 0)
        except sqlite3.OperationalError:
            if not budget.expired():
                raise
            runtime_attestation.note("deadline_exhausted")
            return None
        budget.phase("snapshot")
        if external:
            cap = budget.cap(runtime_attestation.O2_CAP_S)
            if cap is None:
                runtime_attestation.note("deadline_exhausted")
                return None                    # nothing left for O2: nothing of the snapshot escapes
            observed = self._delivery_observation(fresh=True, timeout=cap)
            budget.phase("o2")
            if observed is None:
                runtime_attestation.note("o2_unavailable")
                return None
            second = launch_receipts.runtime_identity(observed)
            if (second is None or second != first
                    or runtime_attestation.attestation_key(observed) != first_attestation):
                runtime_attestation.note("o2_mismatch")
                return None                    # unusable or switched inside the bracket: omit
            release, instance = second
        else:
            release, instance = (launch_receipts.IN_PROCESS_RELEASE,
                                 launch_receipts.IN_PROCESS_INSTANCE)
        rows = {str(turn.get("id")): turn.get("delivery") for turn in users if "delivery" in turn}
        top = {"delivery_scope": {"server_id": server_id, "agent_id": agent_id,
                                  "runtime_release": release, "runtime_instance": instance},
               "delivery_stop_generation": epoch}
        if page is None:
            return rows, top
        continuation = not (bootstrap and since == 0 and until is None)
        if continuation and carried != (release, instance):
            # A cursor is valid only within the exact runtime scope it was
            # taken under (E3): any runtime restart or switch resets it.
            page = {"updates": [], "cursor": None, "more": False, "expired": True,
                    "until": None, "skipped": 0}
        if page["skipped"]:
            log("deliveryPointerSkipped", f"agent={agent_id} skipped={page['skipped']}")
        top.update(delivery_updates=page["updates"], delivery_cursor=page["cursor"],
                   delivery_more=page["more"], delivery_cursor_expired=page["expired"])
        if bootstrap and not page["expired"]:
            top.update(delivery_bootstrap=True, delivery_until=page["until"])
        return rows, top

    def _first_observation(self, budget, client):
        """O1 (ROOT 6f243300): the shared status window when it holds a success at most
        1 s old (no RPC; B stays fresh per bracket). On a miss the bracket first takes
        its probe permit (the same 1-in-flight / 2-per-second gate as B: no O1 RPC while
        it is exhausted), then makes ONE RPC of at most 0.5 s and offers the result to
        the shared window without waiting, never over a newer entry. A busy window
        (another caller's shared RPC) is an omission, not a wait or a second RPC.
        Returns (observation, permit_reserved) or None (omitted and counted)."""
        from lib import runtime_attestation
        shared = backends._RUNTIME_CLIENT is client
        state, cached, generation = (backends.peek_runtime_status(max_age=1.0) if shared
                                     else ("miss", None, 0))
        if state == "busy":
            runtime_attestation.note("status_contended")
            return None
        if state == "hit":
            return cached, False
        if not runtime_attestation.reserve(in_transaction=db.conn().in_transaction):
            return None
        # From here the permit is held: it goes back on EVERY path out (incl. a
        # log write or a BaseException raising), unless it is handed to the caller.
        observed, handed = None, False
        try:
            cap = budget.cap(runtime_attestation.O1_CAP_S)
            if cap is None:
                runtime_attestation.note("deadline_exhausted")
            else:
                fetched_at = backends._clock()
                observed = self._delivery_observation(fresh=False, timeout=cap)
                if observed is None:
                    runtime_attestation.note("o1_unavailable")
                elif shared:
                    backends.offer_runtime_status(client, observed, fetched_at=fetched_at,
                                                  generation=generation)
            handed = observed is not None
        finally:
            if not handed:
                runtime_attestation.release_reservation()
        return (observed, True) if handed else None

    def _interaction_id(self) -> str:
        value = str(self.headers.get("X-Clarp-Interaction-ID") or "").strip()
        if len(value) > 80 or any(ch not in "-0123456789abcdefABCDEF" for ch in value):
            return ""
        return value

    def _handle_message_tool_details(self):
        from lib import message_store
        qs = self._query()
        session = (qs.get("session", [""])[0] or "").strip()
        message_id = (qs.get("message_id", [""])[0] or "").strip()
        if not session or not message_id:
            return self._json_error(400, "session and message_id required")
        agent = identity.resolve(session)
        details = (message_store.message_tool_details(agent=agent, message_id=message_id)
                   if agent is not None else None)
        if details is None:
            return self._json_error(404, "message not found")
        self._json_ok(details)

    def _live_subscription(self) -> set[str] | None:
        """GET /events?live=a,b: the chats whose live item ops this client
        wants (docs/live-items.md §4); absent means no live events."""
        values = self._query().get("live")
        if values is None:
            return None
        return {part.strip() for value in values for part in value.split(",") if part.strip()}

    def _handle_live(self):
        """GET /live?session=…: the open turn's live items (docs/live-items.md §5)."""
        from lib import live_hub
        session = (self._query().get("session", [""])[0] or "").strip()
        agent = agents_db.get_by_session(session) if session else None
        if not agent:
            return self._json_error(404, "unknown session")
        agent_id = agent["agent_id"]
        runtime = getattr(self.ctx, "runtime_client", None)
        try:
            if runtime is not None:
                snapshot = runtime.live_snapshot(session=session, agent_id=agent_id) or {}
            else:
                hub = live_hub.current()
                snapshot = (hub.snapshot(session=session, agent_id=agent_id)
                            or {"epoch": hub.epoch, "missing": True}) if hub else {}
        except Exception as exc:  # noqa: BLE001
            log_exception("liveSnapshotFail", exc, detail=session)
            return self._json_error(503, "live items unavailable")
        if not snapshot.get("conv"):
            # Nothing has happened since the hub started: an idle, empty view.
            snapshot = live_hub.empty_snapshot(
                conv=agents_db.live_backend_session(agent_id) or "", session=session,
                agent_id=agent_id, epoch=str(snapshot.get("epoch") or ""))
        from lib import tool_explanation_settings
        snapshot["tool_explanations"] = tool_explanation_settings.get()
        self._json_ok(snapshot)

    def _sse(self):
        last_event_id = ""
        try:
            qs = self._query()
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
        q = self.ctx.stream.subscribe(live=self._live_subscription())
        from lib import agent_rings
        from lib.http_utils import principal_of
        try:
            sse_principal = principal_of(self).principal
        except Exception:  # noqa: BLE001 - unknown client: it sees no Live ring
            sse_principal = None
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            cursor = replay_after
            while True:
                page = self.ctx.stream.recent(cursor)
                for ev in page:
                    if not agent_rings.event_visible(ev, sse_principal):
                        continue
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
            self.wfile.write(
                b"data: " + json.dumps(events.agent_roster()).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=SERVER_TIMING.sse_queue_timeout_sec)
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        ev = {}
                    if not agent_rings.event_visible(ev, sse_principal):
                        continue
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
                        except Exception as diagnostic_error:
                            _note_http_failure("H04", diagnostic_error)
                        break
                    # A data event, not a `: ping` comment: EventSource
                    # never surfaces comments, so a quiet stream looked dead
                    # to the web's stale check and was reconnected.
                    beat = json.dumps(events.heartbeat(ts=int(time.time() * 1000)))
                    self.wfile.write(f"event: heartbeat\ndata: {beat}\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            try:
                eventlog.emit("server", "sseClientGone", level="info")
            except Exception as diagnostic_error:
                _note_http_failure("H05", diagnostic_error)
        finally:
            self.ctx.stream.unsubscribe(q)

    # --- POST dispatch ---------------------------------------------------

    def _reject_janitor_control(self, data) -> bool:
        """Old clients cannot use chat controls to operate a maintenance agent."""
        if not isinstance(data, dict):
            return False
        session = str(data.get("session") or data.get("replace_sid") or "").strip()
        if not session:
            session = str(getattr(self.ctx, "default_session", "") or "")
        agent = identity.lookup(session) if session else None
        if not agent or not agent.get("is_janitor"):
            return False
        self._json(403, {
 "error": "Janitors are inspection-only. Use their Pause or Configure controls.",
 "code": "janitor_inspection_only",
        })
        return True

    def do_POST(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._oracle_auth_missing(path):
            return self._json_error(401, "Oracle requires authenticated full-device access")
        if self._device_forbidden(path, "POST"):
            return self._json_error(403, "full device access required")
        return route_registry.dispatch(self, "POST", path)

    def do_PUT(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._device_forbidden(path, "PUT"):
            return self._json_error(403, "full device access required")
        return route_registry.dispatch(self, "PUT", path)

    def _handle_live_activity_register(self):
        """POST /devices/live-activity: register (or with "ended": true,
        drop) an ActivityKit push token (docs/live-items.md §8)."""
        from lib import live_activity
        data = self._read_json()
        if not isinstance(data, dict):
            return self._json_error(400, "bad json")
        if data.get("ended") is True:
            removed = live_activity.unregister(
                activity_id=str(data.get("activity_id") or ""), token=str(data.get("token") or ""))
            return self._json_ok({"ok": True, "removed": removed})
        try:
            row = live_activity.register(
                token=str(data.get("token") or ""), activity_id=str(data.get("activity_id") or ""),
                kind=str(data.get("kind") or "agents-working"),
                environment=str(data.get("environment") or ""),
                principal=self._push_owner())
        except ValueError as error:
            return self._json_error(400, str(error))
        return self._json_ok({"ok": True, "activity_id": row["activity_id"]})

    def _push_owner(self) -> str:
        """The paired device a push registration belongs to ('' for the administrator and others),
        so its pushes stop when that device is revoked."""
        who = self.principal()
        return who.principal if who.full_scope and who.principal.startswith("device_") else ""

    def _handle_live_activity_delete(self):
        from lib import live_activity
        data = self._read_json() if int(self.headers.get("Content-Length") or 0) else {}
        query = self._query()
        data = data if isinstance(data, dict) else {}
        removed = live_activity.unregister(
            activity_id=str(data.get("activity_id") or query.get("activity_id", [""])[0] or ""),
            token=str(data.get("token") or query.get("token", [""])[0] or ""))
        return self._json_ok({"ok": True, "removed": removed})

    # --- POST handlers ---------------------------------------------------

    # ---- group calls (docs/group-calls.md) ----

    def _turn_caller(self) -> str:
        """The agent whose Clarp turn sent this request (X-Clarp-Turn), or ""
        when the request carries no turn token. A token whose lookup fails is
        unknown identity, not "no turn": it raises TurnIdentityUnavailable,
        which handle_one_request answers with a retryable 503 (HTTP-N5)."""
        from lib import turn_identity
        from lib.provider_background_jobs import TURN_ENV
        try:
            return turn_identity.caller_agent_id(
                {TURN_ENV: (getattr(self, "headers", None) or {}).get("X-Clarp-Turn", "")})
        except Exception as exc:  # noqa: BLE001 - fail closed: refused, logged and retryable
            log_exception("turnCallerLookupFail", exc)
            raise turn_identity.TurnIdentityUnavailable() from exc

    # ---- agent rings (docs/agent-rings.md) ----

    # Agent-live rings (WIRE-v2): the Live call's socket and its owner's controls.
    def _handle_calls_ring_live_socket(self):
        from lib.agent_ring_live_socket import serve
        return serve(self)

    def _handle_oracle_v2(self):
        from lib.oracle_mode import voice_handler
        return voice_handler(self.path)(self)

    def _handle_oracle_realtime(self):
        from lib.oracle_realtime import serve
        return serve(self)

    def _handle_stop(self):
        """Terminate any in-flight clarp turn for the given agent. With
        there is no long-lived subprocess to send Escape to; instead the per-turn
        clarp subprocess is registered in the backend's process registry and we
        SIGTERM the lot."""
        data = self._read_json() or {}
        if self._reject_janitor_control(data):
            return
        session = (data.get("session") or self.ctx.default_session).strip() or self.ctx.default_session
        actor, verified = self._control_actor(data)
        reason = str(data.get("reason") or "").strip()[:200]
        effects: dict = {}
        try:
            n = self._stop_agent_session(session, strict=False, actor=actor,
                                         actor_verified=verified, reason=reason,
                                         effects=effects)
        except StopNotRecorded:
            return self._json_error(503, "stop was not recorded; send it again")
        # queue_paused and goals_paused appear only as committed effects read
        # back after the Stop; absent means unknown, never "not paused".
        return self._json_ok({"ok": True, "terminated": n, "stop_actor": actor,
                              "stop_actor_verified": verified,
                              "stop_reason": reason, **effects})

    def _control_actor(self, data: dict) -> tuple[str, bool]:
        """Who is stopping or repairing, and whether the Host verified it.

        An agent is verified by the Host-issued identity of the turn its tool
        runs in (X-Clarp-Turn, lib.turn_identity); a paired device acts for
        its user. Anything else is the caller's claim (default "user") and is
        reported as unverified; a verified identity replaces the claim. A turn
        token whose lookup fails is refused (retryable 503 through _turn_caller),
        never read as the claim: an agent's Stop applied as the user's would
        withdraw the user's sends (HTTP-N5)."""
        claimed = str(data.get("actor") or "").strip()[:80] or "user"
        agent_id = self._turn_caller()
        agent = identity.lookup(agent_id) if agent_id else None
        if agent:
            actor = f"agent:{agent['session']}"
            if claimed not in {"user", actor}:
                log("controlActorClaimReplaced",
                    f"claimed={claimed!r} verified={actor}")
            return actor, True
        principal = str(getattr(self, "_request_principal", "") or "")
        if claimed == "user" and principal and principal != "administrator":
            return "user", True
        return claimed, False

    def _handle_repair_slot(self):
        """Give back an agent's slot that no process, launch, account recovery,
        terminal or Stop owns, and let its queued work run. Unlike Stop it
        interrupts nothing and leaves the queue's and goals' pause alone."""
        from lib.turn_dispatch import TurnDispatchService
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        if self._reject_janitor_control(data):
            return
        session = str(data.get("session") or "").strip()
        agent = identity.lookup(session) if session else None
        if not agent:
            return self._json_error(404, "agent not found")
        actor, verified = self._control_actor(data)
        reason = str(data.get("reason") or "").strip()[:200]
        if not reason:
            return self._json_error(400, "reason is required")
        runtime_client = getattr(self.ctx, "runtime_client", None)
        try:
            if runtime_client is not None:
                result = runtime_client.repair_slot(
                    agent["agent_id"], actor=actor, reason=reason,
                    actor_verified=verified)
            else:
                result = TurnDispatchService(self.ctx).repair_slot(
                    agent["agent_id"], actor=actor, reason=reason,
                    actor_verified=verified)
        except ValueError as exc:
            return self._json_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - the caller sees the failure
            log_exception("slotRepairFail", exc, detail=session)
            return self._json_error(502, f"slot repair failed: {exc}")
        self._broadcast_turn_queue(agent["agent_id"], session)
        return self._json_ok({"ok": True, "session": session,
                              "released_trace": result["released_trace"],
                              "queue_recovered": result["queue_recovered"],
                              "repair_actor": actor,
                              "repair_actor_verified": verified,
                              "repair_reason": reason})

    def _stop_agent_session(
        self, session: str, *, strict: bool, defer_finish: bool = False,
        actor: str = "user", actor_verified: bool = False, reason: str = "",
        effects: dict | None = None,
    ):
        """Authoritative local stop shared by /stop and Oracle cancellation."""
        from lib import tts_queue
        from lib import turn_dispatch
        from lib import turn_queue
        agent = identity.lookup(session)
        n = 0
        dropped = 0
        if agent:
            agent_id = agent["agent_id"]
            # Only the user's Stop withdraws the agent's admitted-but-unlaunched
            # user sends. A Stop by another agent, proven by the Host-issued turn
            # identity (never a claimed name), stops the work but leaves them.
            fence_user_receipts = not (actor_verified and str(actor).startswith("agent:"))
            runtime_client = getattr(self.ctx, "runtime_client", None)
            remote_lease = None
            if runtime_client is not None:
                try:
                    stop_options = {"strict": strict, "hold": True}
                    if not fence_user_receipts:
                        stop_options["fence_user_receipts"] = False
                    remote_lease = runtime_client.begin_stop(
                        agent_id,
                        backends.normalize(agent.get("backend")),
                        **stop_options,
                    )
                    n = remote_lease.terminated
                    dropped = remote_lease.dropped
                    stop_snapshot = {"trace_id": remote_lease.trace_id}
                except Exception as exc:
                    log_exception("turnStopRuntimeFail", exc, detail=agent_id)
                    if strict:
                        raise
                    return 0
            else:
                stop_snapshot = turn_dispatch.snapshot_stop_state(agent_id)
                queue_was_paused = bool(turn_queue.state(agent_id)["paused"])
                dropped = turn_dispatch.clear_for_agent(
                    agent_id, preserve_queue=True, pause_queue=True,
                    user_stop=fence_user_receipts)
                try:
                    n = backends.interrupt(
                        backends.normalize(agent.get("backend")), agent_id)
                except Exception as exc:  # barrier still must be released below
                    log_exception("turnStopInterruptFail", exc, detail=agent_id)
                    if strict:
                        turn_dispatch.restore_stop_state(agent_id, stop_snapshot)
                        turn_queue.set_paused(agent_id, queue_was_paused)
                        raise
                if (strict and stop_snapshot.get("trace_id") and n <= 0
                        and not stop_snapshot.get("account_recovery_parked")):
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
            # Pausing goals must not skip recording the stop and its events.
            if fence_user_receipts:
                # The user's Stop drops the agent's speech not yet synthesized
                # instead of speaking it late (VP-06).
                try:
                    tts_queue.cancel(agent_id, reason="user stop")
                except Exception as exc:  # stop already succeeded
                    log_exception("turnStopSpeechCancelFail", exc, detail=agent_id)
            try:
                from lib import goal_ledger, task_plans
                if actor == "user":
                    stopped_by = "User stopped this agent"
                else:
                    stopped_by = f"Stopped by {actor}" + (
                        "" if actor_verified else " (unverified)")
                with goal_ledger.acting_as("user"):
                    goals_paused = task_plans.pause_recovery_for_agent(
                        agent_id, f"{stopped_by}: {reason}" if reason else stopped_by)
                if effects is not None:
                    effects["goals_paused"] = int(goals_paused)
            except Exception as exc:  # stop already succeeded
                log_exception("turnStopBookkeepingFail", exc, detail=agent_id)
            stop_unrecorded: Exception | None = None
            try:
                try:
                    stopped = turn_lifecycle.try_transition(
                        agent_id, TurnEvent.STOP_REQUESTED,
                        {"source": "user_stop", "message": "Turn stopped",
                         "stop_actor": actor, "stop_actor_verified": actor_verified,
                         "stop_reason": reason})
                except Exception as exc:  # the write failed after its bounded retries
                    # The barrier is still released below; the caller hears the
                    # Stop was not recorded and can send it again (TP-N1b).
                    log_exception("turnStopRecordFail", exc, detail=agent_id)
                    stop_unrecorded = exc
                    raise
                if getattr(self.ctx, "stream", None) is not None:
                    if stopped is not None:
                        # The persisted row's ts and detail, as the state
                        # watcher replays it: a ts-less event gets a client's
                        # clock, and the watcher's older ts then looks stale.
                        events.broadcast(self.ctx.stream, events.agent_state(
                            session=session, agent_id=agent_id,
                            kind=stopped.to_state, persona=agent.get("persona"),
                            ts=stopped.ts, detail=stopped.detail))
                    else:
                        # Nothing was recorded (an agent with no state yet).
                        events.broadcast(self.ctx.stream, events.agent_state(
                            session=session, agent_id=agent_id,
                            kind=AgentState.INTERRUPTED))
                    queue_state = turn_queue.state(agent_id)
                    events.broadcast(self.ctx.stream, events.queue_updated(
                        session=session, agent_id=agent_id,
                        queue_depth=queue_state["count"],
                        queue_paused=queue_state["paused"],
                        queue_started=False,
                        queue_revision=queue_state["revision"]))
            except Exception as exc:  # stop already succeeded
                log_exception("turnStopBookkeepingFail", exc, detail=agent_id)
            if effects is not None:
                try:
                    effects["queue_paused"] = bool(
                        turn_queue.state(agent_id)["paused"])
                except Exception as exc:  # noqa: BLE001 - absent means unknown
                    log_exception("turnStopQueueReadFail", exc, detail=agent_id)
            if defer_finish:
                def release(cancelled_trace_ids: set[str]) -> None:
                    if remote_lease is not None:
                        runtime_client.finish_stop(
                            remote_lease.lease_id, cancelled_trace_ids)
                    else:
                        turn_dispatch.prepare_queued_for_finish(
                            agent_id, stop_snapshot, cancelled_trace_ids)
                        turn_dispatch.finish_stop(
                            self.ctx, agent_id, backend_registry=backends)
                # A deferred Stop (Oracle cancellation) completes even when its
                # row could not be written: its caller releases the barrier
                # through ``release``; the failure is logged (turnStopRecordFail).
                log("turnStop", f"session={session} terminated={n} barrier=held")
                return n, release
            elif runtime_client is not None:
                runtime_client.finish_stop(remote_lease.lease_id, set())
            else:
                turn_dispatch.finish_stop(
                    self.ctx, agent_id, backend_registry=backends)
            if stop_unrecorded is not None:
                raise StopNotRecorded(agent_id) from stop_unrecorded
        log("turnStop", f"session={session} terminated={n} actor={actor} "
                        f"reason={reason!r} queued_preserved={turn_queue.pending_count(agent_id) if agent else 0}")
        if defer_finish:
            return n, (lambda _trace_ids: None)
        return n

    def do_DELETE(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if self._device_forbidden(path, "DELETE"):
            return self._json_error(403, "full device access required")
        return route_registry.dispatch(self, "DELETE", path)

    def do_PATCH(self):
        if not self._authorized():
            return self._reject_unauthorized()
        path = self.path.split("?", 1)[0]
        if path.startswith("/schedules/") and self._device_forbidden(path, "PATCH"):
            # Only the route: an unmatched PATCH keeps its 404 (HTTP-02).
            return self._json_error(403, "full device access required")
        return route_registry.dispatch(self, "PATCH", path)

    def _handle_transcription_result_delete(self, job_id: str):
        from lib import transcription_results
        try:
            job_id = transcription_results.normalize_job_id(unquote(job_id))
        except ValueError as e:
            return self._json_error(400, str(e))
        transcription_results.delete(job_id)
        return self._json_ok({"ok": True})

    def _handle_artifact_events(self, artifact_id: str, *, write: bool = True):
        # Telemetry may contain personal answers; limited devices cannot read it.
        if getattr(self, "_request_device_scope", "") == "limited":
            return self._json_error(403, "full device access required")
        from lib import html_form_events
        try:
            if write:
                return self._json_ok(html_form_events.append(artifact_id, self._read_json()))
            query = parse_qs(urlparse(self.path).query)
            through = query.get("through", [None])[0]
            result = html_form_events.read(artifact_id, after=int(query.get("after", [0])[0]),
                limit=int(query.get("limit", [100])[0]), through=int(through) if through is not None else None)
            format = query.get("format", ["json"])[0]
            if format == "jsonl":
                body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in result["events"]).encode()
                return self._send(200, body, "application/x-ndjson")
            if format != "json": raise ValueError("unsupported event format")
            return self._json_ok(result)
        except (ValueError, TypeError) as exc:
            return self._json_error(409 if write else 400, str(exc))

    def _deliver_pending_decisions(self) -> set[str]:
        return _dispatch_adapters(self.ctx).deliver_pending_decisions()

    def _handle_turn_queue(self):
        from lib import turn_queue
        query = self._query()
        session = (query.get("session", [self.ctx.default_session])[0]
                   or self.ctx.default_session).strip()
        agent = identity.lookup(session)
        if not agent:
            return self._json_error(404, "agent not found")
        state = turn_queue.state(agent["agent_id"])
        items = [{
            "id": row["queue_id"],
            "text": row["text"],
            "enqueued_at": row["enqueued_at"],
            "synthesize_audio": bool(row["synthesize_audio"]),
        } for row in turn_queue.pending(agent["agent_id"])]
        return self._json_ok({
            "items": items,
            "paused": state["paused"],
            "revision": state["revision"],
        })

    def _handle_turn_queue_resume(self):
        """Lift a Stop's pause and let the waiting messages run in order."""
        from lib import turn_queue
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        session = str(data.get("session") or "").strip()
        agent = identity.lookup(session) if session else None
        if not agent:
            return self._json_error(404, "agent not found")
        changed = turn_queue.set_paused(agent["agent_id"], False)
        if changed:
            log("queueResumed", f"session={session}")
            # The runtime owns turns; when it is reachable let it pick the waiting
            # items up, otherwise this process dispatches them itself.
            runtime_client = getattr(self.ctx, "runtime_client", None)
            try:
                if runtime_client is not None:
                    runtime_client.recover_queued()
                else:
                    TurnDispatchService(self.ctx).recover_queued()
            except Exception as e:  # noqa: BLE001
                log_exception("queueResumeRecoverFail", e, detail=session)
        self._broadcast_turn_queue(agent["agent_id"], session)
        state = turn_queue.state(agent["agent_id"])
        return self._json_ok({"session": session, "paused": state["paused"],
                              "count": state["count"], "revision": state["revision"]})

    def _broadcast_turn_queue(self, agent_id: str, session: str) -> None:
        from lib import turn_queue
        if getattr(self.ctx, "stream", None) is None:
            return
        state = turn_queue.state(agent_id)
        events.broadcast(self.ctx.stream, events.queue_updated(
            session=session, agent_id=agent_id,
            queue_depth=state["count"], queue_paused=state["paused"],
            queue_started=False, queue_revision=state["revision"]))

    def _handle_turn_queue_update(self, queue_id: str):
        from lib import turn_queue
        queue_id = unquote(queue_id)
        if turn_queue.status(queue_id) == "unknown":
            # A listen-first opening whose launch cannot be known: the durable
            # audit, never queued work (docs/agent-rings.md "Claimed once").
            return self._json_error(409, "opening launch unknown")
        row = turn_queue.get(queue_id)
        data = self._read_json()
        if row and self._reject_janitor_control({"session": row["session"]}):
            return
        text = str((data or {}).get("text") or "").strip()
        if data is None or not text:
            return self._json_error(400, "text required")
        if not row or not turn_queue.update_text(queue_id, text):
            return self._json_error(404, "queued message not found")
        self._broadcast_turn_queue(str(row["agent_id"]), str(row["session"]))
        return self._json_ok({"ok": True})

    def _handle_turn_queue_delete(self, queue_id: str):
        from lib import turn_queue
        queue_id = unquote(queue_id)
        if turn_queue.status(queue_id) == "unknown":
            return self._json_error(409, "opening launch unknown")      # kept as the audit
        row = turn_queue.get(queue_id)
        if row and self._reject_janitor_control({"session": row["session"]}):
            return
        if not row or not turn_queue.remove(queue_id):
            return self._json_error(404, "queued message not found")
        self._broadcast_turn_queue(str(row["agent_id"]), str(row["session"]))
        return self._json_ok({"ok": True})

    def _handle_turn_queue_send(self, queue_id: str):
        try:
            result = TurnDispatchService(self.ctx).dispatch_queued(unquote(queue_id))
        except DispatchError as exc:
            return self._json_error(exc.status, str(exc))
        return self._json(202, {
 "ok": True,
 "session": result.session,
 "queued": result.queued,
 "queue_depth": result.queue_depth,
 "queue_revision": result.queue_revision,
        })

    def _expected_generation(self) -> tuple[bool, int | None]:
        """`expected_generation` from the query, else the JSON body: (valid, value).

        Absent means the legacy unfenced cancel. The query parameter wins over
        the body. It must be a positive integer up to 2**53 - 1: in the query a
        decimal string of digits, in JSON an integer (never a boolean).
        """
        from lib.background_jobs import MAX_GENERATION
        values = self._query().get("expected_generation")
        if values is not None:
            raw = values[0].strip()
            if not raw.isdigit():
                return False, None
            value = int(raw)
            return (True, value) if 1 <= value <= MAX_GENERATION else (False, None)
        if int(self.headers.get("Content-Length") or 0) <= 0:
            return True, None
        body = self._read_json()
        if body is None:
            return False, None
        if "expected_generation" not in body:
            return True, None
        raw = body["expected_generation"]
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_GENERATION:
            return False, None
        return True, raw

    def _handle_background_job_cancel(self, job_id: str):
        from lib import background_jobs
        valid, expected = self._expected_generation()
        if not valid:
            return self._json(400, {"ok": False, "error": "invalid expected_generation"})
        outcome = background_jobs.cancel_run(unquote(job_id), expected_generation=expected)
        job, cancellation_changed = outcome["job"], outcome["changed"]
        if not job:
            return self._json_error(404, "job not found")
        receipt = {
            "changed": cancellation_changed,
            "job": job,
            "job_handle": background_jobs.job_handle(job),
        }
        if outcome["mismatch"] == "unsupported":
            return self._json(409, {"ok": False, "error": "provider_task_cancel_unsupported", **receipt})
        if outcome["mismatch"] == "generation":
            return self._json(409, {
                "ok": False, "error": "generation_mismatch", "expected_generation": expected,
                "current_generation": int(job.get("generation") or 1), **receipt,
            })
        if job["status"] != "cancelled":
            return self._json(409, {
                    "ok": False, "error": f"job already {job['status']}", **receipt,
                })
        # Cancellation immediately closes the durable gate checked by workers
        # before each delivery/action. The prompt is cleanup: it lets the owner
        # tear down or reconfigure a shared process, but correctness does not
        # depend on that best-effort dispatch succeeding. A computer-owned job
        # (no session) has no owner to clean up; an empty forced session would
        # route the prompt to the default or sticky agent (AUT-04).
        if cancellation_changed and job.get("session"):
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
                TurnDispatchService(self.ctx).submit(DispatchCommand(
                    text=(f"the user cancelled background job {job['job_id']}: "
                          f"{job['title']} (handle {cancelled_handle}; "
                          f"{worker_identity}). Before stopping anything, run "
                          f"`{gate_command}`. Proceed only if it returns 0 and "
                          "the target process still matches that exact worker identity; "
                          "otherwise this cleanup was superseded by a newer generation."),
                    requested_session=job["session"],
                    forced_session=job["session"],
                    trace_id=_trace.new_trace_id(),
                    synthesize_audio=False,
                    origin="automation",
                ))
            except Exception as exc:
                log_exception("backgroundJobCancelPromptFail", exc, detail=job_id)
        return self._json_ok({"ok": True, **receipt})

    def _bookkeeping_lineage_refusal(self, *names: str) -> str:
        """Why an agent may not be created or forked here, or "".

        A bookkeeping delegate is held to its scope; an agent it starts would not
        be. The creator is the turn identity the CLI sent (X-Clarp-Turn), not a
        name in the request; the named parent or fork source is checked too."""
        from lib import goal_ledger
        creator = self._turn_caller()   # a failed lookup is a retryable 503 (HTTP-N5)
        candidates = [creator] + [(identity.lookup(n) or {}).get("agent_id", "") for n in names if n]
        if any(goal_ledger.descends_from_active_delegate(a) for a in candidates if a):
            return "a bookkeeping delegate does not start helpers or forks"
        return ""


    def _send_via_orchestrator(self, req: SendRequest, prompt_admission,
                               only_sessions: tuple[str, ...] | None = None) -> bool:
        """Hands-free routing. Returns True when a reply was written; False
        means the orchestrator declined and the caller dispatches directly."""
        from lib import transcription_results
        orchestrated = OrchestratorService(self.ctx).handle_send(
            text=req.text,
            requested_session=req.session,
            trace_id=req.trace_id,
            prompt_admission=prompt_admission,
            hands_free=req.hands_free,
            synthesize_audio=req.synthesize_audio,
            unheard_audio_sessions=req.unheard_audio_sessions,
            dispatch=TurnDispatchService(self.ctx).dispatch,
            fallback_request=req.orchestrator_fallback,
            only_sessions=only_sessions,
        )
        if (req.call_id and orchestrated is not None and orchestrated.ok
                and orchestrated.session and orchestrated.session != req.session):
            from lib import group_calls
            from lib.http_utils import principal_of
            group_calls.routed(principal_of(self).principal, req.call_id, orchestrated.session)
        if orchestrated is None and req.orchestrator_fallback:
            body = {
                "ok": True,
                "session": "",
                "dispatch": "",
                "trace_id": req.trace_id,
                "orchestrator": {
                    "action": "disabled",
                    "decision_id": None,
                    "decision": None,
                },
            }
            if req.transcription_id:
                transcription_results.delete(req.transcription_id)
            self._json_ok(body)
            return True
        if orchestrated is None or (
            orchestrated.action == FINAL_FALLBACK and not req.orchestrator_fallback
        ):
            return False
        status = orchestrated.status
        body = {
            "ok": orchestrated.ok,
            "session": orchestrated.session,
            "dispatch": orchestrated.dispatch,
            "trace_id": req.trace_id,
            "orchestrator": {
                "action": orchestrated.action,
                "decision_id": orchestrated.decision_id,
                "decision": orchestrated.decision,
            },
        }
        if orchestrated.error:
            body["error"] = orchestrated.error
        if orchestrated.ok and req.transcription_id:
            transcription_results.delete(req.transcription_id)
        if orchestrated.ok and req.voice_utterance_id:
            self._record_voice(
                "send", session=orchestrated.session or None,
                trace_id=req.trace_id, utterance_id=req.voice_utterance_id,
                text=req.text, detail={"orchestrator": orchestrated.action,
                                       "dispatch": orchestrated.dispatch,
                                       "client_msg_id": req.client_msg_id})
        self._json(status, body)
        return True

    def _send_direct(self, req: SendRequest, prompt_admission, opening=None) -> None:
        from lib import transcription_results
        try:
            result = TurnDispatchService(self.ctx).submit(DispatchCommand(
                text=req.text, requested_session=req.session,
                trace_id=req.trace_id,
                synthesize_audio=req.synthesize_audio,
                forced_session=req.session if req.force_session else "",
                client_msg_id=req.client_msg_id,
                origin=req.origin, sender_agent_id=req.sender_agent_id,
                prompt_admission=prompt_admission,
                queue_if_busy=req.queue_if_busy,
                unheard_audio_sessions=tuple(req.unheard_audio_sessions),
            ))
        except DispatchError as e:
            if opening is not None and opening["route"] == "greeting":
                self._greeting_admitted(opening, req, ok=False)
            return self._send(e.status, str(e).encode(), "text/plain")
        if opening is not None and opening["route"] == "greeting":
            self._greeting_admitted(opening, req, ok=True)
        if req.origin == "agent" and req.sender_agent_id:
            self._note_helper_message(req.sender_agent_id, result.session)
        if req.transcription_id:
            transcription_results.delete(req.transcription_id)
        # File the timeline row before answering: a client that reads
        # /voice-events straight after its 200 must already see this turn.
        if req.voice_utterance_id:
            self._record_voice(
                "send", session=result.session, trace_id=req.trace_id,
                utterance_id=req.voice_utterance_id, text=req.text,
                detail={"dispatch": result.backend, "queued": result.queued,
                        "client_msg_id": req.client_msg_id,
                        "hands_free": req.hands_free})
        body = {"ok": True, "session": result.session,
                "dispatch": result.backend,
                "queued": result.queued,
                "queue_depth": result.queue_depth,
                "queue_revision": result.queue_revision,
                "trace_id": req.trace_id}
        if opening is not None:
            body["opening"] = opening["opening"]
        # F4: a /send 200 carries no delivery state (the client learns it only
        # from a bracketed GET /log); any delivery a dispatch result still
        # carries internally is never forwarded.
        self._json_ok(body)

    @staticmethod
    def _greeting_admitted(opening, req, *, ok: bool) -> None:
        from lib import agent_rings
        agent_rings.greeting_admitted(opening["opening"]["ring_id"], req.client_msg_id, ok=ok)

    def _note_helper_message(self, sender_agent_id: str, target_session: str) -> None:
        """A helper reporting to its parent, or a parent re-tasking it."""
        from lib import helper_agents
        try:
            target = identity.resolve(target_session)
            if target is not None:
                helper_agents.note_agent_message(
                    self.ctx.stream, sender_agent_id=sender_agent_id,
                    target_agent_id=target.agent_id)
        except Exception as e:  # noqa: BLE001 - never fail an admitted send
            log_exception("helperMessageNoteFail", e, detail=sender_agent_id)

    def _handle_transcribe(self):
        from lib import transcription_results

        requested_model = (self.headers.get("X-Transcription-Model") or "").strip()
        # Voice timeline identity: the client names the utterance (defaults to
        # its idempotent transcription id) and may stamp its own clock.
        utterance_id = (self.headers.get("X-Utterance-ID")
                        or self.headers.get("X-Transcription-ID") or "").strip()[:128]
        client_ts = _header_int(self.headers.get("X-Client-Ts"))
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError as e:
            log_exception("transcribeReadFail", e)
            self._record_voice_corrupt(utterance_id, client_ts, "bad_content_length")
            return self._json_error(400, str(e))
        if n <= 0 or n > 64 * 1024 * 1024:  # bounded long-recording uploads (64 MiB)
            self._record_voice_corrupt(utterance_id, client_ts, "bad_size", bytes=n)
            return self._json_error(400, "bad size")
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
            self._record_voice_corrupt(utterance_id, client_ts, "upload_read_error",
                                       expected=n, error=str(e)[:200])
            return self._json_error(408, "incomplete audio upload")
        if len(audio_bytes) != n:
            log("transcribeReadIncomplete",
                f"expected={n} received={len(audio_bytes)}")
            self.close_connection = True
            self._record_voice_corrupt(utterance_id, client_ts, "incomplete_upload",
                                       expected=n, received=len(audio_bytes))
            return self._json_error(408, "incomplete audio upload")

        ctype = self.headers.get("Content-Type", "audio/webm")
        hands_free = _truthy_header(self.headers.get("X-Hands-Free"))
        try:
            transcription_id = transcription_results.normalize_job_id(
                self.headers.get("X-Transcription-ID"))
        except ValueError as e:
            return self._json_error(400, str(e))
        fingerprint = transcription_results.request_fingerprint(
            audio_bytes, ctype, requested_model, hands_free)
        call_scope = None
        if self.headers.get("X-Clarp-Call-Id") is not None:
            # A clip that names its call is validated before anything else,
            # a cached result and any provider included: it refuses rather
            # than silently becoming chat transcription (docs/group-calls.md).
            from lib import call_transcription, group_calls
            try:
                call_scope = call_transcription.validate_scope(
                    self.principal().principal, self.headers.get("X-Clarp-Call-Id") or "",
                    (self.headers.get("X-Transcription-Session") or "").strip())
            except group_calls.CallError as exc:
                return self._json(exc.status, exc.body())

        # A retry can arrive while the original request is still finishing.
        # Serialize equal IDs, then consult the durable result before invoking
        # Whisper so a lost HTTP response never causes duplicate inference.
        with transcription_results.serialize(transcription_id):
            try:
                cached = transcription_results.load(transcription_id, fingerprint)
            except transcription_results.JobIDCollisionError as e:
                return self._json_error(409, str(e))
            if cached is not None:
                cached["cached"] = True
                self._record_voice("retry", utterance_id=utterance_id,
                                   client_ts=client_ts,
                                   trace_id=cached.get("trace_id"),
                                   text=cached.get("text"),
                                   detail={"bytes": n, "content_type": ctype})
                return self._json_ok(cached)
            target = self._transcription_session()
            outcome = run_transcription(
                self.ctx, audio_bytes=audio_bytes, ctype=ctype,
                hands_free=hands_free, requested_model=requested_model,
                transcription_id=transcription_id, fingerprint=fingerprint,
                utterance_id=utterance_id, client_ts=client_ts,
                record_voice=self._record_voice,
                spawn_voice_metrics=_spawn_voice_metrics,
                **({"call_scope": call_scope} if call_scope is not None
                   else {"focus_session": lambda: target} if target else {}))
            if outcome.trace_id:
                self._trace_id = outcome.trace_id
            return self._json(outcome.status, outcome.payload)

    def _transcription_session(self) -> str:
        """The chat a recording is for: the client's explicit
        `X-Transcription-Session`, else the chat this device last opened.
        Empty means fall back to the shared focus."""
        from lib import client_chat
        explicit = (self.headers.get("X-Transcription-Session") or "").strip()
        if explicit and identity.lookup(explicit):
            return explicit
        last = client_chat.current(self.principal().principal)
        return last if last and identity.lookup(last) else ""

    def _focused_session(self) -> str:
        return current_focus_session()

    def _handle_media_content(self, asset_id: str):
        if not asset_id or "/" in asset_id or "." in pathlib.PurePath(asset_id).parts:
            return self._send(400, b"bad asset id")
        row = media_store.get(asset_id)
        if not row:
            return self._send(404, b"not found")
        path = pathlib.Path(row["storage_path"]).resolve()
        root = (self.ctx.media_dir or _runtime_paths().media_dir).resolve()
        if not path.is_file() or (path != root and root not in path.parents):
            return self._send(404, b"not found")
        return self._send_file(path, str(row["mime_type"]), secure=True)


def _dispatch_service(ctx: ServerContext):
    """Resolve `TurnDispatchService` from this module at call time so tests
    that monkeypatch `server.TurnDispatchService` intercept scheduler turns."""
    return TurnDispatchService(ctx)


def _dispatch_adapters(ctx: ServerContext) -> DispatchAdapters:
    return DispatchAdapters(ctx, _dispatch_service)


def _deliver_decision_rows(ctx: ServerContext) -> None:
    _dispatch_adapters(ctx).deliver_decision_rows()


def _configure_runtime_client(ctx: ServerContext) -> None:
    """Process ownership queries (busy state, stop, steering, compaction
    fences) must reach the same runtime that owns dispatch. Reset this
    explicitly for injected/local tests so module state cannot leak between
    server instances."""
    runtime_client = getattr(ctx, "runtime_client", None)
    backends.configure_runtime_client(runtime_client)
    import lib.turn_dispatch as _turn_dispatch_module
    _turn_dispatch_module.configure_runtime_client(runtime_client)
    from lib import compaction as _compaction_module
    _compaction_module.configure_runtime_client(runtime_client)
    # Codex commands are remembered in the runtime; the explainer asks it.
    from lib import tool_explanation_commands as _explanation_commands
    _explanation_commands.configure(getattr(runtime_client, "explanation_command", None))


def _seed_startup_invariants(ctx: ServerContext) -> None:
    """Persona definitions are a startup-owned invariant. Historically they
    were materialized lazily by the first persona/snapshot read, which made
    zero-session definitions depend on endpoint call order. Initialize them
    before request threads start so every read model remains read-only."""
    from lib import personas
    personas.ensure_builtins()
    from lib.janitor_bootstrap import initialize as initialize_janitors
    initialize_janitors(cwd=str(ctx.root))


def _server_workers(ctx: ServerContext, srv: "ContextHTTPServer", cfg,
                    adapters: DispatchAdapters, port: int) -> list[Worker]:
    """The ordered worker catalog. Order is start order and stop order.

    Lives in this module (not `lib.workers`) on purpose: every class is
    resolved from `server.<Name>` when the record starts, which is what lets
    tests swap one worker for a fake by monkeypatching this module.
    """
    herald = getattr(ctx, "herald", None)
    _paths = _runtime_paths()

    def start_relay():
        from lib.relay_connector import ManagedRelay
        srv.relay = ManagedRelay(ctx.relay_settings, srv.server_port)
        srv.relay.start()
        return srv.relay

    def start_tool_explanations():
        if ctx.tool_explanations is None:
            from lib.tool_explanations import ToolExplanations
            ctx.install_tool_explanations(ToolExplanations())
        return ctx.tool_explanations

    def start_bonjour():
        from lib.bonjour import BonjourAdvertiser
        from lib.server_identity import get_server_info
        server_info = get_server_info()
        bonjour = BonjourAdvertiser(
            name=str(server_info["name"]),
            server_id=str(server_info["server_id"]),
            port=port, auth_required=bool(ctx.auth_token))
        return bonjour if bonjour.start() else None

    def start_runtime_events():
        from lib.runtime_events import RuntimeEventWatcher
        watcher = started(RuntimeEventWatcher(ctx.stream))
        srv.runtime_event_watcher = watcher
        return watcher

    def live_explainer(patch):
        """What watches the live events in this process: the tool explainer
        and the Live Activity pusher (docs/live-items.md §6, §8)."""
        from lib import apns, live_activity
        from lib.agents import list_agents as agents_db_list
        from lib.live_explain import LiveExplainer
        explainer = LiveExplainer(lambda: ctx.tool_explanations, patch)
        from concurrent.futures import ThreadPoolExecutor
        # One sender thread: APNs I/O never runs inside the hub's lock, and
        # pushes leave in the order they were decided.
        sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-activity")
        def in_background(job):
            threading.Thread(target=job, daemon=True, name="live-activity-attention").start()

        pusher = live_activity.LiveActivityPusher(
            lambda payload, priority: sender.submit(apns.send_live_activity, payload, priority),
            attention=live_activity.badge_attention, roster=agents_db_list, background=in_background)
        srv.live_activity_pusher = pusher

        class _Observers:
            resync = staticmethod(explainer.resync)

            @staticmethod
            def observe(event):
                explainer.observe(event)
                pusher.observe(event)
        return _Observers()

    def start_live_hub():
        # No separate runtime: the agents run here, so does the hub.
        from lib import live_hub
        holder = {}

        def sink(event):
            ctx.stream.broadcast_live(event)
            holder["explainer"].observe(event)

        hub = live_hub.LiveHub(sink=sink)
        holder["explainer"] = live_explainer(hub.patch)
        live_hub.install(hub)
        from lib import server_identity
        server_identity.set_live_probe(lambda: live_hub.current() is hub)
        return hub

    def stop_live_hub(_hub):
        from lib import live_hub, server_identity
        live_hub.install(None)
        server_identity.set_live_probe(None)

    def start_live_relay():
        from lib import live_hub
        socket_path = getattr(ctx.runtime_client, "socket_path", None)
        if socket_path is None:
            return None
        watcher = getattr(srv, "runtime_event_watcher", None)
        explainer = live_explainer(ctx.runtime_client.live_patch)

        def catch_up():
            # Tools that started while this process was down, and jobs that
            # died with the previous one, would otherwise never be explained.
            from lib import agents as agents_db
            from lib.log import log_exception
            for agent in agents_db.list_agents():
                if agent.get("archived_at"):
                    continue
                try:
                    snapshot = ctx.runtime_client.live_snapshot(agent_id=agent["agent_id"])
                except Exception as exc:  # noqa: BLE001 - the next reconnect retries
                    log_exception("liveExplainCatchUpFail", exc, f"agent={agent['agent_id']}")
                    return
                explainer.resync(snapshot)
        relay = live_hub.LiveRelay(
            socket_path, ctx.stream, on_event=explainer.observe, on_connect=catch_up,
            on_nudge=watcher.poll_now if watcher is not None else None).start()
        from lib import server_identity
        # Advertised only while the runtime's hub is connected and serving.
        server_identity.set_live_probe(lambda: relay.serving)
        return relay

    def stop_live_relay(relay):
        from lib import server_identity
        server_identity.set_live_probe(None)
        if relay:
            relay.stop()

    def start_tts_worker():
        from lib.clip_delivery import build_from_config, DeliveryDeps
        # The delivery is what decides how clip bytes reach the client
        # (chunked-file + broker today; hls tomorrow). Config knob:
        # [audio] delivery = "hls" (or CLAUDE_PWA_DELIVERY=hls).
        delivery = build_from_config(cfg, deps=DeliveryDeps(broker=ctx.clip_broker))
        return started(TTSWorker(
            audio_dir=_paths.audio_dir, stream=ctx.stream, herald=herald,
            broker=ctx.clip_broker, delivery=delivery))

    def start_autonomy_janitors():
        from lib import apns
        # A Codex switch through Hotseat rewrites auth.json; the Codex backend
        # restarts its idle app-servers from on_credential_change(), exactly
        # as after `codex login`.
        # The desk's switch and the switcher's switch share one lock per provider,
        # and the switcher's scheduled reads keep the stored desk fresh.
        desk = getattr(ctx, "hotseat_desk", None)
        return started(AutonomyJanitors(
            adapters.decided_heartbeat, apns.send_user_notification,
            recover=adapters.recover_janitor_quota,
            hotseat_switch_lock=desk.switch_lock if desk is not None else None,
            hotseat_observe=desk.absorb_switcher_read if desk is not None else None,
            hotseat_switched=desk.mark_switched if desk is not None else None))

    def start_janitor_runner():
        from lib.janitor_http import runtime_available
        return started(JanitorRunner(
            adapters.janitor_run, admission_ready=lambda: runtime_available(ctx),
            after_tick=adapters.janitor_after_tick))

    def start_local_https():
        from lib.local_transport import LocalHTTPS
        try:
            srv.local_transport = LocalHTTPS(
                srv, Handler, ctx.local_tls_directory, ctx.local_tls_port)
            srv.local_transport.start()
        except Exception as error:
            srv.local_transport = None
            srv.local_transport_error = "Local HTTPS listener could not start"
            log_exception("localHTTPSStartFailed", error)
            return None
        return srv.local_transport

    def start_bookkeeping_listeners():
        # A listener is its own user service; after a reboot or a crash that
        # exhausted its restarts, bring back the one each active delegation needs.
        from lib import bookkeeping_listener

        def ensure():
            try:
                started = bookkeeping_listener.ensure_running()
                if started:
                    log("bookkeepingListenersStarted", ",".join(started))
            except Exception as error:  # noqa: BLE001 - never block serving
                log_exception("bookkeepingListenersFail", error)
        thread = threading.Thread(target=ensure, name="bookkeeping-listeners", daemon=True)
        thread.start()
        return thread

    def has_runtime_client() -> bool:
        return getattr(ctx, "runtime_client", None) is not None

    return [
        # SSE hub first: every watcher below broadcasts into it.
        Worker("stream", lambda: started(ctx.stream)),
        Worker("relay", start_relay, stop=lambda relay: relay.close(),
               enabled=lambda: ctx.relay_settings is not None),
        Worker("tool-explanations", start_tool_explanations,
               stop=lambda explanations: explanations.close()),
        Worker("bonjour", start_bonjour,
               enabled=lambda: bool(getattr(cfg, "network_advertise_lan", False))),
        # Tail state_log + push every new row as an `agent-state` SSE event.
        Worker("state-log-watcher", lambda: started(StateLogWatcher(ctx.stream))),
        Worker("background-job-watcher",
               lambda: started(BackgroundJobWatcher(ctx.stream))),
        Worker("runtime-event-watcher", start_runtime_events, enabled=has_runtime_client),
        Worker("live-hub", start_live_hub, stop=stop_live_hub,
               enabled=lambda: not has_runtime_client()),
        Worker("live-relay", start_live_relay, stop=stop_live_relay,
               enabled=has_runtime_client),
        # TTS worker: drains tts_queue, does the provider call from the
        # server process (not from short-lived hook subprocesses). The hooks
        # only write queue rows; this is the executor side. It starts before
        # the transcript streamer, which enqueues into the same queue.
        Worker("tts-worker", start_tts_worker),
        Worker("maintenance", lambda: started(MaintenanceWorker(
            audio_dir=_paths.audio_dir, stream=ctx.stream, hook_log=_paths.hook_log))),
        Worker("usage-refresh",
               lambda: started(backend_usage.UsageRefreshWorker(stream=ctx.stream))),
        Worker("resource-telemetry", lambda: started(ResourceTelemetryWorker())),
        # Warm the heavy read caches (pair rooms, dashboard previews, model
        # catalogue, artifacts) once the listeners are up, so the first client
        # after a restart does not pay for a cold page cache on the request path.
        Worker("cache-warmup", lambda: started(CacheWarmupWorker()), stage=SERVING),
        Worker("bookkeeping-listeners", start_bookkeeping_listeners,
               stop=lambda thread: None, stage=SERVING),
        Worker("decision-delivery", lambda: started(
            DecisionDeliveryWorker(lambda: _deliver_decision_rows(ctx)))),
        # Transcript streamer: tails ~/.claude/projects/.../<uuid>.jsonl via
        # inotify for every live agent. As Claude finishes each text block,
        # the streamer enqueues it in tts_queue (the same queue the Stop hook
        # uses) so audio starts synthesizing before the turn ends. Lifecycle
        # is auto-managed: it reconciles with the agents/runtimes tables every
        # second and binds/unbinds inotify watches accordingly.
        Worker("transcript-streamer", lambda: started(TranscriptStreamer(stream=ctx.stream))),
        # Autonomous team-leader loop: periodically nudges idle team leaders to
        # review and unstick stalled teammates. Only fires when there is
        # something to do; an idle team costs nothing.
        Worker("team-leader", lambda: started(TeamLeaderScheduler(send_tick=adapters.leader_tick))),
        # Per-agent heartbeat loop: opt-in and idle-only. It sends a hidden
        # heartbeat prompt; HEARTBEAT_OK replies are suppressed by
        # message_store. Restart recovery (after this stage) reads it back
        # through `WorkerSet.get("heartbeat")`.
        Worker("heartbeat", lambda: started(HeartbeatScheduler(send_heartbeat=adapters.heartbeat))),
        Worker("autonomy-janitors", start_autonomy_janitors),
        Worker("dreaming", lambda: started(DreamingScheduler(send_dream=adapters.dream))),
        Worker("agent-scheduler",
               lambda: started(AgentScheduleRunner(dispatch_turn=adapters.scheduled_job, dispatch_goal=adapters.goal_continuation,
                                                    goal_wake_retired=adapters.goal_wake_retired))),
        Worker("janitor-runner", start_janitor_runner),
        # Listeners that accept outside requests on their own thread start
        # only after restart recovery has repaired state.
        Worker("local-https", start_local_https, stop=lambda transport: transport.close(),
               enabled=lambda: bool(ctx.local_tls_port), stage=SERVING),
    ]


def _recover_queued_after_start(ctx: ServerContext) -> None:
    try:
        recovered_queues = TurnDispatchService(ctx).recover_queued()
    except Exception as exc:  # runtime may still be starting; its own boot recovers
        from lib.runtime_bridge import RuntimeUnavailable
        if not isinstance(exc, RuntimeUnavailable):
            if getattr(ctx, "runtime_client", None) is None:
                raise
            log_exception("runtimeQueueRecoveryFail", exc)
            return
        log_exception("runtimeQueueRecoveryDeferred", exc)
        recovered_queues = 0
    if recovered_queues:
        log("queuedRecovery", f"recovered={recovered_queues}")


def _recover_after_start(ctx: ServerContext, workers: WorkerSet, *,
                         restart_recovery: bool) -> None:
    """Boot-time repair, after the boot workers and before serving listeners."""
    broadcast_boot_version(ctx)
    runtime_client = getattr(ctx, "runtime_client", None)
    if runtime_client is None:
        resume_persisted_agents(ctx)
    if restart_recovery and runtime_client is None:
        # Mark the turns the previous process took down with it before the
        # restarting agent chooses explicit continuations (issue #11).
        from lib import interrupted_turns
        interrupted = interrupted_turns.recover_after_restart(
            stream=getattr(ctx, "stream", None))
        if interrupted:
            log("turnRestartRecovery", f"marked={len(interrupted)}")
    if runtime_client is None:
        _recover_queued_after_start(ctx)
    else:
        # The runtime owns the queue and recovers it on its own boot; asking
        # it here is a courtesy that must never hold up listening (a busy
        # runtime once kept a new Host silent past the installer's 20 s check).
        threading.Thread(target=_recover_queued_after_start, args=(ctx,),
                         name="queued-recovery", daemon=True).start()
    try:
        # A listen-first initiative resolved but never admitted (the Host died
        # in between) is admitted now, exactly once (docs/agent-rings.md).
        from lib import agent_rings
        agent_rings.bind(ctx)
        recovered_openings = agent_rings.recover_openings()
        if recovered_openings:
            log("agentRingOpeningRecovery", f"admitted={recovered_openings}")
    except Exception as exc:  # noqa: BLE001 - a boot never fails on it
        log_exception("agentRingOpeningRecoveryFail", exc)


def build_server(ctx: ServerContext, port: int,
                 bind_addr: str | None = None, *,
                 restart_recovery: bool = False,
                 herald=None) -> ContextHTTPServer:
    """Wire a fully-injected HTTP server. Tests use this with a fake ctx.

    `herald` is the HeraldManager production attaches so off-focus agents
    raise their hand. It is stored on `ctx.herald` before any worker or
    handler runs (the TTS worker captures it at start), so passing it here is
    the same as setting `ctx.herald` beforehand, which tests still do.
    """
    _configure_runtime_client(ctx)
    _seed_startup_invariants(ctx)
    # /server-info offers Android push only while a service account is usable.
    from lib import apns, config as config_module, server_identity
    server_identity.set_fcm_probe(lambda: apns.fcm_configured(config_module.load()))
    if herald is not None:
        ctx.install_herald(herald)
    from lib import oracle_handoffs
    oracle_handoffs.bind(ctx)
    oracle_handoffs.recover()
    if ctx.hotseat_desk is None:
        from lib.hotseat_desk import HotseatDesk
        ctx.replace_service("hotseat_desk", HotseatDesk())
    listener_addr = bind_addr or BIND_ADDR
    if listener_addr not in {"127.0.0.1", "::1", "localhost"} and not ctx.auth_token:
        raise ValueError("a non-loopback listener requires authentication")
    if ctx.relay_settings is not None and not ctx.auth_token:
        raise ValueError("relay networking requires authentication")
    srv = ContextHTTPServer((listener_addr, port), Handler, ctx)
    workers = WorkerSet(_server_workers(ctx, srv, _CFG, _dispatch_adapters(ctx), port))
    srv.workers = workers
    workers.start(srv, BOOT)
    _recover_after_start(ctx, workers, restart_recovery=restart_recovery)
    if ctx.local_tls_port and (not ctx.auth_token or ctx.local_tls_directory is None):
        srv.server_close()
        raise ValueError("local HTTPS requires authentication and an identity directory")
    workers.start(srv, SERVING)
    return srv


if __name__ == "__main__":
    # This process is the Host service. It refuses a database newer than its
    # own code, and is otherwise the one that upgrades the database it opens.
    from lib import db as _state_db, db_schema as _db_schema, schema_guard as _schema_guard
    from lib.deployment import LAYOUT as _layout
    _code_root = pathlib.Path(__file__).resolve().parent     # the release, not the link to it
    try:
        _schema_guard.check_host_start(
            _state_db.DB_PATH, _layout.share, code=_db_schema._SCHEMA_VERSION,
            release=_code_root.name, code_root=_code_root)
        if not _state_db.run_as_service("host", code_root=_code_root, share=_layout.share):
            _schema_guard.check_foreign_start(
                _code_root, _state_db.DB_PATH, _layout.share, code=_db_schema._SCHEMA_VERSION)
    except (_schema_guard.OlderCode, _schema_guard.NotThisInstallation) as _refused:
        print(f"clarp: {_refused}", file=sys.stderr, flush=True)
        raise SystemExit(_schema_guard.EXIT_REFUSED)    # restarting changes nothing
    # Keep holding the entrypoint's data-root locks but never pass them to children (agent
    # terminals): a no-op unless fds 8/9 are exactly the lock files.
    from lib.data_root_locks import seal_inherited_lock_fds
    seal_inherited_lock_fds()
    # The instance generation token, before this process opens the state database.
    from lib.host_generation import ensure_instance_generation
    ensure_instance_generation()
    from lib.herald import HeraldManager
    from lib import reconcile
    # Boot-time reconciliation (INV1-INV3, lib.reconcile): a server killed
    # mid-turn leaves busy state rows, ghost session bindings and phantom
    # in-flight slots behind. Re-derive from reality (no process survives a
    # restart) so the app never renders phantom "working" badges and no agent
    # resumes a session that doesn't exist. The same pass runs per agent on
    # every snapshot read.
    prod_ctx = ServerContext.production()
    if prod_ctx.runtime_client is None:
        repaired = reconcile.reconcile_all()
        if repaired:
            log("bootReconcile", f"repaired {repaired} agent(s)")
    # The herald manager lets off-focus agents raise their hand. build_server
    # attaches it to the context before any worker or handler can read it.
    herald = HeraldManager(
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
    srv = build_server(prod_ctx, PORT, restart_recovery=True, herald=herald)
    log("serverStart", f"port={PORT}")
    print(f"claude-pwa listening on :{PORT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
