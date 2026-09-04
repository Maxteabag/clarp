"""APNs push notifications: config, device-token store, sender, and the
state-watcher "your turn" hook. No network: httpx is faked; the ES256 JWT is
minted from a throwaway P-256 key so the real crypto path is still exercised.
"""
from __future__ import annotations

import pathlib

import pytest

from lib import apns, config


@pytest.fixture(autouse=True)
def _fast_notification_policy(monkeypatch):
    from lib import user_notifications
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _p256_key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    p = tmp_path / "AuthKey_TEST123.p8"
    p.write_text(pem)
    return p


def _apns_config(tmp_path):
    key = _p256_key(tmp_path)
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        "[apns]\n"
        f'key_path = "{key}"\n'
        'key_id = "ABC123KEYID"\n'
        'team_id = "TEAMID1234"\n'
        'bundle_id = "com.maxteabag.clarp"\n'
    )
    config.reset_cache_for_tests()
    return config.load(cfgfile)


class _FakeResp:
    def __init__(self, status, reason=None, apns_id=""):
        self.status_code = status
        self._reason = reason
        self.text = ""
        self.headers = {"apns-id": apns_id} if apns_id else {}

    def json(self):
        return {"reason": self._reason} if self._reason else {}


class _FakeClient:
    """Maps each POST to a response by the token in the URL path."""
    def __init__(self, by_token, calls):
        self._by_token = by_token
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, content=None):
        token = url.rsplit("/", 1)[-1]
        self._calls.append({"url": url, "token": token, "headers": headers,
                            "content": content})
        return self._by_token[token]


def _user_turn(origin: str = "user", *, updated_at: int | None = None,
               message_id: str = "u1") -> None:
    from lib import db
    ts = db.now_ms() if updated_at is None else updated_at
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, "
        "tools_json, updated_at, origin) VALUES (?,?,?,?,?,?,?,?)",
        (message_id, "a1", -1, "user", "prompt", "[]", ts, origin))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def test_apns_disabled_by_default():
    config.reset_cache_for_tests()
    cfg = config.load()
    assert cfg.apns_enabled() is False
    # With no key configured, a send is a cheap no-op (never raises).
    assert apns.send_turn_done("sess", "Mike") == {
        "enabled": False, "sent": 0, "failed": 0, "disabled": 0}


def test_apns_config_parses(tmp_path):
    cfg = _apns_config(tmp_path)
    assert cfg.apns_enabled() is True
    assert cfg.apns_key_id == "ABC123KEYID"
    assert cfg.apns_team_id == "TEAMID1234"
    assert cfg.apns_bundle_id == "com.maxteabag.clarp"
    assert cfg.apns_environment == "production"


def test_apns_legacy_config_directory_falls_back_to_current_key(tmp_path):
    current = tmp_path / "clarp"
    current.mkdir()
    key = current / "AuthKey_TEST123.p8"
    key.write_text("key")
    cfgfile = current / "config.toml"
    legacy_key = tmp_path / "claude-pwa" / key.name
    cfgfile.write_text(
        "[apns]\n"
        f'key_path = "{legacy_key}"\n'
        'key_id = "ABC123KEYID"\n'
        'team_id = "TEAMID1234"\n'
    )
    config.reset_cache_for_tests()

    cfg = config.load(cfgfile)

    assert cfg.apns_key_file() == str(key)
    assert cfg.apns_enabled() is True


def test_apns_legacy_fallback_anchors_relative_config_path(tmp_path, monkeypatch):
    current = tmp_path / "config" / "clarp"
    current.mkdir(parents=True)
    key = current / "AuthKey_TEST123.p8"
    key.write_text("key")
    legacy_key = current.parent / "claude-pwa" / key.name
    cfgfile = current / "config.toml"
    cfgfile.write_text(
        "[apns]\n"
        f'key_path = "{legacy_key}"\n'
        'key_id = "ABC123KEYID"\n'
        'team_id = "TEAMID1234"\n'
    )
    monkeypatch.chdir(tmp_path)
    config.reset_cache_for_tests()

    cfg = config.load(pathlib.Path("config/clarp/config.toml"))

    assert cfg.apns_key_file() == str(key)
    assert cfg.apns_enabled() is True


