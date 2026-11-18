#!/usr/bin/env python3
"""Writes the vectors the Rust Host's reading of a request for a socket, and its command for an
agent's terminal, are tested against.

Some of what an app asks of a Host is a conversation held over a WebSocket: a terminal, a
stream of speech. Every such route of the Python Host first asks one pure function whether
the request asks for a socket at all (`server/lib/ws.py: is_websocket_upgrade`), with the
request's headers as each route reads them: their names in small letters, and of a name
that came twice the one that came last. This calls that function for every combination of
what the three headers it looks at may say, and for some requests that say a header twice
or in other letters, and writes what it answered to
`crates/clarp-kernel/tests/fixtures/socket-vectors.json`.

One of those conversations is a terminal on an agent's own tool, taken up on the conversation
the agent has with it. What the tool is started with is each backend's own word
(`server/lib/backend/*.py`, `terminal_argv`): asked here of every backend that Host has, for an
agent that has a session of its tool and for one that has none yet. A backend that has no way of
being spoken to in a terminal says so, and its words are written down. Where that Host adds the
directory of its own plugin for Claude Code, the directory is written as `<plugin>`: it is a path
of the machine the vectors were made on.

    python3 server-rs/tools/socket-vectors.py

Nothing of a Host is touched or started: pure functions are called.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'socket-vectors.json'

KEY = 'dGhlIHNhbXBsZSBub25jZQ=='
# What each header may say. `None`: the request does not have it.
UPGRADE = [None, 'websocket', 'WebSocket', ' websocket ', 'WEBSOCKET\t', '\x1fwebsocket\xa0', 'websocket, h2c',
           'h2c', '', 'web socket', 'websockets']
CONNECTION = [None, 'Upgrade', 'upgrade', ' UPGRADE ', 'keep-alive, Upgrade', 'keep-alive,upgrade',
              'upgrade , keep-alive', ',upgrade,', 'upgrade\xa0,close', 'keep-alive,\x1fupgrade\x1f', 'keep-alive',
              'close', '', 'Upgrade;x',
              'upgraded', 'up grade', 'keep-alive Upgrade']
KEYS = [None, KEY, ' k ', '', '   ', '\xa0', '\x1f']
# Requests that say a header twice, or in letters of their own.
ODD = [
    [['Upgrade', 'h2c'], ['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY]],
    [['Upgrade', 'websocket'], ['Upgrade', 'h2c'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY]],
    [['Upgrade', 'websocket'], ['Connection', 'keep-alive'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY]],
    [['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Connection', 'keep-alive'], ['Sec-WebSocket-Key', KEY]],
    [['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY], ['Sec-WebSocket-Key', '']],
    [['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', ''], ['Sec-WebSocket-Key', KEY]],
    [['UPGRADE', 'websocket'], ['CONNECTION', 'Upgrade'], ['SEC-WEBSOCKET-KEY', KEY]],
    [['upgrade', 'websocket'], ['connection', 'Upgrade'], ['sec-websocket-key', KEY]],
    [['uPgRaDe', 'websocket'], ['cOnNeCtIoN', 'Upgrade'], ['sEc-wEbSoCkEt-kEy', KEY]],
    [['Upgrade', 'websocket'], ['upgrade', 'h2c'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY]],
    [['Host', 'a-host'], ['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY],
     ['Sec-WebSocket-Version', '13']],
    [['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Key', KEY], ['Sec-WebSocket-Version', '8']],
    [['Upgrade', 'websocket'], ['Connection', 'Upgrade'], ['Sec-WebSocket-Accept', KEY]],
    [],
]


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib import ws

    def asks(pairs: list[list[str]]) -> bool:
        # As every route reads the headers before it asks.
        headers = {name.lower(): value for name, value in pairs}
        return bool(ws.is_websocket_upgrade(headers))

    table = ''
    for upgrade, connection, key in itertools.product(UPGRADE, CONNECTION, KEYS):
        said = [('Upgrade', upgrade), ('Connection', connection), ('Sec-WebSocket-Key', key)]
        table += '1' if asks([[name, value] for name, value in said if value is not None]) else '0'
    odd = [{'headers': pairs, 'asks': asks(pairs)} for pairs in ODD]

    from lib import backends
    from lib.backend.base import Unsupported
    terminals = []
    for adapter in backends.all_adapters():
        for session in ('', 'a-session-of-the-tool'):
            row = {'tool': adapter.id, 'session': session}
            try:
                argv = list(adapter.terminal_argv(session))
            except Unsupported as none:
                row['none'] = str(none)
            else:
                if '--plugin-dir' in argv:
                    argv[argv.index('--plugin-dir') + 1] = '<plugin>'
                row['program'], row['args'] = argv[0], argv[1:]
            terminals.append(row)
    vectors = {
        'what': 'Whether a request asks for a socket, by server/lib/ws.py is_websocket_upgrade. `table` has one '
                'sign for every combination of what the three headers say, in the order itertools.product goes '
                'through them as they are listed (the last changes fastest); null: the header is not there.',
        'upgrade': UPGRADE,
        'connection': CONNECTION,
        'key': KEYS,
        'table': table,
        'odd': odd,
        'terminals': terminals,
    }
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=True) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(table)} combinations, {table.count("1")} ask for a socket; '
          f'{len(odd)} odd requests, {sum(1 for case in odd if case["asks"])} ask; '
          f'{len(terminals)} terminals, {sum(1 for row in terminals if "none" in row)} of tools that have none')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
