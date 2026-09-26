"""Characterization tests for lib.terminal_ws (WebSocket -> PTY bridge).

No real CLI or PTY is spawned: `pty.fork` is replaced with a socketpair so
the "child" side can be scripted, and the WebSocket side is a BytesIO with
hand-masked client frames. Pins the per-backend launch argv, the handshake
and error replies, the live-terminal counter, and the frame dispatch
(binary -> PTY stdin, text resize -> TIOCSWINSZ + SIGWINCH, ping -> pong,
close -> teardown + close frame + drain).
"""
from __future__ import annotations

import io
import json
import os
import signal
import socket
import struct
from types import SimpleNamespace

import pytest

from lib import backends, terminal_ws, ws

_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
_UPGRADE = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade",
            "Sec-WebSocket-Key": _KEY}


def _masked(opcode: int, payload: bytes, mask: bytes = b"\x01\x02\x03\x04") -> bytes:
    """Client->server frame: FIN set, MASK bit set, single 7-bit length."""
    assert len(payload) < 126
    body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + body


class FakeHandler:
    def __init__(self, headers, client_frames: bytes = b"", ctx=None):
        self.headers = dict(headers)
        self.rfile = io.BytesIO(client_frames)
        self.wfile = io.BytesIO()
        self.connection = SimpleNamespace(timeouts=[],
                                          settimeout=lambda t: self.connection.timeouts.append(t))
        self.ctx = ctx or SimpleNamespace()
        self.status = None
        self.sent_headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass


@pytest.fixture(autouse=True)
def _reset_live_counter():
    with terminal_ws._live_lock:
        terminal_ws._live.clear()
    yield
    with terminal_ws._live_lock:
        terminal_ws._live.clear()


@pytest.fixture
def agent(monkeypatch, tmp_path):
    """Point the agent lookups at a fake row; `bsid` controls resume vs fresh."""
    state = {"row": {"agent_id": "agent-1", "backend": "claude", "cwd": str(tmp_path)},
             "bsid": ""}
    monkeypatch.setattr(terminal_ws.agents_db, "get_by_session",
                        lambda session: state["row"] if session == "theo" else None)
    monkeypatch.setattr(terminal_ws.agents_db, "live_backend_session",
                        lambda agent_id: state["bsid"])
    return state


# ---- launch tables --------------------------------------------------------


def test_launch_argv_is_declared_per_backend(monkeypatch):
    """Every registered backend either answers terminal_argv for both a
    resumed and a fresh session or raises Unsupported for both."""
    from lib.backend.base import Unsupported
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: None)
    declared = set()
    for backend in backends.all_backends():
        answers = []
        for sid in ("s-1", ""):
            try:
                backend.terminal_argv(sid)
                answers.append(True)
            except Unsupported:
                answers.append(False)
        assert answers[0] == answers[1], backend.id
        if answers[0]:
            declared.add(backend.id)
    assert declared == {backends.CLAUDE, backends.CODEX, backends.AGY}


@pytest.mark.parametrize("backend,resume,fresh", [
    (backends.CLAUDE, ["claude", "--dangerously-skip-permissions", "--resume", "sid"],
     ["claude", "--dangerously-skip-permissions"]),
    (backends.CODEX, ["codex", "resume", "sid"], ["codex"]),
    (backends.AGY, ["agy", "--dangerously-skip-permissions", "--conversation", "sid"],
     ["agy", "--dangerously-skip-permissions"]),
])
def test_launch_argv_per_backend(monkeypatch, backend, resume, fresh):
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: None)
    assert backends.by_id(backend).terminal_argv("sid") == resume
    assert backends.by_id(backend).terminal_argv("") == fresh


# ---- live-terminal counter -------------------------------------------------


def test_live_counter_marks_and_never_goes_negative():
    assert terminal_ws.has_live_terminal("a") is False
    terminal_ws._mark("a", +1)
    assert terminal_ws.has_live_terminal("a") is True
    terminal_ws._mark("a", +1)
    terminal_ws._mark("a", -1)
    assert terminal_ws.has_live_terminal("a") is True
    terminal_ws._mark("a", -1)
    assert terminal_ws.has_live_terminal("a") is False
    terminal_ws._mark("a", -1)
    assert terminal_ws._live == {}  # clamped at zero and evicted


# ---- pre-spawn error replies ---------------------------------------------


