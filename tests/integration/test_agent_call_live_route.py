"""Issue #216 over HTTP: POST /calls/live with snapshot discovery for a user-started Live call.

A paired phone that runs Live starts a Live call with an opted-in agent and gets the existing Live answer
(ring + grant); a refusal keeps its fixed code; POST /calls (Normal) is untouched for every agent.
"""
from __future__ import annotations

import hashlib
import http.client
import json
from urllib.parse import urlsplit

import pytest

from lib import agent_ring_live as live
from lib import agent_rings
from tests.integration.test_server_di import _model_catalog_never_probes_installed_clis, fake_ctx, running_server  # noqa: F401 - fixtures
from tests.unit.test_agent_rings import _agent


def _req(base, token, method, path, body=None):
    target = urlsplit(base)
    client = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
    try:
        client.request(method, path, body=None if body is None else json.dumps(body),
                       headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        client.close()


@pytest.fixture
def live_host(running_server, tmp_path, monkeypatch):
    from lib import device_pairing
    base, ctx, srv = running_server
    srv.ctx = ctx.with_(auth_token="local-administrator")
    _agent("a-sander", "Sandra", "rk-sander-0001", voice="voice-s")
    _agent("a-theo", "Theo", "theo-97e5")
    folder = tmp_path / "agent-live" / "profiles" / "rk-sander"
    folder.mkdir(parents=True)
    texts = {"agents": ("AGENTS.md", "Sandra er driftsoperatør."), "brief": ("USER-CALL.md", "Brukeren ringer selv.")}
    pins = {}
    for key, (name, text) in texts.items():
        (folder / name).write_text(text, encoding="utf-8")
        pins[key] = {"file": name, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    (folder / "profile.json").write_text(json.dumps({
        "profile_id": "rk-sander", "version": "1", "agent_session": "rk-sander-0001", "display_name": "Sandra",
        "files": pins, "stories": {"user_call": ["agents", "brief"]},
        "user_call_default": "live", "user_call_opening": "Hei, det er Sandra."}), encoding="utf-8")
    monkeypatch.setattr(live, "ROOT", tmp_path / "agent-live")
    monkeypatch.setattr(live, "provider_problem", lambda: None)
    phone = device_pairing.exchange(device_pairing.issue()["code"])
    agent_rings.register_voip_token(phone["device_id"], "cd" * 32,
                                    supported_conversation_modes=["classic", "agent_live_v1"])
    return base, srv, phone


def test_a_phone_starts_a_live_call_with_an_opted_in_agent(live_host):
    base, _srv, phone = live_host
    status, body = _req(base, phone["token"], "POST", "/calls/live",
                        {"agent": "a-sander", "request_id": "req-216", "client_nonce": "n-216"})
    assert status == 200 and body["conversation_mode"] == "agent_live_v1" and body["ring"]["origin"] == "user_call"
    assert body["ring"]["state"] == "answered" and body["ring"]["answered_by"] == phone["device_id"]
    assert (body["live"]["model"], body["live"]["voice"]) == ("gpt-live-1", "marin")
    assert live.snapshot_of(body["ring"]["ring_id"])["event_context"]["kind"] == "user_call"
    again = _req(base, phone["token"], "POST", "/calls/live",
                 {"agent": "a-sander", "request_id": "req-216", "client_nonce": "n-216"})
    assert again[0] == 200 and again[1]["live"] == body["live"]          # the same call, no second one


def test_a_live_refusal_keeps_its_code_and_starts_nothing(live_host, monkeypatch):
    base, _srv, phone = live_host
    monkeypatch.setattr(live, "provider_problem", lambda: "no key")
    status, body = _req(base, phone["token"], "POST", "/calls/live",
                        {"agent": "a-sander", "request_id": "req-b", "client_nonce": "n-b"})
    assert (status, body.get("error")) == (503, "live_unavailable")
    status, body = _req(base, phone["token"], "POST", "/calls/live",
                        {"agent": "a-theo", "request_id": "req-c", "client_nonce": "n-c"})
    assert (status, body.get("error")) == (409, "live_context_unavailable")
    status, body = _req(base, "local-administrator", "POST", "/calls/live",
                        {"agent": "a-sander", "request_id": "req-d", "client_nonce": "n-d"})
    assert (status, body.get("error")) == (403, "live_auth")
    assert not any(r.get("conversation_mode") for r in agent_rings._load())


def test_post_calls_stays_the_normal_call_for_every_agent(live_host):
    base, _srv, phone = live_host
    for agent in ("rk-sander-0001", "theo-97e5"):
        status, body = _req(base, phone["token"], "POST", "/calls", {"agents": [agent], "request_id": "n-" + agent})
        assert body.get("error") not in ("live_call_rings_back", "live_context_unavailable")
    assert not any(r.get("conversation_mode") for r in agent_rings._load())


def test_snapshot_discovery_matches_raw_agent_and_advertised_feature_without_call(live_host, monkeypatch):
    base, _srv, phone = live_host
    monkeypatch.setattr(live, "provider_problem", lambda: "no key")
    status, body = _req(base, phone["token"], "GET", "/agents/snapshot")
    assert status == 200
    rows = {a["agent_id"]: a for a in body["agents"]}
    assert rows["a-sander"].get("call_mode") == "agent_live_v1"
    assert rows["a-theo"].get("call_mode") == "classic"
    status, info = _req(base, phone["token"], "GET", "/server-info")
    assert status == 200 and "agent_call_live" in info["capabilities"]["features"]
    assert not agent_rings._load()


@pytest.mark.parametrize("extra", [{"context": {}}, {"agent": ["a-sander"]}, {"client_nonce": ""},
                                   {"request_id": "x" * 129}, {"agent": "rk-sander-0001"}])
def test_invalid_intent_or_non_raw_id_persists_nothing(live_host, extra):
    base, _srv, phone = live_host
    body = {"agent": "a-sander", "request_id": "r-invalid", "client_nonce": "n"}
    body.update(extra)
    status, out = _req(base, phone["token"], "POST", "/calls/live", body)
    assert status in (400, 404) and out["ok"] is False
    assert not agent_rings._load()


def test_missing_field_is_refused_before_persistence(live_host):
    base, _srv, phone = live_host
    status, out = _req(base, phone["token"], "POST", "/calls/live", {"agent": "a-sander", "request_id": "r"})
    assert (status, out["error"]) == (400, "live_request_invalid")
    assert not agent_rings._load()


def test_another_phone_cannot_read_private_user_call_by_id_or_current(live_host):
    from lib import device_pairing
    base, _srv, owner = live_host
    other = device_pairing.exchange(device_pairing.issue()["code"])
    agent_rings.register_voip_token(other["device_id"], "ef" * 32,
                                    supported_conversation_modes=["classic", "agent_live_v1"])
    status, out = _req(base, owner["token"], "POST", "/calls/live",
                       {"agent": "a-sander", "request_id": "private", "client_nonce": "n"})
    assert status == 200
    path = "/calls/ring?ring_id=" + out["ring"]["ring_id"]
    status, read = _req(base, other["token"], "GET", path)
    assert status == 404 and read["error"] == "unknown call"
    status, read = _req(base, owner["token"], "GET", path)
    assert status == 200 and read["ring"]["ring_id"] == out["ring"]["ring_id"]
    for control in ("decline", "cancel"):
        status, read = _req(base, other["token"], "POST", "/calls/ring/" + control,
                            {"ring_id": out["ring"]["ring_id"]})
        assert status == 404 and "ring" not in read
    for phone in (owner, other):
        status, read = _req(base, phone["token"], "GET", "/calls/ring")
        assert status == 200 and read["ring"] is None
