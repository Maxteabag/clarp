"""Ring delivery must check the phone's current pairing, even for an old registration."""
from types import SimpleNamespace

import pytest

from lib import agent_rings as rings, apns, config, db, device_pairing

REASON = "A private reason"
CALL = "aa" * 32
ALERT = "bb" * 32
MODES = ["classic", "agent_live_v1"]


def pair(scope="full"):
    code = device_pairing.issue(scope=scope)["code"]
    return device_pairing.exchange(code)["device_id"]


def register(owner, token=CALL):
    rings.register_voip_token(owner, token, supported_conversation_modes=MODES)


@pytest.fixture
def pushes(monkeypatch):
    cfg = SimpleNamespace(apns_direct_ready=lambda: True, apns_enabled=lambda: True,
                          apns_environment="sandbox", fcm_service_account_file=lambda: "")
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(apns, "_server_instance_id", lambda: "scratch")
    monkeypatch.setattr(apns, "_avatar_details", lambda *_: (None, False))
    monkeypatch.setattr(rings, "_push_live_fcm", lambda *_: None)
    calls, fail = [], set()

    class Transport:
        def send(self, token, env, payload, **options):
            calls.append((options.get("push_type", "alert"), token, payload))
            return (503, "Unavailable", "") if token in fail else (200, "", "synthetic")

    monkeypatch.setattr(apns, "_DirectTransport", lambda _: Transport())
    monkeypatch.setattr(apns, "_transport", lambda _: Transport())
    return calls, fail


def ring(live=False):
    row = {"ring_id": "rng_test", "state": "ringing", "session": "synthetic",
           "persona": "Tester", "reason": REASON}
    if live:
        row["conversation_mode"] = "agent_live_v1"
    return rings._announce(row, "ring", notice=False)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("show", [False, True])
def test_ring_delivers_once_per_eligible_token_and_respects_reason(pushes, live, show):
    calls, _ = pushes
    revoked = pair()
    device_pairing.revoke(revoked)
    register(revoked)  # stale row from an older release
    for owner, octet in [(pair("limited"), "11"), ("device_unknown", "22"),
                         ("admin", "33"), ("", "44")]:
        register(owner, octet * 32)
    downgraded = pair()
    register(downgraded, "55" * 32)
    db.conn().execute("UPDATE paired_devices SET scope='limited' WHERE device_id=?", (downgraded,))
    register(pair())
    register(pair())  # two active pairings for one physical phone
    rings.update_settings({"show_reason": show})
    assert ring(live)["voip"]["sent"] == 1
    assert [(kind, token) for kind, token, _ in calls] == [("voip", CALL)]
    assert ("reason" in calls[0][2]) is show
    if show:
        assert calls[0][2]["reason"] == REASON


def test_failed_eligible_call_falls_back_to_paired_phones_alert(pushes):
    calls, fail = pushes
    stale = pair()
    device_pairing.revoke(stale)
    register(stale, "cc" * 32)  # stand-in would accept this token if called
    owner = pair()
    register(owner)
    apns.register_token(ALERT, owner=owner)
    fail.add(CALL)
    result = ring()
    assert (result["voip"]["sent"], result["voip"]["failed"], result["sent"]) == (0, 1, 1)
    assert [(kind, token) for kind, token, _ in calls] == [("voip", CALL), ("alert", ALERT)]


def test_revoke_then_repair_replaces_call_registration(pushes):
    calls, _ = pushes
    owner = pair()
    register(owner)
    assert device_pairing.revoke(owner)
    assert rings.voip_tokens() == []
    assert ring()["voip"]["sent"] == 0 and calls == []
    replacement = pair()
    register(replacement)
    assert [row["principal"] for row in rings.voip_tokens()] == [replacement]
    assert ring()["voip"]["sent"] == 1
    assert [(kind, token) for kind, token, _ in calls] == [("voip", CALL)]


def test_revoke_cleanup_failure_is_logged_and_leftover_cannot_receive(pushes, monkeypatch):
    from lib import log as host_log
    from unittest.mock import Mock

    owner = pair()
    register(owner)
    failures = Mock()
    monkeypatch.setattr(host_log, "log_exception", failures)
    monkeypatch.setattr(rings, "register_voip_token", Mock(side_effect=RuntimeError("synthetic")))
    assert device_pairing.revoke(owner)
    assert [entry["principal"] for entry in rings.voip_tokens()] == [owner]
    assert any(call.args[0] == "devicePairingVoipForgetFail" for call in failures.call_args_list)
    assert ring()["voip"]["sent"] == 0 and pushes[0] == []
