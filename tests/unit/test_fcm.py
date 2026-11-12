"""Android push through FCM HTTP v1 (lib/fcm.py and its hook in lib/apns.py).

No network: httpx.Client is replaced by a fake that answers Google's token
endpoint and the FCM send endpoint from a script. The service account key is
a throwaway RSA key, so the real RS256 signing path runs and is verified
against the key's public half. Nothing here proves delivery to a real phone.
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from lib import apns, config, fcm

PROJECT = "clarp-test-project"
EMAIL = "fcm-sender@clarp-test-project.iam.gserviceaccount.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND = f"https://fcm.googleapis.com/v1/projects/{PROJECT}/messages:send"
ANDROID = "dGVzdC1mY20tdG9rZW4:APA91bH" + "x" * 140       # FCM-shaped: base64url plus ':'
ANDROID_2 = "cm90YXRlZC10b2tlbg:APA91bH" + "y" * 140
IPHONE = "ab" * 32


# --------------------------------------------------------------------------
# fixtures and fakes
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_fcm_state(monkeypatch):
    from lib import user_notifications
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    fcm._reset_state()
    apns.reset_jwt_cache()
    yield
    fcm._reset_state()


@pytest.fixture
def rsa_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return key, pem


def _service_account(tmp_path, pem, **override):
    data = {
        "type": "service_account", "project_id": PROJECT, "private_key_id": "kid123",
        "private_key": pem, "client_email": EMAIL, "client_id": "1",
        "token_uri": TOKEN_URI,
    }
    data.update(override)
    path = tmp_path / "fcm-service-account.json"
    path.write_text(json.dumps(data))
    return path


def _config(tmp_path, service_account=None, apns_mode="off", extra=""):
    cfgfile = tmp_path / "config.toml"
    text = f'[apns]\nmode = "{apns_mode}"\n'
    if service_account is not None:
        text += f'[fcm]\nservice_account = "{service_account}"\n'
    cfgfile.write_text(text + extra)
    config.reset_cache()
    return config.load(cfgfile)


@pytest.fixture
def fcm_host(tmp_path, rsa_key):
    """A Host with only Android push: APNs off, a valid service account."""
    path = _service_account(tmp_path, rsa_key[1])
    return _config(tmp_path, path)


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _fcm_error(status, code="", rpc="", message="err"):
    details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": code}] if code else []
    return _Resp(status, {"error": {"code": status, "message": message, "status": rpc,
                                    "details": details}})


class Google:
    """Fake Google: the OAuth token endpoint and FCM messages:send (and APNs,
    for mixed batches). Each send answer is taken from `sends` in order; the
    last one repeats."""

    def __init__(self, sends=None, token_answers=None):
        self.sends = list(sends or [_Resp(200, {"name": f"projects/{PROJECT}/messages/1"})])
        self.token_answers = list(token_answers or [])
        self.token_calls: list[dict] = []
        self.send_calls: list[dict] = []
        self.apns_calls: list[dict] = []
        self.issued = 0

    def client(self, *a, **k):
        return self

    def close(self):
        pass

    def post(self, url, headers=None, content=None, data=None, json=None):
        if url == TOKEN_URI:
            self.token_calls.append({"data": dict(data or {})})
            if self.token_answers:
                return self.token_answers.pop(0)
            self.issued += 1
            return _Resp(200, {"access_token": f"ya29.token-{self.issued}",
                               "expires_in": 3599, "token_type": "Bearer"})
        if url == SEND:
            self.send_calls.append({"headers": dict(headers or {}), "json": json})
            return self.sends.pop(0) if len(self.sends) > 1 else self.sends[0]
        if "/3/device/" in url:
            self.apns_calls.append({"url": url, "headers": headers, "content": content})
            return _Resp(200, headers={"apns-id": "apns-1"})
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def google(monkeypatch):
    import httpx
    fake = Google()
    monkeypatch.setattr(httpx, "Client", fake.client)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(fcm, "_sleep", waited.append)
    return waited


@pytest.fixture
def logs(monkeypatch):
    seen: list[tuple[str, str]] = []
    real = fcm.log

    def capture(event, detail=""):
        seen.append((event, detail))
        real(event, detail)
    monkeypatch.setattr(fcm, "log", capture)
    return seen


def _notification(**extra):
    base = {"push": True, "preview": "The build is green.", "persona": "Mike",
            "session": "mike", "agent_id": "a1", "notification_id": "pn-123",
            "source_message_id": "m1", "badge": True, "unread": True,
            "needs_response": False}
    base.update(extra)
    return base


def _register_android(token=ANDROID, owner="", **kw):
    apns.register_token(token, session="mike", platform="android", owner=owner, **kw)


def _rows():
    from lib import db
    return {r["token"]: dict(r) for r in db.conn().execute(
        "SELECT token, platform, provider, principal, disabled_at FROM device_tokens")}


# --------------------------------------------------------------------------
# configuration and capability
# --------------------------------------------------------------------------
def test_service_account_path_comes_from_the_fcm_section(fcm_host, tmp_path):
    assert fcm_host.fcm_service_account_file() == str(tmp_path / "fcm-service-account.json")
    assert fcm.configured(fcm_host) is True
    assert apns.push_enabled(fcm_host) is True          # APNs is off; FCM alone is a push path
    assert fcm.load_service_account(fcm_host.fcm_service_account_file()).project_id == PROJECT


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"type": "service_account"}),
                                     json.dumps({"type": "authorized_user", "project_id": "p",
                                                 "client_email": "e", "private_key": "k"})])
def test_a_missing_or_wrong_service_account_is_not_configured(tmp_path, content):
    path = tmp_path / "sa.json"
    if content is not None:
        path.write_text(content)
    cfg = _config(tmp_path, path)
    assert fcm.configured(cfg) is False
    with pytest.raises(fcm.ServiceAccountError):
        fcm.load_service_account(str(path))


def test_no_fcm_section_means_no_fcm(tmp_path):
    cfg = _config(tmp_path)
    assert cfg.fcm_service_account_file() == "" and fcm.configured(cfg) is False
    assert apns.push_enabled(cfg) is False


def test_push_fcm_is_advertised_only_with_a_service_account(tmp_path, rsa_key, monkeypatch):
    from lib import server_identity
    # build_server installs this probe; here it is installed by hand.
    monkeypatch.setattr(server_identity, "_fcm_probe",
                        lambda: apns.fcm_configured(config.load()))
    _config(tmp_path)
    info = server_identity.get_server_info()
    assert "push_fcm" not in info["capabilities"]["features"]
    assert "push_fcm" not in info["contract"]["features"]
    _config(tmp_path, _service_account(tmp_path, rsa_key[1]))
    info = server_identity.get_server_info()
    assert "push_fcm" in info["capabilities"]["features"]
    assert info["contract"]["features"]["push_fcm"] == 88 <= info["contract"]["host"]


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------
def test_android_registration_is_stored_with_the_fcm_provider():
    _register_android()
    row = _rows()[ANDROID]
    assert (row["platform"], row["provider"], row["disabled_at"]) == ("android", "fcm", None)
    assert [t["provider"] for t in apns.active_tokens()] == ["fcm"]


def test_ios_registration_is_unchanged_and_apns():
    apns.register_token(IPHONE, session="mike", owner="")
    assert _rows()[IPHONE]["provider"] == "apns"
    assert _rows()[IPHONE]["platform"] == "ios"


def test_re_registering_the_same_token_is_idempotent():
    _register_android(owner="device_a")
    _register_android(owner="device_a")
    rows = _rows()
    assert list(rows) == [ANDROID] and rows[ANDROID]["disabled_at"] is None
    assert rows[ANDROID]["principal"] == "device_a"


def test_a_rotated_token_replaces_the_devices_old_row(monkeypatch):
    from lib import agent_ring_live
    monkeypatch.setattr(agent_ring_live, "device_ok", lambda principal: True)
    _register_android(owner="device_a")
    apns.register_token(IPHONE, session="mike", owner="device_b")     # another phone, untouched
    _register_android(ANDROID_2, owner="device_a")
    rows = _rows()
    assert rows[ANDROID]["disabled_at"] is not None
    assert rows[ANDROID_2]["disabled_at"] is None
    assert rows[IPHONE]["disabled_at"] is None


def test_another_device_cannot_take_an_owned_android_token(monkeypatch):
    from lib import agent_ring_live
    monkeypatch.setattr(agent_ring_live, "device_ok", lambda principal: True)   # device_a is still paired
    _register_android(owner="device_a")
    with pytest.raises(apns.TokenOwnedElsewhere):
        _register_android(owner="device_b")


@pytest.mark.parametrize("token,provider", [
    ("short", ""), ("has spaces in it but is long enough", ""), (ANDROID, "gcm"),
])
def test_a_bad_android_registration_is_refused(token, provider):
    with pytest.raises(apns.InvalidDeviceToken):
        _register_android(token, provider=provider)
    assert _rows() == {}


# --------------------------------------------------------------------------
# OAuth: the signed JWT, the exchange, the cache
# --------------------------------------------------------------------------
def test_the_assertion_is_an_rs256_jwt_signed_with_the_service_account_key(fcm_host, rsa_key):
    import jwt
    sa = fcm.load_service_account(fcm_host.fcm_service_account_file())
    assertion = fcm.signed_assertion(sa, now=1_800_000_000)
    header = jwt.get_unverified_header(assertion)
    assert header["alg"] == "RS256" and header["kid"] == "kid123" and header["typ"] == "JWT"
    claims = jwt.decode(assertion, rsa_key[0].public_key(), algorithms=["RS256"],
                        audience=TOKEN_URI,
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iss": EMAIL, "scope": fcm.SCOPE, "aud": TOKEN_URI,
                      "iat": 1_800_000_000, "exp": 1_800_003_600}


def test_the_access_token_is_exchanged_once_and_cached(fcm_host, google, rsa_key):
    import jwt
    transport = fcm.Transport(fcm_host)
    payload = apns.turn_done_payload("Mike", "mike", "hi", notification_id="pn-1")
    assert transport.send(ANDROID, payload)[0] == 200
    assert transport.send(ANDROID, payload)[0] == 200
    assert len(google.token_calls) == 1
    form = google.token_calls[0]["data"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    jwt.decode(form["assertion"], rsa_key[0].public_key(), algorithms=["RS256"], audience=TOKEN_URI)
    assert [c["headers"]["authorization"] for c in google.send_calls] == ["Bearer ya29.token-1"] * 2


def test_the_access_token_is_refreshed_shortly_before_it_expires(fcm_host, google, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(fcm.time, "time", lambda: clock[0])
    transport = fcm.Transport(fcm_host)
    payload = apns.turn_done_payload("Mike", "mike", "hi", notification_id="pn-1")
    transport.send(ANDROID, payload)
    clock[0] += 3599 - fcm._TOKEN_MARGIN_SEC - 1          # still inside the margin: cached
    transport.send(ANDROID, payload)
    assert len(google.token_calls) == 1
    clock[0] += 2                                         # within 5 minutes of expiry: refreshed
    transport.send(ANDROID, payload)
    assert len(google.token_calls) == 2
    assert google.send_calls[-1]["headers"]["authorization"] == "Bearer ya29.token-2"


def test_a_401_from_fcm_mints_a_new_token_once(fcm_host, google, sleeps):
    google.sends = [_fcm_error(401, rpc="UNAUTHENTICATED"), _Resp(200, {"name": "n"})]
    status, _, name = fcm.Transport(fcm_host).send(
        ANDROID, apns.turn_done_payload("Mike", "mike", "hi"))
    assert (status, name) == (200, "n")
    assert len(google.token_calls) == 2 and sleeps == []


# --------------------------------------------------------------------------
# the message
# --------------------------------------------------------------------------
def test_a_finished_turn_is_a_data_only_high_priority_message(fcm_host, google):
    from lib import db
    _register_android()
    summary = apns.send_user_notification(_notification())
    assert summary == {"enabled": True, "sent": 1, "failed": 0, "disabled": 0}
    [call] = google.send_calls
    message = call["json"]["message"]
    assert set(message) == {"token", "data", "android"}          # no "notification": the app draws it
    assert message["token"] == ANDROID
    assert message["android"] == {"priority": "HIGH"}
    data = message["data"]
    assert all(isinstance(v, str) for v in data.values())
    assert data["kind"] == "user-notification"
    assert data["payload_version"] == "1"
    assert data["title"] == "Mike" and data["body"] == "The build is green."
    assert data["session"] == "mike" and data["persona"] == "Mike" and data["agent_id"] == "a1"
    assert data["notification_id"] == data["collapse_id"] == "pn-123"
    assert data["needs_response"] == "false"
    assert data["badge"] == "true" and data["unread"] == "true"
    assert data["interruption_level"] == "active" and data["thread_id"] == "mike"
    assert data["sent_at"].isdigit()
    assert "sound" not in data
    assert set(data) <= {"payload_version", "kind", "title", "body", "session", "persona",
                         "agent_id", "notification_id", "collapse_id", "server_instance_id",
                         "needs_response", "badge", "unread", "interruption_level",
                         "thread_id", "sent_at", "avatar_url", "avatar_custom"}
    row = db.conn().execute("SELECT last_push_at FROM device_tokens WHERE token=?", (ANDROID,)).fetchone()
    assert row[0] is not None


def test_a_decision_carries_its_deep_link_fields(fcm_host, google):
    _register_android()
    artifact = {"artifact_id": "art1", "session": "mike", "agent_name": "Mike",
                "agent_id": "a1", "title": "Deploy?",
                "decision": {"decision_id": "d1", "status": "pending", "question": "Ship it?",
                             "blocks_progress": True, "urgency": "time_sensitive",
                             "response_type": "text_input", "revision": 2,
                             "expires_at": 1_800_000_000_000}}
    assert apns.send_decision_created(artifact)["sent"] == 1
    data = google.send_calls[0]["json"]["message"]["data"]
    assert data["reason"] == "decision" and data["decision_id"] == "d1"
    assert data["artifact_id"] == "art1" and data["notification_id"] == "decision-d1"
    assert data["subtitle"] == "Deploy?" and data["body"] == "Ship it?"
    assert data["interruption_level"] == "time-sensitive" and data["sound"] == "default"
    assert data["category"] == "input-request" and data["response_type"] == "text_input"
    assert data["revision"] == "2" and data["expires_at"] == "1800000000000"
    assert data["needs_response"] == "true" and data["agent_id"] == "a1"


def test_a_ring_alert_reaches_android_with_its_ring_fields(fcm_host, google, monkeypatch):
    from lib import agent_rings
    monkeypatch.setattr(agent_rings, "settings", lambda: {"show_reason": False})
    _register_android()
    ring = {"ring_id": "rng_1", "state": "ringing", "session": "mike", "persona": "Mike",
            "agent_id": "a1", "reason": "Build broke", "expires_at": 1_800_000_000_000}
    assert apns.send_agent_ring(ring, "ring")["sent"] == 1
    data = google.send_calls[0]["json"]["message"]["data"]
    assert data["kind"] == "agent-ring" and data["ring_id"] == "rng_1"
    assert data["title"] == "Mike is calling" and data["collapse_id"] == "ring-rng_1"
    assert data["category"] == "agent-call" and data["agent_id"] == "a1"


def test_background_sync_is_a_normal_priority_collapsible_hint(tmp_path, rsa_key, google):
    _config(tmp_path, _service_account(tmp_path, rsa_key[1]),
            extra="")
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(cfgfile.read_text().replace('mode = "off"\n', 'mode = "off"\nbackground_sync = true\n'))
    config.reset_cache()
    config.load(cfgfile)
    apns._reset_background_budget()
    _register_android()
    assert apns.send_background_sync("mike", "a1")["sent"] == 1
    message = google.send_calls[0]["json"]["message"]
    assert message["android"] == {"priority": "NORMAL", "collapse_key": "sync-mike"}
    assert message["data"]["kind"] == "sync" and message["data"]["agent_id"] == "a1"


def test_a_long_preview_is_cut_to_fit_and_identifiers_are_kept():
    payload = apns.turn_done_payload("Mike", "s" * 200, "é\"\n" * 5000, notification_id="pn-" + "9" * 60,
                                     server_instance_id="srv_" + "1" * 40)
    data = fcm.data_payload(payload, {"agent_id": "a" * 300})
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= fcm.MAX_DATA_BYTES
    assert data["session"] == "s" * 200 and data["agent_id"] == "a" * 300
    assert data["notification_id"] == "pn-" + "9" * 60 and data["server_instance_id"] == "srv_" + "1" * 40
    assert data["body"].endswith("…") and data["body"].startswith("é\"\n")


def test_a_payload_whose_identifiers_alone_overflow_is_not_sent(fcm_host, google, logs):
    payload = apns.turn_done_payload("Mike", "s" * 5000, "hi")
    assert fcm.data_payload(payload) is None
    assert fcm.Transport(fcm_host).send(ANDROID, payload) == (0, "PayloadTooLarge", "")
    assert google.send_calls == [] and logs[0][0] == "fcmPayloadTooLarge"


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------
@pytest.mark.parametrize("answer", [_fcm_error(404, "UNREGISTERED", "NOT_FOUND"), _Resp(404, {}),
                                    _fcm_error(400, "UNREGISTERED", "INVALID_ARGUMENT")])
def test_an_unregistered_token_is_disabled(fcm_host, google, answer):
    _register_android()
    google.sends = [answer]
    summary = apns.send_user_notification(_notification())
    assert summary == {"enabled": True, "sent": 0, "failed": 0, "disabled": 1}
    assert _rows()[ANDROID]["disabled_at"] is not None
    assert apns.active_tokens() == []


@pytest.mark.parametrize("first", [_fcm_error(429, "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED"),
                                   _fcm_error(503, "UNAVAILABLE", "UNAVAILABLE"),
                                   _fcm_error(500, "INTERNAL", "INTERNAL")])
def test_quota_and_server_errors_retry_with_backoff(fcm_host, google, sleeps, first):
    google.sends = [first, first, _Resp(200, {"name": "n"})]
    assert fcm.Transport(fcm_host).send(ANDROID, apns.turn_done_payload("M", "s", "b"))[0] == 200
    assert len(google.send_calls) == 3 and sleeps == [1.0, 2.0]


def test_retry_after_is_honoured_and_capped(fcm_host, google, sleeps):
    slow = _fcm_error(429, "QUOTA_EXCEEDED")
    slow.headers = {"retry-after": "7"}
    huge = _fcm_error(429, "QUOTA_EXCEEDED")
    huge.headers = {"retry-after": "3600"}
    google.sends = [slow, huge, _Resp(200, {"name": "n"})]
    fcm.Transport(fcm_host).send(ANDROID, apns.turn_done_payload("M", "s", "b"))
    assert sleeps == [7.0, fcm._RETRY_AFTER_CAP_SEC]


def test_a_persistent_server_error_gives_up_and_counts_a_failure(fcm_host, google, sleeps, logs):
    _register_android()
    google.sends = [_fcm_error(503, "UNAVAILABLE", "UNAVAILABLE")]
    summary = apns.send_user_notification(_notification())
    assert summary == {"enabled": True, "sent": 0, "failed": 1, "disabled": 0}
    assert len(google.send_calls) == fcm._MAX_ATTEMPTS
    assert _rows()[ANDROID]["disabled_at"] is None                 # a server error is not a dead token
    assert [e for e, _ in logs].count("fcmSendReject") == 1


def test_a_network_error_is_retried_then_logged_by_the_batch(fcm_host, monkeypatch, sleeps):
    import httpx
    from lib import apns as apns_mod

    class Down(Google):
        def post(self, url, **kw):
            if url == SEND:
                self.send_calls.append(kw)
                raise httpx.ConnectError("no route")
            return super().post(url, **kw)
    down = Down()
    monkeypatch.setattr(httpx, "Client", down.client)
    failures = []
    monkeypatch.setattr(apns_mod, "log_exception",
                        lambda event, exc, detail="": failures.append((event, type(exc).__name__)))
    _register_android()
    assert apns.send_user_notification(_notification())["failed"] == 1
    assert len(down.send_calls) == fcm._MAX_ATTEMPTS and sleeps == [1.0, 2.0]
    assert failures == [("apnsSendFail", "ConnectError")]


def test_sender_id_mismatch_is_a_failure_not_an_auth_problem(fcm_host, google, logs):
    google.sends = [_fcm_error(403, "SENDER_ID_MISMATCH", "PERMISSION_DENIED")]
    assert fcm.Transport(fcm_host).send(ANDROID, apns.turn_done_payload("M", "s", "b")) == (
        403, "SENDER_ID_MISMATCH", "")
    assert "fcmAuthFail" not in [e for e, _ in logs]


def test_a_rejected_service_account_is_logged_once_and_never_raises(fcm_host, google, logs):
    google.token_answers = [_Resp(400, {"error": "invalid_grant",
                                        "error_description": "Invalid JWT Signature."})] * 3
    _register_android()
    for _ in range(3):
        summary = apns.send_user_notification(_notification())
        assert summary == {"enabled": True, "sent": 0, "failed": 1, "disabled": 0}
    auth = [d for e, d in logs if e == "fcmAuthFail"]
    assert len(auth) == 1 and "Invalid JWT Signature" in auth[0] and EMAIL in auth[0]
    assert google.send_calls == []
    assert _rows()[ANDROID]["disabled_at"] is None
    # Once Google accepts it again, the recovery is logged and pushes flow.
    assert apns.send_user_notification(_notification())["sent"] == 1
    assert "fcmAuthRecovered" in [e for e, _ in logs]


def test_permission_denied_on_send_is_an_auth_problem_logged_once(fcm_host, google, logs):
    google.sends = [_fcm_error(403, "", "PERMISSION_DENIED", "caller lacks cloudmessaging.messages.create")]
    transport = fcm.Transport(fcm_host)
    for _ in range(2):
        assert transport.send(ANDROID, apns.turn_done_payload("M", "s", "b"))[0] == 403
    assert [e for e, _ in logs].count("fcmAuthFail") == 1


def test_a_service_account_removed_after_registration_is_logged_once(tmp_path, rsa_key, google, logs):
    path = _service_account(tmp_path, rsa_key[1])
    _config(tmp_path, path, apns_mode="auto")       # APNs still reaches granted phones
    _register_android()
    path.unlink()
    for _ in range(2):
        assert apns.send_user_notification(_notification())["sent"] == 0
    auth = [d for e, d in logs if e == "fcmAuthFail"]
    assert len(auth) == 1 and "1 Android device(s) registered" in auth[0]
    assert google.send_calls == []


# --------------------------------------------------------------------------
# one decision, two transports
# --------------------------------------------------------------------------
def _mixed_host(tmp_path, rsa_key):
    """A Host with both paths: a direct APNs key and an FCM service account."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    p8 = tmp_path / "AuthKey_TEST.p8"
    p8.write_text(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode())
    _config(tmp_path, _service_account(tmp_path, rsa_key[1]), apns_mode="direct")
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(cfgfile.read_text().replace(
        'mode = "direct"\n',
        f'mode = "direct"\nkey_path = "{p8}"\nkey_id = "K"\nteam_id = "T"\n'))
    config.reset_cache()
    return config.load(cfgfile)


