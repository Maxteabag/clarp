from __future__ import annotations

import base64
import json
import pathlib
import sys
import threading

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import cartesia_ws  # noqa: E402


@pytest.fixture(autouse=True)
def _join_raw_attempts(monkeypatch):
    """Join every attempt created by SDK stand-ins before restoring patches."""
    attempts = []
    class AttemptThread(threading.Thread):
        def start(self):
            attempts.append(self)
            super().start()
    monkeypatch.setattr(cartesia_ws, '_AttemptThread', AttemptThread, raising=False)
    yield
    for attempt in attempts:
        attempt.join(5)
    assert all(not t.is_alive() for t in attempts), 'raw SDK stand-in leaked an attempt'


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.frames = [
            json.dumps({
                "type": "chunk",
                "data": base64.b64encode(b"pcm-a").decode(),
                "done": False,
                "status_code": 206,
                "context_id": "ctx",
            }),
            json.dumps({
                "type": "chunk",
                "data": base64.b64encode(b"pcm-b").decode(),
                "done": True,
                "status_code": 206,
                "context_id": "ctx",
            }),
        ]
        self.closed = False

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self) -> str:
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


def test_cartesia_websocket_streams_raw_pcm_chunks(monkeypatch, tmp_path):
    fake = _FakeWS()
    captured = {}

    def fake_open(url, **kw):
        captured["url"] = url
        captured["header"] = kw["header"]
        return fake

    monkeypatch.setattr(cartesia_ws, "_open_ws", fake_open)
    out = tmp_path / "clip.pcm"
    chunks: list[tuple[int, bytes]] = []

    total = cartesia_ws.synthesize_raw_pcm(
        text="hello",
        voice_id="voice",
        out_path=out,
        api_key="key",
        model="sonic-3.5",
        on_chunk=lambda idx, chunk: chunks.append((idx, chunk)),
    )

    assert "cartesia_version=2026-03-01" in captured["url"]
    assert captured["header"] == ["X-API-Key: key"]
    assert fake.sent[0]["output_format"] == {
        "container": "raw",
        "encoding": "pcm_f32le",
        "sample_rate": 44100,
    }
    assert fake.sent[0]["continue"] is False
    assert fake.sent[0]["max_buffer_delay_ms"] == 0
    assert chunks == [(0, b"pcm-a"), (1, b"pcm-b")]
    assert out.read_bytes() == b"pcm-apcm-b"
    assert total == len(b"pcm-apcm-b")
    assert fake.closed is True



