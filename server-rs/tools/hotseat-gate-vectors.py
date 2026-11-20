#!/usr/bin/env python3
"""Writes down what the Python Host answers at the doors of the account seats before its desk is asked anything.

`GET /hotseat/desk`, `POST /hotseat/switch` and `POST /hotseat/resume` first ask whether the owner turned the desk
off (`[hotseat] desk = false`: the plain 404 of any unknown route), then whether the desk is there (503), then, for
the two that change something, whether the body is a JSON object (400). Any other action under /hotseat/ is no route
(the dispatcher's 404). The handlers are the Python Host's own (server/server.py, lib/routes/registry.py), run here
with a desk that only writes down what it was asked: never the owner's tool, a provider's tool or a login.

    <the Python Host's interpreter> server-rs/tools/hotseat-gate-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'hotseat-gate-vectors.json'
BODIES = ('{"provider": "claude", "alias": "seat-b"}', '{"id": "stop-1", "kind": "clarp", "action": "continue"}',
          '{}', '', '[1]', '"text"', 'not json', '{"provider": 7, "alias": null}')
DOORS = (('GET', '/hotseat/desk'), ('POST', '/hotseat/switch'), ('POST', '/hotseat/resume'),
         ('POST', '/hotseat/renew'), ('GET', '/hotseat/renew'), ('POST', '/hotseat/'), ('POST', '/hotseat/switch/x'))


class Desk:
    """A stand-in desk: it writes down what it is asked and answers 200 with that."""
    def __init__(self):
        self.asked = None

    def payload(self, refresh=False):
        self.asked = {'payload': {'refresh': refresh}}
        return self.asked

    def switch_default(self, provider, alias):
        self.asked = {'switch_default': [provider, alias]}
        return 200, self.asked

    def resume_work(self, ident, *, kind, backend, action, **_):
        self.asked = {'resume_work': [ident, kind, backend, action]}
        return 200, self.asked


@tool_scratch.case('clarp-hotseat-gate-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import server
    from lib import server_identity
    from lib.routes import registry

    class Asked:
        """What a handler reads of a request and writes of its answer."""
        def __init__(self, body: str, desk, method: str):
            self.body, self.answer = body, None
            self.ctx = type('Ctx', (), {'hotseat_desk': desk})()
            # What the dispatcher reads of the request before its route (request_body.refused):
            # a change says the length its body has; a read sends no body and says nothing of one.
            self.headers = {'Content-Length': str(len(body.encode()))} if method != 'GET' else {}

        def _read_json(self):
            raw = self.body.encode()
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return None

        def _query(self):
            return {}

        def _send(self, code, body=b'', content_type='text/plain', extra_headers=None):
            self.answer = {'status': code, 'type': content_type, 'body': body.decode()}

        def _json(self, code, payload):
            self._send(code, json.dumps(payload).encode(), 'application/json')

        def _json_error(self, code, message):
            self._json(code, {'error': message})

        def _json_ok(self, payload):
            self._json(200, payload)

        def __getattr__(self, name):
            # Every other handler of the Host is the Host's own.
            found = getattr(server.Handler, name)
            return found.__get__(self) if callable(found) and hasattr(found, '__get__') else found

    cases = []
    for enabled in (True, False):
        server_identity.hotseat_desk_enabled = (lambda on: lambda: on)(enabled)
        for there in (True, False):
            for method, path in DOORS:
                for body in BODIES if method == 'POST' else ('',):
                    desk = Desk() if there else None
                    asked = Asked(body, desk, method)
                    registry.dispatch(asked, method, path)
                    answer = asked.answer or {'status': None}
                    if answer.get('type') == 'application/json':
                        answer['body'] = json.loads(answer['body'])
                    cases.append({'desk_on': enabled, 'desk_there': there, 'method': method, 'path': path,
                                  'body': body, 'answer': answer, 'desk_asked': desk.asked if desk else None})
    return {'what': 'server/server.py: _hotseat_desk, _handle_hotseat_desk, _handle_hotseat_switch, '
                    '_handle_hotseat_resume, through lib/routes/registry.dispatch', 'cases': cases}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        sys.stdout.write(json.dumps(run()) + '\n')
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    cases = said['cases']
    lines = ['{', f' "what": {json.dumps(said["what"])},', ' "cases": [']
    lines.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(cases) - 1 else '')
                 for n, one in enumerate(cases))
    lines += [' ]', '}']
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(cases)} requests')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