def test_a_mixed_batch_sends_apns_to_the_iphone_and_fcm_to_the_android(tmp_path, rsa_key, google):
    _mixed_host(tmp_path, rsa_key)
    apns.register_token(IPHONE, session="mike")
    _register_android()
    summary = apns.send_user_notification(_notification())
    assert summary == {"enabled": True, "sent": 2, "failed": 0, "disabled": 0}
    assert [c["url"].rsplit("/", 1)[-1] for c in google.apns_calls] == [IPHONE]
    assert [c["json"]["message"]["token"] for c in google.send_calls] == [ANDROID]
    # The iPhone's APNs payload is what it always was: no FCM-only keys.
    apns_payload = json.loads(google.apns_calls[0]["content"])
    assert "agent_id" not in apns_payload and "payload_version" not in apns_payload


def test_policy_suppression_applies_to_android_too(fcm_host, google, monkeypatch):
    from lib import desktop_presence
    _register_android()
    assert apns.send_user_notification(_notification(push=False))["sent"] == 0
    monkeypatch.setattr(desktop_presence, "active", lambda: True)
    assert apns.send_user_notification(_notification())["reason"] == "desktop-active"
    assert google.send_calls == []


def test_without_fcm_an_android_row_never_goes_to_apns(tmp_path, google):
    _config(tmp_path, apns_mode="auto")          # APNs (grants) on, no service account
    _register_android()
    apns.send_user_notification(_notification())
    assert google.apns_calls == [] and google.send_calls == []


