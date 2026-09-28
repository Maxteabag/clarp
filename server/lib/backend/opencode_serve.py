"""Live OpenCode turns through ``opencode serve``.

``opencode run --format json`` reports a tool only once it has finished and a
text part only once it is complete, so the apps showed "Thinking" through a
long command and the first spoken acknowledgement waited for the whole
paragraph. ``run`` is itself a client of OpenCode's HTTP server: it subscribes
to ``/event``, sends the prompt and stops at ``session.status`` idle. This
module does the same from Clarp and so sees every ``message.part.updated``
(a tool starting) and ``message.part.delta`` (text as it streams).

One server per turn, started in the turn's own process group, rather than one
shared server: a turn then starts, stops and fails exactly as an ``opencode
run`` turn does (a stop signals the group; nothing survives it), a crashed
server takes one turn with it, and the working directory, environment and
permissions are the agent's own. Startup costs what ``run`` costs, because
``run`` starts the same server in-process.

The server runs under ``SUPERVISOR``, which kills it when the runtime closes
its stdin: at the end of every turn, and when the runtime dies or restarts
without a chance to clean up. Nothing is left listening.
"""
from __future__ import annotations

import base64
import http.client
import json
import queue
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from ..process_registry import TurnHandle

# Runs `sys.argv[1:]` and ends it when stdin reaches EOF. The runtime holds the
# only writer, so EOF means "turn over" or "runtime gone". A stop signals the
# whole process group, this included, so it needs no handler of its own.
SUPERVISOR = r"""
import subprocess, sys, threading
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)
def watch():
    try:
        while sys.stdin.buffer.read(65536):
            pass
    except Exception:
        pass
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(5)
        except subprocess.TimeoutExpired:
            child.kill()
threading.Thread(target=watch, daemon=True).start()
rc = child.wait()
sys.exit(128 - rc if rc < 0 else rc)
"""

STARTUP_TIMEOUT = 30.0
# The server sends a heartbeat every ten seconds; this long without one means
# it has stopped responding.
EVENT_READ_TIMEOUT = 90.0
REQUEST_TIMEOUT = 30.0
ABORT_TIMEOUT = 3.0

_LISTENING_RE = re.compile(r"listening on (http://[^\s]+)")


def serve_cmd(binary: str) -> list[str]:
    return [sys.executable, "-c", SUPERVISOR, binary, "serve",
            "--hostname", "127.0.0.1", "--port", "0"]


def server_password() -> str:
    return secrets.token_urlsafe(24)


@dataclass(eq=False)
class OpenCodeTurnHandle(TurnHandle):
    """A turn handle that remembers a stop and asks OpenCode to abort first.

    Aborting through the API lets OpenCode record the stop (open tool calls
    settle, the message is marked aborted) before the group is signalled.
    """
    stop_requested: bool = False
    abort: Optional[Callable[[], None]] = None

    def terminate(self) -> bool:
        self.stop_requested = True
        abort = self.abort
        if abort is not None:
            try:
                abort()
            except Exception:  # noqa: BLE001
                pass
        return super().terminate()


def wait_for_url(proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT) -> str:
    """The server's base URL from its stdout, or "" if it did not come up.

    Keeps draining stdout afterwards so the server never blocks on a full pipe.
    """
    lines: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        try:
            for line in proc.stdout or ():
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    threading.Thread(target=read, daemon=True,
                     name=f"opencode-serve-stdout-{proc.pid}").start()
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        while not deadline.is_set():
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                return ""
            match = _LISTENING_RE.search(line)
            if match:
                return match.group(1).rstrip("/")
        return ""
    finally:
        timer.cancel()


class ServeError(RuntimeError):
    pass


class ServeClient:
    """The few endpoints a turn needs, with the server's basic auth."""

    def __init__(self, base_url: str, password: str, directory: str):
        parsed = urllib.parse.urlsplit(base_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.directory = directory

    def _path(self, path: str) -> str:
        return f"{path}?{urllib.parse.urlencode({'directory': self.directory})}"

    def request(self, method: str, path: str, body: Any = None, *,
                timeout: float = REQUEST_TIMEOUT) -> Any:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            headers = dict(self.headers)
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, self._path(path), body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", "replace")
            if response.status >= 400:
                raise ServeError(f"{method} {path}: HTTP {response.status} {raw[:300]}")
            if not raw.strip():
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        finally:
            conn.close()

    def events(self, *, timeout: float = EVENT_READ_TIMEOUT) -> Iterator[dict]:
        """Server-sent events from ``/event``, one decoded ``data`` each.

        The connection is open (subscribed) once the first value is produced:
        ``open_events`` primes it so no event of the turn can be missed.
        """
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request("GET", self._path("/event"),
                         headers={**self.headers, "Accept": "text/event-stream"})
            response = conn.getresponse()
            if response.status >= 400:
                raise ServeError(f"GET /event: HTTP {response.status}")
            yield {"type": "clarp.subscribed"}
            data: list[str] = []
            while True:
                raw = response.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                try:
                    event = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    event = None
                data = []
                if isinstance(event, dict):
                    yield event
        finally:
            conn.close()

    def open_events(self) -> Iterator[dict]:
        stream = self.events()
        next(stream)
        return stream


def split_model(model: str) -> dict[str, str] | None:
    """``provider/model`` (the model id may contain slashes) as OpenCode's pair."""
    provider, _, model_id = (model or "").partition("/")
    if not provider or not model_id:
        return None
    return {"providerID": provider, "modelID": model_id}


# Permission rules `opencode run` puts on the sessions it creates: nobody is
# there to answer a question or approve a switch into plan mode.
SESSION_PERMISSION = [
    {"permission": "question", "action": "deny", "pattern": "*"},
    {"permission": "plan_enter", "action": "deny", "pattern": "*"},
    {"permission": "plan_exit", "action": "deny", "pattern": "*"},
]
