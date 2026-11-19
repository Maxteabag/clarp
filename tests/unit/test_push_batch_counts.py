"""A push batch that raises counts the rows it never reached as failed (R105).

A batch whose transport could not even be built (or that raised on a row's payload) logged
apnsBatchFail and counted its rows neither sent nor failed. 0 sent and 0 failed must mean one thing
only, "nobody was meant to get it": the quota keeper settled such a notice for good, though a
transport that cannot be built is the likeliest way a relay is down. Rows already sent stay sent;
the exception is still logged."""
from __future__ import annotations

import pytest

from lib import apns
from tests.unit.test_apns import _apns_config, _FakeClient, _FakeResp

PHONES = ["a" * 64, "b" * 64]


@pytest.fixture
def logged(monkeypatch):
    seen = []
    real = apns.log_exception

    def log_exception(event, exc, *args, **kwargs):
        seen.append(event)
        return real(event, exc, *args, **kwargs)
    monkeypatch.setattr(apns, "log_exception", log_exception)
    return seen


@pytest.fixture
def phones(tmp_path, monkeypatch):
    import httpx
    _apns_config(tmp_path)
    for token in PHONES:
        apns.register_token(token)
    calls = []
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient({t: _FakeResp(200) for t in PHONES}, calls))
    return calls


def _unbuildable(monkeypatch):
    def transport(cfg):
        raise ConnectionError("relay unreachable")
    monkeypatch.setattr(apns, "_transport", transport)


NOTICE = {"session": "mike", "persona": "Mike", "preview": "codex: 20% quota remaining", "push": True,
          "notification_id": "n-1"}


def test_a_transport_that_cannot_be_built_fails_every_row_and_is_logged(phones, monkeypatch, logged):
    _unbuildable(monkeypatch)
    result = apns.send_user_notification(dict(NOTICE))
    assert (result["sent"], result["failed"], result["disabled"]) == (0, 2, 0)
    assert "apnsBatchFail" in logged


def test_a_batch_that_sent_one_row_and_then_raised_keeps_it_sent_and_fails_the_rest(phones, monkeypatch, logged):
    real = apns._avatar_details
    seen = []

    def avatar(*args, **kwargs):
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("planted on the second row")
        return real(*args, **kwargs)
    monkeypatch.setattr(apns, "_avatar_details", avatar)
    result = apns.send_user_notification(dict(NOTICE))
    assert (result["sent"], result["failed"]) == (1, 1) and len(phones) == 1
    assert "apnsBatchFail" in logged


def test_a_decisions_batch_that_cannot_be_built_fails_every_row(phones, monkeypatch, logged):
    _unbuildable(monkeypatch)
    result = apns.send_decision_created({
        "artifact_id": "art-1", "session": "mike", "persona": "Mike", "title": "Pick one", "summary": "Which?",
        "decision": {"decision_id": "d-1", "question": "Which?", "status": "pending", "revision": 1}})
    assert result["sent"] == 0 and result["failed"] == 2
    assert "apnsBatchFail" in logged


def test_a_rings_batch_that_cannot_be_built_fails_every_row(phones, monkeypatch, logged):
    _unbuildable(monkeypatch)
    result = apns.send_agent_ring({"ring_id": "r-1", "agent_id": "a1", "persona": "Mike", "session": "mike",
                                   "reason": "A question", "expires_at": 0}, "ring")
    assert result["sent"] == 0 and result["failed"] == 2
    assert "apnsRingBatchFail" in logged


def test_a_voip_batch_whose_direct_transport_cannot_be_built_fails_every_phone_it_would_ring(
        tmp_path, monkeypatch, logged):
    from lib import agent_ring_live
    _apns_config(tmp_path)
    monkeypatch.setattr(agent_ring_live, "device_ok", lambda principal: True)

    def direct(cfg):
        raise ConnectionError("Apple unreachable")
    monkeypatch.setattr(apns, "_DirectTransport", direct)
    tokens = [{"principal": "device_a", "token": PHONES[0]}, {"principal": "device_b", "token": PHONES[1]},
              {"principal": "device_b", "token": PHONES[1]},             # the same phone twice: rung once
              {"principal": "user_x", "token": "c" * 64}]                # not a device: never rung
    result = apns.send_agent_ring_voip({"ring_id": "r-2", "agent_id": "a1", "persona": "Mike",
                                        "reason": "A question", "expires_at": 0}, tokens)
    assert result == {"enabled": True, "sent": 0, "failed": 2}
    assert "apnsRingVoipBatchFail" in logged