def test_apns_unrelated_legacy_named_directory_does_not_redirect(tmp_path):
    current = tmp_path / "config" / "clarp"
    current.mkdir(parents=True)
    key = current / "AuthKey_TEST123.p8"
    key.write_text("key")
    configured = tmp_path / "backup" / "claude-pwa" / key.name
    cfgfile = current / "config.toml"
    cfgfile.write_text(
        "[apns]\n"
        f'key_path = "{configured}"\n'
        'key_id = "ABC123KEYID"\n'
        'team_id = "TEAMID1234"\n'
    )
    config.reset_cache_for_tests()

    cfg = config.load(cfgfile)

    assert cfg.apns_key_file() == str(configured)
    assert cfg.apns_enabled() is False


def test_apns_missing_key_file_is_disabled(tmp_path):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        "[apns]\n"
        f'key_path = "{tmp_path / "missing.p8"}"\n'
        'key_id = "ABC123KEYID"\n'
        'team_id = "TEAMID1234"\n'
    )
    config.reset_cache_for_tests()

    assert config.load(cfgfile).apns_enabled() is False


# --------------------------------------------------------------------------
# device-token store
# --------------------------------------------------------------------------
def test_register_list_disable_token():
    apns.register_token(
        "abc123", session="mike", base_url="http://192.0.2.10:7682/")
    assert [t["token"] for t in apns.active_tokens()] == ["abc123"]
    assert apns.active_tokens()[0]["base_url"] == "http://192.0.2.10:7682"

    # Re-registering updates in place (one row), not a duplicate.
    apns.register_token("abc123", session="rachel")
    toks = apns.active_tokens()
    assert len(toks) == 1 and toks[0]["session"] == "rachel"
    assert toks[0]["base_url"] == "http://192.0.2.10:7682"

    apns.disable_token("abc123", "Unregistered")
    assert apns.active_tokens() == []

    # Re-registering a previously-disabled token re-enables it.
    apns.register_token("abc123")
    assert len(apns.active_tokens()) == 1


def test_device_avatar_origin_rejects_public_http_and_credentials():
    assert apns._device_base_url("http://192.0.2.10:7682/") == (
        "http://192.0.2.10:7682")
    assert apns._device_base_url("http://192.168.1.4:7682/") == (
        "http://192.168.1.4:7682")
    assert apns._device_base_url("http://example.com:7682") == ""
    assert apns._device_base_url("https://user:secret@example.com") == ""


def test_register_supersedes_prior_token_for_same_session_platform():
    apns.register_token("old-ios", session="mike", platform="ios")
    apns.register_token("other-session", session="rachel", platform="ios")
    apns.register_token("other-platform", session="mike", platform="mac")

    apns.register_token("new-ios", session="mike", platform="ios")

    tokens = {t["token"] for t in apns.active_tokens()}
    assert tokens == {"new-ios", "other-session", "other-platform"}


def test_register_rejects_empty():
    with pytest.raises(ValueError):
        apns.register_token("   ")


def test_turn_done_payload():
    p = apns.turn_done_payload("Mike", "mike", server_instance_id="server-a")
    assert p["aps"]["alert"]["title"] == "Mike"
    assert p["aps"]["thread-id"] == "mike"
    assert p["aps"]["content-available"] == 1
    assert p["session"] == "mike"
    assert p["persona"] == "Mike"
    assert p["kind"] == "user-notification"
    # Empty persona falls back to a friendly default.
    assert apns.turn_done_payload("", None)["aps"]["alert"]["title"] == "Clarp"
    assert p["server_instance_id"] == "server-a"
    custom = apns.turn_done_payload(
        "Nova", "nova", avatar_url="https://example/avatar",
        avatar_custom=True)
    assert custom["avatar_custom"] is True


