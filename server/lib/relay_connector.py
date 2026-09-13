"""Host-owned outbound relay transport. No public listener or admin credential.

Protocol: [kind u8][stream id u32 big-endian][payload]. Local HTTP and
WebSockets both retain device authentication and are marked as remote traffic.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import http.client
import ipaddress
import json
import logging
import socket
import struct
import threading
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect

REQ, REQ_BODY, REQ_END = 0x01, 0x02, 0x03
RES_HEAD, RES_BODY, RES_END = 0x11, 0x12, 0x13
WS_OPEN, WS_ACCEPT, WS_MSG, WS_CLOSE = 0x21, 0x22, 0x23, 0x24
ABORT = 0x7F
CHUNK = 256 * 1024
MAX_BODY = 64 * 1024 * 1024
MAX_STREAMS = 64
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer', 'upgrade',
       'proxy-connection', 'proxy-authorization', 'host', 'content-length'}
log = logging.getLogger(__name__)


def forward_request_headers(meta_headers: dict, local_netloc: str, body_len: int) -> dict:
    lower = {k.lower(): v for k, v in meta_headers.items()}
    connection_headers = {s.strip().lower() for s in lower.get('connection', '').split(',')}
    headers = {k: v for k, v in meta_headers.items()
               if k.lower() not in HOP | connection_headers | {'accept-encoding', 'forwarded', 'x-real-ip'}
               and not k.lower().startswith(('x-forwarded-', 'x-clarp-', 'tailscale-', 'cf-', 'sec-websocket'))}
    headers.update({'Host': local_netloc, 'Accept-Encoding': 'identity', 'X-Clarp-Transport': 'relay'})
    try:
        headers['X-Forwarded-For'] = str(ipaddress.ip_address(lower.get('x-forwarded-for', '')))
    except ValueError:
        pass
    if body_len:
        headers['Content-Length'] = str(body_len)
    return headers


def frame(kind: int, sid: int, payload: bytes = b'') -> bytes:
    return struct.pack('!BI', kind, sid) + payload


def jframe(kind: int, sid: int, obj) -> bytes:
    return frame(kind, sid, json.dumps(obj, separators=(',', ':')).encode())


@dataclass
class Stream:
    sid: int
    generation: int
    body: bytearray = field(default_factory=bytearray)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None
    connection: http.client.HTTPConnection | None = None
    response: http.client.HTTPResponse | None = None
    websocket: object = None
    head_sent: bool = False
    is_http: bool = True
    http_submitted: bool = False
    websocket_messages: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=16))

    def interrupt_http(self):
        self.cancelled.set()
        connection = self.connection
        # HTTPResponse may own the socket after getresponse() (Connection: close).
        sockets = [getattr(connection, 'sock', None)]
        fp = getattr(self.response, 'fp', None)
        sockets.append(getattr(getattr(fp, 'raw', None), '_sock', None))
        for sock in sockets:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if connection:
            connection.close()


class Connector:
    def __init__(self, relay: str, host_id: str, key: str, local: str, status=None):
        self.url = relay.rstrip('/') + '/connect?' + urlencode({'host': host_id, 'key': key})
        self.local = urlsplit(local)
        if self.local.scheme != 'http' or self.local.hostname not in {'127.0.0.1', '::1'}:
            raise ValueError('relay upstream must be an explicit loopback HTTP address')
        self.out = asyncio.Queue(maxsize=64)
        self.streams: dict[int, Stream] = {}
        self.generation = 0
        self._buffered_body = 0
        self.status = status or (lambda state: None)

    async def start(self):
        self._loop = asyncio.get_running_loop()
        delay = 1
        try:
            while True:
                self.status('connecting')
                try:
                    async with connect(self.url, max_size=1024 * 1024, ping_interval=25,
                                       ping_timeout=20, open_timeout=20) as ws:
                        self.generation += 1
                        self.out = asyncio.Queue(maxsize=64)
                        self.status('connected')
                        delay = 1
                        sender = asyncio.create_task(self._sender(ws))
                        pinger = asyncio.create_task(self._pinger(ws))
                        try:
                            async for message in ws:
                                if isinstance(message, bytes):
                                    await self._dispatch(message)
                        finally:
                            sender.cancel(); pinger.cancel()
                            await self._teardown()
                            await asyncio.gather(sender, pinger, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Exceptions can contain the credential-bearing connect URL.
                    log.warning('relay connection lost (%s)', type(exc).__name__)
                self.status('reconnecting')
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        finally:
            await self._teardown()
            self.status('stopped')

    async def _sender(self, ws):
        while True:
            stream, data = await self.out.get()
            if stream.generation == self.generation and not stream.cancelled.is_set():
                await ws.send(data)

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(25)
            await ws.send('ping')

    async def _teardown(self):
        streams = list(self.streams.values())
        for stream in streams:
            stream.interrupt_http()
            if stream.task:
                stream.task.cancel()
        await asyncio.gather(*(s.task for s in streams if s.task), return_exceptions=True)
        self.streams.clear()

    async def _send(self, stream, data):
        if not stream.cancelled.is_set() and stream.generation == self.generation:
            await self.out.put((stream, data))

    def _thread_send(self, stream, data):
        if stream.cancelled.is_set():
            return
        pending = asyncio.run_coroutine_threadsafe(self._send(stream, data), self._loop)
        try:
            pending.result(timeout=10)
        except BaseException:
            pending.cancel()
            raise

    def _release_body(self, stream):
        self._buffered_body -= len(stream.body)
        stream.body.clear()

    def _forget(self, stream):
        if self.streams.get(stream.sid) is stream:
            self.streams.pop(stream.sid)

    def _task_done(self, stream, task):
        # A task cancelled before its first execution never enters its finally.
        self._forget(stream)
        if not stream.http_submitted:
            self._release_body(stream)
        if not task.cancelled():
            task.exception()

    def _http_done(self, stream, future):
        # Retain the request's memory reservation until its worker really exits,
        # including when the awaiting async task has already been cancelled.
        self._release_body(stream)
        if not future.cancelled():
            future.exception()

    async def _dispatch(self, data):
        if len(data) < 5:
            raise ValueError('short relay frame')
        kind, sid = struct.unpack('!BI', data[:5])
        payload = data[5:]
        if kind in {REQ, WS_OPEN}:
            if sid in self.streams:
                raise ValueError('duplicate relay stream')
            stream = Stream(sid, self.generation)
            if len(self.streams) >= MAX_STREAMS:
                await self._send(stream, frame(ABORT, sid))
                return
            meta = json.loads(payload)
            path = meta.get('path', '')
            if (not isinstance(path, str) or not path.startswith('/')
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or not isinstance(meta.get('headers'), dict)):
                raise ValueError('invalid relay request')
            stream.is_http = kind == REQ
            self.streams[sid] = stream
            stream.task = asyncio.create_task(self._serve_http(stream, meta) if kind == REQ else self._serve_ws(stream, meta))
            stream.task.add_done_callback(lambda task: self._task_done(stream, task))
            return
        stream = self.streams.get(sid)
        if stream is None:
            return
        if kind in {REQ_BODY, REQ_END} and not stream.is_http:
            raise ValueError("HTTP frame on a WebSocket stream")
        if kind == REQ_BODY:
            if stream.ended.is_set():
                raise ValueError("request body after end frame")
            if self._buffered_body + len(payload) > MAX_BODY:
                await self._send(Stream(sid, self.generation), frame(ABORT, sid))
                stream.interrupt_http()
                stream.task.cancel()
            else:
                stream.body.extend(payload)
                self._buffered_body += len(payload)
        elif kind == REQ_END:
            stream.ended.set()
        elif kind == WS_MSG and payload:
            try:
                stream.websocket_messages.put_nowait(payload[1:].decode() if payload[0] == 1 else payload[1:])
            except asyncio.QueueFull:
                await self._send(Stream(sid, self.generation), frame(ABORT, sid))
                stream.interrupt_http()
                stream.task.cancel()
        elif kind in {ABORT, WS_CLOSE}:
            stream.interrupt_http()
            stream.task.cancel()

    async def _serve_http(self, stream, meta):
        try:
            await asyncio.wait_for(stream.ended.wait(), timeout=60)
            future = self._loop.run_in_executor(None, self._do_http, stream, meta, stream.body)
            stream.http_submitted = True
            future.add_done_callback(lambda done: self._http_done(stream, done))
            await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('relay HTTP stream failed (%s)', type(exc).__name__)
            if stream.head_sent:
                await self._send(stream, frame(ABORT, stream.sid))
            else:
                await self._send(stream, jframe(RES_HEAD, stream.sid, {'status': 502, 'headers': {'content-type': 'application/json'}}))
                await self._send(stream, frame(RES_BODY, stream.sid, b'{"error":"Host request failed"}'))
                await self._send(stream, frame(RES_END, stream.sid))
        finally:
            self._forget(stream)

    def _do_http(self, stream, meta, body):
        connection = http.client.HTTPConnection(self.local.hostname, self.local.port or 80, timeout=60)
        stream.connection = connection
        try:
            if stream.cancelled.is_set():
                return
            headers = forward_request_headers(meta['headers'], self.local.netloc, len(body))
            connection.request(meta.get('method', 'GET'), meta['path'], body=body or None, headers=headers)
            if stream.cancelled.is_set():
                return
            response = connection.getresponse()
            stream.response = response
            headers = {k: v for k, v in response.getheaders() if k.lower() not in HOP}
            self._thread_send(stream, jframe(RES_HEAD, stream.sid, {'status': response.status, 'headers': headers}))
            stream.head_sent = True
            while not stream.cancelled.is_set():
                chunk = response.read1(CHUNK)
                if not chunk:
                    break
                self._thread_send(stream, frame(RES_BODY, stream.sid, chunk))
            self._thread_send(stream, frame(RES_END, stream.sid))
        finally:
            connection.close()
            if stream.response:
                stream.response.close()

    async def _serve_ws(self, stream, meta):
        try:
            headers = forward_request_headers(meta['headers'], self.local.netloc, 0)
            headers.pop('Host', None)
            headers.pop('Accept-Encoding', None)
            async with connect(f'ws://{self.local.netloc}{meta["path"]}', additional_headers=headers,
                               max_size=1024 * 1024, ping_interval=25, open_timeout=20) as ws:
                stream.websocket = ws
                await self._send(stream, jframe(WS_ACCEPT, stream.sid, {'ok': True}))
                async def forward_client():
                    while True:
                        await ws.send(await stream.websocket_messages.get())
                sender = asyncio.create_task(forward_client())
                try:
                    async for message in ws:
                        payload = b'\x01' + message.encode() if isinstance(message, str) else b'\x00' + message
                        await self._send(stream, frame(WS_MSG, stream.sid, payload))
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                await self._send(stream, jframe(WS_CLOSE, stream.sid, {'code': ws.close_code or 1000, 'reason': 'Host connection closed'}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('relay WebSocket stream failed (%s)', type(exc).__name__)
            await self._send(stream, jframe(WS_CLOSE, stream.sid, {'code': 1011, 'reason': 'Host WebSocket failed'}))
        finally:
            self._forget(stream)


class ManagedRelay:
    """The HTTP service owns the connector thread, restart and shutdown."""
    def __init__(self, settings, port):
        self.settings = settings
        self._state = 'starting'
        self._ready = threading.Event()
        self._loop = None
        self._task = None
        self.connector = Connector(settings.websocket_url, settings.host_id, settings.key,
                                   f'http://127.0.0.1:{port}', self._set_state)
        self._thread = threading.Thread(target=self._run, name='clarp-relay', daemon=True)

    def _set_state(self, state):
        self._state = state

    def status(self):
        return {**self.settings.public_status(), 'enabled': True, 'state': self._state}

    def start(self):
        self._thread.start()

    def _run(self):
        async def run():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            self._ready.set()
            try:
                await self.connector.start()
            except asyncio.CancelledError:
                pass
        asyncio.run(run())

    def close(self):
        if self._ready.wait(timeout=2) and self._loop and self._task:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass
        self._thread.join(timeout=12)
        if self._thread.is_alive():
            log.error('relay shutdown is waiting for an upstream request to finish')
