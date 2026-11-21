"""The Host sets a moved mark only when the runtime that owns turns refuses a
moved agent (lib.agent_moves.runtime_enforces); the runtime says so in its
status capabilities."""
from __future__ import annotations

from types import SimpleNamespace

from lib import agent_moves


class _Runtime:
    def __init__(self, status=None, fails=False):
        self._status, self._fails = status, fails

    def status(self, **_kw):
        if self._fails:
            raise OSError("socket gone")
        return self._status


def test_no_runtime_client_means_this_process_enforces_it():
    assert agent_moves.runtime_enforces(SimpleNamespace(runtime_client=None))
    assert agent_moves.runtime_enforces(SimpleNamespace())


def test_only_a_runtime_reporting_the_capability_is_ready():
    ready = SimpleNamespace(runtime_client=_Runtime({"capabilities": {"agent_moves": True}}))
    assert agent_moves.runtime_enforces(ready)
    for status in ({"capabilities": {"janitor_runs": True}}, {"capabilities": {}}, {},
                   {"capabilities": {"agent_moves": 1}}, {"capabilities": ["agent_moves"]}):
        assert not agent_moves.runtime_enforces(SimpleNamespace(runtime_client=_Runtime(status)))
    assert not agent_moves.runtime_enforces(SimpleNamespace(runtime_client=_Runtime(fails=True)))


def test_this_releases_runtime_reports_the_capability(tmp_path):
    from short_sockets import socket_path as short_socket
    from lib.runtime_rpc_server import RuntimeRPCServer
    service = SimpleNamespace(recover_queued=lambda: 0)
    runtime = RuntimeRPCServer(short_socket("moves.sock"), dispatch_service=service,
                               release_id="test")
    try:
        status = runtime.dispatch_request("status", {})
        assert status["ok"]
        assert status["result"]["capabilities"].get(agent_moves.RUNTIME_CAPABILITY) is True
    finally:
        runtime.server_close()