# --------------------------------------------------------------------------
# sender
# --------------------------------------------------------------------------
def test_send_turn_done_delivers_and_prunes_dead_tokens(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    apns.reset_jwt_cache()
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now))
    _user_turn("user", updated_at=now + 50)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("m0", "a1", 0, "assistant", "Heartbeat check: no action needed.",
         "[]", now, "heartbeat"))
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("m1", "a1", 1, "assistant", "<speak>Done.</speak>", "[]", now + 100))
    apns.register_token("goodtoken", session="mike")
    apns.register_token("deadtoken", session="mike", platform="mac")

    calls: list = []
    by_token = {"goodtoken": _FakeResp(200),
                "deadtoken": _FakeResp(410, "Unregistered")}
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(by_token, calls))

    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 100)
    assert summary == {"enabled": True, "sent": 1, "failed": 0, "disabled": 1}
    # Both tokens were attempted; the dead one is now pruned.
    assert len(calls) == 2
    assert {"goodtoken", "deadtoken"} == {c["token"] for c in calls}
    assert {t["token"] for t in apns.active_tokens()} == {"goodtoken"}
    # Every request carried the right topic + a bearer auth header.
    collapse_ids = {c["headers"]["apns-collapse-id"] for c in calls}
    assert len(collapse_ids) == 1
    assert next(iter(collapse_ids)).startswith("pn-")
    for c in calls:
        assert c["headers"]["apns-topic"] == "com.maxteabag.clarp"
        assert c["headers"]["authorization"].startswith("bearer ")


def test_send_after_registration_dedup_hits_one_token(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    apns.reset_jwt_cache()
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now))
    _user_turn("user", updated_at=now + 50)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("m1", "a1", 1, "assistant", "<speak>Fresh spoken turn.</speak>",
         "[]", now + 100))
    apns.register_token("oldtoken", session="mike", platform="ios")
    apns.register_token("newtoken", session="mike", platform="ios")

    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"newtoken": _FakeResp(200)}, calls))

    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 100)

    assert summary == {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}
    assert [c["token"] for c in calls] == ["newtoken"]


def test_each_completed_turn_is_sent_with_its_own_collapse_id(tmp_path, monkeypatch):
    """A newer turn must not suppress an older legitimate notification."""
    from lib import db
    _apns_config(tmp_path)
    now = db.now_ms()
    apns.register_token("tok", session="mike")
    for suffix, done in (("old", now), ("new", now + 1)):
        db.conn().execute(
            """INSERT INTO user_notifications (
                   notification_id, agent_id, session, persona, done_ts,
                   notify, push, badge, unread, preview, reason,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 1, 1, 1, 1, ?, 'text-reply', ?, ?)""",
            (f"pn-{suffix}", "a1", "mike", "Mike", done,
             f"{suffix} reply", now, now),
        )

    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))
    old_summary = apns.send_user_notification({
        "notification_id": "pn-old", "agent_id": "a1", "session": "mike",
        "persona": "Mike", "source_message_id": "m-old", "preview": "old reply",
        "push": True,
    })
    new_summary = apns.send_user_notification({
        "notification_id": "pn-new", "agent_id": "a1", "session": "mike",
        "persona": "Mike", "source_message_id": "m-new", "preview": "new reply",
        "push": True,
    })

    assert old_summary["sent"] == 1
    assert new_summary["sent"] == 1
    assert [call["headers"]["apns-collapse-id"] for call in calls] == [
        "pn-old", "pn-new",
    ]


def test_send_result_carries_apns_correlation_fields(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    now = db.now_ms()
    apns.register_token("tok", session="mike")
    db.conn().execute(
        """INSERT INTO user_notifications (
               notification_id, agent_id, session, persona, done_ts,
               notify, push, badge, unread, preview, reason,
               created_at, updated_at
           ) VALUES ('pn-current', 'a1', 'mike', 'Mike', ?,
                     1, 1, 1, 1, 'current reply', 'text-reply', ?, ?)""",
        (now, now, now),
    )
    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(
        {"tok": _FakeResp(200, apns_id="apple-request-1")}, calls))

    apns.send_user_notification({
        "notification_id": "pn-current", "agent_id": "a1", "session": "mike",
        "persona": "Mike", "source_message_id": "m-current",
        "preview": "current reply", "push": True,
    })

    from lib import telemetry
    rows = telemetry.conn().execute(
        "SELECT detail FROM diagnostic_events WHERE event = 'apnsSendResult'"
    ).fetchall()
    assert any("notification=pn-current" in row["detail"] for row in rows)
    assert any("apns_id=apple-request-1" in row["detail"] for row in rows)


