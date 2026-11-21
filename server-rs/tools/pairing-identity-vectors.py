#!/usr/bin/env python3
"""Record the Python Host's pairing identity, separate budgets and revoke decisions."""
from __future__ import annotations

import importlib.util
from email.message import Message
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/pairing-identity-vectors.json'
FIRST = '192.0.2.10'
SECOND = '192.0.2.11'


@scratch.case('clarp-pairing-identity-vectors-')
def run(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (root / 'scenario-config.toml').write_text('')
    replay.isolate(root)
    replay.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import device_pairing, request_security, server_identity
    from lib.routes.providers import ProviderRoutes

    class Asking:
        client_address = ('127.0.0.1', 8000)

        def __init__(self):
            self.headers = Message()
            self.now = 0.0
            self.server = SimpleNamespace(
                identity_reads=request_security.FailureLimiter(limit=30, clock=lambda: self.now),
                auth_failures=request_security.FailureLimiter(clock=lambda: self.now),
            )
            self.data = {}
            self.who = SimpleNamespace(device_scope='', principal='')
            self.answer = {}

        def _read_json(self):
            return self.data

        def principal(self):
            return self.who

        def _auth_failure_retry(self):
            source = request_security.failure_source(self.client_address[0], self.headers)
            return self.server.auth_failures.failure(source)

        def _send(self, status, body, content_type, extra_headers):
            self.answer = {'status': status, 'body': json.loads(body),
                           'retry': int(extra_headers['Retry-After'])}

        def _json_ok(self, body):
            self.answer = {'status': 200, 'body': body, 'retry': 0}

        def _json_error(self, status, error):
            self.answer = {'status': status, 'body': {'error': error}, 'retry': 0}

    def failed_exchange(*args, **kwargs):
        raise device_pairing.PairingError('fixture rejected')

    def record(asking, kind, source, after=0):
        asking.now += after
        asking.headers.replace_header('X-Forwarded-For', source)
        handle = (ProviderRoutes._handle_pairing_identity if kind == 'identity'
                  else ProviderRoutes._handle_pairing_exchange)
        handle(asking)
        return dict(kind=kind, source=source, after=after, **asking.answer)

    settings = {server_identity._ID_KEY: '79f497f3-c51c-4de4-9422-6b9d621e9b71'}
    identity = []
    with patch.object(server_identity, 'get_text', settings.__getitem__), patch.dict(os.environ, {'CLARP_SERVER_NAME': ' Env Host '}),             patch.object(server_identity.socket, 'gethostname', return_value='fixture-hostname'):
        for name, env_name in [('  Fixture Host  ', ' Env Host '), ('  ', ' Env Host '), ('', '')]:
            settings[server_identity._NAME_KEY] = name
            os.environ['CLARP_SERVER_NAME'] = env_name
            identity.append(server_identity.identity())

    steps = []
    asking = Asking()
    asking.headers['X-Forwarded-For'] = FIRST
    with patch.object(server_identity, 'get_text', settings.__getitem__),             patch.object(server_identity.socket, 'gethostname', return_value='fixture-hostname'),             patch.object(device_pairing, 'exchange', failed_exchange):
        for _ in range(31):
            steps.append(record(asking, 'identity', FIRST))
        for _ in range(21):
            steps.append(record(asking, 'exchange', FIRST))
        steps.append(record(asking, 'identity', SECOND))
        for _ in range(21):
            steps.append(record(asking, 'exchange', SECOND))
        for _ in range(30):
            steps.append(record(asking, 'identity', SECOND))
        steps.append(record(asking, 'identity', FIRST, after=59.5))
        steps.append(record(asking, 'identity', FIRST, after=0.5))

    revokes = []
    for scope, own, requested in [
        ('limited', 'device-a', 'device-a'),
        ('limited', 'device-a', '\x1c device-a \u00a0'),
        ('limited', 'device-a', 'DEVICE-A'),
        ('limited', 'device-a', 'device-a/'),
        ('limited', 'device-a', 'device-b'),
        ('limited', 'device-a', 'device-missing'),
        ('limited', 'device-a', ' \x1f '),
        ('limited', 'device-a', ''),
        ('limited', 'device-missing', 'device-missing'),
        ('full', 'device-a', ' device-b '),
        ('full', 'device-a', 'device-missing'),
        ('', '', 'device-b'),
    ]:
        asking = Asking()
        asking.who = SimpleNamespace(device_scope=scope, principal=own)
        asking.data = {'device_id': requested}
        attempted = []

        def revoke(target):
            attempted.append(target)
            return target in {'device-a', 'device-b'}

        asking.server.disconnect_device = lambda _target: None
        with patch.object(device_pairing, 'revoke', revoke):
            ProviderRoutes._handle_paired_device_revoke(asking)
        revokes.append(dict(scope=scope, own=own, requested=requested, attempted=attempted, **asking.answer))
    return {'identity': identity, 'steps': steps, 'revokes': revokes}


if __name__ == '__main__':
    if sys.argv[1:] == ['--run']:
        print(json.dumps(run()))
        raise SystemExit(0)
    child = subprocess.run([sys.executable, '-I', __file__, '--run'], check=False, capture_output=True,
                           text=True, env=scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}))
    if child.returncode:
        print(child.stderr[-3000:], file=sys.stderr)
        raise SystemExit(child.returncode)
    vectors = json.loads(child.stdout.splitlines()[-1])
    sections = []
    for key, rows in vectors.items():
        sections.append(f' "{key}": [\n' + ',\n'.join('  ' + json.dumps(row) for row in rows) + '\n ]')
    OUT.write_text('{\n' + ',\n'.join(sections) + '\n}\n', encoding='ascii')
    print(', '.join(f'{len(rows)} {key}' for key, rows in vectors.items()))
