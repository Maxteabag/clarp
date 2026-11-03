import asyncio
import http.server
import os
import socket
import socketserver
import subprocess
import sys

import pytest

from tests import no_servers_guard as guard

SENTINEL = "import pathlib, sys; pathlib.Path(sys.argv[0]).with_suffix('.ran').write_text('ran')\n"


@pytest.fixture
def guarded(monkeypatch):
    guard.install(setattr=monkeypatch.setattr)


@pytest.fixture
def sentinels(tmp_path):
    """Inert stand-ins named like Host entry points: if the guard ever let one run, it only writes a marker."""
    for name in ("server.py", "runtime.py"):
        script = tmp_path / name
        script.write_text(f"#!{sys.executable}\n" + SENTINEL)
        script.chmod(0o755)   # runnable on its own, so an executable= override would really run it
    yield tmp_path
    assert not list(tmp_path.glob("*.ran")), "a guarded Host launch ran"


def test_the_guard_is_off_unless_heavy_sets_the_variable():
    assert not guard.enabled({})
    assert not guard.enabled({"CLARP_TEST_NO_SERVERS": "0"})
    assert guard.enabled({"CLARP_TEST_NO_SERVERS": "1"})


@pytest.mark.parametrize("command", [
    ["python3", "server/server.py", "--port", "0"],
    "python3 /opt/clarp/server/server.py --synthetic",
    ["bash", "-c", "cd /x && exec python3 runtime.py --synthetic"],
    ["python3", "-m", "runtime", "--synthetic"],
    ["python3", "-mruntime"],
    ["python3", "-m", "server.runtime"],
    ["python3", "-m", "http.server", "0"],
    [b"python3", b"runtime.py"],
])
def test_host_launch_forms_are_recognized(command):
    assert guard.starts_host(command)


@pytest.mark.parametrize("command", [
    ["python3", "-c", "print(1)"],
    ["python3", "-m", "pytest", "tests/unit/test_runtime.py", "-n", "0"],
    ["git", "log", "--", "server/lib/server_identity.py"],
    "grep -c serve tests/unit/test_server_di.py",
    ["python3", "-m", "server_tools"],
])
def test_ordinary_commands_are_not_host_launches(command):
    assert not guard.starts_host(command)


def test_listeners_of_every_kind_are_refused_before_serving(guarded, tmp_path):
    with pytest.raises(guard.ServerTestRefused, match="run it on heavy-remote"):
        http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    with pytest.raises(guard.ServerTestRefused, match="run it on heavy-remote"):
        socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)

    for family, address in ((socket.AF_INET, ("127.0.0.1", 0)), (socket.AF_UNIX, str(tmp_path / "l.sock"))):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind(address)   # a bare stream bind (picking a port) is allowed
            with pytest.raises(guard.ServerTestRefused, match="listen on"):
                sock.listen()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(guard.ServerTestRefused, match="datagram"):
            sock.bind(("127.0.0.1", 0))

    async def start():
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        server.close()
        await server.wait_closed()
    with pytest.raises(guard.ServerTestRefused, match="listen on"):
        asyncio.run(start())


def test_host_launches_are_refused_and_other_processes_still_run(guarded, sentinels):
    py = sys.executable
    for launch in (
        lambda: subprocess.Popen([py, str(sentinels / "server.py"), "--port", "0"]),
        lambda: subprocess.run(f"{py} {sentinels / 'server.py'} --synthetic", shell=True),
        lambda: subprocess.run([py, "-m", "runtime", "--synthetic"], cwd=sentinels),
        lambda: os.system(f"{py} {sentinels / 'runtime.py'}"),
        lambda: os.posix_spawn(py, [py, str(sentinels / "runtime.py")], dict(os.environ)),
        # keyword and mixed forms, and the executable override
        lambda: os.system(command=f"{py} {sentinels / 'runtime.py'}"),
        lambda: os.spawnv(mode=os.P_WAIT, file=py, args=[py, str(sentinels / "server.py")]),
        lambda: os.spawnv(os.P_WAIT, py, args=[py, str(sentinels / "server.py")]),
        lambda: os.spawnv(os.P_WAIT, file=py, args=[py, str(sentinels / "runtime.py")]),
        lambda: subprocess.Popen(args=[py, str(sentinels / "server.py")]),
        lambda: subprocess.run(["synthetic"], executable=str(sentinels / "server.py"), close_fds=True),
        lambda: subprocess.Popen(["synthetic"], -1, str(sentinels / "runtime.py")),
    ):
        with pytest.raises(guard.ServerTestRefused, match="run it on heavy-remote"):
            launch()

    ok = subprocess.run([py, "-c", "import sys; print(sys.argv[1])", "tests/unit/test_runtime.py"],
                        capture_output=True, text=True)
    assert ok.stdout == "tests/unit/test_runtime.py\n"
