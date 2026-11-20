"""The /send route itself reads X-Clarp-Peer (issue 478, AB; the reviewer's mutant AB7).

A peer Host names itself with the header (lib.server_peers.send); it forwards only its agents'
messages, so its claim of the user's own word is an agent's. Without the header the same request
(a device, the Host's own token) is untouched."""
from __future__ import annotations

import json
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _send(base, headers):
    data = json.dumps({"text": "hello", "session": "claude", "origin": "user",
                       "client_msg_id": "c-" + ("peer" if headers else "plain")}).encode()
    request = urllib.request.Request(base + "/send", data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


@pytest.mark.parametrize("headers, origin", [({"X-Clarp-Peer": "work"}, "agent"), ({}, "user")],
                         ids=["peer-host", "no-header"])
def test_the_send_route_reads_the_peer_header(running_server, monkeypatch, headers, origin):
    from lib.turn_dispatch import DispatchResult, TurnDispatchService
    submitted = []

    def submit(self, command):
        submitted.append(command)
        return DispatchResult(session="claude", backend="claude", queued=False)
    monkeypatch.setattr(TurnDispatchService, "submit", submit)
    base, _ctx, _srv = running_server
    assert _send(base, headers) == 200
    assert [command.origin for command in submitted] == [origin]