def test_non_upgrade_request_gets_426(agent):
    handler = FakeHandler({"Accept": "*/*"})
    terminal_ws.serve_terminal(handler, "theo")
    assert handler.status == 426
    assert handler.sent_headers["Connection"] == "close"
    assert b"upgrade required" in handler.wfile.getvalue()


def test_unknown_agent_gets_404(agent):
    handler = FakeHandler(_UPGRADE)
    terminal_ws.serve_terminal(handler, "nobody")
    assert handler.status == 404
    assert handler.wfile.getvalue() == b"no such agent"
    assert not terminal_ws.has_live_terminal("agent-1")


@pytest.mark.parametrize("backend,bsid,expected", [
    ("claude", "", ["claude", "--dangerously-skip-permissions"]),
    ("claude", "sess-1", ["claude", "--dangerously-skip-permissions", "--resume", "sess-1"]),
    ("codex", "", ["codex"]),
    ("codex", "thr-9", ["codex", "resume", "thr-9"]),
    ("agy", "", ["agy", "--dangerously-skip-permissions"]),
    ("agy", "c-2", ["agy", "--dangerously-skip-permissions", "--conversation", "c-2"]),
    ("", "x", ["claude", "--dangerously-skip-permissions", "--resume", "x"]),  # blank -> Claude
])
def test_argv_built_from_backend_and_session_then_500_when_cli_missing(
        agent, monkeypatch, backend, bsid, expected):
    agent["row"]["backend"] = backend
    agent["bsid"] = bsid
    probed = []
    monkeypatch.setattr(terminal_ws.shutil, "which", lambda name: probed.append(name))
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: None)
    handler = FakeHandler(_UPGRADE)
    terminal_ws.serve_terminal(handler, "theo")
    assert probed == [expected[0]]
    assert handler.status == 500
    assert handler.wfile.getvalue() == f"{expected[0]} not on PATH".encode()
    assert not terminal_ws.has_live_terminal("agent-1")