def test_an_ios_row_never_goes_to_fcm_on_an_android_only_host(fcm_host, google):
    apns.register_token(IPHONE, session="mike")
    assert apns.send_user_notification(_notification())["sent"] == 0
    assert google.send_calls == [] and google.apns_calls == []


def test_post_devices_shape_is_documented():
    """The request and payload keys the Android app relies on are in the docs."""
    import pathlib
    doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "push-android.md").read_text()
    for needle in ('"platform": "android"', '"provider": "fcm"', "push_fcm", "service_account",
                   "payload_version", "notification_id", "collapse_id", "agent_id", "unread"):
        assert needle in doc, needle


def test_the_documented_token_form_is_what_fcm_tokens_look_like():
    assert fcm.valid_token(ANDROID)
    assert not fcm.valid_token("a b") and not fcm.valid_token("x" * 19)
    assert urllib.parse.quote(ANDROID, safe=":_-") == ANDROID


class _PushCfg:
    """Only what apns._deliverable asks a configuration."""

    def __init__(self, apns_on: bool, fcm_on: bool):
        self._apns_on, self._fcm_on = apns_on, fcm_on

    def apns_enabled(self) -> bool:
        return self._apns_on

    def fcm_service_account_file(self) -> str:
        return ""


@pytest.fixture(autouse=False)
def fake_fcm_switch(monkeypatch):
    """apns asks fcm_configured(cfg); a _PushCfg answers it directly."""
    monkeypatch.setattr(apns, "fcm_configured", lambda cfg: cfg._fcm_on)


