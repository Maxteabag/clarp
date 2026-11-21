"""POST /agent-moved, the snapshot's moved field and /send's refusal, through
the real server (lib.agent_moves)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _call(base, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read() or b"null"
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode(errors="replace")


def _turn_token(session):
    from lib import db
    token = os.urandom(12).hex()
    db.conn().execute(
        "INSERT INTO provider_turns (turn_token, agent_id, provider, pid, started_at) "
        "SELECT ?, agent_id, 'claude', ?, ? FROM agents WHERE session = ?",
        (token, os.getpid(), int(time.time() * 1000), session))
    return token


MOVE = {"session": "claude", "moved_to": {"host": "rust-host", "id": "r-1"}}


def _row(base, session="claude"):
    status, snapshot = _call(base, "GET", "/agents/snapshot")
    assert status == 200
    from lib import agents
    agent_id = agents.get_by_session(session)["agent_id"]
    return next(a for a in snapshot["agents"] if a["agent_id"] == agent_id)


def test_the_mark_is_set_answered_idempotent_shown_and_taken_back(running_server):
    base, _ctx, _srv = running_server
    assert _row(base)["moved"] is None
    status, body = _call(base, "POST", "/agent-moved", MOVE)
    assert status == 200 and body["changed"] is True
    assert body["moved"]["to"] == {"host": "rust-host", "id": "r-1"}
    status, body = _call(base, "POST", "/agent-moved", MOVE)
    assert status == 200 and body["changed"] is False
    assert _row(base)["moved"]["to"] == {"host": "rust-host", "id": "r-1"}
    status, body = _call(base, "POST", "/agent-moved",
                         {"session": "claude", "moved_to": {"host": "other", "id": "x"}})
    assert status == 409 and body["error"] == "already_moved"
    status, body = _call(base, "POST", "/agent-moved", {"session": "claude", "moved_to": None})
    assert status == 200 and body["changed"] is True and body["moved"] is None
    status, body = _call(base, "POST", "/agent-moved", {"session": "claude", "moved_to": None})
    assert status == 200 and body["changed"] is False
    assert _row(base)["moved"] is None


def test_agents_and_peers_may_not_set_or_take_it_back(running_server):
    base, _ctx, _srv = running_server
    for headers in ({"X-Clarp-Turn": _turn_token("rachel")}, {"X-Clarp-Peer": "some-peer"}):
        status, body = _call(base, "POST", "/agent-moved", MOVE, headers=headers)
        assert status == 403 and body["error"] == "agents_may_not"
    assert _row(base)["moved"] is None


def test_bad_requests(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/agent-moved", {"session": "claude"})[0] == 400
    assert _call(base, "POST", "/agent-moved", {"session": "nobody", "moved_to": None})[0] == 404
    assert _call(base, "POST", "/agent-moved",
                 {"session": "claude", "moved_to": {"host": "", "id": "x"}})[0] == 400
    assert _call(base, "POST", "/agent-moved", {"session": "claude", "moved_to": "x"})[0] == 400


def test_send_to_a_moved_agent_answers_agent_moved_with_where(running_server):
    base, _ctx, _srv = running_server
    assert _call(base, "POST", "/agent-moved", MOVE)[0] == 200
    status, body = _call(base, "POST", "/send",
                         {"text": "hello", "session": "claude", "client_msg_id": "c-moved"})
    assert status == 409
    assert body["error"] == "agent_moved"
    assert body["moved_to"] == {"host": "rust-host", "id": "r-1"} and body["moved_at"] > 0


def test_a_limited_device_may_not_and_a_full_device_may(running_server):
    from lib import device_pairing
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token="administrator-secret")

    def token(scope):
        issued = device_pairing.issue(device_name=f"{scope} phone", scope=scope)
        return device_pairing.exchange(issued["code"])["token"]

    status, _ = _call(base, "POST", "/agent-moved", MOVE,
                      headers={"Authorization": f"Bearer {token('limited')}"})
    assert status == 403
    assert _call(base, "POST", "/agent-moved", MOVE,
                 headers={"Authorization": "Bearer administrator-secret"})[0] == 200
    assert _call(base, "POST", "/agent-moved", {"session": "claude", "moved_to": None},
                 headers={"Authorization": f"Bearer {token('full')}"})[0] == 200


class _Runtime:
    """A runtime client as the Host holds one: its status names its capabilities."""

    def __init__(self, capabilities=None, *, fails=False):
        self.capabilities, self.fails, self.recovered = capabilities, fails, 0

    def status(self, **_kw):
        if self.fails:
            raise RuntimeError("runtime status unavailable")
        return {"capabilities": dict(self.capabilities or {})}

    def recover_queued(self):
        self.recovered += 1
        return 0


def _with_runtime(srv, ctx, runtime):
    srv.ctx = ctx.with_(runtime_client=runtime)


def test_a_runtime_that_does_not_enforce_moves_refuses_the_mark(running_server):
    """A runtime of a release before agent_moves would store nothing and refuse
    nothing: the Host sets no mark it cannot enforce."""
    base, ctx, srv = running_server
    for runtime in (_Runtime({"janitor_runs": True}), _Runtime(fails=True),
                    _Runtime({"agent_moves": "yes"})):
        _with_runtime(srv, ctx, runtime)
        status, body = _call(base, "POST", "/agent-moved", MOVE)
        assert status == 409 and body["error"] == "runtime_not_ready", body
        assert body["moved"] is None
    from lib import agent_moves, agents
    assert agent_moves.moved(agents.get_by_session("claude")["agent_id"]) is None


def test_a_ready_runtime_accepts_and_taking_back_is_always_allowed(running_server):
    base, ctx, srv = running_server
    ready = _Runtime({"agent_moves": True})
    _with_runtime(srv, ctx, ready)
    status, body = _call(base, "POST", "/agent-moved", MOVE)
    assert status == 200 and body["changed"] is True
    old = _Runtime({"janitor_runs": True})                 # a runtime rolled back meanwhile
    _with_runtime(srv, ctx, old)
    status, body = _call(base, "POST", "/agent-moved", {"session": "claude", "moved_to": None})
    assert status == 200 and body["changed"] is True and body["moved"] is None
    assert old.recovered == 1                              # the held items are released
