"""Local RPC boundary between the replaceable HTTP server and agent runtime.

The HTTP process is allowed to restart during an update.  Agent turns are
therefore submitted to a separate runtime service over a user-private Unix
socket.  The protocol is deliberately small, JSON-only, and versioned so an
old runtime can finish turns while a newer server release is already serving
clients.
"""
from __future__ import annotations

import dataclasses
import errno
import json
import pathlib
import socket
import socketserver
import sys
import threading
import time
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

    # A Unix-stream connect() fails with EAGAIN the instant the listener's
    # backlog is full; Linux neither queues nor blocks the caller.  Under a
    # status poll storm every HTTP thread saw that as "runtime unavailable"
    # (2026-09-12: thousands of ``[Errno 11]`` per ten minutes and lost
    # ``/send`` prompts) although the runtime was healthy.  Retry with a short
    # backoff inside the caller's timeout budget; a fresh socket per attempt
    # because a failed connect leaves the old one unusable.
    _CONNECT_BACKOFF_INITIAL = 0.02
    _CONNECT_BACKOFF_MAX = 0.25

    def _connect(self, timeout: float | None = None) -> socket.socket:
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        delay = self._CONNECT_BACKOFF_INITIAL
        while True:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.settimeout(timeout)
                client.connect(str(self.socket_path))
                return client
            except OSError as exc:
                client.close()
                backlog_full = exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                if not backlog_full or time.monotonic() + delay > deadline:
                    raise
            except BaseException:
                client.close()
                raise
            time.sleep(delay)
            delay = min(delay * 2, self._CONNECT_BACKOFF_MAX)

    def _request(self, method: str, params: dict[str, Any] | None = None, *,
                 timeout: float | None = None, total_s: float | None = None) -> dict:
        """One RPC; ``timeout`` overrides the client's for this call only.

        ``total_s`` (opt-in; None keeps the behaviour above exactly) bounds the
        WHOLE call: connect, send and every read each get only the time left of
        one monotonic deadline, so a slow or dripping answer cannot extend it."""
        request = encode_request(method, params)
        deadline = None if total_s is None else time.monotonic() + max(0.0, float(total_s))

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("runtime call deadline passed")
            return remaining
        try:
            with self._connect(timeout if deadline is None
                               else min(self.timeout if timeout is None else timeout, left())) as client:
                if deadline is not None:
                    client.settimeout(left())
                client.sendall(request)
                chunks: list[bytes] = []
                total = 0
                while True:
                    if deadline is not None:
                        client.settimeout(left())
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
            # F4 S1 from a runtime that computes it; an older runtime sends
            # none, which stays unknown (never a hint invented here).
            delivery=(result.get("delivery")
                      if isinstance(result.get("delivery"), dict) else None),
            delivery_runtime=(result.get("delivery_runtime")
                              if isinstance(result.get("delivery_runtime"), dict) else None),
        )

    def dispatch(self, **kwargs):
        from .turn_dispatch import DispatchCommand
        return self.dispatch_command(DispatchCommand(**kwargs))

    def dispatch_command(self, command):
        """Send one DispatchCommand; the runtime admits and launches it."""
        return self._dispatch_result(self._request("dispatch", command.to_wire()))

    def dispatch_queued(self, queue_id: str):
        return self._dispatch_result(self._request(
            "dispatch_queued", {"queue_id": queue_id}))

    def recover_queued(self) -> int:
        response = self._request("recover_queued")
        if not response.get("ok"):
            raise RuntimeUnavailable(
                str(response.get("error") or "agent runtime recovery failed"))
        return int(response.get("result") or 0)

    def status(self, *, timeout: float | None = None) -> dict[str, Any]:
        """The runtime's status; ``timeout`` bounds this call alone (GET /status
        must answer while the runtime is busy, see runtime_release)."""
        return self._status(self._request("status", timeout=timeout))

    def status_within(self, total_s: float) -> dict[str, Any]:
        """``status`` bounded as a WHOLE by ``total_s`` (F4: a /log bracket's
        observation under its own deadline). Every other caller keeps ``status``
        and this client's default timeout."""
        return self._status(self._request("status", total_s=total_s))

    @staticmethod
    def _status(response: dict) -> dict[str, Any]:
        if not response.get("ok"):
            raise RuntimeUnavailable(
                str(response.get("error") or "agent runtime status unavailable"))
        result = response.get("result") or {}
        return result if isinstance(result, dict) else {}

    def release_leaked_slots(self, agent_id: str) -> dict[str, str]:
        """Have the runtime release this agent's slot if its turn leaked it."""
        response = self._request("release_leaked_slots", {"agent_id": agent_id})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime leak release failed"))
        result = response.get("result") or {}
        return {str(k): str(v) for k, v in result.items()} \
            if isinstance(result, dict) else {}

    def repair_slot(self, agent_id: str, *, actor: str, reason: str,
                    actor_verified: bool = False) -> dict:
        """Have the runtime give back this agent's unowned slot (not a Stop)."""
        response = self._request("repair_slot", {
            "agent_id": agent_id, "actor": actor, "reason": reason,
            "actor_verified": bool(actor_verified)})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime slot repair failed"))
        result = response.get("result") or {}
        return {"released_trace": str(result.get("released_trace") or ""),
                "queue_recovered": int(result.get("queue_recovered") or 0)}

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
                   hold: bool = True, fence_user_receipts: bool = True) -> StopLease:
        params = {"agent_id": agent_id, "backend": backend, "strict": strict, "hold": hold}
        if not fence_user_receipts:
            params["fence_user_receipts"] = False   # absent means the user's Stop
        response = self._request("begin_stop", params)
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

    def recover_janitor_quota(self, provider, owner_id, generation, approval_id=""):
        response = self._request("recover_janitor_quota", {"provider":provider,"owner_id":owner_id,"generation":generation,"approval_id":approval_id})
        return response.get("result", {"status":"runtime_unavailable"})

    def cancel_janitor_run(self, run_id: str) -> dict[str, Any]:
        response = self._request("cancel_janitor_run", {"run_id": run_id})
        if not response.get("ok"):
            from .turn_dispatch import DispatchError
            raise DispatchError(int(response.get("status") or 502),
                                str(response.get("error") or "Maintenance cancellation failed"))
        return dict(response.get("result") or {})

    def release_agent(self, agent_id: str) -> int:
        response = self._request("release_agent", {"agent_id": agent_id})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime release failed"))
        return int(response.get("result") or 0)

    def goal(self, agent_id: str, action: str, *, objective: str = ""):
        response = self._request("goal", {
            "agent_id": agent_id, "action": action, "objective": objective})
        if not response.get("ok"):
            status = int(response.get("status") or 500)
            message = str(response.get("error") or "runtime goal request failed")
            if status == 501:
                from .backends import GoalUnsupported
                raise GoalUnsupported(message)
            raise RuntimeError(message)
        return response.get("result")

    def compact(self, session: str) -> dict[str, Any]:
        response = self._request("compact", {"session": session})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime compaction failed"))
        result = response.get("result") or {}
        return result if isinstance(result, dict) else {}

    def explanation_command(self, agent_id: str, label: str) -> str:
        """The whole command a clipped Codex label stands for, as the runtime
        saw it, or "". Heredoc bodies stay in the runtime (`shapes.redact`)."""
        response = RuntimeClient(self.socket_path, timeout=min(self.timeout, 0.5))._request(
            "explanation_command", {"agent_id": agent_id, "label": label})
        result = response.get("result") if response.get("ok") else ""
        return result if isinstance(result, str) else ""

    def live_snapshot(self, *, session: str = "", agent_id: str = "") -> dict[str, Any] | None:
        """The runtime hub's GET /live answer for one agent, or None."""
        response = self._request("live_snapshot", {"session": session, "agent_id": agent_id})
        if not response.get("ok"):
            raise RuntimeProtocolError(
                str(response.get("error") or "runtime live snapshot failed"))
        result = response.get("result")
        return result if isinstance(result, dict) else None

    def live_observe(self, agent_id: str, event: str, to_state: str,
                     detail: dict[str, Any]) -> None:
        """A state transition recorded in this process, for the runtime's hub."""
        RuntimeClient(self.socket_path, timeout=min(self.timeout, 2.0))._request(
            "live_observe", {"agent_id": agent_id, "event": event, "to_state": to_state,
                             "detail": detail})

    def live_patch(self, agent_id: str, item_id: str, fields: dict[str, Any]) -> None:
        """Merge fields (a tool explanation) into a live item in the runtime hub."""
        self._request("live_patch", {"agent_id": agent_id, "item_id": item_id,
                                     "fields": fields})

    def ping(self) -> bool:
        try:
            return self.status().get("protocol_version") == PROTOCOL_VERSION
        except (RuntimeProtocolError, RuntimeUnavailable):
            return False
