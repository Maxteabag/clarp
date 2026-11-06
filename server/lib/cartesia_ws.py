"""Cartesia WebSocket TTS client for raw PCM low-latency playback.

Cartesia's WebSocket endpoint streams raw PCM chunks. That is not suitable for
the browser `<audio>` pipeline, but it is ideal for the native app where we can
feed PCM directly into AVAudioEngine as chunks arrive.
"""
from __future__ import annotations

import base64
import binascii
import json
import pathlib
import socket
import threading
import time
import uuid
from typing import Callable
from threading import BoundedSemaphore as _BoundedSemaphore, Event as _AttemptEvent, Thread as _AttemptThread
from urllib.parse import urlencode

import websocket

from .cartesia_tts import CartesiaError


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


_WS_URL = "wss://api.cartesia.ai/tts/websocket"
_VERSION = "2026-03-01"
_SAMPLE_RATE = 44100
_ENCODING = "pcm_f32le"
# Leave the live broker's unchanged 30 s empty wait room for fallback.
RAW_FIRST_BYTE_BUDGET_SEC = 10.0
# Match the queue pool: abandoned OS calls cannot accumulate without bound.
_RAW_ATTEMPT_SLOTS = _BoundedSemaphore(3)


class _FirstByteBudget:
    """Caller deadline and ownership of late setup, transports and first PCM."""

    def __init__(self, ws, deadline, trace_id):
        self.ws, self.deadline, self.trace_id = ws, deadline, trace_id
        self._lock = threading.Lock()
        self._timer = None
        self._done = self._expired = self.accepted = False
        self._sockets = []
        self.wake = _AttemptEvent()

    def _remaining_locked(self):
        remaining = self.deadline - time.perf_counter()
        if self._expired or (self._done and not self.accepted) or remaining <= 0:
            raise CartesiaError("cartesia_raw_first_byte_timeout")
        return remaining

    def remaining_timeout(self, timeout):
        with self._lock:
            return min(timeout, self._remaining_locked())

    def start(self):
        self._timer = threading.Timer(self.remaining_timeout(RAW_FIRST_BYTE_BUDGET_SEC), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def attach(self, ws):
        with self._lock:
            self.ws = ws
            self._remaining_locked()

    def own_socket(self, transport):
        try:
            with self._lock:
                self._remaining_locked()
                if all(transport is not known for known in self._sockets):
                    self._sockets.append(transport)
        except CartesiaError:
            self._close_socket(transport)
            raise

    def _cleanup_failure(self, code):
        _emit("cartesia_ws", "deadlineCleanupFail", level="warn",
              trace_id=self.trace_id, detail={"code": code})

    def _close_socket(self, transport):
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except Exception:
            self._cleanup_failure("cartesia_raw_socket_shutdown_failed")
        try:
            transport.close()
        except Exception:
            self._cleanup_failure("cartesia_raw_socket_close_failed")

    def close_transports(self):
        with self._lock:
            transports, ws = list(self._sockets), self.ws
        for transport in transports:
            self._close_socket(transport)
        if ws is not None:
            if not transports:
                try:
                    ws.abort()
                except Exception:
                    self._cleanup_failure("cartesia_raw_deadline_abort_failed")
            try:
                ws.shutdown()
            except Exception:
                self._cleanup_failure("cartesia_raw_deadline_shutdown_failed")

    def _expire(self, *, force=False):
        with self._lock:
            if self._done and not force:
                return
            self._expired = True
        try:
            self.close_transports()
        finally:
            self.wake.set()

    def check_owned(self):
        with self._lock:
            if self._expired or (self._done and not self.accepted):
                raise CartesiaError("cartesia_raw_attempt_cancelled")

    def complete(self):
        with self._lock:
            self._remaining_locked()
            self.accepted = self._done = True
        if self._timer is not None:
            self._timer.cancel()
        self.wake.set()

    def stop(self):
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            self._done = True


class _OwnedWebSocket(websocket.WebSocket):
    """Adopt each SDK transport before Upgrade; reject late DNS/TLS results."""

    def __init__(self, budget, *args, **kwargs):
        self._budget, self._owned_sock = budget, None
        super().__init__(*args, **kwargs)
        budget.attach(self)

    @property
    def sock(self):
        return self._owned_sock

    @sock.setter
    def sock(self, value):
        if value is not None:
            self._budget.own_socket(value)
        self._owned_sock = value


def _open_ws(url: str, *, budget, **kw) -> websocket.WebSocket:
    def create(*args, **kwargs):
        return _OwnedWebSocket(budget, *args, **kwargs)
    return websocket.create_connection(url, class_=create, **kw)


def synthesize_raw_pcm(*,
                       text: str,
                       voice_id: str,
                       out_path: pathlib.Path | None,
                       api_key: str,
                       model: str = "sonic-3.5",
                       language: str = "en",
                       timeout: float = 30.0,
                       on_chunk: Callable[[int, bytes], None] | None = None,
                       trace_id: str | None = None,
                       max_buffer_delay_ms: int = 0,
                       encoding: str = _ENCODING,
                       sample_rate: int = _SAMPLE_RATE,
                       ) -> int:
    """Return at the caller deadline even if libc DNS cannot be interrupted.

    At most three abandoned attempt threads retain ownership until their OS
    calls return. Late transports are closed, and cannot touch fallback audio.
    """
    t_start = time.perf_counter()
    budget = _FirstByteBudget(None, t_start + RAW_FIRST_BYTE_BUDGET_SEC, trace_id)
    if not _RAW_ATTEMPT_SLOTS.acquire(blocking=False):
        _emit("cartesia_ws", "attemptCapacity", level="warn", trace_id=trace_id,
              detail={"code": "cartesia_raw_attempt_capacity"})
        raise CartesiaError("cartesia_raw_attempt_capacity")
    finished, outcome = _AttemptEvent(), {}
    arguments = dict(text=text, voice_id=voice_id, out_path=out_path, api_key=api_key,
                     model=model, language=language, timeout=timeout, on_chunk=on_chunk,
                     trace_id=trace_id, max_buffer_delay_ms=max_buffer_delay_ms,
                     encoding=encoding, sample_rate=sample_rate, _budget=budget, _t_start=t_start)

    def run():
        try:
            outcome["written"] = _synthesize_attempt(**arguments)
        except BaseException as error:
            outcome["error"] = error
        finally:
            try:
                if not budget.accepted:
                    budget.close_transports()
            finally:
                _RAW_ATTEMPT_SLOTS.release()
                finished.set()
                budget.wake.set()

    attempt = _AttemptThread(target=run, name="cartesia-raw-attempt", daemon=True)
    try:
        budget.start()
        attempt.start()
    except BaseException:
        budget.stop()
        _RAW_ATTEMPT_SLOTS.release()
        raise
    try:
        # The independent original-deadline watchdog also wakes this wait.
        budget.wake.wait()
        if budget.accepted:
            finished.wait()  # ordinary post-first-byte behavior is unchanged
        elif not finished.is_set():
            raise CartesiaError("cartesia_raw_first_byte_timeout")
        attempt.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["written"]
    except BaseException:
        if not finished.is_set():
            budget._expire(force=True)
        raise
    finally:
        budget.stop()


def _synthesize_attempt(*,
                       text: str,
                       voice_id: str,
                       out_path: pathlib.Path | None,
                       api_key: str,
                       model: str = "sonic-3.5",
                       language: str = "en",
                       timeout: float = 30.0,
                       on_chunk: Callable[[int, bytes], None] | None = None,
                       trace_id: str | None = None,
                       max_buffer_delay_ms: int = 0,
                       encoding: str = _ENCODING,
                       sample_rate: int = _SAMPLE_RATE,
                       _budget, _t_start,
                       ) -> int:
    """Stream raw PCM from Cartesia over WebSocket.

    `encoding`/`sample_rate` must match what the delivery advertises to the
    client in the clip payload (see clip_delivery.raw_pcm) — the client
    decodes from the advertisement, so a mismatch plays garbage."""
    if not api_key:
        raise CartesiaError("api_key required")
    if not voice_id:
        raise CartesiaError("voice_id required")

    url = f"{_WS_URL}?{urlencode({'cartesia_version': _VERSION})}"
    t_start, budget = _t_start, _budget
    try:
        ws = _open_ws(url, budget=budget, timeout=budget.remaining_timeout(timeout), header=[f"X-API-Key: {api_key}"])
        budget.attach(ws)
    except Exception as e:  # noqa: BLE001
        _emit("cartesia_ws", "connectFail", level="error", trace_id=trace_id,
              detail={"voice_id": voice_id, "error": str(e),
                      "elapsed_ms": int((time.perf_counter() - t_start) * 1000)})
        raise CartesiaError(f"WS connect failed: {e}") from e

    context_id = str(uuid.uuid4())
    bytes_written = 0
    chunk_count = 0
    t_first_chunk: float | None = None
    try:
        request = json.dumps({
            "model_id": model,
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            "language": language,
            "context_id": context_id,
            "continue": False,
            "max_buffer_delay_ms": max_buffer_delay_ms,
            "output_format": {
                "container": "raw",
                "encoding": encoding,
                "sample_rate": sample_rate,
            },
        })
        # Connection/setup already spent part of this same absolute budget.
        ws.settimeout(budget.remaining_timeout(timeout))
        try:
            ws.send(request)
        except (websocket.WebSocketTimeoutException,
                websocket.WebSocketConnectionClosedException,
                ConnectionResetError, OSError) as error:
            raise CartesiaError("cartesia_raw_request_send_failed") from error

        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)

        import contextlib
        with contextlib.ExitStack() as stack:
            f = None  # never open the shared raw target before first accepted PCM
            saw_done = False
            while not saw_done:
                try:
                    if t_first_chunk is None:
                        ws.settimeout(budget.remaining_timeout(timeout))
                    raw = ws.recv()
                    # A transport may return a late frame despite its timeout.
                    # Fence it before parsing or feeding any bytes to the broker.
                    if (t_first_chunk is None
                            and time.perf_counter() - t_start >= RAW_FIRST_BYTE_BUDGET_SEC):
                        raise CartesiaError("cartesia_raw_first_byte_timeout")
                except (websocket.WebSocketTimeoutException,
                        websocket.WebSocketConnectionClosedException,
                        ConnectionResetError, OSError) as e:
                    raise CartesiaError(f"WS closed before done: {e}") from e
                if not raw:
                    raise CartesiaError("empty frame from server")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise CartesiaError(f"server sent non-JSON frame: {e}") from e

                msg_type = msg.get("type")
                if msg_type == "error" or "error" in msg:
                    err = msg.get("error") or msg.get("message") or msg
                    _emit("cartesia_ws", "serverError", level="error",
                          trace_id=trace_id,
                          detail={"voice_id": voice_id, "error": str(err)[:300]})
                    raise CartesiaError(str(err))
                if msg_type == "done":
                    saw_done = True
                    continue
                if msg_type != "chunk":
                    saw_done = bool(msg.get("done"))
                    continue

                audio_b64 = msg.get("data") or ""
                if audio_b64:
                    try:
                        decoded = base64.b64decode(audio_b64)
                    except (ValueError, binascii.Error) as e:
                        raise CartesiaError(f"bad base64 from server: {e}") from e
                    if not decoded:
                        continue
                    if t_first_chunk is None:
                        budget.complete()
                        ws.settimeout(timeout)
                        t_first_chunk = time.perf_counter()
                        _emit("cartesia_ws", "firstChunk", trace_id=trace_id,
                              detail={"voice_id": voice_id,
                                      "encoding": encoding,
                                      "sample_rate": sample_rate,
                                      "bytes": len(decoded),
                                      "ttfb_ms": int(
                                          (t_first_chunk - t_start) * 1000)})
                    budget.check_owned()
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk_count, decoded)
                        except Exception:
                            pass
                    budget.check_owned()
                    if f is None and out_path is not None:
                        f = stack.enter_context(out_path.open("wb"))
                    if f is not None:
                        f.write(decoded)
                        f.flush()
                    bytes_written += len(decoded)
                    chunk_count += 1
                saw_done = bool(msg.get("done"))
    finally:
        try:
            if t_first_chunk is None:
                # No close handshake may spend another 3 s after the budget.
                # websocket-client shutdown closes the transport immediately.
                ws.shutdown()
            else:
                ws.close()
        except Exception:
            pass

    if not bytes_written:
        # An abandoned attempt never owned the target; it must not unlink a
        # fallback file that the caller has since written at that pathname.
        raise CartesiaError("empty response body")

    _emit("cartesia_ws", "synth", trace_id=trace_id,
          duration_ms=int((time.perf_counter() - t_start) * 1000),
          detail={"voice_id": voice_id, "model": model,
                  "text_len": len(text), "bytes": bytes_written,
                  "chunks": chunk_count,
                  "encoding": encoding,
                  "sample_rate": sample_rate,
                  "ttfb_ms": (int((t_first_chunk - t_start) * 1000)
                              if t_first_chunk else None)})
    return bytes_written

