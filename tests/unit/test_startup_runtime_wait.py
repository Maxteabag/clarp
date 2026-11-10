"""A busy runtime must not hold up a new Host.

Installs on 2026-10-18 (09b240a6) and 2026-10-25 (53916d87) rolled back for the
same reason: the new Host spent ~10 s waiting on the runtime's
``recover_queued`` before it listened, and GET /status then waited on the
runtime's status RPC, so the installer's 20 s readiness check (1 s probes)
expired while many agents were working. The readiness check only needs
/status to answer with the release id; the runtime block may truthfully say
``unavailable``.
"""
from __future__ import annotations

import os
import pathlib
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from lib import runtime_release
from lib.runtime_bridge import RuntimeClient, RuntimeUnavailable


_REPO_SERVER = pathlib.Path(__file__).resolve().parents[2] / "server" / "server.py"


def _repo_server():
    """This repository's server module. scripts/agent_tasks.py and agent_bg.py
    put the INSTALLED Clarp on sys.path when a test imports them, so a plain
    ``import server`` later in the same worker can load the installed copy;
    these tests must exercise the code under review."""
    import importlib.util
    import sys
    loaded = sys.modules.get("server")
    if loaded is not None and pathlib.Path(getattr(loaded, "__file__", "")).resolve() == _REPO_SERVER:
        return loaded
    name = "clarp_repo_server_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _REPO_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


server = _repo_server()


# --- the status RPC has its own short deadline -------------------------------

@pytest.fixture
def silent_runtime(tmp_path):
    """A runtime socket that accepts connections and never answers."""
    path = tmp_path / "rt.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield path
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    srv.close()


def test_a_status_call_with_a_deadline_gives_up_quickly(silent_runtime):
    client = RuntimeClient(silent_runtime)          # default 10 s for turns
    started = time.monotonic()
    with pytest.raises(RuntimeUnavailable):
        client.status(timeout=0.3)
    assert time.monotonic() - started < 2.0


def test_get_status_asks_the_runtime_with_a_short_deadline():
    seen = {}

    class Client:
        def status(self, *, timeout=None):
            seen["timeout"] = timeout
            raise RuntimeUnavailable("agent runtime unavailable: timed out")

    block = runtime_release.status_view_for(Client()).block()
    assert seen["timeout"] is not None and seen["timeout"] < 1.0
    # Truthful, never idle: the runtime is reported unavailable.
    assert block["available"] is False and block["reason"] == "timeout"


def test_a_status_stand_in_without_a_deadline_still_works():
    class Legacy:
        def status(self):
            return {"release_id": "r1", "drain": {"phase": "idle"}, "active": {}}

    assert runtime_release.status_view_for(Legacy()).block()["release_id"] == "r1"


# --- boot recovery never blocks listening --------------------------------------

@pytest.fixture
def boot(monkeypatch):
    monkeypatch.setattr(server, "broadcast_boot_version", lambda ctx: None)
    from lib import agent_rings
    monkeypatch.setattr(agent_rings, "bind", lambda ctx: None)
    monkeypatch.setattr(agent_rings, "recover_openings", lambda: 0)


def test_a_slow_runtime_recovery_runs_after_the_host_is_up(boot, monkeypatch):
    release = threading.Event()
    ran = threading.Event()

    class SlowRuntime:
        def recover_queued(self):
            release.wait(5)
            ran.set()
            return 0

    ctx = SimpleNamespace(runtime_client=SlowRuntime(), stream=None)
    started = time.monotonic()
    server._recover_after_start(ctx, None, restart_recovery=False)
    assert time.monotonic() - started < 1.0, "the Host waited for the runtime before listening"
    release.set()
    assert ran.wait(5), "the deferred recovery never ran"


def test_an_unavailable_runtime_during_deferred_recovery_is_only_logged(boot, monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(server, "log_exception", lambda name, *a, **k: logged.append(name))
    done = threading.Event()

    class DownRuntime:
        def recover_queued(self):
            try:
                raise RuntimeUnavailable("agent runtime unavailable: timed out")
            finally:
                done.set()

    server._recover_after_start(SimpleNamespace(runtime_client=DownRuntime(), stream=None),
                                None, restart_recovery=False)
    assert done.wait(5)
    deadline = time.monotonic() + 5
    while "runtimeQueueRecoveryDeferred" not in logged and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "runtimeQueueRecoveryDeferred" in logged