def test_send_turn_done_no_tokens(tmp_path):
    _apns_config(tmp_path)
    assert apns.send_turn_done("mike", "Mike") == {
        "enabled": True, "sent": 0, "failed": 0, "disabled": 0}


# --------------------------------------------------------------------------
# state-watcher hook: a 'done' row pushes only for the user-origin turns
# --------------------------------------------------------------------------
def test_watcher_delegates_done_notification_decision_to_policy(monkeypatch):
    from lib import user_notifications
    user_notifications.settings_store.set_text(
        "automation_special_treatment", "true")
    from lib import agents as agents_db
    from lib import message_store, state_watcher
    from lib.protocol import SSEType

    user_agent = agents_db.create_agent(
        persona="Mike", voice_id="v", cwd="/tmp", session="mike")
    worker_agent = agents_db.create_agent(
        persona="Arnold", voice_id="v", cwd="/tmp", session="arnold")
    sender = agents_db.create_agent(
        persona="Omar", voice_id="v", cwd="/tmp", session="omar")

    calls: list = []
    monkeypatch.setattr(apns, "on_user_notification",
                        lambda notification: calls.append(notification))

    class _Stream:
        def __init__(self):
            self.events = []

        def broadcast(self, *a, **k):
            self.events.append(a[0])

    stream = _Stream()
    w = state_watcher.StateLogWatcher(stream)
    w._last_id = 0
    message_store.record_user_message(
        agent_id=user_agent, backend_session_id="bs-user",
        client_msg_id="u1", text="the user asked", origin="user")
    db = __import__("lib.db", fromlist=["conn"])
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("spoken-user", user_agent, "bs-user", 1, "assistant",
         "<speak>user-facing reply.</speak>", "[]", now + 1, "user"))
    message_store.record_user_message(
        agent_id=worker_agent, backend_session_id="bs-agent",
        client_msg_id="u2", text="worker reported",
        origin="agent", sender_agent_id=sender)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("spoken-worker", worker_agent, "bs-agent", 1, "assistant",
         "<speak>Worker status.</speak>", "[]", now + 2, "agent"))
    agents_db.record_state(user_agent, "thinking", {"backend_session_id": "bs-user"})
    agents_db.record_state(user_agent, "done", {"backend_session_id": "bs-user"})
    agents_db.record_state(worker_agent, "done", {"backend_session_id": "bs-agent"})
    agents_db.record_state(worker_agent, "done", {"backend_session_id": "bs-hidden"})
    w._poll_once()

    assert len(calls) == 1
    assert calls[0]["session"] == "mike"
    assert calls[0]["preview"] == "user-facing reply."
    notification_events = [
        event for event in stream.events
        if event.get("type") == SSEType.USER_NOTIFICATION
    ]
    assert len(notification_events) == 1
    assert notification_events[0]["badge"] is True


# --------------------------------------------------------------------------
# message preview: the push body shows the agent's latest reply
# --------------------------------------------------------------------------
def test_preview_uses_latest_assistant_message(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    apns.reset_jwt_cache()
    c = db.conn()
    now = db.now_ms()
    c.execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now))
    _user_turn("user", updated_at=now + 1)
    # An older reply, then the latest — preview must pick the latest.
    c.execute("INSERT INTO messages (message_id, agent_id, seq, role, text, "
              "tools_json, updated_at) VALUES (?,?,?,?,?,?,?)",
              ("m1", "a1", 1, "assistant", "first reply", "[]", now))
    c.execute("INSERT INTO messages (message_id, agent_id, seq, role, text, "
              "tools_json, updated_at) VALUES (?,?,?,?,?,?,?)",
              ("m2", "a1", 2, "assistant", "<speak>All done, deployed it.</speak>",
               "[]", now + 1))
    apns.register_token("tok", session="mike")

    calls: list = []
    by_token = {"tok": _FakeResp(200)}
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(by_token, calls))

    apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 1)
    import json as _json
    payload = _json.loads(calls[0]["content"])
    assert payload["aps"]["alert"]["title"] == "Mike"
    # <speak> markers stripped, whitespace collapsed, latest message wins.
    assert payload["aps"]["alert"]["body"] == "All done, deployed it."