def test_iphones_are_sent_before_android_phones_in_one_batch(fake_fcm_switch):
    # An Android send can wait on Google through its retries; it must not
    # hold up an iPhone's push that happens to sit after it in the table.
    rows = [
        {"token": "android-1", "provider": "fcm"},
        {"token": "iphone-1", "provider": "apns"},
        {"token": "android-2", "provider": "fcm"},
        {"token": "iphone-2"},  # a row from before the provider column
    ]
    order = [r["token"] for r in apns._deliverable(_PushCfg(True, True), rows)]
    assert order == ["iphone-1", "iphone-2", "android-1", "android-2"]


def test_android_rows_never_change_which_iphones_get_a_push(monkeypatch, fake_fcm_switch):
    rows = [
        {"token": "android-1", "provider": "fcm"},
        {"token": "iphone-1", "provider": "apns"},
    ]
    monkeypatch.setattr(fcm, "report_auth_problem", lambda *a, **k: None)
    # No usable service account: the Android row is dropped, the iPhone stays.
    assert [r["token"] for r in apns._deliverable(_PushCfg(True, False), rows)] == ["iphone-1"]
    # APNs off, FCM on: the reverse, and nothing raises.
    assert [r["token"] for r in apns._deliverable(_PushCfg(False, True), rows)] == ["android-1"]


