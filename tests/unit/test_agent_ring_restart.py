"""Joined Live calls belong to the Host process that consumed their ticket."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import agent_ring_live as live, agent_ring_live_socket as socket, agent_rings as rings
from tests.unit.test_agent_call_live import _call, _join_of, opted
from tests.unit.test_agent_ring_live import PHONE, _live_ring, _refused, sander
from tests.unit.test_agent_ring_live_controls import _close, _fallback
from tests.unit.test_agent_rings import _Handler, _server, env


def _grant(origin):
    if origin == "user":
        return _call()["live"]
    ring = _live_ring()["ring"]
    return rings.answer(PHONE, ring["ring_id"], conversation_mode=live.MODE_LIVE,
                        request_id="incoming", client_nonce="incoming-nonce")["live"]


@pytest.fixture
def boot(monkeypatch):
    """Run the real pre-listener recovery entry point with unrelated workers isolated."""
    from lib import interrupted_turns
    host = _server()
    monkeypatch.setattr(host, "broadcast_boot_version", lambda ctx: None)
    monkeypatch.setattr(host, "resume_persisted_agents", lambda ctx: None)
    monkeypatch.setattr(host, "_recover_queued_after_start", lambda ctx: None)
    monkeypatch.setattr(interrupted_turns, "recover_after_restart", lambda stream: 0)
    monkeypatch.setattr(rings, "bind", lambda ctx: None)
    monkeypatch.setattr(rings, "recover_openings", lambda: 0)

    def recover(*, separate=False, restart=True):
        ctx = SimpleNamespace(runtime_client=object() if separate else None)
        host._recover_after_start(ctx, None, restart_recovery=restart)
    return recover


@pytest.mark.parametrize("origin", ["user", "agent"])
@pytest.mark.parametrize("phase", ["connecting", "ready", "active"])
@pytest.mark.parametrize("separate", [False, True], ids=["embedded", "separate"])
def test_restart_frees_phone_and_preserves_answered_history(opted, boot, origin, phase, separate):
    grant = _grant(origin)
    socket._join(PHONE, _join_of(grant))
    with rings._lock:
        records = rings._load()
        joined = records[-1]
        assert joined["_live_phase"] == "connecting"
        if phase != "connecting":
            joined.update(_live_phase=phase, _live_opening_consumed=True)
            rings._save(records)
    public_before = rings.get(grant["ring_id"])

    def next_call():
        handler = _Handler({"agent": "a-sander", "request_id": "next", "client_nonce": "nonce-next"}, opted)
        handler._handle_calls_live()
        return handler.sent[0]

    boot(separate=separate, restart=False)
    assert next_call() == (409, {"ok": False, "error": "live_call_active"})
    events_before, pushes_before = list(opted.stream.events), list(opted.pushes)
    boot(separate=separate)
    assert opted.stream.events == events_before and opted.pushes == pushes_before
    code, reply = next_call()
    assert code == 200 and reply["live"]["call_id"] != grant["call_id"]
    assert rings.get(grant["ring_id"]) == public_before
    assert rings._load()[0]["_live_retired_reason"] == "host_restart"
    assert "_live_snapshot" not in rings._load()[0]
    with pytest.raises(socket.Refused):
        socket._join(PHONE, _join_of(grant))
    if origin == "agent":
        _refused(409, live.FALLBACK_NOT_ALLOWED, _fallback, grant)
    else:
        _refused(409, live.CALL_STALE, _call)
    assert _close(grant)["state"] == "retired"
    assert rings._load()[-1]["_live_phase"] == "answered_waiting_join"


def test_preopening_restart_retirement_cannot_launch_classic_after_a_new_call(opted):
    grant = _grant("agent")
    socket._join(PHONE, _join_of(grant))
    with rings._lock:
        rows = rings._load()
        assert live.retire(rows[-1], "host_restart")
        rings._save(rows)
    assert _call()["changed"]
    _refused(409, live.FALLBACK_NOT_ALLOWED, _fallback, grant)


def test_restart_saves_all_three_joined_phases_once_and_reports_only_count(opted, boot, monkeypatch, capfd):
    with rings._lock:
        rings._save([{"ring_id": str(i), "state": "answered", "_live_phase": phase}
                     for i, phase in enumerate(("active", "connecting", "ready", "retired", "classic"))])
    saved = Mock(wraps=rings._save)
    monkeypatch.setattr(rings, "_save", saved)
    boot()
    boot()
    rows = rings._load()
    assert [(r["_live_phase"], r.get("_live_retired_reason")) for r in rows] == (
        [("retired", "host_restart")] * 3 + [("retired", None), ("classic", None)])
    assert saved.call_count == 1
    lines = [line.split(" agentRingLiveRestartRecovery ", 1)[1] for line in capfd.readouterr().err.splitlines()
             if " agentRingLiveRestartRecovery " in line]
    assert lines == ["retired=3"]


def test_unanswered_agent_ring_survives_restart_and_can_still_be_answered(opted, boot):
    ring = _live_ring()["ring"]
    original = rings._load()
    boot()
    assert rings._load() == original
    assert rings.answer(PHONE, ring["ring_id"], conversation_mode=live.MODE_LIVE,
                        request_id="answer", client_nonce="owner")["changed"]


@pytest.mark.parametrize("expired", [False, True])
def test_unconsumed_ticket_keeps_its_original_deadline(opted, boot, expired):
    grant = _call()["live"]
    original = rings._load()
    if expired:
        opted.clock.advance(60)
    boot()
    assert rings._load() == original
    if expired:
        with pytest.raises(socket.Refused, match="live_join_expired"):
            socket._join(PHONE, _join_of(grant))
        assert _call(request_id="new", nonce="new")["changed"]
    else:
        assert socket._join(PHONE, _join_of(grant))["_live_phase"] == "connecting"