def test_no_push_when_no_spoken_message(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now))
    _user_turn("user", updated_at=now + 50)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("quiet-heartbeat", "a1", 1, "assistant",
         "Heartbeat check: no action needed.", "[]", now + 100, "heartbeat"))
    apns.register_token("tok", session="mike")
    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))
    monkeypatch.setattr(apns.time, "sleep", lambda *_: None)  # skip ingestion wait
    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 100)
    assert summary == {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}
    assert calls == []


def test_text_only_turn_after_spoken_turn_pushes_current_text(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", db.now_ms()))
    t_prev = db.now_ms()
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("spoken-prev", "a1", 1, "assistant", "<speak>Love it</speak>",
         "[]", t_prev))
    t_done = t_prev + 10_000
    _user_turn("user", updated_at=t_done, message_id="u-current")
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("plain-current", "a1", 2, "assistant", "Plain text follow-up",
         "[]", t_done + 50))
    apns.register_token("tok", session="mike")
    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))
    monkeypatch.setattr(apns.time, "sleep", lambda *_: None)

    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=t_done)

    assert summary == {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}
    import json as _json
    body = _json.loads(calls[0]["content"])["aps"]["alert"]["body"]
    assert body == "Plain text follow-up"


@pytest.mark.parametrize(
    "origin", ["automation", "agent", "schedule", "heartbeat", "dreaming"])
def test_non_user_origin_with_speak_does_not_push(tmp_path, monkeypatch, origin):
    from lib import db, user_notifications
    user_notifications.settings_store.set_text(
        "automation_special_treatment", "true")
    _apns_config(tmp_path)
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now))
    _user_turn(origin, updated_at=now + 50)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("spoken-current", "a1", 1, "assistant",
         "<speak>Worker said words.</speak>", "[]", now + 100, origin))
    apns.register_token("tok", session="mike")
    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))

    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 100)

    assert summary == {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}
    assert calls == []


def test_user_origin_with_speak_pushes_once(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    apns.reset_jwt_cache()
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now))
    _user_turn("user", updated_at=now + 50)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("spoken-current", "a1", 1, "assistant",
         "<speak>user-facing reply.</speak>", "[]", now + 100, "user"))
    apns.register_token("tok", session="mike")
    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))

    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 100)

    assert summary == {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}
    assert [c["token"] for c in calls] == ["tok"]


def test_muted_user_origin_with_speak_does_not_push(tmp_path, monkeypatch):
    from lib import db
    _apns_config(tmp_path)
    apns.reset_jwt_cache()
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at, muted)"
        " VALUES (?,?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", now, 1))
    _user_turn("user", updated_at=now + 50)
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at, origin)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("spoken-current", "a1", 1, "assistant",
         "Badge but do not push.", "[]", now + 100, "user"))
    apns.register_token("tok", session="mike")
    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))

    summary = apns.send_turn_done("mike", "Mike", "a1", done_ts=now + 100)
    row = db.conn().execute("SELECT * FROM user_notifications").fetchone()

    assert summary == {"enabled": True, "sent": 0, "failed": 0, "disabled": 0}
    assert calls == []
    assert row["notify"] == 1
    assert row["push"] == 0
    assert row["badge"] == 1
    assert row["unread"] == 1
    assert row["muted"] == 1
    assert row["reason"] == "text-reply-muted"


# --------------------------------------------------------------------------
# off-by-one regression: the push must preview THIS turn, not the previous one
# --------------------------------------------------------------------------
def _agent_a1():
    from lib import db
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", ("a1", "Mike", "v", "/tmp", "mike", db.now_ms()))