# --------------------------------------------------------------------------
# review findings of 12 Nov (review-fcm.md)
# --------------------------------------------------------------------------
class _SlowGoogle(Google):
    """Google that holds every FCM send until released."""

    def __init__(self):
        super().__init__()
        import threading
        self.fcm_started = threading.Event()
        self.release = threading.Event()

    def post(self, url, headers=None, content=None, data=None, json=None):
        if url == SEND:
            self.fcm_started.set()
            assert self.release.wait(10), "the test never released Google"
        return super().post(url, headers=headers, content=content, data=data, json=json)


def test_a_slow_google_never_holds_the_same_agents_next_iphone_push(tmp_path, rsa_key, monkeypatch):
    """Finding 1: Android rows are sent outside the conversation's iPhone send lock."""
    import threading
    import time as clock
    import httpx
    slow = _SlowGoogle()
    monkeypatch.setattr(httpx, "Client", slow.client)
    _mixed_host(tmp_path, rsa_key)
    apns.register_token(IPHONE, session="mike")
    _register_android()
    first = threading.Thread(target=apns.send_user_notification,
                             args=(_notification(notification_id="pn-first"),), daemon=True)
    first.start()
    assert slow.fcm_started.wait(5)                        # batch 1 now waits on Google
    second = threading.Thread(target=apns.send_user_notification,
                              args=(_notification(notification_id="pn-second"),), daemon=True)
    second.start()
    deadline = clock.monotonic() + 5
    while len(slow.apns_calls) < 2 and clock.monotonic() < deadline:
        clock.sleep(0.01)
    # The second notification reached the iPhone while Google still held the first.
    assert len(slow.apns_calls) == 2 and not slow.release.is_set()
    ids = [json.loads(c["content"])["notification_id"] for c in slow.apns_calls]
    assert ids == ["pn-first", "pn-second"]
    slow.release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    # Android sends of one conversation stay in order among themselves.
    assert [c["json"]["message"]["data"]["notification_id"] for c in slow.send_calls] == [
        "pn-first", "pn-second"]


