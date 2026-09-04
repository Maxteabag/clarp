"""Local RPC boundary between the replaceable HTTP server and agent runtime.

The HTTP process is allowed to restart during an update.  Agent turns are
therefore submitted to a separate runtime service over a user-private Unix
socket.  The protocol is deliberately small, JSON-only, and versioned so an
old runtime can finish turns while a newer server release is already serving
clients.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import socket
import socketserver
import threading
import uuid
from typing import Any


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class RuntimeProtocolError(RuntimeError):
    pass


class RuntimeUnavailable(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class StopLease:
    lease_id: str
    trace_id: str
    terminated: int
    dropped: int


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def encode_request(method: str, params: dict[str, Any] | None = None) -> bytes:
    method = str(method or "").strip()
    if not method:
        raise RuntimeProtocolError("runtime method is required")
    return (json.dumps({
        "version": PROTOCOL_VERSION,
        "method": method,
        "params": _json_value(params or {}),
    }, separators=(",", ":")) + "\n").encode()


def decode_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise RuntimeProtocolError("runtime request is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeProtocolError("invalid runtime request") from exc
    if not isinstance(value, dict):
        raise RuntimeProtocolError("runtime request must be an object")
    if value.get("version") != PROTOCOL_VERSION:
        raise RuntimeProtocolError(
            f"unsupported runtime protocol version: {value.get('version')!r}")
    method = value.get("method")
    params = value.get("params", {})
    if not isinstance(method, str) or not method.strip():
        raise RuntimeProtocolError("runtime method is required")
    if not isinstance(params, dict):
        raise RuntimeProtocolError("runtime params must be an object")
    return {"version": PROTOCOL_VERSION, "method": method, "params": params}


class RuntimeClient:
    def __init__(self, socket_path: pathlib.Path | str, *, timeout: float = 10.0):
        self.socket_path = pathlib.Path(socket_path)
        self.timeout = timeout

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict:
        request = encode_request(method, params)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall(request)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_MESSAGE_BYTES:
                        raise RuntimeProtocolError("runtime response is too large")
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout,
                OSError) as exc:
            raise RuntimeUnavailable(f"agent runtime unavailable: {exc}") from exc
        try:
            response = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeProtocolError("invalid runtime response") from exc
        if not isinstance(response, dict):
            raise RuntimeProtocolError("runtime response must be an object")
        return response

    @staticmethod
    def _dispatch_result(response: dict):
        from .turn_dispatch import DispatchError, DispatchResult

        if not response.get("ok"):
            raise DispatchError(
                int(response.get("status") or 503),
                str(response.get("error") or "agent runtime rejected the request"),
            )
        result = response.get("result") or {}
        return DispatchResult(
            session=str(result.get("session") or ""),
            backend=str(result.get("backend") or ""),
            queued=bool(result.get("queued")),
            queue_depth=int(result.get("queue_depth") or 0),
            queue_revision=int(result.get("queue_revision") or 0),
        )

    def dispatch(self, **kwargs):
        return self._dispatch_result(self._request("dispatch", kwargs))

    def dispatch_queued(self, queue_id: str):
        return self._dispatch_result(self._request(
            "dispatch_queued", {"queue_id": queue_id}))

    def recover_queued(self) -> int:
        response = self._request("recover_queued")
        if not response.get("ok"):
            raise RuntimeUnavailable(
                str(response.get("error") or "agent runtime recovery failed"))
        return int(response.get("result") or 0)

    def status(self) -> dict[str, Any]:
        response = self._request("status")
        if not response.get("ok"):
            raise RuntimeUnavailable(
                str(response.get("error") or "agent runtime status unavailable"))
        result = response.get("result") or {}
        return result if isinstance(result, dict) else {}

    def interrupt(self, backend: str, agent_id: str) -> int:
        response = self._request("interrupt", {
            "backend": backend, "agent_id": agent_id})
        return int(response.get("result") or 0) if response.get("ok") else 0

    def interrupt_any(self, agent_id: str) -> int:
        response = self._request("interrupt_any", {"agent_id": agent_id})
        return int(response.get("result") or 0) if response.get("ok") else 0

    def steer(self, backend: str, agent_id: str, text: str, *,
              client_msg_id: str = "", synthesize_audio: bool = False) -> bool:
        response = self._request("steer", {
            "backend": backend,
            "agent_id": agent_id,
            "text": text,
            "client_msg_id": client_msg_id,
            "synthesize_audio": synthesize_audio,
        })
        return bool(response.get("ok") and response.get("result"))

    def begin_stop(self, agent_id: str, backend: str, *, strict: bool,
                   hold: bool = True) -> StopLease:
        response = self._request("begin_stop", {
            "agent_id": agent_id,
            "backend": backend,
            "strict": strict,
            "hold": hold,
        })
        if not response.get("ok"):
            from .turn_dispatch import DispatchError
            raise DispatchError(
                int(response.get("status") or 502),
                str(response.get("error") or "runtime stop failed"),
            )
        result = response.get("result") or {}
        return StopLease(
            lease_id=str(result.get("lease_id") or ""),
            trace_id=str(result.get("trace_id") or ""),
            terminated=int(result.get("terminated") or 0),
            dropped=int(result.get("dropped") or 0),
        )

    def finish_stop(self, lease_id: str,
                    cancelled_trace_ids: set[str] | None = None) -> None:
        response = self._request("finish_stop", {
            "lease_id": lease_id,
            "cancelled_trace_ids": sorted(cancelled_trace_ids or ()),
        })
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime stop lease failed"))

    def release_agent(self, agent_id: str) -> int:
        response = self._request("release_agent", {"agent_id": agent_id})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime release failed"))
        return int(response.get("result") or 0)

    def compact(self, session: str) -> dict[str, Any]:
        response = self._request("compact", {"session": session})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime compaction failed"))
        result = response.get("result") or {}
        return result if isinstance(result, dict) else {}

    def ping(self) -> bool:
        try:
            return self.status().get("protocol_version") == PROTOCOL_VERSION
        except (RuntimeProtocolError, RuntimeUnavailable):
            return False


class _RuntimeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if not raw or len(raw) > MAX_MESSAGE_BYTES:
            self._write({
                "ok": False, "status": 400,
                "error": "runtime request is empty or too large",
            })
            return
        try:
            request = decode_request(raw)
            response = self.server.dispatch_request(  # type: ignore[attr-defined]
                request["method"], request["params"])
        except RuntimeProtocolError as exc:
            response = {"ok": False, "status": 400, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - isolate malformed RPC calls
            try:
                from .log import log_exception
                log_exception("runtimeRpcFail", exc)
            except Exception:
                pass
            response = {"ok": False, "status": 500,
                        "error": "runtime request failed"}
        self._write(response)

    def _write(self, value: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(
            _json_value(value), separators=(",", ":")) + "\n").encode())


class RuntimeRPCServer(socketserver.ThreadingMixIn,
                       socketserver.UnixStreamServer):
    """User-private runtime endpoint with injectable dispatch for tests."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        socket_path: pathlib.Path | str,
        *,
        dispatch_service,
        status_provider=None,
        release_id: str = "",
        stop_lease_timeout: float = 30.0,
    ):
        self.socket_path = pathlib.Path(socket_path)
        self.dispatch_service = dispatch_service
        self.status_provider = status_provider or self._default_status
        self.release_id = str(release_id or "")
        self.stop_lease_timeout = max(0.01, float(stop_lease_timeout))
        self._stop_leases: dict[str, tuple[str, dict[str, Any]]] = {}
        self._stop_lease_lock = threading.Lock()
        self._admission_lock = threading.RLock()
        self._draining = False
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.socket_path.parent.chmod(0o700)
        except OSError:
            pass
        if self.socket_path.exists() or self.socket_path.is_symlink():
            if not self.socket_path.is_socket():
                raise RuntimeProtocolError(
                    f"refusing to replace non-socket runtime path: {self.socket_path}")
            if RuntimeClient(self.socket_path, timeout=0.5).ping():
                raise RuntimeProtocolError(
                    f"agent runtime is already running: {self.socket_path}")
            # A socket that cannot answer the versioned status call is stale.
            self.socket_path.unlink()
        super().__init__(str(self.socket_path), _RuntimeRequestHandler)
        self.socket_path.chmod(0o600)

    @staticmethod
    def _default_status() -> dict[str, Any]:
        from . import turn_dispatch
        return turn_dispatch.runtime_status()

    def dispatch_request(self, method: str, params: dict[str, Any]) -> dict:
        with self._admission_lock:
            if self._draining and method in {
                "dispatch", "dispatch_queued", "recover_queued", "steer",
            }:
                return {"ok": False, "status": 503,
                        "error": "agent runtime is draining for an update"}
            return self._dispatch_request(method, params)

    def _dispatch_request(self, method: str, params: dict[str, Any]) -> dict:
        if method == "status":
            return {"ok": True, "result": {
                "protocol_version": PROTOCOL_VERSION,
                "draining": self._draining,
                "release_id": self.release_id,
                **dict(self.status_provider()),
            }}
        if method == "recover_queued":
            return {"ok": True,
                    "result": int(self.dispatch_service.recover_queued())}
        if method in {"interrupt", "interrupt_any", "steer"}:
            from . import backends
            agent_id = str(params.get("agent_id") or "")
            if not agent_id:
                return {"ok": False, "status": 400,
                        "error": "agent_id is required"}
            if method == "interrupt":
                result = backends.interrupt(
                    str(params.get("backend") or ""), agent_id)
            elif method == "interrupt_any":
                result = backends.interrupt_any(agent_id)
            else:
                result = backends.steer_turn(
                    str(params.get("backend") or ""), agent_id,
                    str(params.get("text") or ""),
                    client_msg_id=str(params.get("client_msg_id") or ""),
                    synthesize_audio=bool(params.get("synthesize_audio")),
                )
            return {"ok": True, "result": result}
        if method == "begin_stop":
            from . import backends, turn_dispatch, turn_queue
            agent_id = str(params.get("agent_id") or "")
            backend = str(params.get("backend") or "")
            if not agent_id:
                return {"ok": False, "status": 400,
                        "error": "agent_id is required"}
            snapshot, dropped, queue_was_paused = turn_dispatch.begin_stop(agent_id)
            try:
                terminated = int(backends.interrupt(backend, agent_id) or 0)
            except Exception as exc:
                turn_dispatch.restore_stop_state(agent_id, snapshot)
                turn_queue.set_paused(agent_id, queue_was_paused)
                return {"ok": False, "status": 502, "error": str(exc)}
            if (bool(params.get("strict")) and snapshot.get("trace_id")
                    and terminated <= 0):
                turn_dispatch.restore_stop_state(agent_id, snapshot)
                turn_queue.set_paused(agent_id, queue_was_paused)
                return {"ok": False, "status": 502,
                        "error": "backend did not confirm interruption"}
            lease_id = ""
            if bool(params.get("hold", True)):
                lease_id = f"stop-{uuid.uuid4()}"
                with self._stop_lease_lock:
                    self._stop_leases[lease_id] = (agent_id, snapshot)
                timer = threading.Timer(
                    self.stop_lease_timeout,
                    self._expire_stop_lease,
                    args=(lease_id,),
                )
                timer.daemon = True
                timer.start()
            else:
                turn_dispatch.complete_stop(
                    self.dispatch_service.ctx, agent_id, snapshot, set(),
                    backend_registry=self.dispatch_service.backends)
            return {"ok": True, "result": {
                "lease_id": lease_id,
                "trace_id": str(snapshot.get("trace_id") or ""),
                "terminated": terminated,
                "dropped": dropped,
            }}
        if method == "finish_stop":
            lease_id = str(params.get("lease_id") or "")
            with self._stop_lease_lock:
                lease = self._stop_leases.pop(lease_id, None)
            if lease is None:
                return {"ok": False, "status": 404,
                        "error": "runtime stop lease not found"}
            from . import turn_dispatch
            agent_id, snapshot = lease
            turn_dispatch.complete_stop(
                self.dispatch_service.ctx, agent_id, snapshot,
                {str(item) for item in (
                    params.get("cancelled_trace_ids") or [])},
                backend_registry=self.dispatch_service.backends)
            return {"ok": True, "result": True}
        if method == "release_agent":
            from . import agents as agents_db, backends, turn_dispatch
            agent_id = str(params.get("agent_id") or "")
            if not agent_id:
                return {"ok": False, "status": 400,
                        "error": "agent_id is required"}
            turn_dispatch.clear_for_agent(agent_id)
            terminated = int(backends.interrupt_any(agent_id) or 0)
            agents_db.soft_delete(agent_id)
            return {"ok": True, "result": terminated}
        if method == "compact":
            from . import compaction
            session = str(params.get("session") or "").strip()
            if not session:
                return {"ok": False, "status": 400,
                        "error": "session is required"}
            return {"ok": True, "result": compaction.compact_session(session)}
        if method == "dispatch_queued":
            result = self.dispatch_service.dispatch_queued(
                str(params.get("queue_id") or ""))
            return {"ok": True, "result": dataclasses.asdict(result)}
        if method == "dispatch":
            values = dict(params)
            admission = values.get("prompt_admission")
            if isinstance(admission, dict):
                from .prompt_admissions import PromptAdmission
                admission = PromptAdmission.from_json(json.dumps(admission))
                if admission is None:
                    return {"ok": False, "status": 400,
                            "error": "invalid prompt admission"}
                values["prompt_admission"] = admission
            if isinstance(values.get("unheard_audio_sessions"), list):
                values["unheard_audio_sessions"] = tuple(
                    str(item) for item in values["unheard_audio_sessions"])
            try:
                result = self.dispatch_service.dispatch(**values)
            except Exception as exc:
                from .turn_dispatch import DispatchError
                if isinstance(exc, DispatchError):
                    return {"ok": False, "status": exc.status,
                            "error": str(exc)}
                raise
            return {"ok": True, "result": dataclasses.asdict(result)}
        return {"ok": False, "status": 404,
                "error": f"unknown runtime method: {method}"}

    def begin_drain_if_idle(self) -> bool:
        """Fence new work exactly when no runtime-owned work remains."""
        with self._admission_lock:
            if self._draining:
                return True
            with self._stop_lease_lock:
                if self._stop_leases:
                    return False
            status = dict(self.status_provider())
            if (status.get("active") or status.get("spawning")
                    or status.get("terminals") or status.get("queued")
                    or status.get("compactions")):
                return False
            self._draining = True
            return True

    def _expire_stop_lease(self, lease_id: str) -> None:
        with self._stop_lease_lock:
            lease = self._stop_leases.pop(lease_id, None)
        if lease is None:
            return
        from . import turn_dispatch
        agent_id, snapshot = lease
        try:
            turn_dispatch.complete_stop(
                self.dispatch_service.ctx, agent_id, snapshot, set(),
                backend_registry=self.dispatch_service.backends)
        except Exception as exc:  # noqa: BLE001 - never strand the expiry thread
            try:
                from .log import log_exception
                log_exception("runtimeStopLeaseExpiryFail", exc, detail=agent_id)
            except Exception:
                pass

    def server_close(self) -> None:
        super().server_close()
        try:
            if self.socket_path.is_socket():
                self.socket_path.unlink()
        except OSError:
            pass
