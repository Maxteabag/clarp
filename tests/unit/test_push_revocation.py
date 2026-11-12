"""A revoked paired device gets no further pushes of any kind (FCM review finding 9).

Revoking disables its rows (tests/integration/test_push_tokens.py); this pins the send-time check
that also covers rows which outlived a revocation (an older release revoked without disabling) or a
device whose scope changed: iPhone and Android rows alike, alerts, decisions, background sync and
Live Activity updates. Unbound legacy rows cannot be attributed and keep their pushes.
"""
from __future__ import annotations

import pytest

from lib import apns, db

LIVE, REVOKED = "device_live", "device_revoked"


@pytest.fixture
def devices():
    now = db.now_ms()
    for device_id, revoked in ((LIVE, None), (REVOKED, now)):
        db.conn().execute(
            "INSERT INTO paired_devices (device_id, name, token_hash, scope, created_at, last_seen_at, revoked_at)"
            " VALUES (?,?,?,?,?,?,?)", (device_id, device_id, f"hash-{device_id}", "full", now, now, revoked))


class _Cfg:
    def apns_enabled(self):
        return True

    def fcm_service_account_file(self):
        return ""


def _row(token, owner, provider="apns"):
    return {"token": token, "owner": owner, "provider": provider}


def test_rows_of_a_revoked_owner_are_never_pushed(devices, monkeypatch):
    monkeypatch.setattr(apns, "fcm_configured", lambda cfg: True)
    rows = [_row("i-live", LIVE), _row("i-revoked", REVOKED), _row("i-legacy", ""),
            _row("a-revoked", REVOKED, "fcm"), _row("a-live", LIVE, "fcm"),
            _row("i-unknown", "device_never_paired")]
    assert [r["token"] for r in apns._deliverable(_Cfg(), rows)] == [
        "i-live", "i-legacy", "a-live"]


def test_active_tokens_carry_the_owner_the_check_needs(devices):
    apns.register_token("ab" * 32, session="s", owner=LIVE)
    apns.register_token("legacy-token", session="s", platform="mac")
    owners = {r["token"]: r["owner"] for r in apns.active_tokens()}
    assert owners == {"ab" * 32: LIVE, "legacy-token": ""}
    assert apns.owner_can_receive("") and apns.owner_can_receive(LIVE)
    assert not apns.owner_can_receive(REVOKED) and not apns.owner_can_receive("device_never_paired")


def test_revoking_through_device_pairing_disables_that_devices_rows_only(devices):
    from lib import device_pairing
    for token, owner in (("aa" * 32, LIVE), ("bb" * 32, "device_other")):
        apns.register_token(token, session="s", owner=owner)
    db.conn().execute("UPDATE paired_devices SET revoked_at = NULL WHERE device_id = ?", (REVOKED,))
    apns.register_token("cc" * 32, session="s", owner=REVOKED)
    assert device_pairing.revoke(REVOKED)
    rows = {r["token"]: r for r in db.conn().execute("SELECT token, principal, disabled_at FROM device_tokens")}
    assert rows["cc" * 32]["disabled_at"] is not None and rows["cc" * 32]["principal"] == REVOKED
    assert rows["aa" * 32]["disabled_at"] is None and rows["bb" * 32]["disabled_at"] is None


def test_live_activity_rows_record_their_device_and_stop_when_it_is_revoked(devices, monkeypatch):
    from lib import config, live_activity
    live_activity.register(token="11" * 32, activity_id="act-live", principal=LIVE)
    live_activity.register(token="22" * 32, activity_id="act-revoked", principal=REVOKED)
    live_activity.register(token="33" * 32, activity_id="act-legacy")
    assert {r["activity_id"]: r["principal"] for r in live_activity.tokens()} == {
        "act-live": LIVE, "act-revoked": REVOKED, "act-legacy": ""}
    offered = []
    monkeypatch.setattr(apns._LA_GATE, "offer", lambda rows, payload, priority: offered.extend(rows))
    monkeypatch.setattr(apns, "_flush_live_activity", lambda: {"enabled": True})
    monkeypatch.setattr(config.Config, "apns_enabled", lambda self: True)
    apns.send_live_activity({"aps": {}})
    assert sorted(r["activity_id"] for r in offered) == ["act-legacy", "act-live"]


def test_disable_tokens_needs_a_target_and_matches_case_variants(devices):
    with pytest.raises(ValueError):
        apns.disable_tokens()
    apns.register_token("AB" * 32, session="s", owner=LIVE)
    assert apns.disable_tokens(token="ab" * 32, only_owner="device_other") == 0
    assert apns.disable_tokens(token="Ab" * 32, only_owner=LIVE) == 1
    assert apns.disable_tokens(owner=LIVE) == 0
