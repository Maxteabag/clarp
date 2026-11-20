#!/usr/bin/env python3
"""Record the Shortcut doors with the reference reader, event builder and access rule."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/remote-action.json'
NOW = 1234000
DRAW = 'aMadeUpFixtureId'


def record(tmp):
    replay = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    replay['isolate'](tmp)
    replay['loopback_only']()
    sys.path.insert(0, str(ROOT / 'server'))
    from server import Handler
    from lib import controller_events as controls
    from lib.protocol import ClientAction

    class Door:
        _read_json = Handler._read_json
        _read_json_once = Handler._read_json_once
        _LENGTH = Handler._LENGTH
        _query = Handler._query
        _json_ok = Handler._json_ok
        _json_error = Handler._json_error
        _json = Handler._json
        _DEVICE_FULL_ONLY_PREFIXES = Handler._DEVICE_FULL_ONLY_PREFIXES
        _LIMITED_DEVICE_POST_EXACT = Handler._LIMITED_DEVICE_POST_EXACT
        _request_device_scope = 'limited'

        def _send(self, status, body, mime):
            self.reply = [status, json.loads(body) if mime == 'application/json' else body.decode()]

    rows = []
    def ask(method, value):
        door = Door()
        sent = []
        door.ctx = SimpleNamespace(stream=SimpleNamespace(broadcast_ephemeral=sent.append))
        door.path = '/remote-action' + ('?' + value if method == 'GET' else '')
        raw = value.encode()
        door.headers = {'Content-Length': str(len(raw))}
        door.rfile = io.BytesIO(raw)
        handler = Handler._handle_remote_action if method == 'POST' else Handler._handle_remote_action_get
        handler(door)
        rows.append([method, value, *door.reply, sent])

    with ExitStack() as stack:
        stack.enter_context(patch('lib.routes.ops.log'))
        stack.enter_context(patch("server.log"))
        stack.enter_context(patch("server.log_exception"))
        stack.enter_context(patch('lib.routes.ops.time', SimpleNamespace(time=lambda: NOW / 1000)))
        stack.enter_context(patch.object(controls, '_now_ms', return_value=NOW))
        stack.enter_context(patch.object(controls.secrets, 'token_urlsafe', side_effect=lambda n: DRAW if n == 12 else 'unexpected'))
        for raw in ['', '{', 'null', 'false', '1', '"record"', '[]', '{}', '{"action":false}', '{"action":0}']:
            ask('POST', raw)
        for action in sorted(ClientAction.valid()) + ['unknown', ' RECORD ', 'rEcOrD-ToGgLe']:
            ask('GET', 'action=' + action)
            ask('POST', json.dumps({'action':action, 'ts':1, 'queued':'ignored'}))
        for query in ['', 'act=record', 'action=&action=record', 'action=bad&action=record',
                      'action=record&action=stop-agent', 'action=+STOP-AGENT+', '%61ction=%72ecord']:
            ask('GET', query)
        base = dict(action='controller-event', button='primary', controller_event='hold')
        for button in sorted(controls.CONTROLLER_BUTTONS):
            for gesture in sorted(controls.CONTROLLER_EVENTS):
                ask('POST', json.dumps(dict(base, button=button, controller_event=gesture)))
        for field, limit in [('button',16), ('controller_event',32), ('controller_id',128), ('controller_event_id',128)]:
            for value in [None, False, 0, 7, True, ['x'], {'k':'v'}, '', '  ', ' primary ', ' hold ', 'other',
                          chr(0x1f642)*limit, chr(0x1f642)*(limit+1)]:
                ask('POST', json.dumps(dict(base, **{field:value})))
        for field, maximum in [('duration_ms',120000), ('age_ms',120000), ('ts',9999999999999)]:
            for value in [None, True, False, '3', 3.0, -1, 0, 1, maximum-1, maximum, maximum+1]:
                ask('POST', json.dumps(dict(base, **{field:value})))
        for queued in [True, False, None, 'true', 1, []]:
            ask('POST', json.dumps(dict(base, queued=queued)))
        for field in ['button', 'controller_event']:
            ask('POST', json.dumps({k:v for k,v in base.items() if k != field}))
    return dict(now=NOW, draw=DRAW, rows=rows,
                limited={m:Handler._device_forbidden(Door(), '/remote-action', m) for m in ['GET','POST']})


if __name__ == '__main__':
    if '--child' in sys.argv:
        with scratch.directory('remote-action-') as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        output = subprocess.check_output([sys.executable, '-I', __file__, '--child'],
                                         env=scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}), text=True)
        OUT.write_text(json.dumps(json.loads(output.splitlines()[-1]), indent=2) + '\n')