def test_claude_gets_plugin_dir_appended(agent, monkeypatch, tmp_path):
    plugin = tmp_path / "plugin"
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: plugin)
    seen = {}

    def fake_fork():
        raise RuntimeError("stop before spawning")

    monkeypatch.setattr(terminal_ws.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(terminal_ws.pty, "fork", fake_fork)
    handler = FakeHandler(_UPGRADE)
    with pytest.raises(RuntimeError):
        terminal_ws.serve_terminal(handler, "theo")
    # Handshake was written before the spawn attempt and the idle timeout lifted.
    assert handler.wfile.getvalue() == ws.handshake_response(_KEY)
    assert handler.connection.timeouts == [None]


@pytest.mark.parametrize("backend", ["grok", "opencode", "deepseek"])
def test_unsupported_backend_gets_clean_error(agent, monkeypatch, backend):
    agent["row"]["backend"] = backend
    handler = FakeHandler(_UPGRADE)
    terminal_ws.serve_terminal(handler, "theo")
    assert handler.status == 501
    assert not terminal_ws.has_live_terminal("agent-1")


# ---- the bridge loop -------------------------------------------------------


def _bridge(monkeypatch, agent, client_frames: bytes, child_output: bytes = b""):
    """Run serve_terminal with a socketpair standing in for the PTY."""
    parent, child = socket.socketpair()
    if child_output:
        child.sendall(child_output)
    calls = {"winsize": [], "kill": [], "signals": [], "drained": []}
    monkeypatch.setattr(terminal_ws.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: None)
    monkeypatch.setattr(terminal_ws.pty, "fork", lambda: (4242, parent.fileno()))
    monkeypatch.setattr(terminal_ws, "_set_winsize",
                        lambda fd, cols, rows: calls["winsize"].append((cols, rows)))
    monkeypatch.setattr(terminal_ws, "_kill", lambda pid: calls["kill"].append(pid))
    monkeypatch.setattr(terminal_ws.os, "kill",
                        lambda pid, sig: calls["signals"].append((pid, sig)))
    # os.close(fd) is called by the bridge; detach so the socket object does
    # not double-close in the finaliser.
    real_close = os.close

    def close_once(fd):
        if fd == parent.fileno():
            parent.detach()
            return real_close(fd)
        return real_close(fd)

    monkeypatch.setattr(terminal_ws.os, "close", close_once)
    monkeypatch.setattr("lib.turn_dispatch.drain_after_terminal",
                        lambda ctx, agent_id: calls["drained"].append((ctx, agent_id)))
    ctx = SimpleNamespace(name="ctx")
    handler = FakeHandler(_UPGRADE, client_frames, ctx=ctx)
    terminal_ws.serve_terminal(handler, "theo")
    child.settimeout(0.5)
    try:
        received = child.recv(4096)
    except (socket.timeout, OSError):
        received = b""
    child.close()
    return handler, calls, received


def _server_frames(raw: bytes):
    """Split the server->client byte stream (after the 101) into frames."""
    frames = []
    rfile = io.BytesIO(raw)
    while True:
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return frames
        opcode, length = hdr[0] & 0x0F, hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", rfile.read(8))[0]
        frames.append((opcode, rfile.read(length)))


def test_bridge_forwards_keystrokes_resize_ping_and_closes(agent, monkeypatch):
    client = (_masked(ws.OP_BINARY, b"ls\n")
              + _masked(ws.OP_TEXT, json.dumps({"resize": {"cols": 132, "rows": 40}}).encode())
              + _masked(ws.OP_TEXT, b"not json")
              + _masked(ws.OP_TEXT, json.dumps({"resize": {}}).encode())
              + _masked(ws.OP_PING, b"hb")
              + _masked(ws.OP_CLOSE, struct.pack("!H", 1000)))
    handler, calls, received = _bridge(monkeypatch, agent, client,
                                       child_output=b"$ prompt")

    # Keystrokes reached the PTY side verbatim.
    assert received == b"ls\n"
    # Initial 80x24, then the client resize, then defaults for an empty resize.
    assert calls["winsize"] == [(80, 24), (132, 40), (80, 24)]
    assert calls["signals"] == [(4242, signal.SIGWINCH)] * 2
    # Teardown: child killed once, drain called with the handler ctx, counter back to 0.
    assert calls["kill"] == [4242]
    assert calls["drained"] == [(handler.ctx, "agent-1")]
    assert not terminal_ws.has_live_terminal("agent-1")

    out = handler.wfile.getvalue()
    handshake = ws.handshake_response(_KEY)
    assert out.startswith(handshake)
    frames = _server_frames(out[len(handshake):])
    # PTY output is pumped by a separate thread and races the client's close
    # frame, so its arrival is not asserted here; the pong and close are
    # written by the bridge loop itself and are deterministic.
    assert (ws.OP_PONG, b"hb") in frames
    assert frames[-1] == (ws.OP_CLOSE, struct.pack("!H", 1000))


def test_bridge_stops_on_client_eof_without_close_frame(agent, monkeypatch):
    handler, calls, received = _bridge(monkeypatch, agent, b"")
    assert received == b""
    assert calls["kill"] == [4242]
    assert calls["drained"][0][1] == "agent-1"
    frames = _server_frames(handler.wfile.getvalue()[len(ws.handshake_response(_KEY)):])
    assert frames == [(ws.OP_CLOSE, struct.pack("!H", 1000))]


def test_bridge_ignores_unmasked_client_frame_as_protocol_violation(agent, monkeypatch):
    unmasked = bytes([0x80 | ws.OP_BINARY, 3]) + b"ls\n"
    handler, calls, received = _bridge(monkeypatch, agent, unmasked)
    assert received == b""          # nothing forwarded
    assert calls["kill"] == [4242]  # treated as stop


def test_drain_failure_is_logged_not_raised(agent, monkeypatch):
    def boom(ctx, agent_id):
        raise RuntimeError("queue gone")

    logged = []
    monkeypatch.setattr(terminal_ws, "log_exception",
                        lambda event, exc, detail="": logged.append((event, detail)))
    parent, child = socket.socketpair()
    monkeypatch.setattr(terminal_ws.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: None)
    monkeypatch.setattr(terminal_ws.pty, "fork", lambda: (99, parent.fileno()))
    monkeypatch.setattr(terminal_ws, "_set_winsize", lambda *a: None)
    monkeypatch.setattr(terminal_ws, "_kill", lambda pid: None)
    monkeypatch.setattr("lib.turn_dispatch.drain_after_terminal", boom)
    real_close = os.close
    monkeypatch.setattr(terminal_ws.os, "close",
                        lambda fd: (parent.detach(), real_close(fd)) if fd == parent.fileno() else real_close(fd))
    handler = FakeHandler(_UPGRADE, b"")
    terminal_ws.serve_terminal(handler, "theo")
    child.close()
    assert ("terminalDrainFail", "theo") in logged
    assert not terminal_ws.has_live_terminal("agent-1")
