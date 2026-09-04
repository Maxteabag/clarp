"""WebSocket endpoint /terminal/<session>: an interactive terminal into an
agent's CLI session.

On connect we spawn the agent's backend CLI INTERACTIVELY in a PTY, resumed on
the same backend session id, and bridge raw bytes both ways:

  client binary frame                          -> PTY stdin (keystrokes)
  PTY stdout/stderr                            -> client binary frame
  client text {"resize":{"cols":C,"rows":R}}   -> TIOCSWINSZ + SIGWINCH

  claude --resume <id> --dangerously-skip-permissions
  codex resume <id>
  agy --conversation <id> --dangerously-skip-permissions

On disconnect / child exit we kill the child's process group and close. A live
terminal registers the agent_id so turn_dispatch can serialize a normal -p turn
routed to the SAME agent — two processes resuming one session would collide.

The normal voice / dictation / orchestrator / agent-switch pipeline is
untouched: it dispatches turns to OTHER agents as usual. Only a turn routed to
the very agent whose terminal is live waits (queued) until the terminal closes.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import termios
import threading
import json

from . import agents as agents_db
from . import backends
from . import ws
from .log import log, log_exception

# Interactive launch argv when resuming a known session (id appended) and when
# starting fresh (agent has never bound a session id yet).
_LAUNCH_RESUME: dict[str, list[str]] = {
    backends.CLAUDE: ["claude", "--dangerously-skip-permissions", "--resume"],
    backends.CODEX:  ["codex", "resume"],
    backends.AGY:    ["agy", "--dangerously-skip-permissions", "--conversation"],
}
_LAUNCH_FRESH: dict[str, list[str]] = {
    backends.CLAUDE: ["claude", "--dangerously-skip-permissions"],
    backends.CODEX:  ["codex"],
    backends.AGY:    ["agy", "--dangerously-skip-permissions"],
}

_READ_CHUNK = 65536

# agent_id -> number of live terminals (0/1 in practice). turn_dispatch reads
# has_live_terminal() to decide whether to serialize a same-agent turn.
_live: dict[str, int] = {}
_live_lock = threading.Lock()


def has_live_terminal(agent_id: str) -> bool:
    with _live_lock:
        return _live.get(agent_id, 0) > 0


def _mark(agent_id: str, delta: int) -> None:
    with _live_lock:
        n = max(0, _live.get(agent_id, 0) + delta)
        if n:
            _live[agent_id] = n
        else:
            _live.pop(agent_id, None)


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass


def _open_terminal(handler, session: str, key: str):
    """Resolve, spawn, and register a PTY without racing an agent reset."""
    from .agent_lifecycle import AgentLifecycleService

    with AgentLifecycleService._lifecycle_gate.read():
        agent = agents_db.get_by_session(session)
        if not agent:
            _send_http_error(handler, 404, "no such agent")
            return None
        backend = backends.normalize(agent.get("backend"))
        agent_id = agent["agent_id"]
        bsid = agents_db.live_backend_session(agent_id)
        if bsid:
            argv = list(_LAUNCH_RESUME[backend]) + [bsid]
        else:
            argv = list(_LAUNCH_FRESH[backend])
        if backend == backends.CLAUDE:
            from .deployment import plugin_dir
            plugin = plugin_dir()
            if plugin is not None:
                argv += ["--plugin-dir", str(plugin)]
        if shutil.which(argv[0]) is None:
            _send_http_error(handler, 500, f"{argv[0]} not on PATH")
            return None
        from .launch_paths import existing_workspace_path
        cwd = str(existing_workspace_path(agent.get("cwd")))

        try:
            handler.wfile.write(ws.handshake_response(key))
            handler.wfile.flush()
            handler.connection.settimeout(None)
        except OSError as exc:
            log_exception("terminalHandshakeFail", exc)
            return None

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
            except OSError:
                pass
            os.environ["TERM"] = "xterm-256color"
            os.environ.setdefault("LANG", "en_US.UTF-8")
            try:
                os.execvp(argv[0], argv)
            except OSError:
                os._exit(127)
        _mark(agent_id, +1)
        return agent_id, backend, bsid, pid, fd


def serve_terminal(handler, session: str) -> None:
    """Entry point from do_GET. `handler` is the BaseHTTPRequestHandler; the
    socket is hijacked on a successful WS upgrade."""
    headers = {k.lower(): v for k, v in handler.headers.items()}
    if not ws.is_websocket_upgrade(headers):
        return _send_http_error(handler, 426, "upgrade required (websocket)")
    key = headers.get("sec-websocket-key", "").strip()
    if not key:
        return _send_http_error(handler, 400, "missing Sec-WebSocket-Key")

    opened = _open_terminal(handler, session, key)
    if opened is None:
        return
    agent_id, backend, bsid, pid, fd = opened
    log("terminalStart", f"session={session} backend={backend} bsid={bsid} pid={pid}")
    _set_winsize(fd, 80, 24)

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

    # Reader: PTY output -> client binary frames. EIO on Linux == child exited.
    def pump_pty() -> None:
        try:
            while not stop.is_set():
                try:
                    r, _, _ = select.select([fd], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if fd not in r:
                    continue
                try:
                    data = os.read(fd, _READ_CHUNK)
                except OSError:
                    break  # EIO: child gone
                if not data:
                    break
                if not ws_write(ws.binary_frame(data)):
                    break
        finally:
            stop.set()

    reader = threading.Thread(target=pump_pty, daemon=True, name=f"term-pty-{session}")
    reader.start()

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
                try:
                    os.write(fd, payload)
                except OSError:
                    break
                continue
            if opcode == ws.OP_TEXT:
                try:
                    msg = json.loads(payload.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    continue
                rs = msg.get("resize") if isinstance(msg, dict) else None
                if isinstance(rs, dict):
                    _set_winsize(fd, int(rs.get("cols") or 80), int(rs.get("rows") or 24))
                    try:
                        os.kill(pid, signal.SIGWINCH)
                    except OSError:
                        pass
                continue
    except (BrokenPipeError, ConnectionResetError):
        pass
    except OSError as e:
        log_exception("terminalLoopFail", e, detail=session)
    finally:
        stop.set()
        _kill(pid)
        try:
            os.close(fd)
        except OSError:
            pass
        reader.join(timeout=1.0)
        _mark(agent_id, -1)
        try:
            handler.wfile.write(ws.close_frame(1000))
            handler.wfile.flush()
        except OSError:
            pass
        # Drain any normal turns that queued behind this terminal while it was
        # live (e.g. a voice command routed to this same agent).
        try:
            from . import turn_dispatch
            turn_dispatch.drain_after_terminal(handler.ctx, agent_id)
        except Exception as e:  # noqa: BLE001
            log_exception("terminalDrainFail", e, detail=session)
        log("terminalClose", f"session={session} pid={pid}")


def _kill(pid: int) -> None:
    """Kill the child's whole process group (claude may spawn helpers)."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    try:
        for _ in range(20):
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid:
                return
            threading.Event().wait(0.05)
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _send_http_error(handler, code: int, message: str) -> None:
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(message)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(message.encode("utf-8"))
    except OSError as e:
        log_exception("terminalErrorReplyFail", e, detail=str(code))
