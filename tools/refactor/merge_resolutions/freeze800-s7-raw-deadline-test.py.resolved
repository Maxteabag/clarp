"""Caller deadlines across SDK phases, including uninterruptible late returns."""
from __future__ import annotations

import base64
import importlib
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from lib import agents, cartesia_ws, config, db, tts_queue, tts_worker, tts_registry, turn_queue
from lib.clip_delivery.raw_pcm import RawPcmDelivery
from lib.clip_stream import ClipStreamBroker
from lib.protocol import TurnSource
from tests.unit.test_tts_worker import env, _fallback_wav


PHASES = ('dns', 'tcp', 'tls', 'upgrade', 'config', 'send', 'receive')


class PhaseWire:
    """Real SDK setup/parser, synthetic network boundaries and a virtual timer.

    A gate deliberately ignores transport abort until released. This models
    libc DNS and makes the caller deadline independent of cooperative cleanup.
    Every gate and attempt thread is released/joined by the owning test.
    """
    def __init__(self, monkeypatch, phase):
        self.phase, self.block = phase, True
        self.now, self.timers, self.sockets, self.attempts = 0.0, [], [], []
        self.gates, self.entered = [], threading.Condition()
        self.sent = []
        wire = self

        class Timer:
            daemon = False
            def __init__(self, interval, callback):
                self.when, self.callback, self.cancelled = wire.now + interval, callback, False
            def start(self):
                wire.timers.append(self)
            def cancel(self):
                self.cancelled = True

        class AttemptThread(threading.Thread):
            def start(self):
                wire.attempts.append(self)
                super().start()

        monkeypatch.setattr(cartesia_ws, 'time', SimpleNamespace(perf_counter=lambda: self.now))
        monkeypatch.setattr(cartesia_ws, 'threading', SimpleNamespace(Timer=Timer, Lock=threading.Lock))
        monkeypatch.setattr(cartesia_ws, '_AttemptThread', AttemptThread, raising=False)

        class Socket:
            timeout, closed = 10.0, False
            def __init__(self):
                frame = cartesia_ws.websocket.ABNF.create_frame(json.dumps({
                    'type': 'chunk', 'data': base64.b64encode(b'PCM!').decode(), 'done': True}),
                    cartesia_ws.websocket.ABNF.OPCODE_TEXT)
                frame.mask_value = 0
                self.buffer = frame.format()
                self.read_started = False
                wire.sockets.append(self)
            def settimeout(self, value):
                self.timeout = value
            def gettimeout(self):
                return self.timeout
            def shutdown(self, *args):
                self.closed = True
            def close(self):
                self.closed = True
            def send(self, data):
                wire.step('send')
                if self.closed:
                    raise OSError('closed synthetic transport')
                wire.sent.append(bytes(data))
                return len(data)
            def recv(self, size):
                if not self.read_started:
                    self.read_started = True
                    wire.step('receive')
                if self.closed:
                    raise cartesia_ws.websocket.WebSocketConnectionClosedException('aborted')
                data, self.buffer = self.buffer[:size], self.buffer[size:]
                return data

        http = importlib.import_module('websocket._http')
        def dns(*args):
            self.step('dns')
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1',443))], False, None
        def tcp(*args):
            transport = Socket()
            self.step('tcp')
            return transport
        def tls(transport, *args):
            self.step('tls')
            return transport
        def upgrade(*args, **kwargs):
            self.step('upgrade')
            return SimpleNamespace(status=101, headers={})
        monkeypatch.setattr(http, '_get_addrinfo_list', dns)
        monkeypatch.setattr(http, '_open_socket', tcp)
        monkeypatch.setattr(http, '_ssl_socket', tls)
        monkeypatch.setitem(cartesia_ws.websocket.WebSocket.connect.__globals__, 'handshake', upgrade)
        def dumps(*args, **kwargs):
            self.step('config')
            return json.dumps(*args, **kwargs)
        monkeypatch.setattr(cartesia_ws, 'json', SimpleNamespace(
            dumps=dumps, loads=json.loads, JSONDecodeError=json.JSONDecodeError))

    def step(self, phase):
        if not self.block or phase != self.phase:
            return
        gate = threading.Event()
        with self.entered:
            self.gates.append(gate)
            self.entered.notify_all()
        assert gate.wait(10), f'{phase} stand-in was not released'

    def wait_entered(self, count):
        with self.entered:
            assert self.entered.wait_for(lambda: len(self.gates) >= count, timeout=5), \
                f'{self.phase} never started; queue errors={[r.get("error") for r in tts_queue.recent()]}'

    def deadline(self):
        self.now = 10.0
        for timer in list(self.timers):
            if not timer.cancelled and timer.when <= self.now:
                timer.cancelled = True
                timer.callback()

    def release_and_join(self, owners):
        self.block = False
        for gate in self.gates:
            gate.set()
        for thread in owners + self.attempts:
            thread.join(5)
        assert all(not t.is_alive() for t in owners + self.attempts), 'deadline test leaked a thread'
        assert all(s.closed for s in self.sockets), 'late transport was not closed'


def _configure(monkeypatch):
    monkeypatch.setattr(config, '_CACHED', config.Config(
        tts_provider='cartesia', tts_fallback='elevenlabs',
        cartesia_api_key='primary-key', eleven_api_key='backup-key',
        cartesia_voices={'Mike': 'primary-voice'}))


