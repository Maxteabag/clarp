import asyncio
import json
import socket
import struct

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.frames import Close

from lib import relay_connector
from lib.relay_connector import Connector, close_details, forward_request_headers, frame, reconnect_delay


def test_relay_always_marks_both_authenticated_and_anonymous_requests_remote():
    for meta in [{}, {'X-Clarp-Transport': 'local', 'Authorization': 'Bearer device'}]:
        headers = forward_request_headers(meta, '127.0.0.1:7682', 0)
        assert headers['X-Clarp-Transport'] == 'relay'
        assert headers['Accept-Encoding'] == 'identity'


def test_relay_preserves_verified_client_address_for_failure_throttling():
    headers = forward_request_headers({'x-forwarded-for': '192.0.2.10'}, '127.0.0.1:7682', 0)
    assert headers['X-Forwarded-For'] == '192.0.2.10'


# --- loss diagnostics and reconnect schedule --------------------------------


def test_close_details_reports_the_peer_close_code_and_reason():
    exc = ConnectionClosedError(Close(1012, 'replaced'), None)
    detail = close_details(exc)
    assert detail['error_type'] == 'ConnectionClosedError'
    assert (detail['code'], detail['reason']) == (1012, 'replaced')
    assert (detail['rcvd_code'], detail['rcvd_reason']) == (1012, 'replaced')
    assert detail['sent_code'] is None


def test_close_details_distinguishes_our_keepalive_timeout_from_a_dropped_tcp_link():
    # websockets closes with 1011 itself when a pong is late; the peer sends nothing.
    timeout = close_details(ConnectionClosedError(None, Close(1011, 'keepalive ping timeout'), None))
    assert timeout['code'] == 1006 and timeout['sent_code'] == 1011
    assert timeout['sent_reason'] == 'keepalive ping timeout'
    dropped = close_details(ConnectionClosedError(None, None))
    assert dropped['code'] == 1006 and dropped['sent_code'] is None and dropped['rcvd_code'] is None


def test_close_details_never_includes_the_exception_text():
    url = 'wss://relay.example/connect?host=h&key=secret'
    exc = socket.gaierror(-3, f'Temporary failure resolving {url}')
    detail = close_details(exc)
    assert detail == {'error_type': 'gaierror', 'errno': -3}
    assert 'secret' not in repr(detail)
    clean = close_details(ConnectionClosedOK(Close(1000, 'bye'), Close(1000, 'bye'), True))
    assert clean['code'] == 1000 and clean['reason'] == 'bye'


def test_close_details_reports_a_rejected_handshake_status():
    from websockets.http11 import Response
    exc = InvalidStatus(Response(403, 'Forbidden', []))
    assert close_details(exc)['http_status'] == 403


def test_reconnect_after_a_lost_live_session_is_fast_then_backs_off():
    lost = {'rcvd_code': None, 'code': 1006}
    first = reconnect_delay(30.0, was_connected=True, close=lost)
    assert first == relay_connector.RECONNECT_FAST < 0.5
    schedule = [first]
    for _ in range(8):
        schedule.append(reconnect_delay(schedule[-1], was_connected=False, close=lost))
    assert schedule == [0.3, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert reconnect_delay(None, was_connected=False, close=None) == 1.0


def test_reconnect_waits_when_another_connector_replaced_this_host():
    replaced = close_details(ConnectionClosedError(Close(1012, 'replaced'), None))
    assert reconnect_delay(0.3, was_connected=True, close=replaced) == relay_connector.RECONNECT_REPLACED
    # 1012 is also what a plain service restart sends; only the Worker's
    # "replaced" contract means another connector owns this host id.
    restart = close_details(ConnectionClosedError(Close(1012, 'test restart'), None))
    assert reconnect_delay(0.3, was_connected=True, close=restart) == relay_connector.RECONNECT_FAST
    other = close_details(ConnectionClosedError(Close(1011, 'error'), None))
    assert reconnect_delay(0.3, was_connected=True, close=other) == relay_connector.RECONNECT_FAST


def test_lost_session_emits_a_telemetry_row_with_code_age_and_streams(monkeypatch):
    rows = []
    async def scenario():
        connector = Connector('ws://127.0.0.1:1', 'host-test', 'test', 'http://127.0.0.1:2',
                              telemetry=lambda event, level, detail: rows.append((event, level, detail)))
        connector._loop = asyncio.get_running_loop()
        connector.generation = 3
        exc = ConnectionClosedError(Close(1012, 'host disconnected'), None)
        connector._report_loss(close_details(exc), connected_at=100.0, lost_at=1900.5,
                               torn_down={'http': 2, 'ws': 1})
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    assert len(rows) == 1
    event, level, detail = rows[0]
    assert (event, level) == ('relayConnectionLost', 'warning')
    assert detail['code'] == 1012 and detail['reason'] == 'host disconnected'
    assert detail['session'] == 3 and detail['session_age_s'] == 1800.5
    assert (detail['streams_torn_down'], detail['http_streams'], detail['ws_streams']) == (3, 2, 1)


def test_failed_dial_is_recorded_without_a_session_age(caplog):
    rows = []
    async def scenario():
        connector = Connector('ws://127.0.0.1:1', 'host-test', 'test', 'http://127.0.0.1:2',
                              telemetry=lambda *row: rows.append(row))
        connector._loop = asyncio.get_running_loop()
        with caplog.at_level('WARNING', logger='lib.relay_connector'):
            connector._report_loss(close_details(socket.gaierror(-3, 'nope')), None, 5.0, {'http': 0, 'ws': 0})
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    detail = rows[0][2]
    assert detail['session_age_s'] is None and detail['session'] is None and detail['errno'] == -3
    assert 'relay connect failed (gaierror errno=-3)' in caplog.text
    assert 'ws://' not in caplog.text


def test_frames_for_a_stream_this_connection_never_saw_get_a_retryable_close():
    def unpack(data):
        kind, sid = struct.unpack('!BI', data[:5])
        return kind, sid, data[5:]
    async def scenario():
        connector = Connector('ws://127.0.0.1:1', 'host-test', 'test', 'http://127.0.0.1:2')
        await connector._dispatch(frame(0x23, 7, b'\x01hello'))
        kind, sid, payload = unpack((await asyncio.wait_for(connector.out.get(), 0.5))[1])
        assert (kind, sid) == (0x24, 7)
        assert json.loads(payload) == {'code': 1012, 'reason': 'Host stream gone'}
        await connector._dispatch(frame(0x03, 8))
        assert unpack((await asyncio.wait_for(connector.out.get(), 0.5))[1])[:2] == (0x7f, 8)
        # Frames the Worker already ignores for unknown ids stay silent.
        await connector._dispatch(frame(0x24, 9, b'{}'))
        await connector._dispatch(frame(0x7f, 10))
        assert connector.out.empty()
    asyncio.run(scenario())
