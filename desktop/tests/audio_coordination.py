#!/usr/bin/env python3
"""Exercise actual coordinator processes with fake playback and no microphone use."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time


class Probe:
    def __init__(self, binary, env, scope="offline-test-host"):
        self.process = subprocess.Popen([binary, scope], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, text=False)
        self.events = []
        self.buffer = b''
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.wait('ready')

    def pump(self, timeout=0.05):
        for key, _ in self.selector.select(timeout):
            data = os.read(key.fileobj.fileno(), 65536)
            assert data, f'Probe exited: {self.process.stderr.read().decode()}'
            self.buffer += data
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                event = json.loads(line)
                assert event.get('kind') != 'error', event
                self.events.append(event)

    def wait(self, kind, **values):
        deadline = time.monotonic() + 6
        while True:
            for index, event in enumerate(self.events):
                if event['kind'] == kind and all(event.get(k) == v for k, v in values.items()):
                    return self.events.pop(index)
            assert time.monotonic() < deadline, (kind, values, self.events)
            self.pump()

    def send(self, op, **values):
        self.process.stdin.write(json.dumps(dict(op=op, **values)).encode() + b'\n')
        self.process.stdin.flush()
        self.wait('done', op=op)

    def no_clips(self, seconds=0.7):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
        assert not [e for e in self.events if e['kind'] == 'clip'], self.events

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
        self.selector.close()


def main():
    with tempfile.TemporaryDirectory(prefix='clarp-audio-test-') as temp:
        env = dict(os.environ)
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
            directory = Path(temp) / key
            directory.mkdir(mode=0o700)
            env[key] = str(directory)
        probes = []
        try:
            first = Probe(sys.argv[1], env)
            probes.append(first)
            second = Probe(sys.argv[1], env)
            probes.append(second)
            first.send('status')
            assert first.wait('status')['owner']
            second.send('status')
            assert not second.wait('status')['owner']
            # The same broadcast in both windows yields one playback dispatch.
            first.send('submit', id=1)
            second.send('submit', id=1)
            first.wait('clip', id=1)
            first.no_clips()
            second.no_clips()
            first.send('begin', id=1)
            assert first.wait('begin')['ok']
            first.send('publish')
            second.wait('state', playing=True, available=True)
            for action in ('pause', 'resume', 'toggle', 'stop'):
                second.send('control', action=action)
                first.wait('command', action=action)
            first.send('finish', id=1)
            second.send('control', action='mute', muted=True)
            first.wait('state', muted=True)
            second.wait('state', muted=True)
            second.send('submit', id=2)
            first.no_clips()
            first.events = [e for e in first.events if e['kind'] != 'state']
            second.send('control', action='mute', muted=False)
            first.wait('state', muted=False)
            # A follower can own the microphone, with its destination pinned.
            second.send('record', session='bella')
            assert second.wait('record') == {'kind': 'record', 'ok': True, 'target': 'bella'}
            first.send('record', session='rachel')
            assert not first.wait('record')['ok']
            other_host = Probe(sys.argv[1], env, 'another-host')
            probes.append(other_host)
            other_host.send('status')
            assert other_host.wait('status')['owner']
            other_host.send('record', session='other-host-chat')
            assert not other_host.wait('record')['ok']
            other_host.send('submit', id=1)
            other_host.wait('clip', id=1)
            other_host.close()
            second.send('record', session='newly-focused-chat')
            assert second.wait('record') == {'kind': 'record', 'ok': False, 'target': 'bella'}
            second.send('stopRecording')
            assert second.wait('stopRecording')['target'] == 'bella'
            first.send('record', session='rachel')
            assert first.wait('record')['ok']
            # Preserve queue order; an interrupted clip is not replayed.
            for clip in (9, 10, 11):
                first.send('submit', id=clip)
                first.wait('clip', id=clip)
            first.send('begin', id=9)
            assert first.wait('begin')['ok']
            first.close()
            second.wait('owner', owner=True)
            assert second.wait('clip')['id'] == 10
            assert second.wait('clip')['id'] == 11
            second.send('submit', id=1)
            second.send('submit', id=9)
            second.no_clips()
            # A dead recording owner releases its OS lock without a timeout.
            second.send('record', session='after-crash')
            assert second.wait('record')['ok']
            second.send('stopRecording')
            second.wait('stopRecording', target='after-crash')
            for clip in (10, 11):
                second.send('begin', id=clip)
                assert second.wait('begin')['ok']
                second.send('finish', id=clip)
            second.send('quit')
            second.process.wait(timeout=5)
            third = Probe(sys.argv[1], env)
            probes.append(third)
            for clip in (1, 9, 10, 11):
                third.send('submit', id=clip)
            third.no_clips()
            print('PASS: exclusive playback, shared controls/mute/status, crash recovery, durable deduplication, exclusive microphone and pinned recording destination')
        finally:
            for probe in probes:
                probe.close()


if __name__ == '__main__':
    main()
