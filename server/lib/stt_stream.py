"""Provider-owned turn taking over one live socket.

Native Hands-Free decides end-of-turn on the phone and posts a finished clip.
When the turn-taking strategy is `provider`, the phone instead streams raw
16 kHz PCM here and the recogniser decides: Deepgram Flux emits
StartOfTurn, Update, EagerEndOfTurn, TurnResumed and EndOfTurn on one
continuous stream. This module relays the audio up, normalises those events
into Clarp's turn vocabulary (`start`, `update`, `eager`, `resumed`, `end`),
and keeps the transparency contract: every ended turn gets a trace id, a
vocab run row with the keyterms that were on the socket, the transcript and
the turn's latency, and - when retention is on - the audio the provider heard.

The API key never leaves the Host; the phone only ever talks to Clarp.
"""
from __future__ import annotations

import io
import json
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode

from . import ws
from .log import log, log_exception

SAMPLE_RATE = 16_000
FLUX_URL = "wss://api.deepgram.com/v2/listen"
FLUX_MODEL = "flux-general-en"
UPSTREAM_TIMEOUT_SEC = 15.0
# After the phone says close, how long to wait for the provider's final turn.
UPSTREAM_FLUSH_GRACE_SEC = 4.0

# Flux tuning. eot_threshold is how sure the model must be before EndOfTurn;
# eager_eot_threshold opens the speculative EagerEndOfTurn earlier; the
# timeout is the provider's own hard ceiling in ms.
DEFAULT_EOT_THRESHOLD = 0.7
DEFAULT_EAGER_EOT_THRESHOLD = 0.5
DEFAULT_EOT_TIMEOUT_MS = 5000

_FLUX_EVENTS = {
    "StartOfTurn": "start",
    "Update": "update",
    "EagerEndOfTurn": "eager",
    "TurnResumed": "resumed",
    "EndOfTurn": "end",
}


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True)
class TurnEvent:
    kind: str                    # start | update | eager | resumed | end
    turn: int
    text: str = ""
    confidence: float | None = None
    words: tuple = ()


