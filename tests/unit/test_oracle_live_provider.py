import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from lib import oracle_live_provider as provider
from lib.oracle_live_wire import LiveWire

SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=sendrecv\r\n"
CFG = SimpleNamespace(openai_key=lambda: "fixture-api")


def response(body, headers=None):
    value = io.BytesIO(body)
    value.headers = headers or {}
    return value


def test_api_creation_uses_json_session_identity_and_attaches_without_starting_again():
    requests, attaches = [], []
    socket = object()
    def open_http(request, **kwargs):
        requests.append(request)
        return response(json.dumps({"session": {"id": "live-owned"}, "transport": {"type": "webrtc", "sdp": SDP}}).encode())
    def attach(url, headers): attaches.append((url, headers)); return socket
    wire = LiveWire()
    result = provider.negotiate(SDP, wire=wire, session=wire.session("Wait."), cfg=CFG, open_http=open_http, attach=attach)
    assert requests[0].full_url == provider.API_SESSIONS
    assert json.loads(requests[0].data)["transport"] == {"type": "webrtc", "sdp": SDP}
    assert attaches[0][0] == "wss://api.openai.com/v1/live/sessions/live-owned/attach"
    assert result["socket"] is socket and result["session_id"] == "live-owned"


def test_subscription_uses_only_selected_credentials_and_private_endpoint(monkeypatch):
    requests = []
    monkeypatch.setattr(provider, "credentials", lambda mode, cfg: {"Authorization": "Bearer fixture-subscription", "chatgpt-account-id": "fixture-account"})
    def open_http(request, **kwargs):
        requests.append(request)
        return response(SDP.encode(), {"openai-session-id": "rtc-owned"})
    attached = []
    wire = LiveWire("subscription")
    provider.negotiate(SDP, wire=wire, session=wire.session("Wait."), cfg=CFG, open_http=open_http,
                       attach=lambda url, headers: attached.append(url) or object())
    assert requests[0].full_url == provider.SUBSCRIPTION_CALLS
    assert requests[0].get_header("Authorization") == "Bearer fixture-subscription"
    assert "transport" not in json.loads(requests[0].data)
    assert attached == ["wss://api.openai.com/v1/live/rtc-owned"]


def test_http_failure_is_not_retried_or_fallen_back(monkeypatch):
    urls = []
    monkeypatch.setattr(provider, "credentials", lambda *args: {"Authorization": "Bearer fixture-subscription"})
    def fail(request, **kwargs):
        urls.append(request.full_url); raise HTTPError(request.full_url, 403, "private details", {}, None)
    with pytest.raises(provider.ProviderUnavailable, match="HTTP 403"):
        provider.negotiate(SDP, wire=LiveWire("subscription"), session={}, cfg=CFG, open_http=fail)
    assert urls == [provider.SUBSCRIPTION_CALLS]


def test_failed_attach_closes_created_session_before_reporting_failure():
    class Socket:
        sent = []; closed = False
        def send(self, raw): self.sent.append(json.loads(raw))
        def recv(self): return '{"type":"session.closed"}'
        def close(self): self.closed = True
    socket = Socket(); attempts = []
    def attach(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1: raise OSError("network")
        return socket
    wire = LiveWire()
    with pytest.raises(provider.ProviderUnavailable, match="session closed") as error:
        provider.negotiate(SDP, wire=wire, session=wire.session("Wait."), cfg=CFG,
            open_http=lambda *a, **k: response(json.dumps({"session": {"id": "live-owned"}, "transport": {"type": "webrtc", "sdp": SDP}}).encode()), attach=attach)
    assert socket.closed and socket.sent == [{"type": "session.close"}]
    assert error.value.orphan_session_id is None


def test_private_credential_cannot_be_reflected_into_phone_answer():
    with pytest.raises(provider.ProviderUnavailable, match="invalid session contract"):
        provider.negotiate(SDP, wire=LiveWire(), session={}, cfg=CFG,
            open_http=lambda *a, **k: response(json.dumps({"session": {"id": "live-owned"},
                "transport": {"type": "webrtc", "sdp": SDP + "a=fixture-api\r\n"}}).encode()),
            attach=lambda *a, **k: pytest.fail("credential-bearing answer accepted"))
