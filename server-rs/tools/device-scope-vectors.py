#!/usr/bin/env python3
"""Writes down which requests the Python Host refuses a device that pairs with limited access.

A device paired with limited access may read and send at named routes and revoke its own pairing.
The fixture runs the Python Host's `Handler._device_forbidden` for exact paths, families, their neighbors,
and the methods the handlers pass into it. It also records the limited `/server-info` projection.

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
    for exact in handler._LIMITED_DEVICE_GET_EXACT:
        out.update({exact, exact + '/', exact + '/x', exact + 'x'})
    for prefix in handler._LIMITED_DEVICE_GET_PREFIXES:
        bare = prefix.rstrip('/')
        out.update({bare, bare + '/', bare + '/x', bare + 'x', bare[:-1]})
    out.update({'/artifacts/a/export', '/artifacts/a/export/', '/artifacts/a/export//',
                '/artifacts/a/exported', '/artifacts/a/export/x', '/remote-action',
                '/agent-files', '/a-path-no-route-serves', '/%73erver-info',
                '/media%2Fx', '/media/%78', '/artifacts/a/ex%70ort'})
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
    from server import Handler
    from lib.routes.settings import limited_server_info

    cases = []
    for scope in ('limited', 'full', ''):
        handler = Handler.__new__(Handler)
        handler._request_device_scope = scope
        for path in paths(handler):
            for method in METHODS:
                cases.append({'scope': scope, 'method': method, 'path': path,
                              'refused': handler._device_forbidden(path, method)})

    connection = {'enabled': True, 'port': 7683, 'certificate_sha256': 'ab',
                  'service_type': '_clarps._tcp', 'urls': ['https://192.0.2.1:7683'],
                  'future_field': 'private'}
    info = {'server_id': 'host-id', 'name': 'Test Host', 'deployment_mode': 'native',
            'version': '1', 'image_version': '', 'clarp_version': '1', 'min_app_version': '1',
            'capabilities': {'version': 1, 'features': []}, 'contract': {'host': 42},
            'client': {'status': 'unknown'}, 'default_cwd': '/synthetic/home',
            'future_field': 'private'}
    projections = [
        {},
        info,
        {**info, 'local_connection': connection},
        {**info, 'local_connection': {**connection, 'error': 'bind failed on /synthetic/socket'}},
        {**info, 'local_connection': {**connection, 'error': ''}},
        {**info, 'local_connection': None},
    ]
    return {'what': 'server/server.py: Handler._device_forbidden, as do_GET, do_POST, do_PUT, do_DELETE and do_PATCH '
                    'ask it; the refusal is 403 {"error": "full device access required"}',
            'cases': cases,
            'server_info': [{'full': full, 'limited': limited_server_info(full)} for full in projections]}



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
    lines += [' ],', ' "server_info": [']
    projections = said['server_info']
    lines.extend('  ' + json.dumps(one) + (',' if n < len(projections) - 1 else '')
                 for n, one in enumerate(projections))
    lines += [' ]', '}']
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    refused = sum(1 for one in cases if one['refused'])
    print(f'{OUT.relative_to(REPO)}: {len(cases)} requests, {refused} refused')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
