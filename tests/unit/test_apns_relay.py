"""APNs relay mode: pushes go through Clarp Audio Central with the Computer's
cav1 credential, so the Computer holds no .p8. HTTP is faked; the fake relay
enforces the same device-binding rule as the Worker."""
from __future__ import annotations

import json

import pytest

from lib import apns, config

CREDENTIAL = ("cav1.00000000-0000-4000-8000-000000000000.0123456789abcdef."
              + "s" * 64)
RELAY = "https://central.test"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from lib import desktop_presence, user_notifications
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    monkeypatch.setattr(desktop_presence, "active", lambda: False)
    apns._reset_relay_state()
    apns._reset_pooled_client()
    apns._reset_background_budget()
    apns.reset_jwt_cache()
    yield
    apns._reset_relay_state()
    apns._reset_pooled_client()


def _config(tmp_path, *, relay=True, direct=False, extra=""):
    text = extra
    if relay:
        text += f'[audio_central]\nurl = "{RELAY}/"\ncredential = "{CREDENTIAL}"\n'
    else:
        # No credential, but the same (fake) Audio Central for phone grants.
        text += f'[audio_central]\nurl = "{RELAY}"\n'
    if direct:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = tmp_path / "AuthKey_TEST.p8"
        key.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        text += f'[apns]\nkey_path = "{key}"\nkey_id = "K"\nteam_id = "T"\n'
    path = tmp_path / "config.toml"
    path.write_text(text)
    config.reset_cache()
    return config.load(path)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)
        self.headers = {}

    def json(self):
        return self._body


class _FakeRelay:
    """Speaks the Worker's contract; also answers direct APNs posts."""

    def __init__(self, *, apple_status=200, apple_reason="", http_status=None,
                 raise_error=None, apns_unreachable=False):
        self.bound: dict[str, str] = {}
        self.requests: list[dict] = []
        self.apple_direct: list[dict] = []
        self.apple_status = apple_status
        self.apple_reason = apple_reason
        self.http_status = http_status
        self.raise_error = raise_error
        self.apns_unreachable = apns_unreachable
        self.grants: dict[str, str] = {}

    def client(self, *a, **k):
        return self

    def close(self):
        pass

    # relay (HTTP/1.1 JSON)
    def request(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})
        if self.raise_error:
            raise self.raise_error
        if self.http_status:
            return _Resp(self.http_status, {"error": {"code": "push_unconfigured"}})
        bearer = headers.get("authorization", "").removeprefix("Bearer ")
        if bearer.startswith("pg1."):
            state = self.grants.get(bearer)
            if url != f"{RELAY}/v1/push/send" or state is None or state == "revoked":
                return _Resp(401, {"error": {"code": "invalid_push_grant"}})
            if state == "no-token":
                return _Resp(409, {"error": {"code": "device_token_missing"}})
            return _Resp(200, {"results": [
                {"status": self.apple_status, "reason": self.apple_reason, "apnsId": "apns-g"}
                for _ in json["notifications"]]})
        if headers.get("authorization") != f"Bearer {CREDENTIAL}":
            return _Resp(401, {"error": {"code": "invalid_computer_credential"}})
        if url == f"{RELAY}/v1/push/devices" and method == "POST":
            for device in json["devices"]:
                self.bound[device["deviceToken"]] = device["environment"]
            return _Resp(200, {"registered": len(json["devices"])})
        if url == f"{RELAY}/v1/push/send":
            results = []
            for item in json["notifications"]:
                if item["deviceToken"] not in self.bound:
                    results.append({"deviceToken": item["deviceToken"], "status": 403,
                                    "reason": "DeviceNotRegistered", "apnsId": ""})
                    continue
                if self.apns_unreachable:
                    results.append({"deviceToken": item["deviceToken"], "status": 502,
                                    "reason": "RelayDeliveryFailed", "apnsId": ""})
                    continue
                if self.apple_status == 410:
                    self.bound.pop(item["deviceToken"], None)
                results.append({"deviceToken": item["deviceToken"],
                                "status": self.apple_status,
                                "reason": self.apple_reason, "apnsId": "apns-9"})
            return _Resp(200, {"results": results})
        return _Resp(404, {"error": {"code": "not_found"}})

    # direct APNs (HTTP/2 via _send_one)
    def post(self, url, headers=None, content=None):
        self.apple_direct.append({"url": url, "headers": headers})
        return _Resp(200)