def test_connection_and_send_share_the_absolute_first_byte_budget(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import pytest
    now = [0.0]
    class Socket:
        closed = False
        timeout = 10.0
        def settimeout(self, value):self.timeout = value
        def send(self, payload):
            duration = 8.0
            now[0] += min(duration, self.timeout)
            if duration >= self.timeout:
                raise cartesia_ws.websocket.WebSocketTimeoutException('send timed out')
        def recv(self):pytest.fail('expired first-byte attempt reached recv')
        def close(self):self.closed = True
        def shutdown(self):self.closed = True
    socket = Socket()
    def opened(*args, **kwargs):
        socket.timeout = kwargs['timeout']
        now[0] += 8.0  # legitimate connection/setup cost below its timeout
        return socket
    monkeypatch.setattr(cartesia_ws, 'time', SimpleNamespace(perf_counter=lambda: now[0]))
    monkeypatch.setattr(cartesia_ws, '_open_ws', opened)
    with pytest.raises((cartesia_ws.CartesiaError, cartesia_ws.websocket.WebSocketTimeoutException)):
        cartesia_ws.synthesize_raw_pcm(text='Synthetic', voice_id='v', api_key='key', out_path=tmp_path/'raw.pcm')
    assert socket.closed
    assert now[0] <= cartesia_ws.RAW_FIRST_BYTE_BUDGET_SEC, 'connect + send consumed separate budgets before fallback could begin'



def _ping_wire(monkeypatch, ping_count, *, after_audio=False):
    """Real SDK receive loop; virtual frames and an interruptible transport.

    Advancing a frame also advances scheduled watchdogs. Socket closure wakes
    the pending arrival at its deadline, like shutdown(SHUT_RDWR) on a real recv.
    """
    import threading
    from types import SimpleNamespace
    now, timers, closed, pongs, timeouts = [0.0], [], [], [], []

    class Timer:
        daemon = False
        def __init__(self, interval, function):
            self.when, self.function = now[0] + interval, function
            self.cancelled = False
        def start(self):
            timers.append(self)
        def cancel(self):
            self.cancelled = True

    def advance(seconds):
        target = now[0] + seconds
        while True:
            pending = [t for t in timers if not t.cancelled and t.when <= target]
            if not pending:
                break
            timer = min(pending, key=lambda t: t.when)
            timer.cancelled = True
            now[0] = timer.when
            timer.function()
            if closed:
                return
        now[0] = target

    ws = cartesia_ws.websocket.WebSocket()
    abnf = cartesia_ws.websocket.ABNF
    audio = json.dumps({'type': 'chunk', 'data': base64.b64encode(b'PCM!').decode(),
                        'done': not after_audio})
    pings = [abnf.create_frame(b'heartbeat', abnf.OPCODE_PING) for _ in range(ping_count)]
    frames = ([abnf.create_frame(audio, abnf.OPCODE_TEXT)] + pings +
              [abnf.create_frame(json.dumps({'type': 'done'}), abnf.OPCODE_TEXT)]
              if after_audio else pings + [abnf.create_frame(audio, abnf.OPCODE_TEXT)])

    def frame():
        advance(4.0)
        if closed:
            raise cartesia_ws.websocket.WebSocketConnectionClosedException('transport aborted')
        return frames.pop(0)

    monkeypatch.setattr(ws, 'recv_frame', frame)
    monkeypatch.setattr(ws, 'pong', pongs.append)
    monkeypatch.setattr(ws, 'send', lambda payload: None)
    monkeypatch.setattr(ws, 'settimeout', timeouts.append)
    monkeypatch.setattr(ws, 'abort', lambda: closed.append('abort'))
    monkeypatch.setattr(ws, 'shutdown', lambda: closed.append('shutdown'))
    monkeypatch.setattr(ws, 'close', lambda: closed.append('close'))
    monkeypatch.setattr(cartesia_ws, '_open_ws', lambda *args, **kwargs: ws)
    monkeypatch.setattr(cartesia_ws, 'time', SimpleNamespace(perf_counter=lambda: now[0]))
    monkeypatch.setattr(cartesia_ws, 'threading',
                        SimpleNamespace(Timer=Timer, Lock=threading.Lock), raising=False)
    return SimpleNamespace(now=now, closed=closed, pongs=pongs, timeouts=timeouts)


@pytest.mark.parametrize('ping_count,after_audio', [
    pytest.param(1, False, id='below-budget'),
    pytest.param(3, False, id='over-budget'),
    pytest.param(3, True, id='after-first-audio'),
])
def test_protocol_pings_cannot_extend_the_first_byte_deadline(monkeypatch, tmp_path,
                                                            ping_count, after_audio):
    wire = _ping_wire(monkeypatch, ping_count, after_audio=after_audio)
    delivered = []
    def run():
        return cartesia_ws.synthesize_raw_pcm(
            text='Synthetic', voice_id='v', api_key='key', out_path=tmp_path/'raw.pcm',
            on_chunk=lambda index, chunk: delivered.append(chunk))
    if ping_count == 3 and not after_audio:
        with pytest.raises(cartesia_ws.CartesiaError):
            run()
        assert delivered == []
        assert wire.now[0] <= 10, 'control frames extended the absolute first-byte budget'
        assert len(wire.pongs) == 2
    else:
        assert run() == 4 and delivered == [b'PCM!']
        assert wire.now[0] == (20 if after_audio else 8)
        assert len(wire.pongs) == ping_count
        assert 30.0 in wire.timeouts
        assert wire.closed == ['close']
    assert wire.closed


@pytest.mark.parametrize('phase_seconds', [2.0, 8.0], ids=['within-budget', 'setup-over-budget'])
def test_connection_and_upgrade_share_the_first_byte_deadline(monkeypatch, tmp_path, phase_seconds):
    from types import SimpleNamespace
    import threading
    now, timers, closed, calls = [0.0], [], [False], []
    class Timer:
        daemon = False
        def __init__(self, interval, callback):
            self.when, self.callback, self.cancelled = now[0]+interval, callback, False
        def start(self):timers.append(self)
        def cancel(self):self.cancelled=True
    def advance(seconds):
        target = now[0]+seconds
        while True:
            pending=[t for t in timers if not t.cancelled and t.when<=target]
            if not pending:break
            t=min(pending,key=lambda t:t.when)
            now[0]=t.when;t.cancelled=True;t.callback()
            if closed[0]:return
        now[0]=target
    class Socket:
        timeout=10.0
        def gettimeout(self):return self.timeout
        def settimeout(self,value):self.timeout=value
        def shutdown(self,*args):closed[0]=True
        def close(self):closed[0]=True
        def send(self,data):return len(data)
        def recv(self,size):
            advance(self.timeout)
            raise cartesia_ws.websocket.WebSocketTimeoutException('read timeout')
    sock=Socket()
    def connect(url, options, *args, **kwargs):
        calls.append('connect')
        assert options.timeout == 10.0
        advance(phase_seconds)
        if closed[0]:raise cartesia_ws.websocket.WebSocketTimeoutException('setup aborted')
        return sock, ('api.cartesia.ai',443,'/tts/websocket')
    def handshake(*args, **kwargs):
        calls.append('upgrade')
        assert sock.gettimeout()==10.0
        advance(phase_seconds)
        if closed[0]:raise cartesia_ws.websocket.WebSocketTimeoutException('upgrade aborted')
        return SimpleNamespace(status=101,headers={})
    core=cartesia_ws.websocket.WebSocket.connect.__globals__
    assert 'connect' in core and 'handshake' in core
    monkeypatch.setitem(core,'connect',connect)
    monkeypatch.setitem(core,'handshake',handshake)
    monkeypatch.setattr(cartesia_ws,'time',SimpleNamespace(perf_counter=lambda:now[0]))
    monkeypatch.setattr(cartesia_ws,'threading',SimpleNamespace(Timer=Timer,Lock=threading.Lock))
    with pytest.raises(cartesia_ws.CartesiaError):
        cartesia_ws.synthesize_raw_pcm(text='Synthetic',voice_id='v',api_key='key',out_path=tmp_path/'raw.pcm')
    assert calls==['connect','upgrade'] and closed[0]
    assert now[0]<=cartesia_ws.RAW_FIRST_BYTE_BUDGET_SEC, 'watchdog was armed only after TCP/TLS and HTTP Upgrade consumed separate budgets'


@pytest.mark.parametrize('finish', ['complete','stop'])
def test_a_queued_deadline_callback_after_finish_cannot_abort_the_stream(monkeypatch, finish):
    import threading
    from types import SimpleNamespace
    callbacks, closed = [], []
    class Timer:
        daemon = False
        def __init__(self, interval, callback):self.callback=callback
        def start(self):callbacks.append(self.callback)
        def cancel(self):pass  # model callback already queued when cancel was called
    monkeypatch.setattr(cartesia_ws,'threading',SimpleNamespace(Timer=Timer,Lock=threading.Lock))
    ws=SimpleNamespace(abort=lambda:closed.append('abort'),shutdown=lambda:closed.append('shutdown'))
    budget=cartesia_ws._FirstByteBudget(ws,cartesia_ws.time.perf_counter()+100,None)
    budget.start()
    getattr(budget,finish)()
    callbacks[0]()
    assert closed==[]
