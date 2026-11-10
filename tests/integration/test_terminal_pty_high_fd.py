"""Real-PTY test for the terminal bridge when its PTY master fd is above 1024.

select() rejects fds >= FD_SETSIZE (1024) and the reader used to swallow that
and exit, so a terminal opened in a busy process showed no output and only the
closing frame. Main CI's home-terminal failure has that shape, but the CI PTY
fd number was never measured. This drives the real
_serve_pty with a real PTY child over a socketpair; pty.fork is only observed,
never faked. The database is the private per-test one from conftest's
autouse _isolated_db, and agent_id=None keeps the bridge off agent rows.
"""
from __future__ import annotations

import os
import pathlib
import resource
import socket
import struct
import sys
import threading
import time

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import terminal_ws, ws   # noqa: E402

_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
_MARK = b"CLARP_PTY_MARK"


class _Handler:
    """The attributes _serve_pty uses, over one end of a socketpair."""

    def __init__(self, sock: socket.socket):
        self.connection = sock
        self.rfile = sock.makefile("rb")
        self.wfile = sock.makefile("wb")
        self.close_connection = False


def _read_session(cli: socket.socket, deadline: float) -> tuple[bytes, list[tuple[int, bytes]]]:
    """Read the 101 handshake, then frames until the close frame or deadline."""
    cli.settimeout(max(0.1, deadline - time.monotonic()))
    buf = cli.makefile("rb")
    head = b""
    frames: list[tuple[int, bytes]] = []
    try:
        while not head.endswith(b"\r\n\r\n"):
            c = buf.read(1)
            if not c:
                return head, frames
            head += c
        while time.monotonic() < deadline:
            h = buf.read(2)
            if len(h) < 2:
                break
            op, ln = h[0] & 0x0F, h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", buf.read(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", buf.read(8))[0]
            frames.append((op, buf.read(ln)))
            if op == ws.OP_CLOSE:
                break
    except (socket.timeout, OSError):
        pass
    finally:
        buf.close()
    return head, frames


@pytest.mark.parametrize("high", [False, True], ids=["master-as-found", "master-above-1024"])
def test_terminal_delivers_pty_output_whatever_the_master_fd_number(tmp_path, monkeypatch, high):
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft >= 1100, f"RLIMIT_NOFILE soft limit {soft} cannot place fds above 1024"
    real_fork = terminal_ws.pty.fork
    forks: list[tuple[int, int]] = []

    def observed_fork():
        pid, fd = real_fork()
        if pid != 0:
            forks.append((pid, fd))
        return pid, fd

    monkeypatch.setattr(terminal_ws.pty, "fork", observed_fork)
    srv, cli = socket.socketpair()
    handler = _Handler(srv)
    serve = threading.Thread(target=terminal_ws._serve_pty, args=(handler,), daemon=True, kwargs=dict(
        key=_KEY, argv=["/bin/sh", "-c", f"printf {_MARK.decode()}; sleep 1"], cwd=str(tmp_path),
        session="pty-high-fd-test", agent_id=None, backend="shell", bsid=""))
    placeholders: list[int] = []
    try:
        while high:                       # occupy every free fd number below 1024
            fd = os.open(os.devnull, os.O_RDONLY)
            if fd >= 1024:
                os.close(fd)
                break
            placeholders.append(fd)
        serve.start()
        head, frames = _read_session(cli, time.monotonic() + 8.0)
        serve.join(timeout=8.0)
    finally:
        for fd in placeholders:
            os.close(fd)
        for f in (handler.rfile, handler.wfile, srv, cli):
            f.close()

    assert len(forks) == 1
    pid, master = forks[0]
    if high:     # LOW only checks delivery: a busy worker may already be above 1024
        assert master >= 1024
    assert head.startswith(b"HTTP/1.1 101")
    output = b"".join(p for op, p in frames if op == ws.OP_BINARY)
    assert _MARK in output
    assert frames[-1] == (ws.OP_CLOSE, struct.pack("!H", 1000))
    assert not serve.is_alive()
    with pytest.raises(ChildProcessError):    # _serve_pty reaped its child
        os.waitpid(pid, os.WNOHANG)