def test_an_unbound_iphone_row_is_never_turned_into_an_android_row():
    """Finding 4: the provider of an existing token never changes."""
    legacy = "Gx" * 40                                     # FCM-shaped (not hex), stored as an APNs row
    apns.register_token(legacy, session="mike", platform="mac")
    with pytest.raises(apns.InvalidDeviceToken):
        _register_android(legacy, owner="device_a")
    row = _rows()[legacy]
    assert (row["platform"], row["provider"], row["principal"], row["disabled_at"]) == (
        "mac", "apns", "", None)


def test_an_android_token_is_never_turned_into_an_apns_row():
    _register_android()
    with pytest.raises(apns.InvalidDeviceToken):
        apns.register_token(ANDROID, session="mike", platform="mac")
    assert _rows()[ANDROID]["provider"] == "fcm"


def test_rotation_retires_only_rows_of_the_same_provider():
    """Finding 5: a device's FCM rotation never disables its row of another provider."""
    apns.register_token(IPHONE, session="mike", platform="android", provider="apns", owner="device_a")
    _register_android(owner="device_a")
    rows = _rows()
    assert rows[IPHONE]["disabled_at"] is None and rows[ANDROID]["disabled_at"] is None
    _register_android(ANDROID_2, owner="device_a")
    rows = _rows()
    assert rows[ANDROID]["disabled_at"] is not None
    assert rows[IPHONE]["disabled_at"] is None and rows[ANDROID_2]["disabled_at"] is None