def _msg(mid, seq, text, updated_at):
    from lib import db
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, text, tools_json, updated_at)"
        " VALUES (?,?,?,?,?,?,?)", (mid, "a1", seq, "assistant", text, "[]", updated_at))


def test_freshness_floor_rejects_previous_turn_message():
    """Repro of the off-by-one: at DONE time only the PREVIOUS turn's reply is in
    the table (the new one is still ingesting). Without a floor the stale reply is
    returned; the freshness floor rejects it so the caller waits for the real one."""
    from lib import db
    _agent_a1()
    t_prev = db.now_ms()
    _msg("m1", 1, "previous reply", t_prev)
    t_done = t_prev + 10_000
    floor = t_done - apns._SETTLE_MARGIN_MS
    # Bug shape: a naive latest-message read grabs the stale previous reply.
    assert apns._latest_assistant_text("a1") == "previous reply"
    # Fix: this turn's floor rejects it (→ caller waits instead of previewing it).
    assert apns._latest_assistant_text("a1", not_before=floor) is None
    # Once THIS turn's reply lands, it's the one selected.
    _msg("m2", 2, "this turn reply", t_done + 50)
    assert apns._latest_assistant_text("a1", not_before=floor) == "this turn reply"


def test_send_turn_done_waits_for_this_turns_message(tmp_path, monkeypatch):
    """End-to-end: the push body waits for THIS turn's reply to ingest (it lands
    during the settle wait, mimicking the transcript streamer racing DONE) rather
    than sending the previous turn's reply."""
    from lib import db
    _apns_config(tmp_path)
    apns.reset_jwt_cache()
    from lib import user_notifications
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 1)
    _agent_a1()
    t_prev = db.now_ms()
    _msg("m1", 1, "previous reply", t_prev)
    apns.register_token("tok", session="mike")
    t_done = t_prev + 10_000
    _user_turn("user", updated_at=t_done, message_id="u-current")

    # The new message lands on the first poll-sleep — the ingest race, simulated.
    def _land(_):
        if not getattr(_land, "fired", False):
            _land.fired = True
            _msg("m2", 2, "<speak>fresh reply landed</speak>", t_done + 10)
    monkeypatch.setattr(user_notifications.time, "sleep", _land)

    calls: list = []
    import httpx
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient({"tok": _FakeResp(200)}, calls))
    apns.send_turn_done("mike", "Mike", "a1", done_ts=t_done)
    import json as _json
    body = _json.loads(calls[0]["content"])["aps"]["alert"]["body"]
    assert body == "fresh reply landed"   # not "previous reply"


# --------------------------------------------------------------------------
# avatar url for the notification service extension
# --------------------------------------------------------------------------
def test_avatar_url_uses_public_https_origin_only(tmp_path):
    """The notification extension has no tunnel of its own, so a plaintext
    bind-address URL was unreachable there and only burned its 30s budget.
    Emit an avatar URL only when a public HTTPS origin is configured."""
    from lib import apns, config
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text('[server]\nbind_addr = "192.0.2.10"\nport = 7682\n')
    config.reset_cache_for_tests()
    cfg = config.load(cfgfile)
    assert apns._avatar_url(cfg, "Mike") is None
    cfgfile.write_text('[server]\nbind_addr = "192.0.2.10"\nport = 7682\n'
                       'public_base_url = "https://computer.example.ts.net/"\n')
    config.reset_cache_for_tests()
    cfg = config.load(cfgfile)
    assert apns._avatar_url(cfg, "Mike") == \
        "https://computer.example.ts.net/static/avatars/mike.png"
    # No persona → no url.
    assert apns._avatar_url(cfg, "") is None


def test_avatar_url_none_when_loopback(tmp_path):
    from lib import apns, config
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text('[server]\nbind_addr = "127.0.0.1"\nport = 7682\n')
    config.reset_cache_for_tests()
    cfg = config.load(cfgfile)
    # Loopback isn't device-reachable → skip the avatar rather than send a bad URL.
    assert apns._avatar_url(cfg, "Mike") is None
