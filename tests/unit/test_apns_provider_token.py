"""AUT-10: the APNs provider token (JWT) was cached for about 50 minutes and
keyed by nothing, and a 403 InvalidProviderToken or ExpiredProviderToken was
an ordinary failure: after fixing a wrong team id or rotating the key without
a restart, every direct and VoIP push failed for up to 50 minutes."""
from __future__ import annotations

from types import SimpleNamespace

import httpx

from lib import apns
from tests.unit.test_apns import _apns_config, _FakeResp

TOKEN = "ab" * 32


def test_a_changed_team_or_key_mints_a_new_provider_token(tmp_path):
    cfg = _apns_config(tmp_path)
    apns.reset_jwt_cache()
    first = apns._auth_jwt(cfg)
    assert apns._auth_jwt(cfg) == first            # still cached
    fixed = SimpleNamespace(apns_team_id="FIXEDTEAM9", apns_key_id=cfg.apns_key_id,
                            apns_key_file=cfg.apns_key_file)
    assert apns._auth_jwt(fixed) != first


def test_a_rejected_provider_token_is_minted_again_and_the_send_retried(tmp_path, monkeypatch):
    cfg = _apns_config(tmp_path)
    apns.reset_jwt_cache()
    replies = [_FakeResp(403, "InvalidProviderToken"), _FakeResp(200, apns_id="x")]
    auths = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, content=None):
            auths.append(headers["authorization"])
            return replies.pop(0)

        def close(self):
            pass
    monkeypatch.setattr(httpx, "Client", Client)
    apns._reset_pooled_client()
    transport = apns._DirectTransport(cfg)
    assert transport.send(TOKEN, "production", {"aps": {}})[0] == 200
    assert len(auths) == 2 and auths[0] != auths[1]
    apns._reset_pooled_client()

# Reviewer C's controls on f0f61107: real httpx clients over MockTransport.
import os

import jwt
import pytest

from lib import config

_REAL_CLIENT = httpx.Client

@pytest.fixture
def real_clients(monkeypatch):
    apns._reset_pooled_client()
    made, requests = [], []
    responses = []
    def respond(request):
        requests.append(request)
        response = responses.pop(0)
        if callable(response):
            return response(request)
        return response
    def factory(*args, **kwargs):
        client = _REAL_CLIENT(transport=httpx.MockTransport(respond))
        made.append(client)
        return client
    monkeypatch.setattr(httpx, "Client", factory)
    yield made, requests, responses
    apns._reset_pooled_client()


def test_provider_token_retry_can_recover_a_closed_client_once(tmp_path, real_clients):
    made, requests, responses = real_clients
    cfg = _apns_config(tmp_path)
    def refuse_then_other_batch_resets(request):
        apns._reset_pooled_client(made[-1])
        return httpx.Response(403, json={"reason":"InvalidProviderToken"})
    responses.extend([refuse_then_other_batch_resets, httpx.Response(200)])
    transport = apns._DirectTransport(cfg)
    assert transport.send(TOKEN, "production", {"aps":{}})[0] == 200
    assert len(requests) == 2 and len(made) == 2


def test_voip_retries_a_rejected_provider_token(tmp_path, real_clients, monkeypatch):
    made, requests, responses = real_clients
    cfg = _apns_config(tmp_path)
    monkeypatch.setattr(config, "load", lambda:cfg)
    monkeypatch.setattr(apns, "_server_instance_id", lambda:"test-host")
    responses.extend([httpx.Response(403, json={"reason":"ExpiredProviderToken"}), httpx.Response(200)])
    result = apns.send_agent_ring_voip({"ring_id":"test-ring","session":"mike","expires_at":1234567890000}, [{"token":TOKEN,"environment":"production"}])
    assert result["sent"] == 1 and result["failed"] == 0
    assert len(requests) == 2
    assert all(request.headers["apns-push-type"] == "voip" for request in requests)
    assert all(request.headers["apns-expiration"] == "1234567890" for request in requests)


@pytest.mark.parametrize("field", ["key_id", "key_path", "mtime"])
def test_each_other_cache_identity_component_remints(tmp_path, monkeypatch, field):
    cfg = _apns_config(tmp_path)
    apns.reset_jwt_cache()
    original = jwt.encode
    calls = []
    def encode(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(jwt, "encode", encode)
    before = apns._auth_jwt(cfg)
    assert apns._auth_jwt(cfg) == before and len(calls) == 1
    path = cfg.apns_key_file()
    if field == "key_path":
        from pathlib import Path
        changed = tmp_path / "rotated.p8"
        changed.write_bytes(Path(path).read_bytes())
        path = str(changed)
    if field == "mtime":
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    replacement = SimpleNamespace(apns_team_id=cfg.apns_team_id, apns_key_id="ROTATEDID" if field == "key_id" else cfg.apns_key_id, apns_key_file=lambda:path)
    apns._auth_jwt(replacement)
    assert len(calls) == 2