def test_config_does_not_import_the_push_side():
    """Finding 2: lib.config (core) never imports lib.fcm (a feature)."""
    import inspect
    source = inspect.getsource(config)
    assert "import fcm" not in source and "fcm_enabled" not in source and "push_enabled" not in source


def test_without_the_probe_push_fcm_is_never_advertised(fcm_host, monkeypatch):
    """A process that did not build the server (a script, clarp-admin) does not claim push_fcm."""
    from lib import server_identity
    monkeypatch.setattr(server_identity, "_fcm_probe", None)
    assert "push_fcm" not in server_identity.get_server_info()["capabilities"]["features"]


# --------------------------------------------------------------------------
# review of PR 274 (review-274.md): decisions on the real path, the documented
# 400 rule, a trickling Google, a bounded Android queue
# --------------------------------------------------------------------------
def _decision(decision_id: str) -> dict:
    return {"artifact_id": f"art-{decision_id}", "session": "mike", "agent_name": "Mike",
            "agent_id": "a1", "title": "Deploy?",
            "decision": {"decision_id": decision_id, "status": "pending", "question": "Ship it?"}}


def test_a_decisions_iphone_push_leaves_while_google_hangs(tmp_path, rsa_key, monkeypatch):
    """Two decisions of one agent: the second reaches the iPhone while Google still holds the first's
    Android send (send_decision_created on the conversation lock alone fails this)."""
    import threading
    import time as clock
    import httpx
    slow = _SlowGoogle()
    monkeypatch.setattr(httpx, "Client", slow.client)
    _mixed_host(tmp_path, rsa_key)
    apns.register_token(IPHONE, session="mike")
    _register_android()
    first = threading.Thread(target=apns.send_decision_created, args=(_decision("d1"),), daemon=True)
    first.start()
    assert slow.fcm_started.wait(5)
    second = threading.Thread(target=apns.send_decision_created, args=(_decision("d2"),), daemon=True)
    second.start()
    deadline = clock.monotonic() + 5
    while len(slow.apns_calls) < 2 and clock.monotonic() < deadline:
        clock.sleep(0.01)
    assert len(slow.apns_calls) == 2 and not slow.release.is_set()
    assert [json.loads(c["content"])["decision_id"] for c in slow.apns_calls] == ["d1", "d2"]
    slow.release.set()
    first.join(10)
    second.join(10)
    assert [c["json"]["message"]["data"]["decision_id"] for c in slow.send_calls] == ["d1", "d2"]


def test_the_groups_are_the_conversation_lock_then_the_android_lane():
    groups = apns._send_groups("mike", [{"token": "a", "provider": "fcm"}, {"token": "i"}], "pn-1")
    assert [[r["token"] for r in rows] for _, rows in groups] == [["i"], ["a"]]
    assert groups[0][0] is apns._send_lock("mike")
    assert isinstance(groups[1][0], apns._AndroidTurn)


def _bad_request(*fields, code="INVALID_ARGUMENT", status=400, message="Request contains an invalid argument."):
    details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}]
    if fields:
        details.append({"@type": "type.googleapis.com/google.rpc.BadRequest",
                        "fieldViolations": [{"field": f, "description": "Invalid value"} for f in fields]})
    return _Resp(status, {"error": {"code": status, "message": message, "status": "INVALID_ARGUMENT",
                                    "details": details}})


def test_a_400_naming_the_token_field_disables_the_token(fcm_host, google):
    """Google: INVALID_ARGUMENT is an invalid registration when its google.rpc.BadRequest names the token."""
    _register_android()
    google.sends = [_bad_request("message.token", message="The registration token is not a valid FCM registration token")]
    assert apns.send_user_notification(_notification())["disabled"] == 1
    assert _rows()[ANDROID]["disabled_at"] is not None


@pytest.mark.parametrize("answer", [
    # About the payload, even when its text mentions the registration token: the token is kept.
    _bad_request("message.data[0].value", message="Invalid value; the registration token is fine"),
    _bad_request("message.android.ttl"),
    # No field named at all: Google's docs make this a token problem only if the payload is valid,
    # which the Host cannot know, so the token is kept and the push counts as failed.
    _bad_request(),
    _Resp(400, {"error": {"code": 400, "message": "Request contains an invalid argument.",
                          "status": "INVALID_ARGUMENT"}}),
])
def test_any_other_400_keeps_the_token(fcm_host, google, sleeps, answer):
    _register_android()
    google.sends = [answer]
    assert apns.send_user_notification(_notification())["failed"] == 1
    assert _rows()[ANDROID]["disabled_at"] is None and sleeps == []


def test_the_token_field_disables_only_on_a_400(fcm_host, google, sleeps):
    """The status is part of the rule: the same body on a 429 is retried, never a dead token."""
    _register_android()
    google.sends = [_bad_request("message.token", status=429)]
    assert apns.send_user_notification(_notification())["failed"] == 1
    assert _rows()[ANDROID]["disabled_at"] is None
    assert len(google.send_calls) == fcm._MAX_ATTEMPTS