def _owner(env, broker, events, *, agent_id=None):
    agent_id = agent_id or env['agent_id']
    qid = tts_queue.enqueue(agent_id=agent_id, text='Synthetic phase', voice_id='backup-voice',
                            session='claude', source=TurnSource.PWA)
    done, failures = threading.Event(), []
    class Stream:
        def broadcast(self, event):
            events.append(event)
    def run():
        try:
            assert tts_worker.synth_one(audio_dir=env['audio_dir'], broker=broker,
                                       delivery=RawPcmDelivery(broker=broker), stream=Stream())
        except BaseException as error:
            failures.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=run)
    thread.start()
    return SimpleNamespace(thread=thread, done=done, failures=failures, qid=qid)


@pytest.mark.parametrize('phase', PHASES)
def test_each_raw_phase_falls_back_at_ten_without_late_audio_or_overwrite(env, monkeypatch, phase):
    _configure(monkeypatch)
    wire, broker, events, calls = PhaseWire(monkeypatch, phase), ClipStreamBroker(), [], []
    def backup(*, out_path, on_chunk, **kwargs):
        calls.append(wire.now)
        assert on_chunk is None
        out_path.write_bytes(_fallback_wav())
        return out_path.stat().st_size
    monkeypatch.setattr(tts_registry, 'synthesize_streaming', backup)
    owner = _owner(env, broker, events)
    try:
        wire.wait_entered(1)
        wire.deadline()
        assert owner.done.wait(3), f'caller stayed blocked past the {phase} deadline'
        assert owner.failures == []
        assert calls == [10.0]
        row = next(r for r in tts_queue.recent() if r['queue_id'] == owner.qid)
        assert row['status'] == tts_queue.DONE
        clip = db.conn().execute('SELECT clip_id,path FROM clips').fetchone()
        from pathlib import Path
        path = Path(clip['path'])
        expected = path.read_bytes()
        assert len(expected) == 17640
        assert b''.join(broker.get(clip['clip_id']).chunks) == expected
        before = list(events)
    finally:
        wire.release_and_join([owner.thread])
    assert path.read_bytes() == expected, f'late {phase} completion overwrote fallback'
    assert b''.join(broker.get(clip['clip_id']).chunks) == expected
    assert events == before
    assert len([e for e in events if e['type'] == 'audio']) == 1


def test_three_abandoned_calls_bound_threads_and_release_slots(env, monkeypatch):
    _configure(monkeypatch)
    wire, broker, events, calls, owners = PhaseWire(monkeypatch, 'dns'), ClipStreamBroker(), [], [], []
    def backup(*, out_path, **kwargs):
        calls.append(wire.now)
        out_path.write_bytes(_fallback_wav())
        return out_path.stat().st_size
    monkeypatch.setattr(tts_registry, 'synthesize_streaming', backup)
    try:
        for index in range(3):
            aid = agents.create_agent(persona='Mike', voice_id='backup-voice',
                                      cwd=str(env['tmp_path']), session=f'raw-owner-{index}')
            owners.append(_owner(env, broker, events, agent_id=aid))
            wire.wait_entered(index + 1)
        wire.deadline()
        assert all(o.done.wait(3) for o in owners), 'abandoned callers did not fall back'
        assert len(wire.attempts) == 3
        fourth = _owner(env, broker, events)
        owners.append(fourth)
        assert fourth.done.wait(3), 'capacity exhaustion did not fall back immediately'
        assert len(wire.attempts) == 3, 'fourth attempt grew the blocked thread set'
        assert len(wire.gates) == 3 and calls == [10.0] * 4
        assert all(not o.failures for o in owners)
    finally:
        wire.release_and_join([o.thread for o in owners])
    # A successful raw request, not another fallback, proves the slots returned.
    before = len(calls)
    fifth = _owner(env, broker, events)
    try:
        assert fifth.done.wait(3) and fifth.failures == []
        assert len(calls) == before and len(wire.attempts) == 4
    finally:
        wire.release_and_join([fifth.thread])


def test_stop_during_an_abandoned_attempt_fences_fallback_and_late_writes(env, monkeypatch):
    _configure(monkeypatch)
    wire, broker, events = PhaseWire(monkeypatch, 'dns'), ClipStreamBroker(), []
    entered, release = threading.Event(), threading.Event()
    def backup(*, out_path, **kwargs):
        entered.set()
        assert release.wait(5), 'fallback stand-in was not released'
        out_path.write_bytes(_fallback_wav())
        return out_path.stat().st_size
    monkeypatch.setattr(tts_registry, 'synthesize_streaming', backup)
    owner = _owner(env, broker, events)
    try:
        wire.wait_entered(1)
        wire.deadline()
        assert entered.wait(3), 'fallback never started at deadline'
        turn_queue.set_paused(env['agent_id'], True, user_stop=True)
        turn_queue.set_paused(env['agent_id'], False)
        release.set()
        assert owner.done.wait(3) and owner.failures == []
        row = next(r for r in tts_queue.recent() if r['queue_id'] == owner.qid)
        assert row['status'] == tts_queue.FAILED and row['error'].startswith(tts_queue.CANCELLED_PREFIX)
        clip = db.conn().execute('SELECT clip_id,path,producer_status FROM clips').fetchone()
        assert clip['producer_status'] == 'failed' and broker.get(clip['clip_id']).chunks == []
        from pathlib import Path
        path = Path(clip['path'])
        assert not path.exists()
        # Simulate a subsequent owner of that pathname; late cleanup must not unlink it.
        path.write_bytes(b'next owner')
        before = list(events)
    finally:
        release.set()
        wire.release_and_join([owner.thread])
    assert path.read_bytes() == b'next owner' and events == before
    assert not [e for e in events if e['type'] == 'tts-error']