def _install(monkeypatch, relay: _FakeRelay):
    import httpx
    monkeypatch.setattr(httpx, "Client", relay.client)


def _notification(**extra):
    return {"push": True, "preview": "Finished the migration", "persona": "Mike",
            "session": "mike", "notification_id": "n-1", **extra}


def _sends(relay):
    return [r for r in relay.requests if r["url"].endswith("/v1/push/send")]


def test_transport_selection(tmp_path):
    assert _config(tmp_path, relay=True).apns_transport() == "relay"
    assert _config(tmp_path, relay=True, direct=True).apns_transport() == "relay"
    assert _config(tmp_path, relay=False, direct=True).apns_transport() == "direct"
    assert _config(tmp_path, relay=False).apns_transport() == "grant"
    off = tmp_path / "off.toml"
    off.write_text(f'[apns]\nmode = "off"\n[audio_central]\ncredential = "{CREDENTIAL}"\n')
    config.reset_cache()
    assert config.load(off).apns_transport() == ""
    assert config.load(off).apns_enabled() is False


def test_direct_mode_wins_when_forced(tmp_path):
    path = tmp_path / "c.toml"
    key = tmp_path / "k.p8"
    key.write_text("key")
    path.write_text(f'[apns]\nmode = "direct"\nkey_path = "{key}"\nkey_id = "K"\nteam_id = "T"\n'
                    f'[audio_central]\ncredential = "{CREDENTIAL}"\n')
    config.reset_cache()
    assert config.load(path).apns_transport() == "direct"


