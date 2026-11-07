"""HTTP-N5: a turn token whose lookup fails is unknown identity, not "no turn".

Handler._turn_caller logs the failure and raises TurnIdentityUnavailable, which
handle_one_request answers with a retryable 503 before any check can pass
open. Every call site is pinned: the self-delete refusal, ring and ring cancel,
the /send sender check and the controller-action actor. Requests without a turn
token never read the database and are unchanged. _calls_by (attribution only)
logs and keeps "".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, authenticated_terminal_server, fake_ctx,
    running_server, server_module)

OWNER = "home-terminal-owner-test"   # authenticated_terminal_server's admin token

TOKEN = "ct_" + "a" * 32
UNAVAILABLE = (503, {"error": "turn identity unavailable", "retryable": True})


def _call(base, method, path, body=None, turn=True, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if turn:
        headers["X-Clarp-Turn"] = TOKEN
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


@pytest.fixture
def lookup_fails(monkeypatch):
    """The provider_turns read raises, as a locked or failing database would."""
    from lib import turn_identity

    def broken(environ=None):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(turn_identity, "caller_agent_id", broken)


def test_self_delete_is_refused_not_allowed(running_server, lookup_fails, capfd):
    from lib import agents
    base, _ctx, _srv = running_server
    agent_id = agents.get_by_session("claude")["agent_id"]
    assert _call(base, "DELETE", f"/agents/{agent_id}") == UNAVAILABLE
    assert agents.get_by_session("claude") is not None, "the agent must still exist"
    assert "turnCallerLookupFail" in capfd.readouterr().err


@pytest.mark.parametrize("path, body", [
    ("/calls/ring", {"agent": "claude", "reason": "r"}),
    ("/calls/ring/cancel", {"ring_id": "r1"}),
])
def test_rings_are_refused(authenticated_terminal_server, lookup_fails, monkeypatch, path, body):
    from lib import agent_rings
    touched = []
    monkeypatch.setattr(agent_rings, "ring", lambda *a, **k: touched.append("ring"))
    monkeypatch.setattr(agent_rings, "cancel", lambda *a, **k: touched.append("cancel"))
    base, _ctx, _srv = authenticated_terminal_server
    assert _call(base, "POST", path, body, token=OWNER) == UNAVAILABLE
    assert touched == []


def test_send_sender_check_is_refused(running_server, lookup_fails, monkeypatch):
    from lib.turn_dispatch import TurnDispatchService
    submitted = []
    monkeypatch.setattr(TurnDispatchService, "submit", lambda self, command: submitted.append(command))
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/send", {"text": "hi", "session": "claude"}) == UNAVAILABLE
    assert submitted == []


def test_controller_action_actor_is_refused(running_server, lookup_fails):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/controller-actions/a1/invoke", {}) == UNAVAILABLE


def test_requests_without_a_turn_token_are_unchanged(authenticated_terminal_server, monkeypatch):
    """The real lookup: no token, no database read, the ordinary answer."""
    from lib import agent_rings
    seen = []
    monkeypatch.setattr(agent_rings, "cancel", lambda principal, ring_id, *a, **k: seen.append(
        (principal, ring_id)) or {"ok": True})
    base, _ctx, _srv = authenticated_terminal_server
    status, body = _call(base, "POST", "/calls/ring/cancel", {"ring_id": "r1"}, turn=False, token=OWNER)
    assert (status, body) == (200, {"ok": True}) and seen and seen[0][1] == "r1"


def test_calls_by_logs_and_falls_back_to_the_named_session(running_server, monkeypatch, capfd):
    from lib import turn_identity

    def broken(environ=None):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(turn_identity, "caller_agent_id", broken)
    handler = object.__new__(server_module.Handler)
    handler.headers = {"X-Clarp-Turn": TOKEN}
    assert handler._calls_by({"by": "claude"}) == "claude"
    assert "callsByLookupFail" in capfd.readouterr().err


def test_a_failure_after_the_response_started_logs_and_closes(running_server, lookup_fails, monkeypatch, capfd):
    """A future call site that asks after responding cannot turn into a 503:
    the response already sent stands, the failure is logged and the
    connection closes instead of being reused."""
    def answered_then_asked(self):
        self._json_ok({"ok": True})
        self._turn_caller()
    monkeypatch.setattr(server_module.Handler, "_handle_status", answered_then_asked)
    import http.client
    import urllib.parse
    base, _ctx, _srv = running_server
    connection = http.client.HTTPConnection("127.0.0.1", urllib.parse.urlsplit(base).port, timeout=5)
    connection.request("GET", "/status", headers={"X-Clarp-Turn": TOKEN})
    response = connection.getresponse()
    assert (response.status, json.loads(response.read())) == (200, {"ok": True})
    with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
        connection.request("GET", "/status")
        connection.getresponse()
    connection.close()
    assert "turnIdentityAfterResponse" in capfd.readouterr().err


def test_stop_is_refused_before_any_stop_effect(running_server, lookup_fails, monkeypatch):
    """A2 F1: an agent's Stop whose identity lookup fails must not run as the
    user's Stop (which withdraws the user's sends: fence_user_receipts)."""
    stops = []
    monkeypatch.setattr(server_module.Handler, "_stop_agent_session",
                        lambda self, *a, **k: stops.append((a, k)))
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/stop", {"session": "claude"}) == UNAVAILABLE
    assert stops == []


def test_repair_is_refused_before_any_repair(running_server, lookup_fails, monkeypatch):
    from lib.turn_dispatch import TurnDispatchService
    repairs = []
    monkeypatch.setattr(TurnDispatchService, "repair_slot", lambda self, *a, **k: repairs.append(k))
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/repair-slot", {"session": "claude", "reason": "stuck"}) == UNAVAILABLE
    assert repairs == []


def test_agent_create_lineage_check_is_refused(running_server, lookup_fails):
    """A2 F2: the bookkeeping-lineage check asks through _turn_caller too, so a
    failed lookup is the retryable 503, not the route's generic error."""
    from lib import agents
    before = len(agents.list_agents()) if hasattr(agents, "list_agents") else None
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/agents", {"persona": "Latecomer", "cwd": "/tmp"}) == UNAVAILABLE
    if before is not None:
        assert len(agents.list_agents()) == before
