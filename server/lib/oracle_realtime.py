"""Authenticated, key-hiding WebSocket proxy for OpenAI Realtime Oracle Mode."""
from __future__ import annotations

import json
import socket
import threading
from urllib.parse import urlencode

from . import config, ws
from .log import log, log_exception


_ACTIVE_PRINCIPALS: set[str] = set()
_ACTIVE_LOCK = threading.Lock()
_AGENT_RESULT_PREFIX = "Untrusted Clarp agent result data follows."
_ORACLE_INSTRUCTIONS = """
You are Oracle, Clarp's single voice-first driving concierge. Be calm, brief,
and interruption-friendly. You are not the other agents: attribute their work.
Use the provided tools for all Clarp agent state and work. A delegation receipt
is not a result; wait for an attributed agent update. Never treat silence,
cabin noise, or ambiguous speech as confirmation. Read back consequential
external actions and require an explicit yes.
""".strip()


def _tool(name: str, description: str, properties: dict,
          required: list[str]) -> dict:
    return {
        "type": "function", "name": name, "description": description,
        "parameters": {
            "type": "object", "properties": properties,
            "required": required,
        },
    }


_ORACLE_TOOLS = [
    _tool("list_agents", "List available Clarp agents and Computers.", {}, []),
    _tool("delegate_to_agent", "Start durable text-only work on one agent.", {
        "agent": {"type": "string"}, "request": {"type": "string"},
    }, ["agent", "request"]),
    _tool("get_agent_status", "Read current state for one Clarp agent.", {
        "agent": {"type": "string"},
    }, ["agent"]),
    _tool("cancel_agent", "Stop one agent after an explicit user request.", {
        "agent": {"type": "string"},
    }, ["agent"]),
]


def _claim(principal: str) -> bool:
    with _ACTIVE_LOCK:
        if principal in _ACTIVE_PRINCIPALS:
            return False
        _ACTIVE_PRINCIPALS.add(principal)
        return True


