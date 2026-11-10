"""Characterization of the group-call and Oracle handlers' branches the full
suite did not reach, as server.py serves them today: G0 for the S3
calls/oracle recipe entries (route-registry.md section 8). Library calls are
stubbed at their module only to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, _running_context_server, fake_ctx, server_module)

TOKEN = "calls-oracle-g0-admin"


def _call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


@pytest.fixture
def admin_server(fake_ctx):
    yield from _running_context_server(fake_ctx.with_(auth_token=TOKEN))


def _bare_handler(headers=None):
    handler = object.__new__(server_module.Handler)
    handler.headers = headers or {}
    return handler


# ---- group calls ----------------------------------------------------------

def test_calls_snapshot_refusal(admin_server, monkeypatch):
    from lib import group_calls

    def refuse(*_args, **_kwargs):
        raise group_calls.CallError(409, "no such call", epoch=4)
    monkeypatch.setattr(group_calls, "snapshot", refuse)
    base, _ctx, _srv = admin_server
    assert _call(base, "GET", "/calls") == (409, {"ok": False, "error": "no such call", "epoch": 4})


def test_turn_caller_and_calls_by_without_proof(monkeypatch):
    from lib import turn_identity

    def unprovable(_environ=None):
        raise RuntimeError("no turn record")
    monkeypatch.setattr(turn_identity, "caller_agent_id", unprovable)
    handler = _bare_handler({"X-Clarp-Turn": "t1"})
    # HTTP-N5: an unprovable turn is unknown identity, refused (503 upstream);
    # _calls_by is attribution only and stays unattributed.
    with pytest.raises(turn_identity.TurnIdentityUnavailable):
        handler._turn_caller()
    assert handler._calls_by({}) == ""


def test_calls_by_prefers_the_turns_agent(monkeypatch):
    from lib import identity, turn_identity
    monkeypatch.setattr(turn_identity, "caller_agent_id", lambda _environ=None: "a1")
    real = identity.lookup
    monkeypatch.setattr(identity, "lookup",
                        lambda key: {"agent_id": "a1", "session": "rachel"} if key == "a1" else real(key))
    assert _bare_handler({"X-Clarp-Turn": "t1"})._calls_by({"by": "claude"}) == "rachel"


# ---- Oracle calls ---------------------------------------------------------

def test_oracle_call_create_and_close(admin_server, monkeypatch):
    from lib import oracle_calls
    base, _ctx, _srv = admin_server
    for path in ("/oracle/calls", "/oracle/calls/close"):
        assert _call(base, "POST", path, raw=b"[]") == (400, {"error": "bad json"})
    monkeypatch.setattr(oracle_calls, "create_call", lambda **kwargs: {"sdp": "answer"})
    assert _call(base, "POST", "/oracle/calls", {"attempt_id": "a1", "sdp": "offer"}) == (
        200, {"sdp": "answer"})

    def upstream_down(**_kwargs):
        raise RuntimeError("upstream refused")
    monkeypatch.setattr(oracle_calls, "create_call", upstream_down)
    assert _call(base, "POST", "/oracle/calls", {"attempt_id": "a1", "sdp": "offer"}) == (
        502, {"error": "Oracle connection unavailable"})


# ---- Oracle delegations ---------------------------------------------------

def test_oracle_delegations_bad_limit_falls_back_to_50(admin_server, monkeypatch):
    from lib import oracle_delegations
    seen = []
    monkeypatch.setattr(oracle_delegations, "reconcile_orphans", lambda *, is_live: 0)
    monkeypatch.setattr(oracle_delegations, "undelivered",
                        lambda *, owner_principal, limit: seen.append(limit) or [])
    base, _ctx, _srv = admin_server
    assert _call(base, "GET", "/oracle/delegations?limit=x") == (200, {"delegations": []})
    assert seen == [50]


def test_oracle_delegation_create_failures(admin_server, monkeypatch):
    from lib import oracle_delegations
    base, _ctx, _srv = admin_server
    assert _call(base, "POST", "/oracle/delegations", raw=b"[]") == (400, {"error": "bad json"})

    def collide(**_kwargs):
        raise oracle_delegations.DelegationCollision("delegation d1 exists with other text")
    monkeypatch.setattr(oracle_delegations, "dispatch", collide)
    assert _call(base, "POST", "/oracle/delegations", {"delegation_id": "d1"}) == (
        409, {"error": "delegation d1 exists with other text"})

    class Busy(RuntimeError):
        status = 503

    def busy(**_kwargs):
        raise Busy("agent busy")
    monkeypatch.setattr(oracle_delegations, "dispatch", busy)
    assert _call(base, "POST", "/oracle/delegations", {"delegation_id": "d1"}) == (
        503, {"error": "agent busy"})


def test_oracle_delegation_ack(admin_server, monkeypatch):
    from lib import oracle_delegations
    base, _ctx, _srv = admin_server
    assert _call(base, "POST", "/oracle/delegations/ack", raw=b"[]") == (400, {"error": "bad json"})
    monkeypatch.setattr(oracle_delegations, "normalize_id", lambda value: value)
    monkeypatch.setattr(oracle_delegations, "acknowledge", lambda delegation_id, *, owner_principal: True)
    assert _call(base, "POST", "/oracle/delegations/ack", {"delegation_id": "d1"}) == (
        200, {"ok": True})


def test_oracle_delegation_cancel(admin_server, monkeypatch):
    from lib import oracle_delegations
    base, _ctx, _srv = admin_server
    post = lambda body=None, raw=None: _call(base, "POST", "/oracle/delegations/cancel", body, raw)  # noqa: E731
    assert post(raw=b"[]") == (400, {"error": "bad json"})

    def bad_session(session, *, stop, owner_principal):
        raise ValueError("bad session")
    monkeypatch.setattr(oracle_delegations, "cancel_for_session", bad_session)
    assert post({"session": "claude"}) == (400, {"error": "bad session"})

    monkeypatch.setattr(oracle_delegations, "normalize_id", lambda value: value)
    outcomes = iter([oracle_delegations.DelegationNotCancellable("already finished"), None,
                     {"delegation_id": "d1", "state": "cancelled"}])

    def cancel(delegation_id, *, owner_principal):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(oracle_delegations, "cancel", cancel)
    assert post({"delegation_id": "d1"}) == (409, {"error": "already finished"})
    assert post({"delegation_id": "d1"}) == (404, {"error": "delegation not found"})
    assert post({"delegation_id": "d1"}) == (
        200, {"delegation": {"delegation_id": "d1", "state": "cancelled"}})
