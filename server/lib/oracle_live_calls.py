"""Authenticated Live WebRTC sessions using the normal Oracle conversation.

Primary audio stays on the peer connection. This manager owns the provider
sideband, work routing and a bounded resumable stream of application events.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import threading
import time

from . import config, oracle_delegations, oracle_live, oracle_live_provider, oracle_memory
from .oracle_calls import AgentTools, validate_offer
from .oracle_live_wire import LiveWire

_LOCK = threading.RLock()
_ATTEMPTS = {}
MAX_RETAINED_ATTEMPTS = 128
CONTROL_TIMEOUT = 30.0


class CallError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _podcast(data):
    value = data.get("podcast")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CallError("Invalid podcast context", 400)
    from . import artifacts, podcast_history, podcast_live
    try:
        artifact = artifacts.get(value["artifact_id"])
        if not artifact or artifact["type"] != "audio": raise ValueError("missing episode")
        episode = artifact["payload"]["podcast"]
        if value["revision"] != episode["revision"]: raise ValueError("changed episode")
        source = podcast_history.source_artifact(value.get("source_artifact_id") or episode.get("source_artifact_id", ""))
        position = value["position"]
        context = podcast_live.context_for(episode, position, float(artifact.get("duration_ms", 0))/1000, source=source)
        return {"artifact": artifact, "source": source, "position": position, "context": context,
                "images": value.get("images") is True}
    except (ValueError, TypeError, KeyError):
        raise CallError("Invalid podcast artifact, revision or playhead", 400) from None


class Call:
    def __init__(self, *, principal, attempt, digest, token, wire, tools, cfg, podcast, ctx, clock=time.monotonic):
        self.principal, self.attempt, self.digest, self.token = principal, attempt, digest, token
        self.wire, self.tools, self.cfg, self.podcast, self.ctx = wire, tools, cfg, podcast, ctx
        self.clock = clock
        self.lock = threading.RLock()
        self.events = deque(maxlen=512)
        self.cursor = 0
        self.last_control = clock()
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self.conversation = None
        self.upstream = None
        self.reader_started = False
        self.response = None
        self.failure = None
        self.created_at = time.time()
        self.history_id = None
        self.memory = None

    def emit(self, event):
        # The media tracks already deliver audio. Never feed a second PCM
        # playback path on the phone or poll it through the relay.
        if event.get("type") == "session.output_audio.delta": return
        with self.lock:
            self.cursor += 1
            self.events.append({"cursor": self.cursor, "json": json.dumps(event, ensure_ascii=False)})

    def open(self, sdp, negotiate):
        history = self.memory.startup_history(roster=self.tools.execute("list_agents", {}, "startup")) if self.memory else []
        session = oracle_live.live_config(wire=self.wire, webrtc=True, history=history)
        if self.podcast:
            from . import podcast_live, podcast_history
            session["instructions"] += "\n" + podcast_live.PROMPT
            session.setdefault("input" if self.wire.mode == "api" else "initial_items", []).append({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Reference data for the paused episode:\n" + self.podcast["context"]}]})
            self.history_id = podcast_history.create(artifact=self.podcast["artifact"], position=self.podcast["position"],
                context=self.podcast["context"], source=self.podcast["source"], model=self.wire.model, voice=self.wire.voice,
                configuration=session)
        result = negotiate(sdp, wire=self.wire, session=session, cfg=self.cfg)
        self.upstream = result["socket"]
        if self.podcast:
            from .podcast_live import PodcastConversation
            conversation = PodcastConversation(result["socket"], self.emit, self.cfg.openai_key(), self.podcast["context"],
                tools=self.tools, router_backend=self.cfg.oracle_router_backend, wire=self.wire,
                images=self.podcast["images"], history_id=self.history_id, media_dir=getattr(self.ctx, "media_dir", None),
                memory=self.memory, provider_session=result["session_id"])
            self.emit({"type": "podcast.history", "conversation_id": self.history_id, "saved": True,
                       "position": self.podcast["position"]})
        else:
            conversation = oracle_live.Conversation(result["socket"], self.emit, self.tools, self.cfg.openai_key(),
                router_backend=self.cfg.oracle_router_backend, wire=self.wire,
                memory=self.memory, provider_session=result["session_id"])
        self.conversation = conversation
        if self.cfg.oracle_diagnostics:
            from .oracle_diagnostics import OracleJournal
            conversation.journal = OracleJournal()
            conversation.journal.record("session.open", {"transport": "live-webrtc", "model": self.wire.model,
                "voice": self.wire.voice, "mode": self.wire.mode, "attempt_id": self.attempt, "session_id": result["session_id"]})
        with self.lock:
            self.conversation = conversation
            self.response = {"sdp": result["sdp"], "session_id": result["session_id"], "attempt_id": self.attempt,
                             "model": self.wire.model, "voice": self.wire.voice, "mode": self.wire.mode,
                             "thread_id": self.memory.thread_id if self.memory else None}
        if self.memory:
            self.emit({"type": "oracle_v2.context", "thread_id": self.memory.thread_id,
                       "items": self.memory.contexts(), "revision": conversation.revision})
        threading.Thread(target=self.run, daemon=True, name="oracle-live-sideband").start()
        self.reader_started = True
        if self.cancelled.is_set():
            self.close()
            raise CallError("Oracle connection superseded")
        return self.response

    def abort_startup(self):
        """No created provider session is abandoned after a local setup error."""
        if self.reader_started:
            self.close()
            return
        if self.upstream:
            try:
                self.upstream.send(json.dumps({"type": "session.close"}))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        event = self.wire.incoming(json.loads(self.upstream.recv()))
                    except Exception:
                        continue
                    if self.conversation: self.conversation.usage.observe(event)
                    if event.get("type") == "session.closed": break
            except Exception:
                pass
            finally:
                try: self.upstream.close()
                except Exception: pass
        if self.conversation:
            self.conversation.stop.set()
            self.conversation.pool.shutdown(wait=False, cancel_futures=True)

    def run(self):
        import websocket
        conversation = self.conversation
        next_tick = self.clock()
        try:
            while not conversation.stop.is_set():
                try:
                    raw = conversation.upstream.recv()
                    if not raw: break
                    conversation.receive(json.loads(raw))
                except websocket.WebSocketTimeoutException:
                    pass
                if conversation.closed.is_set(): break
                now = self.clock()
                if now >= next_tick:
                    next_tick = now + .25
                    if self.principal.startswith("device_") and not conversation.close_sent:
                        from . import db
                        device = db.conn().execute("SELECT scope,revoked_at FROM paired_devices WHERE device_id=?", (self.principal,)).fetchone()
                        if device is None or device["revoked_at"] is not None or device["scope"] != "full":
                            self.emit({"type": "oracle_v2.notice", "message": "Voice access for this device was revoked."})
                            conversation.input({"type": "session.close"})
                    conversation.tick()
                    if now-self.last_control >= CONTROL_TIMEOUT and not conversation.close_sent:
                        self.emit({"type": "oracle_v2.notice", "message": "Voice ended after the phone stopped checking in. Agent work continues."})
                        conversation.input({"type": "session.close"})
                    if conversation.close_sent and not hasattr(self, "close_deadline"):
                        self.close_deadline = now + 3
                    if conversation.close_sent and now >= self.close_deadline: break
        except Exception as exc:
            self.failure = type(exc).__name__
            self.emit({"type": "error", "error": {"message": "Oracle v2 sideband disconnected"}})
        finally:
            conversation.stop.set()
            conversation.pool.shutdown(wait=False, cancel_futures=True)
            try: conversation.upstream.close()
            except Exception: pass
            try:
                if conversation.journal:
                    conversation.journal.record("session.summary", {"voice_usage": conversation.usage.snapshot(),
                        "closed_by_upstream": conversation.closed.is_set(), "error_type": self.failure})
                    conversation.journal.close()
                if self.history_id:
                    from . import podcast_history
                    podcast_history.finish(self.history_id, "closed" if conversation.closed.is_set() else "interrupted")
            except Exception as exc:
                self.failure = "Final diagnostics could not be saved: " + type(exc).__name__
            finally:
                oracle_live.release_connection(self.principal, self.token)
                self.finished.set()

    def snapshot(self, after=0):
        with self.lock:
            self.last_control = self.clock()
            first = self.events[0]["cursor"] if self.events else self.cursor + 1
            return {"attempt_id": self.attempt, "cursor": self.cursor,
                    "gap": bool(after and after < first-1),
                    "events": [dict(event) for event in self.events if event["cursor"] > after],
                    "closed": self.finished.is_set(),
                    "error": self.failure,
                    "usage": self.conversation.usage.snapshot() if self.conversation else None,
                    "work": self.conversation.work_snapshot or [] if self.conversation else []}

    def close(self):
        self.cancelled.set()
        conversation = self.conversation
        if conversation and not self.finished.is_set():
            conversation.input({"type": "session.close"})
            self.finished.wait(3.5)
            if not self.finished.is_set():
                conversation.stop.set()
                try: conversation.upstream.close()
                except Exception: pass
        return self.snapshot()


def create(*, ctx, principal, data, stop, negotiate=oracle_live_provider.negotiate):
    if not isinstance(data, dict): raise CallError("Invalid call request", 400)
    try:
        attempt = oracle_delegations.normalize_id(data.get("attempt_id"))
        sdp = validate_offer(data.get("sdp"))
    except ValueError as exc:
        raise CallError(str(exc), 400) from None
    cfg = config.load()
    wire = LiveWire(cfg.oracle_voice_backend)
    if data.get("mode") != wire.mode:
        raise CallError("Oracle voice account changed; refresh the connection settings", 409)
    if cfg.oracle_router_backend == "api" and not cfg.openai_key():
        raise CallError("Oracle API routing requires an OpenAI key on this Host", 503)
    podcast = _podcast(data)
    fallback = str(data.get("oracle_session") or "")
    tools = AgentTools(ctx, principal, fallback, stop)
    if fallback: tools.resolve(fallback)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = (principal, attempt)
    with _LOCK:
        previous = _ATTEMPTS.get(key)
        if previous:
            if previous.digest != digest: raise CallError("Call attempt reused with different context", 400)
            if previous.finished.is_set() or previous.failure: raise CallError("Previous attempt ended; start a new voice attempt")
            if previous.response: return previous.response
            raise CallError("Call creation is still in progress")
        # Do not evict a failed attempt and accidentally retry its paid create.
        old = sorted(_ATTEMPTS.items(), key=lambda item: item[1].created_at)
        for old_key, call in old:
            if len(_ATTEMPTS) < MAX_RETAINED_ATTEMPTS: break
            if call.finished.is_set() and time.time()-call.created_at > 3600:
                del _ATTEMPTS[old_key]
        if len(_ATTEMPTS) >= MAX_RETAINED_ATTEMPTS:
            raise CallError("Oracle connection history is temporarily full", 503)
        token = oracle_live.claim_connection(principal)
        if not token: raise CallError("Another Oracle session is still closing")
        call = Call(principal=principal, attempt=attempt, digest=digest, token=token,
                    wire=wire, tools=tools, cfg=cfg, podcast=podcast, ctx=ctx)
        try:
            call.memory = oracle_memory.open_thread(principal, fallback, connection_id=token,
                thread_id=data.get("thread_id"), fresh=data.get("new_conversation") is True)
            call.memory.reconcile()
        except Exception:
            oracle_live.release_connection(principal, token)
            raise
        _ATTEMPTS[key] = call
        oracle_live.register_stop(principal, token,
            lambda: threading.Thread(target=call.close, daemon=True).start())
    try:
        return call.open(sdp, negotiate)
    except Exception as exc:
        call.failure = str(exc) if isinstance(exc, (CallError, oracle_live_provider.ProviderUnavailable)) else "Oracle connection unavailable"
        call.abort_startup()
        call.finished.set()
        oracle_live.release_connection(principal, token)
        if call.history_id:
            from . import podcast_history
            try: podcast_history.finish(call.history_id, "failed")
            except Exception: pass
        if isinstance(exc, CallError): raise
        raise CallError(call.failure, 502) from None


def get(principal, attempt):
    with _LOCK:
        call = _ATTEMPTS.get((principal, attempt))
    if not call: raise CallError("Oracle call is no longer available", 410)
    return call


def handle(handler, action):
    if not (getattr(handler, "_request_auth_validated", False) and
            getattr(handler, "_request_device_scope", "") == "full" and getattr(handler, "_request_principal", "")):
        return handler._send(403, b'{"error":"Oracle requires full-device authentication"}', "application/json")
    from urllib.parse import parse_qs, urlparse
    try:
        principal = handler._request_principal
        if action == "status":
            query = parse_qs(urlparse(handler.path).query)
            try: cursor = int(query.get("after", ["0"])[0])
            except ValueError: raise CallError("Invalid event cursor", 400) from None
            if cursor < 0: raise CallError("Invalid event cursor", 400)
            result = get(principal, query.get("attempt_id", [""])[0]).snapshot(cursor)
        else:
            data = handler._read_json()
            if not isinstance(data, dict): raise CallError("Invalid Oracle command", 400)
            if action == "create":
                result = create(ctx=handler.ctx, principal=principal, data=data,
                    stop=lambda session: handler._stop_agent_session(session, strict=True, defer_finish=True)[1])
            else:
                call = get(principal, str(data.get("attempt_id") or ""))
                if action == "close": result = call.close()
                else:
                    command = oracle_live.client_event(json.dumps(data.get("event")))
                    if not command or command["type"] == "session.input_audio.append":
                        raise CallError("Unsupported Oracle WebRTC command", 400)
                    if call.finished.is_set(): raise CallError("Oracle session already ended", 410)
                    if call.conversation is None: raise CallError("Oracle is still connecting")
                    call.last_control = call.clock()
                    call.conversation.input(command)
                    result = {"accepted": True}
        return handler._send(200, json.dumps(result).encode(), "application/json")
    except CallError as exc:
        return handler._send(exc.status, json.dumps({"error": str(exc)}).encode(), "application/json")
    except (ValueError, TypeError):
        return handler._send(400, b'{"error":"Invalid Oracle call"}', "application/json")


def context_image(handler):
    if not (getattr(handler, "_request_auth_validated", False) and getattr(handler, "_request_device_scope", "") == "full"):
        return handler._send(403, b"Forbidden", "text/plain")
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(handler.path).query)
    try:
        store = oracle_memory.ThreadStore(query.get("thread_id", [""])[0], handler._request_principal, "")
        row = next((row for row in store.contexts(include_images=True) if row["context_id"] == query.get("context_id", [""])[0] and row["image"] is not None), None)
        if row: return handler._send(200, row["image"], row["mime_type"])
    except ValueError:
        pass
    return handler._send(404, b"Image not found", "text/plain")