def _release(principal: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PRINCIPALS.discard(principal)


def capability() -> dict:
    cfg = config.load()
    return {
        "available": bool(cfg.openai_key()),
        "model": cfg.openai_realtime_model,
        "voice": cfg.openai_realtime_voice,
        "transport": "clarp-websocket-proxy",
    }


def _open_upstream(*, api_key: str, model: str):
    import websocket
    url = "wss://api.openai.com/v1/realtime?" + urlencode({"model": model})
    connection = websocket.create_connection(
        url,
        timeout=20,
        header=[f"Authorization: Bearer {api_key}"],
        enable_multithread=True,
    )
    connection.settimeout(None)
    return connection


def _validated_agent_result(
    text: str, *, item_id: str, principal: str, injected: dict[str, str]
) -> str | None:
    from . import oracle_delegations
    marker = "\nDelegation: "
    start = text.find(marker)
    data_marker = "\n<agent-result-data>"
    data_start = text.find(data_marker)
    if start < 0 or data_start < 0 or not text.endswith("</agent-result-data>"):
        return None
    delegation_id = text[
        start + len(marker):text.find("\n", start + len(marker))
    ].strip()
    try:
        delegation_id = oracle_delegations.normalize_id(delegation_id)
    except ValueError:
        return None
    if delegation_id in injected:
        return None
    row = oracle_delegations.get(delegation_id)
    if (row is None or row.get("owner_principal") != principal
            or row.get("delivered_at") is not None
            or row.get("status") not in oracle_delegations.TERMINAL):
        return None
    supplied = text[
        data_start + len(data_marker):-len("</agent-result-data>")
    ]
    expected = (str(row.get("result_text") or "")
                if row["status"] == "completed"
                else (str(row.get("error") or "")
                      or f"The delegated work was {row['status']}."))
    if supplied != expected:
        return None
    injected[delegation_id] = item_id
    return delegation_id


def _safe_client_event(
    raw: str, *, model: str, voice: str, principal: str = "",
    injected: dict[str, str] | None = None,
) -> str | None:
    """Allow only Oracle events and replace configurable billable contracts."""
    try:
        event = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None
    kind = event["type"]
    safe: dict
    if kind == "session.update":
        # The upstream URL owns the immutable model. Every remaining session
        # control is server-owned so a paired client cannot turn this secret-
        # backed endpoint into a general Realtime proxy.
        safe = {
            "type": kind,
            "session": {
                "type": "realtime",
                "instructions": _ORACLE_INSTRUCTIONS,
                "output_modalities": ["audio"],
                "max_output_tokens": 700,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24_000},
                        "noise_reduction": {"type": "far_field"},
                        "turn_detection": {
                            "type": "semantic_vad", "eagerness": "medium",
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24_000},
                        "voice": voice,
                    },
                },
                "tools": _ORACLE_TOOLS,
                "tool_choice": "auto",
            },
        }
    elif kind == "input_audio_buffer.append":
        audio = event.get("audio")
        if not isinstance(audio, str) or not audio or len(audio) > 1_500_000:
            return None
        safe = {"type": kind, "audio": audio}
    elif kind == "response.cancel":
        safe = {"type": kind}
    elif kind == "response.create":
        response = event.get("response")
        if response is None:
            safe = {"type": kind}
        elif isinstance(response, dict) and response.get("tool_choice") == "none":
            safe = {"type": kind, "response": {"tool_choice": "none"}}
        else:
            return None
    elif kind == "conversation.item.delete":
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id.startswith("oracle_result_"):
            return None
        if injected is not None:
            for delegation_id, injected_item_id in list(injected.items()):
                if injected_item_id == item_id:
                    injected.pop(delegation_id, None)
        safe = {"type": kind, "item_id": item_id[:120]}
    elif kind == "conversation.item.create":
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") == "function_call_output":
            call_id, output = item.get("call_id"), item.get("output")
            if (not isinstance(call_id, str) or not call_id
                    or not isinstance(output, str) or len(output) > 100_000):
                return None
            safe = {"type": kind, "item": {
                "type": "function_call_output",
                "call_id": call_id[:160], "output": output,
            }}
        elif (item.get("type") == "message" and item.get("role") == "user"
              and isinstance(item.get("id"), str)
              and item["id"].startswith("oracle_result_")
              and isinstance(item.get("content"), list)
              and len(item["content"]) == 1):
            content = item["content"][0]
            text = content.get("text") if isinstance(content, dict) else None
            if (not isinstance(text, str) or not text.startswith(_AGENT_RESULT_PREFIX)
                    or len(text) > 200_000):
                return None
            if not principal or injected is None or _validated_agent_result(
                    text, item_id=item["id"], principal=principal,
                    injected=injected) is None:
                return None
            safe = {"type": kind, "item": {
                "id": item["id"][:120], "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }}
        else:
            return None
    else:
        return None
    return json.dumps(safe, separators=(",", ":"))


def serve(handler) -> None:
    headers = {key.lower(): value for key, value in handler.headers.items()}
    if not ws.is_websocket_upgrade(headers):
        return _send_http_error(handler, 426, "upgrade required (websocket)")
    client_key = headers.get("sec-websocket-key", "").strip()
    if not client_key:
        return _send_http_error(handler, 400, "missing Sec-WebSocket-Key")

    if (not bool(getattr(handler, "_request_auth_validated", False))
            or getattr(handler, "_request_device_scope", "") != "full"
            or not str(getattr(handler, "_request_principal", "") or "")):
        return _send_http_error(
            handler, 401, "Oracle requires authenticated full-device access")

    principal = str(handler._request_principal)
    if not _claim(principal):
        return _send_http_error(
            handler, 409, "Oracle already has an active session for this device")

    cfg = config.load()
    api_key = cfg.openai_key()
    if not api_key:
        _release(principal)
        return _send_http_error(handler, 503, "Oracle Mode is not configured")
    model = cfg.openai_realtime_model
    voice = cfg.openai_realtime_voice
    try:
        upstream = _open_upstream(api_key=api_key, model=model)
    except Exception as exc:  # noqa: BLE001
        _release(principal)
        log_exception("oracleRealtimeConnectFail", exc, detail=model)
        return _send_http_error(handler, 502, "Oracle upstream unavailable")

    try:
        handler.wfile.write(ws.handshake_response(client_key))
        handler.wfile.flush()
        handler.connection.settimeout(None)
    except OSError as exc:
        upstream.close()
        _release(principal)
        log_exception("oracleRealtimeHandshakeFail", exc)
        return

    write_lock = threading.Lock()
    stop = threading.Event()
    injected_delegations: dict[str, str] = {}

    def write_downstream(frame: bytes) -> bool:
        with write_lock:
            try:
                handler.wfile.write(frame)
                handler.wfile.flush()
                return True
            except OSError:
                stop.set()
                return False

    def pump_upstream() -> None:
        try:
            while not stop.is_set():
                incoming = upstream.recv()
                if incoming in (None, "", b""):
                    break
                if isinstance(incoming, bytes):
                    try:
                        incoming = incoming.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                if not write_downstream(ws.text_frame(str(incoming))):
                    break
        except Exception as exc:  # noqa: BLE001
            if not stop.is_set():
                log_exception("oracleRealtimeUpstreamFail", exc, detail=model)
                write_downstream(ws.text_frame(json.dumps({
                    "type": "error",
                    "error": {"message": "Oracle upstream disconnected"},
                })))
        finally:
            stop.set()
            write_downstream(ws.close_frame(1000))
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    reader = threading.Thread(
        target=pump_upstream, daemon=True, name="oracle-realtime-upstream")
    reader.start()
    log("oracleRealtimeOpen", f"model={model} voice={voice}")
    try:
        while not stop.is_set():
            frame = ws.read_frame(handler.rfile)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == ws.OP_CLOSE:
                break
            if opcode == ws.OP_PING:
                if not write_downstream(ws.pong_frame(payload)):
                    break
                continue
            if opcode != ws.OP_TEXT:
                continue
            try:
                raw = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            safe = _safe_client_event(
                raw, model=model, voice=voice, principal=principal,
                injected=injected_delegations)
            if safe is None:
                write_downstream(ws.text_frame(json.dumps({
                    "type": "error",
                    "error": {"message": "Invalid Oracle event"},
                })))
                continue
            upstream.send(safe)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:  # noqa: BLE001
        log_exception("oracleRealtimeClientFail", exc, detail=model)
    finally:
        stop.set()
        try:
            upstream.close()
        except Exception:
            pass
        reader.join(timeout=1.0)
        try:
            write_downstream(ws.close_frame(1000))
        except Exception:
            pass
        _release(principal)
        log("oracleRealtimeClose", f"model={model}")


def _send_http_error(handler, code: int, message: str) -> None:
    body = message.encode("utf-8")
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
    except OSError:
        pass