def normalize_flux_message(msg: dict) -> TurnEvent | None:
    """Flux's TurnInfo -> our turn event. Anything else is None."""
    if not isinstance(msg, dict) or msg.get("type") != "TurnInfo":
        return None
    kind = _FLUX_EVENTS.get(str(msg.get("event") or ""))
    if kind is None:
        return None
    try:
        turn = int(msg.get("turn_index") or 0)
    except (TypeError, ValueError):
        turn = 0
    confidence = msg.get("end_of_turn_confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    words = msg.get("words") or []
    return TurnEvent(
        kind=kind, turn=turn, text=str(msg.get("transcript") or ""),
        confidence=confidence,
        words=tuple(w for w in words if isinstance(w, dict)))


def flux_url(*, model: str = FLUX_MODEL, sample_rate: int = SAMPLE_RATE,
             eot_threshold: float = DEFAULT_EOT_THRESHOLD,
             eager_eot_threshold: float | None = DEFAULT_EAGER_EOT_THRESHOLD,
             eot_timeout_ms: int = DEFAULT_EOT_TIMEOUT_MS,
             keyterms: list[str] | None = None) -> str:
    params: list[tuple[str, str]] = [
        ("model", model), ("encoding", "linear16"),
        ("sample_rate", str(int(sample_rate))),
        ("eot_threshold", f"{max(0.5, min(1.0, eot_threshold)):.2f}"),
        ("eot_timeout_ms", str(max(500, min(60_000, int(eot_timeout_ms))))),
    ]
    if eager_eot_threshold is not None:
        params.append(("eager_eot_threshold",
                       f"{max(0.3, min(0.9, eager_eot_threshold)):.2f}"))
    params.extend(("keyterm", t) for t in (keyterms or []))
    return f"{FLUX_URL}?{urlencode(params)}"


def pcm16_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buf.getvalue()


@dataclass
class TurnLedger:
    """Per-stream bookkeeping that turns provider events into Clarp records.

    Pure apart from the callbacks it is given, so the relay loop can be
    exercised with a fake upstream and the ledger with none at all.
    """
    session: str
    provider: str
    model: str
    keyterms: tuple[str, ...]
    capacity: int
    new_trace: Callable[[], str]
    record_turn: Callable[..., int]          # -> vocab run id
    retain: Callable[[str, bytes], None]     # (trace_id, wav)
    now: Callable[[], float] = time.monotonic
    trace_id: str = ""
    started_at: float = 0.0
    audio: bytearray = field(default_factory=bytearray)
    turns_ended: int = 0
    max_buffer_bytes: int = SAMPLE_RATE * 2 * 120   # two minutes of PCM16

    def open_turn_if_needed(self) -> None:
        if not self.trace_id:
            self.trace_id = self.new_trace()
            self.started_at = self.now()
            self.audio = bytearray()

    def heard(self, pcm: bytes) -> None:
        """Audio belongs to whichever turn is open; a turn opens on first audio
        so the provider's late StartOfTurn never loses the first syllable."""
        self.open_turn_if_needed()
        self.audio.extend(pcm)
        if len(self.audio) > self.max_buffer_bytes:
            del self.audio[: len(self.audio) - self.max_buffer_bytes]

    def apply(self, event: TurnEvent) -> dict:
        """Client payload for one event; closes the ledger's turn on `end`."""
        self.open_turn_if_needed()
        payload = {
            "type": "turn", "event": event.kind, "turn": event.turn,
            "text": event.text, "trace_id": self.trace_id,
        }
        if event.confidence is not None:
            payload["confidence"] = round(event.confidence, 3)
        if event.kind != "end":
            return payload
        latency_ms = int((self.now() - self.started_at) * 1000)
        run_id = 0
        try:
            run_id = int(self.record_turn(
                session=self.session, trace_id=self.trace_id,
                provider=self.provider, model=self.model,
                keyterms=self.keyterms, capacity=self.capacity,
                transcript=event.text, latency_ms=latency_ms) or 0)
        except Exception as e:  # noqa: BLE001 - a record is not worth a turn
            log_exception("sttStreamRecordFail", e, detail=self.trace_id)
        try:
            if self.audio:
                self.retain(self.trace_id, pcm16_wav(bytes(self.audio)))
        except Exception as e:  # noqa: BLE001
            log_exception("sttStreamRetainFail", e, detail=self.trace_id)
        payload["vocab_run_id"] = run_id or None
        payload["turn_ms"] = latency_ms
        self.turns_ended += 1
        self.trace_id = ""
        self.audio = bytearray()
        return payload


def record_turn_run(*, session: str, trace_id: str, provider: str, model: str,
                    keyterms: tuple[str, ...], capacity: int, transcript: str,
                    latency_ms: int) -> int:
    """One vocab run row per ended turn: the keyterms on the socket are what
    the model was sent, exactly as a batch compile would have been."""
    from . import vocab_store
    from .vocab_budget import CompileResult, Form, Term, Unit
    result = CompileResult(
        payload=", ".join(keyterms),
        terms=[Term(text=t, pack="stream") for t in keyterms],
        used=len(keyterms), capacity=capacity, unit=Unit.TERMS, form=Form.TERMS)
    return vocab_store.record_run(
        result, provider=provider, model=model, session=session,
        trace_id=trace_id, transcript=transcript, latency_ms=latency_ms)


def _open_upstream(url: str, api_key: str):
    import websocket
    return websocket.create_connection(
        url, timeout=UPSTREAM_TIMEOUT_SEC,
        header=[f"Authorization: Token {api_key}"])


def _send_http_error(handler, code: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    try:
        handler._send(code, body, "application/json")
    except Exception:  # noqa: BLE001
        pass


def serve_stt_stream(handler, query: dict[str, str], *,
                     open_upstream: Callable = _open_upstream) -> None:
    """Entry point from do_GET for `/stt/stream`. Hijacks the socket."""
    headers = {k.lower(): v for k, v in handler.headers.items()}
    if not ws.is_websocket_upgrade(headers):
        return _send_http_error(handler, 426, "upgrade required (websocket)")
    key = headers.get("sec-websocket-key", "").strip()
    if not key:
        return _send_http_error(handler, 400, "missing Sec-WebSocket-Key")

    from . import agents as agents_db
    from . import stt_providers
    from .config import load
    session = (query.get("session") or "").strip()
    engine = (query.get("model") or stt_providers.selected_engine()).strip()
    provider = stt_providers.provider_of(engine)
    definition = next((d for d in stt_providers.CATALOG if d["id"] == provider), None)
    if definition is None or definition.get("turn_detection") != "own":
        return _send_http_error(
            handler, 409, f"engine does not detect turns on a stream: {engine or 'local'}")
    api_key = stt_providers._key_for(load(), provider)
    if not api_key:
        return _send_http_error(handler, 503, f"{provider} API key is not configured")
    agent = agents_db.get_by_session(session) if session else None

    # Keyterms for the socket: the same compile a batch turn would get,
    # recorded as the stream's opening run.
    keyterms: tuple[str, ...] = ()
    capacity = stt_providers.budget_for(provider).capacity
    vocab_fn = getattr(handler.ctx, "vocab_for_transcription", None)
    from . import trace as _trace
    stream_trace = _trace.new_id()
    if callable(vocab_fn):
        try:
            vocab = vocab_fn(delegated=False, session=session,
                             trace_id=stream_trace, requested_model=f"{provider}:nova-3")
            keyterms = tuple(stt_providers.split_terms(vocab.payload))[:capacity or None]
        except Exception as e:  # noqa: BLE001
            log_exception("sttStreamVocabFail", e)

    model = definition.get("turn_detection_model") or FLUX_MODEL
    url = flux_url(
        model=model,
        eot_threshold=_float(query.get("eot_threshold"), DEFAULT_EOT_THRESHOLD),
        eager_eot_threshold=_float(query.get("eager_eot_threshold"), DEFAULT_EAGER_EOT_THRESHOLD),
        eot_timeout_ms=int(_float(query.get("eot_timeout_ms"), DEFAULT_EOT_TIMEOUT_MS)),
        keyterms=list(keyterms))
    try:
        upstream = open_upstream(url, api_key)
    except Exception as e:  # noqa: BLE001
        log_exception("sttStreamUpstreamFail", e, detail=provider)
        return _send_http_error(handler, 502, f"{provider} stream connect failed: {e}")

    try:
        handler.wfile.write(ws.handshake_response(key))
        handler.wfile.flush()
        handler.connection.settimeout(None)
    except OSError as e:
        try:
            upstream.close()
        except Exception:  # noqa: BLE001
            pass
        return log_exception("sttStreamHandshakeFail", e)

    from .heard_audio import retain as retain_clip
    from .paths import RuntimePaths
    import pathlib
    cache_dir = RuntimePaths.from_home(pathlib.Path.home()).cache_dir

    def retain(trace_id: str, wav: bytes) -> None:
        retain_clip(cache_dir, trace_id=trace_id, audio_bytes=wav,
                    content_type="audio/wav", session=session, model=f"{provider}:{model}")

    ledger = TurnLedger(
        session=session, provider=provider, model=model, keyterms=keyterms,
        capacity=capacity, new_trace=_trace.new_id, record_turn=record_turn_run,
        retain=retain)
    run_relay(handler, upstream, ledger, session=session,
              agent_id=(agent or {}).get("agent_id"), engine=f"{provider}:{model}",
              stream_trace=stream_trace)


def _float(value, default):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def run_relay(handler, upstream, ledger: TurnLedger, *, session: str,
              agent_id: str | None, engine: str, stream_trace: str) -> None:
    """Pump client audio up and provider events down until either side closes."""
    wlock = threading.Lock()
    stop = threading.Event()

    def ws_write(frame: bytes) -> bool:
        with wlock:
            try:
                handler.wfile.write(frame)
                handler.wfile.flush()
                return True
            except OSError:
                return False

    def send_json(obj: dict) -> bool:
        return ws_write(ws.text_frame(json.dumps(obj)))

    send_json({"type": "ready", "engine": engine, "turn_detection": "provider",
               "keyterms": len(ledger.keyterms), "trace_id": stream_trace})
    _emit("server", "sttStreamOpen", session=session or None, agent_id=agent_id,
          trace_id=stream_trace, detail={"engine": engine, "keyterms": len(ledger.keyterms)})
    log("sttStreamOpen", f"session={session} engine={engine}")

    def pump_upstream() -> None:
        try:
            while not stop.is_set():
                try:
                    raw = upstream.recv()
                except Exception:  # noqa: BLE001 - closed / reset / timeout
                    break
                if raw is None or raw == "":
                    break
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") in ("Error", "error"):
                    send_json({"type": "error", "message": str(
                        msg.get("description") or msg.get("message") or msg)[:300]})
                    continue
                event = normalize_flux_message(msg)
                if event is None:
                    continue
                payload = ledger.apply(event)
                if event.kind == "end":
                    _emit("server", "sttStreamTurn", session=session or None,
                          agent_id=agent_id, trace_id=payload.get("trace_id"),
                          duration_ms=payload.get("turn_ms"),
                          detail={"text": event.text, "turn": event.turn,
                                  "vocab_run_id": payload.get("vocab_run_id"),
                                  "engine": engine})
                if not send_json(payload):
                    break
        finally:
            stop.set()

    reader = threading.Thread(target=pump_upstream, daemon=True,
                              name=f"stt-stream-{session or 'anon'}")
    reader.start()
    graceful = False
    try:
        while not stop.is_set():
            frame = ws.read_frame(handler.rfile)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == ws.OP_CLOSE:
                break
            if opcode == ws.OP_PING:
                if not ws_write(ws.pong_frame(payload)):
                    break
                continue
            if opcode == ws.OP_BINARY:
                ledger.heard(payload)
                try:
                    upstream.send_binary(payload)
                except Exception:  # noqa: BLE001
                    break
                continue
            if opcode == ws.OP_TEXT:
                try:
                    msg = json.loads(payload.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict) and msg.get("type") == "close":
                    # The phone is done talking. Tell the provider to flush:
                    # its final EndOfTurn arrives after this, so the reader
                    # keeps running until the provider closes its side.
                    try:
                        upstream.send(json.dumps({"type": "CloseStream"}))
                    except Exception:  # noqa: BLE001
                        pass
                    graceful = True
                    break
    except (BrokenPipeError, ConnectionResetError):
        pass
    except OSError as e:
        log_exception("sttStreamLoopFail", e, detail=session)
    finally:
        if graceful:
            reader.join(timeout=UPSTREAM_FLUSH_GRACE_SEC)
        stop.set()
        try:
            upstream.close()
        except Exception:  # noqa: BLE001
            pass
        reader.join(timeout=2.0)
        send_json({"type": "closed", "turns": ledger.turns_ended})
        try:
            handler.wfile.write(ws.close_frame(1000))
            handler.wfile.flush()
        except OSError:
            pass
        _emit("server", "sttStreamClose", session=session or None, agent_id=agent_id,
              trace_id=stream_trace, detail={"turns": ledger.turns_ended, "engine": engine})
        log("sttStreamClose", f"session={session} turns={ledger.turns_ended}")
