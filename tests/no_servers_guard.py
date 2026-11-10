"""Fail fast when a test starts a server under bin/heavy on laptopstudio.

Peter's standing order (2026-10-12): no test servers on the laptop, which runs his live Clarp Host. The program's
bin/heavy refuses test files that visibly start servers before running them; this is the runtime layer behind it.
heavy sets CLARP_TEST_NO_SERVERS=1, and tests/conftest.py then installs this guard, so a test that starts one anyway
(through a helper or fixture the static check cannot see) fails at once instead of serving:

- listening sockets: socket.socket.listen() on any socket (raw TCP and Unix stream sockets, asyncio's create_server and
  start_server, socketserver), and bind() on any datagram socket (a UDP or Unix datagram server). A stream bind()
  without listen() stays allowed: tests pick free ports that way. socketserver servers are refused already when they
  are constructed, before they bind;
- Host launches: a subprocess (subprocess.Popen and everything built on it: run, check_output, os.popen, asyncio
  subprocesses; its executable= override counts), os.system, os.posix_spawn[p] or os.spawnv* (positional and keyword
  arguments alike) whose command line runs a server.py or runtime.py script
  (also inside a shell string, with arguments) or a server/runtime module with -m (also -mruntime, -m server.x,
  -m http.server). The check reads the whole command line, so it refuses conservatively: a third-party server.py is
  refused too, and runs on heavy-remote instead.

This is defence in depth for an honest test suite, not a sandbox: native code, os.exec* (which replaces the test
process) and sockets made through _socket directly are not covered. Without the variable nothing is patched: CI,
heavy-remote and ordinary runs are unchanged.
"""
from __future__ import annotations

import os
import re
import socket
import socketserver
import subprocess

ENV = "CLARP_TEST_NO_SERVERS"
MESSAGE = "server test under heavy (CLARP_TEST_NO_SERVERS=1): run it on heavy-remote"
_SCRIPT = re.compile(r"""(?:^|[\s/"'=])(?:server|runtime)\.py\b""")
_MODULE = re.compile(r"""(?:^|[\s"'])-m\s*(?:[\w.]*\.)?(?:server|runtime)\b""")


class ServerTestRefused(RuntimeError):
    pass


def enabled(environ=os.environ) -> bool:
    return environ.get(ENV) == "1"


def _command_line(args) -> str:
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    parts = []
    for arg in args or ():
        try:
            parts.append(os.fsdecode(arg))
        except TypeError:
            parts.append(str(arg))
    return " ".join(parts)


def starts_host(args) -> bool:
    """Whether a command (argument list, or a shell string) runs a server.py/runtime.py script or module."""
    line = _command_line(args)
    return bool(_SCRIPT.search(line) or _MODULE.search(line))


def install(setattr=setattr) -> None:
    """Patch the server entry points. ``setattr`` may be monkeypatch.setattr, so a test can undo it."""
    def refuse(what):
        raise ServerTestRefused(f"{MESSAGE} ({what})")

    def refuse_server(self, *args, **kwargs):
        refuse(type(self).__name__)

    def refuse_listen(self, *args, **kwargs):
        refuse(f"listen on a {self.family.name} socket")

    original_bind = socket.socket.bind

    def guarded_bind(self, address):
        if self.type == socket.SOCK_DGRAM:
            refuse(f"bind of a {self.family.name} datagram socket")
        return original_bind(self, address)

    original_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, args, *rest, **kwargs):
        # what runs is the executable override (keyword, or the third positional parameter) plus the args
        executable = kwargs.get("executable", rest[1] if len(rest) > 1 else None)
        command = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args or ())
        if executable is not None:
            command.insert(0, executable)
        if starts_host(command):
            refuse(f"subprocess runs {_command_line(command)[:120]!r}")
        original_popen_init(self, args, *rest, **kwargs)

    setattr(socketserver.BaseServer, "__init__", refuse_server)
    setattr(socket.socket, "listen", refuse_listen)
    setattr(socket.socket, "bind", guarded_bind)
    setattr(subprocess.Popen, "__init__", guarded_popen_init)

    def guard(name, params):
        """Wrap os.<name>; ``params`` names its leading parameters in order, so positional and keyword arguments
        (and mixes of the two) are normalized the same way before the command is classified."""
        original = getattr(os, name, None)
        if original is None:
            return

        def guarded(*args, **kwargs):
            bound = dict(zip(params, args))
            bound.update((k, v) for k, v in kwargs.items() if k in params)
            command = []
            for key in ("command", "path", "file"):
                if bound.get(key) is not None:
                    command.append(bound[key])
            for key in ("argv", "args"):
                if bound.get(key) is not None:
                    command.extend(bound[key])
            if starts_host(command):
                refuse(f"os.{name} runs {_command_line(command)[:120]!r}")
            return original(*args, **kwargs)
        setattr(os, name, guarded)

    guard("system", ("command",))
    for name in ("posix_spawn", "posix_spawnp"):
        guard(name, ("path", "argv", "env"))
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
        guard(name, ("mode", "file", "args"))