class _Trickle:
    """A local HTTP server that answers one byte at a time, never finishing: each read succeeds well
    inside the read timeout, so only a total deadline ends the request."""

    def __init__(self, gap=0.05):
        import socket
        import threading
        self.gap, self.requests, self.stop = gap, 0, threading.Event()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.url = f"http://127.0.0.1:{self.server.getsockname()[1]}/v1/send"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import threading
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.requests += 1
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
            while not self.stop.is_set():
                conn.sendall(b" ")
                self.stop.wait(self.gap)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.server.close()


def test_a_trickling_google_is_cut_at_the_attempt_deadline(monkeypatch):
    import time as clock
    import httpx
    trickle = _Trickle()
    try:
        monkeypatch.setattr(fcm, "_ATTEMPT_DEADLINE_SEC", 0.5)
        fcm._reset_client()
        client = fcm.http_client()
        started = clock.monotonic()
        with pytest.raises(httpx.TimeoutException):
            with fcm._attempt(fcm._ATTEMPT_DEADLINE_SEC):
                client.post(trickle.url, json={})
        assert clock.monotonic() - started < 2.0          # the read timeout alone is 10 s per read
    finally:
        trickle.close()
        fcm._reset_client()


def test_a_trickling_google_bounds_a_whole_android_send(fcm_host, monkeypatch, sleeps):
    """Three attempts, each cut at its deadline, then the batch logs a failure; the token is kept."""
    import time as clock
    trickle = _Trickle()
    try:
        monkeypatch.setattr(fcm, "_ATTEMPT_DEADLINE_SEC", 0.4)
        monkeypatch.setattr(fcm, "SEND_URL", trickle.url + "?project={project}")
        monkeypatch.setattr(fcm, "access_token", lambda sa, client: "ya29.test")
        fcm._reset_client()
        _register_android()
        started = clock.monotonic()
        assert apns.send_user_notification(_notification())["failed"] == 1
        assert clock.monotonic() - started < 3 * 0.4 + 2.0
        assert trickle.requests == fcm._MAX_ATTEMPTS and sleeps == [1.0, 2.0]
        assert _rows()[ANDROID]["disabled_at"] is None
    finally:
        trickle.close()
        fcm._reset_client()


def test_the_google_client_has_a_ten_second_timeout_and_a_deadline_backend(monkeypatch):
    """The per-read timeout and the per-attempt deadline that bound an Android send (finding 1)."""
    import httpx
    made = []

    class Recorder:
        def __init__(self, *a, **k):
            made.append(k)

        def close(self):
            pass
    monkeypatch.setattr(httpx, "Client", Recorder)
    fcm._reset_client()
    fcm.http_client()
    assert made[0]["timeout"] == 10.0
    backend = made[0]["transport"]._pool._network_backend
    assert type(backend).__name__ == "Backend" and hasattr(backend, "_inner")
    assert fcm._TIMEOUT_SEC == 10.0 and fcm._ATTEMPT_DEADLINE_SEC == 10.0 and fcm._MAX_ATTEMPTS == 3
    fcm._reset_client()


def test_a_slow_google_drops_the_oldest_waiting_android_alert_never_an_iphone_push(
        tmp_path, rsa_key, monkeypatch, logs):
    """One in flight, ANDROID_MAX_WAITING waiting: a newer notification drops the oldest waiting
    one (logged); every iPhone push leaves at once."""
    import threading
    import time as clock
    import httpx
    slow = _SlowGoogle()
    monkeypatch.setattr(httpx, "Client", slow.client)
    dropped = []
    real_log = apns.log
    monkeypatch.setattr(apns, "log", lambda event, detail="": (
        dropped.append(detail) if event == "apnsAndroidDropped" else None) or real_log(event, detail))
    _mixed_host(tmp_path, rsa_key)
    apns.register_token(IPHONE, session="mike")
    _register_android()
    ids = [f"pn-{n}" for n in range(1, 5)]                 # 1 in flight, 2 may wait, the 4th drops pn-2
    threads = []
    results = {}

    def send(notification_id):
        results[notification_id] = apns.send_user_notification(_notification(notification_id=notification_id))
    for n, notification_id in enumerate(ids):
        threads.append(threading.Thread(target=send, args=(notification_id,), daemon=True))
        threads[-1].start()
        if n == 0:
            assert slow.fcm_started.wait(5)
        deadline = clock.monotonic() + 5
        while len(slow.apns_calls) < n + 1 and clock.monotonic() < deadline:
            clock.sleep(0.01)
        lane = apns._android_lane("mike")
        while n and len(lane.waiting) + len(lane.dropped) < n and clock.monotonic() < deadline:
            clock.sleep(0.01)
    assert len(slow.apns_calls) == 4 and not slow.release.is_set()   # iPhones never waited
    slow.release.set()
    for thread in threads:
        thread.join(10)
    sent = [c["json"]["message"]["data"]["notification_id"] for c in slow.send_calls]
    assert sent[0] == "pn-1" and sorted(sent[1:]) == ["pn-3", "pn-4"]
    assert len(dropped) == 1 and "notification=pn-2" in dropped[0] and "queue-full" in dropped[0]
    assert results["pn-2"]["failed"] == 1 and results["pn-2"]["sent"] == 1     # its iPhone push went
