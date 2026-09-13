"""Actual loopback WebSockets and HTTP; no public relay or production state."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import struct
import threading

from websockets.asyncio.server import serve
from lib.relay_connector import Connector, ManagedRelay, frame, jframe


def unpack(data):
    kind, sid = struct.unpack('!BI', data[:5])
    return kind, sid, data[5:]


def test_websocket_frames_sent_during_upstream_handshake_are_not_lost():
    async def scenario():
        captured = []
        async def upstream(ws):
            captured.append(dict(ws.request.headers))
            async for message in ws:
                await ws.send(message)
        complete = asyncio.get_running_loop().create_future()
        async with serve(upstream, '127.0.0.1', 0) as local:
            port = local.sockets[0].getsockname()[1]
            async def relay(ws):
                try:
                    await ws.send(jframe(0x21, 1, {'path': '/terminal/test', 'headers': {'Authorization': 'Bearer device-token'}}))
                    await ws.send(frame(0x23, 1, b'\x01early resize'))
                    results = []
                    while len(results) < 2:
                        results.append(unpack(await asyncio.wait_for(ws.recv(), 2)))
                    assert results[0][0] == 0x22
                    assert results[1] == (0x23, 1, b'\x01early resize')
                    assert captured[0]['x-clarp-transport'] == 'relay'
                    assert captured[0]['authorization'] == 'Bearer device-token'
                    complete.set_result(True)
                except BaseException as error:
                    if not complete.done(): complete.set_exception(error)
            async with serve(relay, '127.0.0.1', 0) as public:
                c = Connector(f'ws://127.0.0.1:{public.sockets[0].getsockname()[1]}', 'host-test', 'test-secret', f'http://127.0.0.1:{port}')
                task = asyncio.create_task(c.start())
                try:
                    await asyncio.wait_for(complete, 4)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_http_and_cancelled_sse_close_the_upstream_socket():
    closed = threading.Event()
    captured = []
    class HTTP(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            captured.append(dict(self.headers))
            if self.path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b': ping\n\n'); self.wfile.flush()
                self.connection.settimeout(3)
                try:
                    assert self.rfile.read(1) == b''
                    closed.set()
                except OSError:
                    pass
            else:
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
    http = ThreadingHTTPServer(('127.0.0.1', 0), HTTP)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    async def scenario():
        complete = asyncio.get_running_loop().create_future()
        async def relay(ws):
            try:
                await ws.send(jframe(1, 1, {'method': 'GET', 'path': '/status', 'headers': {'Authorization': 'Bearer device-token', 'Accept-Encoding': 'gzip'}}))
                await ws.send(frame(3, 1))
                body = b''
                while True:
                    kind, sid, payload = unpack(await asyncio.wait_for(ws.recv(), 3))
                    assert sid == 1
                    if kind == 0x12: body += payload
                    if kind == 0x13: break
                assert json.loads(body) == {'ok': True}
                assert captured[0]['X-Clarp-Transport'] == 'relay'
                assert captured[0]['Accept-Encoding'] == 'identity'
                await ws.send(jframe(1, 2, {'method': 'GET', 'path': '/events', 'headers': {}}))
                await ws.send(frame(3, 2))
                while True:
                    kind, _, _ = unpack(await asyncio.wait_for(ws.recv(), 3))
                    if kind == 0x12: break
                await ws.send(frame(0x7f, 2))
                assert await asyncio.to_thread(closed.wait, 2), 'cancel did not close the local SSE socket'
                complete.set_result(True)
            except BaseException as error:
                if not complete.done(): complete.set_exception(error)
        async with serve(relay, '127.0.0.1', 0) as public:
            c = Connector(f'ws://127.0.0.1:{public.sockets[0].getsockname()[1]}', 'host-test', 'test-secret', f'http://127.0.0.1:{http.server_port}')
            task = asyncio.create_task(c.start())
            try:
                await asyncio.wait_for(complete, 8)
            finally:
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
    try:
        asyncio.run(scenario())
    finally:
        http.shutdown(); http.server_close(); thread.join(timeout=2)


def test_managed_connector_reconnects_and_stops_with_its_owner():
    from types import SimpleNamespace
    async def scenario():
        connections = [0]
        connected_again = asyncio.Event()
        async def relay(ws):
            connections[0] += 1
            if connections[0] == 1:
                await ws.close(1012, 'test restart')
            else:
                connected_again.set()
                await ws.wait_closed()
        async with serve(relay, '127.0.0.1', 0) as public:
            settings = SimpleNamespace(
                websocket_url=f'ws://127.0.0.1:{public.sockets[0].getsockname()[1]}',
                host_id='host-test', key='test-secret', public_status=lambda: {'configured': True})
            owner = ManagedRelay(settings, 7682)
            owner.start()
            try:
                await asyncio.wait_for(connected_again.wait(), 4)
                async with asyncio.timeout(2):
                    while owner.status()['state'] != 'connected':
                        await asyncio.sleep(0.01)
            finally:
                await asyncio.to_thread(owner.close)
            assert not owner._thread.is_alive()
            assert owner.status()['state'] == 'stopped'
    asyncio.run(scenario())


def test_inflight_uploads_count_towards_the_total_body_budget(monkeypatch):
    import lib.relay_connector as module
    monkeypatch.setattr(module, 'MAX_BODY', 4)
    async def scenario():
        connector = Connector('ws://127.0.0.1:1', 'host-test', 'test', 'http://127.0.0.1:2')
        connector._loop = asyncio.get_running_loop()
        started, release = threading.Event(), threading.Event()
        def slow_request(stream, meta, body):
            assert bytes(body) == b'abcd'
            started.set()
            release.wait(2)
        monkeypatch.setattr(connector, '_do_http', slow_request)
        try:
            await connector._dispatch(jframe(1, 1, {'path': '/upload', 'headers': {}, 'method': 'POST'}))
            await connector._dispatch(frame(2, 1, b'abcd'))
            await connector._dispatch(frame(3, 1))
            assert await asyncio.to_thread(started.wait, 1)
            await connector._dispatch(jframe(1, 2, {'path': '/upload', 'headers': {}, 'method': 'POST'}))
            await connector._dispatch(frame(2, 2, b'x'))
            _, data = await asyncio.wait_for(connector.out.get(), 0.5)
            assert unpack(data)[:2] == (0x7f, 2)
        finally:
            release.set()
            await connector._teardown()
    asyncio.run(scenario())


def test_cancelling_a_stream_before_its_task_starts_releases_the_slot():
    async def scenario():
        connector = Connector('ws://127.0.0.1:1', 'host-test', 'test', 'http://127.0.0.1:2')
        await connector._dispatch(jframe(1, 1, {'path': '/status', 'headers': {}}))
        await connector._dispatch(frame(0x7f, 1))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not connector.streams
    asyncio.run(scenario())
