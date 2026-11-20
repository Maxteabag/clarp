#!/usr/bin/env python3
"""Writes down which actions the Python Host has a route for under four doors of changes this Host has not built.

`POST /backend-auth/<action>`, `/harness-providers/<action>`, `/agent-goal/<action>` and `/oracle/<rest>`: an action the
Python Host registers is a route there (this Host answers it 501, not built); anything else is no route, the
dispatcher's plain 404. Asked of the Python Host's own dispatcher (lib/routes/registry.dispatch with its exact table
`Handler._POST_ROUTES`), with a handler that answers nothing itself: a route that is found only writes down its name.

    <the Python Host's interpreter> server-rs/tools/action-doors-vectors.py

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
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'action-doors-vectors.json'
DOORS = ('/backend-auth', '/harness-providers', '/agent-goal', '/oracle')
# Words that are an action somewhere, and words that are none: each is asked under every door.
WORDS = ('login', 'login-code', 'logout', 'pause', 'resume', 'clear', 'start', 'models', 'status', 'renew',
         'calls', 'calls/close', 'calls/status', 'v2', 'v2/calls', 'v2/calls/close', 'v2/calls/control',
         'v2/calls/status', 'contact', 'voice-provider', 'connect', 'handoff', 'handoffs', 'handoffs/ack',
         'delegations', 'delegations/ack', 'delegations/cancel', 'realtime', 'nothing-there')


def probes():
    for door in DOORS:
        yield door
        yield door + '/'
        for word in WORDS:
            yield f'{door}/{word}'
            yield f'{door}/{word}/'
            yield f'{door}/{word}/x'
            yield f'{door}/{word.upper()}'
        yield f'{door}/log%69n'
        yield f'{door}//login'


@tool_scratch.case('clarp-action-doors-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import server
    from lib.routes import registry

    class Asked:
        """A request whose route, where it has one, only writes down its name."""
        def __init__(self):
            self.answer = None
            # What the dispatcher reads of the request before its route (request_body.refused):
            # a change with no body, as a client sends one, says its length is nothing.
            self.headers = {'Content-Length': '0'}

        def _read_json(self):
            return {}

        def _send(self, code, body=b'', content_type='text/plain', extra_headers=None):
            self.answer = {'status': code, 'body': body.decode()}

        def _dispatch_exact(self, routes, path):
            return server.Handler._dispatch_exact(self, routes, path)

        def __getattr__(self, name):
            if name.startswith('_handle'):
                def found():
                    self.answer = {'route': name}
                return found
            if name.startswith('_') and name.endswith('_ROUTES'):
                return getattr(server.Handler, name)
            raise AttributeError(name)

    cases = []
    seen = set()
    for path in probes():
        if path in seen:
            continue
        seen.add(path)
        asked = Asked()
        registry.dispatch(asked, 'POST', path)
        cases.append({'path': path, 'route': (asked.answer or {}).get('route'),
                      'status': (asked.answer or {}).get('status')})
    return {'what': 'lib/routes/registry.dispatch, POST, with server.Handler._POST_ROUTES', 'cases': cases}


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