def test_malformed_credential_or_plain_http_url_does_not_select_relay(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[audio_central]\ncredential = "cav1.not-enough"\n')
    config.reset_cache()
    # A malformed credential is ignored; granted phones stay reachable.
    assert config.load(path).apns_transport() == "grant"
    path.write_text(f'[audio_central]\nurl = "http://central.test"\ncredential = "{CREDENTIAL}"\n')
    config.reset_cache()
    assert config.load(path).apns_transport() == ""


def test_alert_push_registers_then_relays_without_a_local_key(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", environment="sandbox")

    summary = apns.send_user_notification(_notification())

    assert summary == {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}
    assert relay.bound == {"tok-a": "sandbox"}
    assert relay.apple_direct == []
    [send] = _sends(relay)
    item = send["json"]["notifications"][0]
    assert item["deviceToken"] == "tok-a"
    assert item["pushType"] == "alert" and item["priority"] == "10"
    assert item["collapseId"] == "n-1"
    assert item["payload"]["aps"]["alert"] == {"title": "Mike", "body": "Finished the migration"}
    assert item["payload"]["session"] == "mike"


def test_each_token_is_registered_once_per_process(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    apns.send_user_notification(_notification())
    apns.send_user_notification(_notification(notification_id="n-2"))

    registrations = [r for r in relay.requests if r["url"].endswith("/v1/push/devices")]
    assert len(registrations) == 1
    assert len(_sends(relay)) == 2


def test_background_sync_relays_as_background_priority_5(tmp_path, monkeypatch):
    _config(tmp_path, extra="[apns]\nbackground_sync = true\n")
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_background_sync("mike", "a1")

    assert summary["sent"] == 1
    item = _sends(relay)[0]["json"]["notifications"][0]
    assert item["pushType"] == "background"
    assert item["priority"] == "5"
    assert item["collapseId"] == "sync-mike"
    assert item["payload"] == {"aps": {"content-available": 1}, "kind": "sync",
                               "session": "mike", "agent_id": "a1"}


def test_decision_push_goes_through_the_relay(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_decision_created({
        "artifact_id": "art-1", "session": "mike", "agent_name": "Mike",
        "title": "Deploy?", "decision": {"decision_id": "d-1", "question": "Ship it?"},
    })

    assert summary["sent"] == 1
    item = _sends(relay)[0]["json"]["notifications"][0]
    assert item["payload"]["decision_id"] == "d-1"
    assert item["collapseId"] == "decision-d-1"


def test_dead_token_reported_by_apple_is_disabled(tmp_path, monkeypatch):
    from lib import db
    _config(tmp_path)
    relay = _FakeRelay(apple_status=410, apple_reason="Unregistered")
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_user_notification(_notification())

    assert summary["disabled"] == 1
    row = db.conn().execute(
        "SELECT disabled_at FROM device_tokens WHERE token = 'tok-a'").fetchone()
    assert row["disabled_at"] is not None


def test_lost_binding_is_re_registered_and_retried_once(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")
    apns.send_user_notification(_notification())
    relay.bound.clear()  # e.g. the Computer was revoked and reclaimed

    summary = apns.send_user_notification(_notification(notification_id="n-2"))

    assert summary["sent"] == 1
    assert relay.bound == {"tok-a": "production"}
    assert len(_sends(relay)) == 3


def test_relay_outage_falls_back_to_a_configured_direct_key(tmp_path, monkeypatch):
    _config(tmp_path, direct=True)
    relay = _FakeRelay(http_status=503)
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_user_notification(_notification())

    assert summary["sent"] == 1
    assert [c["url"] for c in relay.apple_direct] == [
        "https://api.push.apple.com/3/device/tok-a"]


def test_relay_outage_without_a_direct_key_fails_quietly(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay(raise_error=OSError("network down"))
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_user_notification(_notification())

    assert summary == {"enabled": True, "sent": 0, "failed": 1, "disabled": 0}
    assert relay.apple_direct == []


def test_forced_relay_mode_never_uses_the_local_key(tmp_path, monkeypatch):
    path = tmp_path / "c.toml"
    key = tmp_path / "k.p8"
    key.write_text("key")
    path.write_text(f'[apns]\nmode = "relay"\nkey_path = "{key}"\nkey_id = "K"\nteam_id = "T"\n'
                    f'[audio_central]\nurl = "{RELAY}"\ncredential = "{CREDENTIAL}"\n')
    config.reset_cache()
    config.load(path)
    relay = _FakeRelay(http_status=503)
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_user_notification(_notification())

    assert summary["failed"] == 1
    assert relay.apple_direct == []


def test_revoked_credential_counts_as_failed(tmp_path, monkeypatch):
    revoked = "cav1.00000000-0000-4000-8000-000000000000.0123456789abcdef." + "r" * 64
    path = tmp_path / "c.toml"
    path.write_text(f'[audio_central]\nurl = "{RELAY}"\ncredential = "{revoked}"\n')
    config.reset_cache()
    config.load(path)
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_user_notification(_notification())

    assert summary == {"enabled": True, "sent": 0, "failed": 1, "disabled": 0}
    assert _sends(relay) == []


def test_relay_never_logs_the_credential(tmp_path, monkeypatch):
    from lib import apns as module
    _config(tmp_path)
    relay = _FakeRelay(http_status=503)
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")
    lines: list[str] = []
    monkeypatch.setattr(module, "log", lambda *a, **k: lines.append(" ".join(map(str, a))))
    monkeypatch.setattr(module, "log_exception",
                        lambda *a, **k: lines.append(" ".join(map(str, a)) + str(k)))

    apns.send_user_notification(_notification())

    assert lines
    assert all("s" * 64 not in line for line in lines)


def test_relay_that_cannot_reach_apple_falls_back_to_direct(tmp_path, monkeypatch):
    _config(tmp_path, direct=True)
    relay = _FakeRelay(apns_unreachable=True)
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike")

    summary = apns.send_user_notification(_notification())

    assert summary["sent"] == 1
    assert len(relay.apple_direct) == 1


def test_one_relay_outage_is_not_retried_for_every_token(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay(raise_error=OSError("network down"))
    _install(monkeypatch, relay)
    from lib import db
    for token in ("tok-a", "tok-b", "tok-c"):
        apns.register_token(token, session=None)
    assert len(apns.active_tokens()) == 3 and db

    summary = apns.send_user_notification(_notification())

    assert summary["failed"] == 3
    assert len(relay.requests) == 1


# --------------------------------------------------------------------------
# phone-issued push grants
# --------------------------------------------------------------------------
GRANT = "pg1." + "1" * 64 + "." + "2" * 16 + "." + "3" * 64
GRANT_2 = "pg1." + "4" * 64 + "." + "5" * 16 + "." + "6" * 64


def test_grant_is_stored_kept_on_re_register_and_validated(tmp_path):
    _config(tmp_path, relay=False)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)
    apns.register_token("tok-a", session="mike")
    apns.register_token("tok-b", session="other", push_grant="pg1.not-a-grant")
    rows = {row["token"]: row["push_grant"] for row in apns.active_tokens()}
    assert rows == {"tok-a": GRANT, "tok-b": ""}


def test_grant_pushes_without_key_credential_or_token(tmp_path, monkeypatch):
    _config(tmp_path, relay=False)
    relay = _FakeRelay()
    relay.grants[GRANT] = "live"
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)
    apns.register_token("tok-old", session="other")

    summary = apns.send_user_notification(_notification())

    assert summary == {"enabled": True, "sent": 1, "failed": 1, "disabled": 0}
    [send] = _sends(relay)
    assert send["headers"]["authorization"] == f"Bearer {GRANT}"
    item = send["json"]["notifications"][0]
    assert "deviceToken" not in item
    assert item["payload"]["aps"]["alert"]["body"] == "Finished the migration"
    assert relay.bound == {} and relay.apple_direct == []


def test_grant_is_preferred_over_the_computer_credential(tmp_path, monkeypatch):
    _config(tmp_path)
    relay = _FakeRelay()
    relay.grants[GRANT] = "live"
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)
    apns.register_token("tok-b", session="other")

    summary = apns.send_user_notification(_notification())

    assert summary["sent"] == 2
    auths = sorted(r["headers"]["authorization"] for r in _sends(relay))
    assert auths == sorted([f"Bearer {GRANT}", f"Bearer {CREDENTIAL}"])
    assert list(relay.bound) == ["tok-b"]


def test_revoked_grant_disables_the_token(tmp_path, monkeypatch):
    from lib import db
    _config(tmp_path, relay=False)
    relay = _FakeRelay()
    relay.grants[GRANT] = "revoked"
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)

    summary = apns.send_user_notification(_notification())

    assert summary["disabled"] == 1
    row = db.conn().execute(
        "SELECT disabled_at FROM device_tokens WHERE token = 'tok-a'").fetchone()
    assert row["disabled_at"] is not None


def test_grant_whose_phone_lost_its_token_fails_without_disabling(tmp_path, monkeypatch):
    _config(tmp_path, relay=False)
    relay = _FakeRelay()
    relay.grants[GRANT] = "no-token"
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)

    summary = apns.send_user_notification(_notification())

    assert summary == {"enabled": True, "sent": 0, "failed": 1, "disabled": 0}
    assert [row["token"] for row in apns.active_tokens()] == ["tok-a"]


def test_grant_relay_outage_falls_back_to_a_local_key(tmp_path, monkeypatch):
    _config(tmp_path, relay=False, direct=True)
    relay = _FakeRelay(http_status=503)
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)
    apns.register_token("tok-b", session="other", push_grant=GRANT_2)

    summary = apns.send_user_notification(_notification())

    assert summary["sent"] == 2
    assert len(relay.requests) == 1  # one outage, then straight to the key
    assert sorted(c["url"].rsplit("/", 1)[-1] for c in relay.apple_direct) == ["tok-a", "tok-b"]


def test_background_sync_uses_the_grant(tmp_path, monkeypatch):
    _config(tmp_path, relay=False, extra="[apns]\nbackground_sync = true\n")
    relay = _FakeRelay()
    relay.grants[GRANT] = "live"
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)

    summary = apns.send_background_sync("mike", "a1")

    assert summary["sent"] == 1
    item = _sends(relay)[0]["json"]["notifications"][0]
    assert item["pushType"] == "background" and item["priority"] == "5"


def test_direct_mode_ignores_grants(tmp_path, monkeypatch):
    path = tmp_path / "c.toml"
    key = tmp_path / "k.p8"
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    path.write_text(f'[apns]\nmode = "direct"\nkey_path = "{key}"\nkey_id = "K"\nteam_id = "T"\n')
    config.reset_cache()
    config.load(path)
    relay = _FakeRelay()
    _install(monkeypatch, relay)
    apns.register_token("tok-a", session="mike", push_grant=GRANT)

    summary = apns.send_user_notification(_notification())

    assert summary["sent"] == 1
    assert relay.requests == [] and len(relay.apple_direct) == 1
