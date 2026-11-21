#!/usr/bin/env python3
"""Writes down which requests the Python Host refuses a device that pairs with limited access.

A device a user paired with limited access (a watch) may read, send, and ask to revoke its own pairing. The rule is the Python
Host's `Handler._device_forbidden` (server/server.py), with `do_PATCH`'s own narrowing (a PATCH is looked at only
under /schedules/), run here as it is for every prefix of its lists, every route it lets through, the ones next to
them, and every method a request may have. A refusal is 403 {"error": "full device access required"}.

    <the Python Host's interpreter> server-rs/tools/device-scope-vectors.py

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
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'device-scope-vectors.json'
METHODS = ('GET', 'POST', 'PUT', 'DELETE', 'PATCH')


def paths(handler) -> list:
    """Every path whose answer the rule decides, and some it does not."""
    out = set()
    for prefix in handler._DEVICE_FULL_ONLY_PREFIXES:
        bare = prefix.rstrip('/')
        out.update({bare, bare + '/', bare + '/x', bare + 'x', bare[:-1]})
    for exact in handler._LIMITED_DEVICE_POST_EXACT:
        out.update({exact, exact + '/', exact + '/x', exact + 'x'})
    out.update({'/launch-directories', '/launch-directories/x', '/favorite-paths', '/favorite-paths/x'})
    out.update({'/dirs', '/dirs/x', '/past-sessions', '/past-sessions/x', '/terminal', '/terminal/', '/terminal/echo',
                '/terminal/echo/x', '/controller-actions', '/controller-actions/', '/controller-actions/a',
                '/controller-actions/a/invoke', '/controller-actions/a%2Fb/invoke', '/controller-actions//invoke',
                '/controller-actions/a/b/invoke', '/controller-actions/audit', '/controller-actionsx',
                '/schedules', '/schedules/', '/schedules/s1', '/agent-schedules/s1', '/log', '/agents/snapshot',
                '/agents', '/agents/echo', '/stop', '/send-delayed', '/pairing/issue', '/pairing/exchange',
                '/paired-devices/revoke', '/paired-devices/revoke/', '/paired-devices/revokex',
                '/devices', '/devices/push-tokens', '/devices/live-activity', '/artifacts', '/artifacts/a/html',
                '/artifacts/a/submit', '/decisions/d/resolve', '/attention/inbox', '/server-info', '/status',
                '/', '/x', '/application-activity', '/desktop-presence', '/clips/ack/x', '/hotseat/status',
                '/hotseat/other'})
    return sorted(out)


@tool_scratch.case('clarp-device-scope-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import server

    class Asking:
        """What the rule reads of a request: its device's scope, and the Host's lists."""
        _DEVICE_FULL_ONLY_PREFIXES = server.Handler._DEVICE_FULL_ONLY_PREFIXES
        _LIMITED_DEVICE_POST_EXACT = server.Handler._LIMITED_DEVICE_POST_EXACT

        def __init__(self, scope):
            self._request_device_scope = scope

    cases = []
    for scope in ('limited', 'full', ''):
        asking = Asking(scope)
        for path in paths(asking):
            for method in METHODS:
                refused = server.Handler._device_forbidden(asking, path, method)
                if method == 'PATCH':
                    # do_PATCH asks the rule only under /schedules/: any other PATCH keeps its 404.
                    refused = path.startswith('/schedules/') and refused
                cases.append({'scope': scope, 'method': method, 'path': path, 'refused': refused})
    return {'what': 'server/server.py: Handler._device_forbidden, as do_GET, do_POST, do_PUT, do_DELETE and do_PATCH '
                    'ask it; the refusal is 403 {"error": "full device access required"}',
            'cases': cases}


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
    lines = ['{', f' "what": {json.dumps(said["what"])},', ' "cases": [']
    cases = said['cases']
    lines.extend('  ' + json.dumps(one) + (',' if n < len(cases) - 1 else '') for n, one in enumerate(cases))
    lines += [' ]', '}']
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    refused = sum(1 for one in cases if one['refused'])
    print(f'{OUT.relative_to(REPO)}: {len(cases)} requests, {refused} refused')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
