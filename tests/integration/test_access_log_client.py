"""The access line names the client build (the strict X-Clarp-Client form) and who
called (device:<name>, admin, agent, peer), and never a token."""
from __future__ import annotations

import time
import urllib.request

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)

ADMIN = "administrator-secret-for-the-log-test"


def _get(base, headers):
    request = urllib.request.Request(base + "/agents/snapshot", headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def _access_lines(capfd):
    time.sleep(0.05)                      # the handler prints after the response is sent
    out = capfd.readouterr().out
    return [line for line in out.splitlines() if '"GET /agents/snapshot' in line]


def test_the_line_names_the_client_and_the_caller_never_the_token(running_server, capfd):
    from lib import device_pairing
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token=ADMIN)
    issued = device_pairing.issue(device_name="Test phone (kitchen)", scope="full")
    device_token = device_pairing.exchange(issued["code"])["token"]
    capfd.readouterr()

    _get(base, {"Authorization": f"Bearer {device_token}",
                "X-Clarp-Client": "ios/3015 contract=83"})
    _get(base, {"Authorization": f"Bearer {ADMIN}", "X-Clarp-Client": "android/41 contract=80"})
    _get(base, {"Authorization": f"Bearer {ADMIN}", "X-Clarp-Client": "anything <i>free</i> form"})
    lines = _access_lines(capfd)
    assert len(lines) == 3, lines
    device_line, admin_line, free_line = lines
    assert device_line.endswith(" ios/3015;contract=83 device:Test_phone__kitchen_"), device_line
    assert admin_line.endswith(" android/41;contract=80 admin"), admin_line
    assert free_line.endswith(" - admin"), free_line
    for line in lines:
        assert device_token not in line and ADMIN not in line
        assert "free" not in line


def test_an_agent_and_a_peer_are_named_as_such(running_server, capfd):
    base, ctx, srv = running_server
    capfd.readouterr()
    _get(base, {"X-Clarp-Peer": "some-peer"})
    lines = _access_lines(capfd)
    assert lines and lines[-1].endswith(" - peer"), lines


def test_a_token_in_the_path_is_redacted_also_when_the_path_is_malformed(running_server, capfd):
    """A token sent in the query, well formed or malformed (a second '?', or '&'
    with no '?'), never reaches the access line."""
    import http.client
    import urllib.parse
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token=ADMIN)
    port = urllib.parse.urlsplit(base).port
    paths = [f"/agents/snapshot?token={ADMIN}",                 # well formed
             f"/agents/snapshot?x=1?token={ADMIN}",             # a second '?'
             f"/agents/snapshot&token={ADMIN}",                 # '&' with no '?'
             f"/agents/snapshot&x=1&token={ADMIN}&y=2"]
    capfd.readouterr()
    for path in paths:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", path)
        connection.getresponse().read()
        connection.close()
    time.sleep(0.05)
    out = capfd.readouterr().out
    lines = [line for line in out.splitlines() if '"GET /agents/snapshot' in line]
    assert len(lines) == len(paths), out
    for line in lines:
        assert ADMIN not in line, line
        assert "token=REDACTED" in line, line
