#!/usr/bin/env python3
"""Writes down which chat the Python Host takes a recording to be for, and what it keeps of the chat each client opened.

`lib/client_chat.py` (`record`, `current`: the chat a client last opened by `/select` or `/focus`, kept twelve hours
under the digest of who it is) and `Handler._transcription_session` (the session a client names in
`X-Transcription-Session` where the Host has that agent, else the chat it last opened, else nothing: the shared focus),
run here with a settings store and a clock that only write down, and a stand-in for the Host's agents.

    <the Python Host's interpreter> server-rs/tools/client-chat-vectors.py

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
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'client-chat-vectors.json'
HOUR = 3600 * 1000
PRINCIPALS = ('administrator', 'dev-0a1b2c', 'Æøå-device', '')
SESSIONS = ('mike', 'rachel-7f3a', 'Zoë', 'nobody-here', ' mike ', '')
KNOWN = {'mike', 'rachel-7f3a', 'Zoë'}
KEPT = ('', '{}', '[]', 'not json', '{"session":"mike","at":1000}', '{"session":"mike","at":"1000"}',
        '{"session":"mike","at":1000.9}', '{"session":"mike","at":true}', '{"session":"mike","at":null}',
        '{"session":7,"at":1000}', '{"session":"","at":1000}', '{"session":"mike"}', '{"at":1000}',
        '{"session":"mike","at":"soon"}')
NOWS = (1000, 1000 + 12 * HOUR, 1000 + 12 * HOUR + 1, 1000 + 13 * HOUR, 0)


@tool_scratch.case('clarp-client-chat-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import server
    from lib import client_chat

    store: dict[str, str] = {}
    clock = {'now': 0}
    client_chat.settings_store = type('Store', (), {
        'set_text': staticmethod(lambda key, value: store.__setitem__(key, value)),
        'get_text': staticmethod(lambda key: store.get(key)),
    })
    client_chat.db = type('Db', (), {'now_ms': staticmethod(lambda: clock['now'])})
    client_chat.log_exception = lambda *a, **k: None

    recorded = []
    for principal in PRINCIPALS:
        for session in ('mike', 'Zoë', ''):
            for at in (1000, 5):
                store.clear()
                clock['now'] = at
                client_chat.record(principal, session)
                recorded.append({'principal': principal, 'session': session, 'at': at,
                                 'key': client_chat._key(principal) if principal else None,
                                 'kept': store.get(client_chat._key(principal)) if principal else None})
    read = []
    for kept in KEPT:
        for now in NOWS:
            store.clear()
            store[client_chat._key('dev-0a1b2c')] = kept
            clock['now'] = now
            try:
                said = client_chat.current('dev-0a1b2c')
            except Exception as error:  # what the reference cannot read at all
                said = {'raised': type(error).__name__}
            read.append({'kept': kept, 'now': now, 'current': said})

    lookup = server.identity.lookup
    server.identity.lookup = lambda name: {'session': name} if name in KNOWN else None
    chosen = []

    class Asked:
        def __init__(self, header, last):
            self.headers = {} if header is None else {'X-Transcription-Session': header}
            self._last = last

        def principal(self):
            return type('P', (), {'principal': 'dev-0a1b2c'})()

    for header in (None, '', '  ', 'mike', ' mike ', 'rachel-7f3a', 'Zoë', 'nobody-here'):
        for last in ('', 'mike', 'Zoë', 'nobody-here'):
            client_chat.current = (lambda value: lambda principal: value)(last)
            said = server.Handler._transcription_session(Asked(header, last))
            chosen.append({'header': header, 'last': last, 'session': said})
    server.identity.lookup = lookup
    return {'what': 'server/lib/client_chat.py record, current; server.py Handler._transcription_session',
            'known': sorted(KNOWN), 'recorded': recorded, 'read': read, 'chosen': chosen}


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
    lines = ['{', f' "what": {json.dumps(said["what"])},', f' "known": {json.dumps(said["known"], ensure_ascii=True)},']
    for part in ('recorded', 'read', 'chosen'):
        rows = said[part]
        lines.append(f' "{part}": [')
        lines.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(rows) - 1 else '')
                     for n, one in enumerate(rows))
        lines.append(' ],' if part != 'chosen' else ' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(said["recorded"])} kept, {len(said["read"])} read, {len(said["chosen"])} chosen')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
