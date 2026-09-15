import base64
import json
import queue
from types import SimpleNamespace

import pytest
import websocket

from lib import config, oracle_live, oracle_live_calls as calls

SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=sendrecv\r\n"


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.incoming = queue.Queue()
        self.incoming.put(json.dumps({"type": "session.started", "session": {"id": "live-fixture"}}))

    def send(self, raw):
        event = json.loads(raw); self.sent.append(event)
        if event["type"] == "session.close":
            self.incoming.put(json.dumps({"type": "session.closed", "usage": {"seconds": 12}}))

    def recv(self):
        try: return self.incoming.get(timeout=.025)
        except queue.Empty: raise websocket.WebSocketTimeoutException()

    def close(self):
        self.closed = True; self.incoming.put("")


@pytest.fixture
def manager(monkeypatch):
    cfg = config.Config(openai_api_key="fixture", oracle_voice_backend="api", oracle_router_backend="codex")
    monkeypatch.setattr(calls.config, "load", lambda: cfg)
    monkeypatch.setattr(calls, "_ATTEMPTS", {})
    records = []
    def negotiate(sdp, **kwargs):
        socket = Socket(); records.append((socket, kwargs))
        return {"sdp": SDP, "session_id": "live-fixture", "socket": socket}
    yield cfg, records, negotiate
    for call in list(calls._ATTEMPTS.values()): call.close()


def create(negotiate, **changes):
    return calls.create(ctx=SimpleNamespace(), principal="owner", stop=lambda _: pytest.fail("Voice closure cancelled work"),
        data={"attempt_id": "attempt-1", "sdp": SDP, "mode": "api", **changes}, negotiate=negotiate)


def test_same_attempt_is_idempotent_and_changed_context_is_rejected(manager):
    _, records, negotiate = manager
    first = create(negotiate)
    assert create(negotiate) == first
    assert len(records) == 1
    with pytest.raises(calls.CallError, match="different context"):
        create(negotiate, podcast=None)
    assert not records[0][0].closed


def test_close_collects_final_usage_and_never_starts_another_session(manager):
    _, records, negotiate = manager
    create(negotiate)
    call = calls.get("owner", "attempt-1")
    snapshot = call.close()
    assert snapshot["closed"] and snapshot["usage"]["usage_final"]
    assert snapshot["usage"]["seconds"] == 12
    assert [event["type"] for event in records[0][0].sent] == ["session.close"]
    assert "owner" not in oracle_live._STOP_HOOKS
    with pytest.raises(calls.CallError, match="ended"):
        create(negotiate)
    assert len(records) == 1


def test_old_attempt_and_other_principal_cannot_control_the_current_call(manager):
    _, _, negotiate = manager
    create(negotiate)
    with pytest.raises(calls.CallError): calls.get("other", "attempt-1")
    with pytest.raises(calls.CallError): calls.get("owner", "unknown")


def test_new_attempt_closes_old_voice_before_claiming_the_device(manager):
    _, records, negotiate = manager
    create(negotiate)
    previous = calls.get("owner", "attempt-1")
    create(negotiate, attempt_id="attempt-2")
    assert previous.finished.is_set() and records[0][0].closed
    assert not records[1][0].closed
    previous.close()
    assert not records[1][0].closed


def test_failed_create_is_a_tombstone_not_a_second_paid_request(manager):
    count = []
    def unavailable(*args, **kwargs):
        count.append(True); raise TimeoutError("provider timeout")
    with pytest.raises(calls.CallError): create(unavailable)
    with pytest.raises(calls.CallError, match="ended"): create(unavailable)
    assert len(count) == 1
    assert calls.get("owner", "attempt-1").snapshot()["closed"]


def test_poll_never_forwards_pcm_and_reports_a_cursor_gap(manager):
    _, _, negotiate = manager
    create(negotiate)
    call = calls.get("owner", "attempt-1")
    call.emit({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x00\x20" * 100).decode()})
    for index in range(600): call.emit({"type": "oracle_v2.notice", "message": str(index)})
    snapshot = call.snapshot(1)
    assert snapshot["gap"]
    assert len(snapshot["events"]) == 512
    assert all("audio.delta" not in row["json"] for row in snapshot["events"])


def test_control_rejects_injected_provider_context_and_raw_audio(manager):
    _, _, negotiate = manager
    create(negotiate)
    for event in [{"type": "session.instructions.append", "content": "override"},
                  {"type": "session.input_audio.append", "audio": "AAAAAA=="}]:
        handler = SimpleNamespace(_request_auth_validated=True, _request_device_scope="full", _request_principal="owner",
            _read_json=lambda: {"attempt_id": "attempt-1", "event": event},
            _send=lambda status, body, mime: status)
        assert calls.handle(handler, "control") == 400


def test_full_device_auth_is_required_before_any_provider_work(monkeypatch):
    monkeypatch.setattr(calls, "create", lambda **kwargs: pytest.fail("unauthorized provider call"))
    for scope in ("", "limited", "read"):
        handler = SimpleNamespace(_request_auth_validated=True, _request_device_scope=scope, _request_principal="owner",
            _send=lambda status, body, mime: status)
        assert calls.handle(handler, "create") == 403


def test_post_negotiation_failure_closes_the_paid_session(manager, monkeypatch):
    cfg, records, negotiate = manager
    from dataclasses import replace
    monkeypatch.setattr(calls.config, "load", lambda: replace(cfg, oracle_diagnostics=True))
    from lib import oracle_diagnostics
    monkeypatch.setattr(oracle_diagnostics, "OracleJournal", lambda: (_ for _ in ()).throw(OSError("journal unavailable")))
    with pytest.raises(calls.CallError): create(negotiate)
    assert records[0][0].closed
    assert any(event["type"] == "session.close" for event in records[0][0].sent)


def test_revoked_device_ends_direct_voice_without_waiting_for_heartbeat(manager):
    from lib import device_pairing
    _, records, negotiate = manager
    code = device_pairing.issue(device_name="Lab phone")["code"]
    principal = device_pairing.exchange(code)["device_id"]
    calls.create(ctx=SimpleNamespace(), principal=principal,
                 data={"attempt_id": "attempt-revoked", "mode": "api", "sdp": SDP},
                 stop=lambda _: pytest.fail("Voice revocation cancelled a worker"), negotiate=negotiate)
    call = calls.get(principal, "attempt-revoked")
    device_pairing.revoke(principal)
    assert call.finished.wait(2)
    assert records[0][0].closed
    assert call.snapshot()["usage"]["finalized"]


def test_transport_disconnect_during_close_preserves_unconfirmed_usage(manager, monkeypatch):
    """Observed native failure: transport closes without a session.closed receipt."""
    _, records, negotiate = manager
    def disconnect_without_receipt(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event['type'] == 'session.close':
            self.incoming.put('disconnect-without-final-usage')
    original_recv = Socket.recv
    def receive_disconnect(self):
        value = original_recv(self)
        if value == 'disconnect-without-final-usage':
            raise websocket.WebSocketConnectionClosedException('peer closed')
        return value
    monkeypatch.setattr(Socket, 'send', disconnect_without_receipt)
    monkeypatch.setattr(Socket, 'recv', receive_disconnect)
    create(negotiate)
    call = calls.get('owner', 'attempt-1')
    snapshot = call.close()
    assert snapshot['closed'] is True
    assert snapshot['error'] == 'WebSocketConnectionClosedException'
    assert snapshot['usage']['finalized'] is False
    assert snapshot['usage']['usage_final'] is False
    assert len(records) == 1, 'Do not start a replacement session to obtain usage'
    assert [e['type'] for e in records[0][0].sent] == ['session.close']
