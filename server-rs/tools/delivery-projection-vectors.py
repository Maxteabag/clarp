#!/usr/bin/env python3
"""Writes the vectors the Rust Host's projection of a send's delivery is tested against.

What a client is shown of a user send (`delivery: {state, rev, seq, for}`, feature
`send_delivery_state`) is the Python Host's `launch_receipts.project`: a receipt and the agent's
user Stop generation in, the object or nothing out. This calls it for receipts of every state and
of every malformed shape its checks tell apart, at generations below, at and above the one a
receipt was admitted with, with and without the origin and sender a row gives, and writes what it
answered to `crates/clarp-kernel/tests/fixtures/delivery-projection-vectors.json`.

    python3 server-rs/tools/delivery-projection-vectors.py

A pure function is called; nothing of a Host is started or written.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'delivery-projection-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-delivery-projection-vectors')


def isolate() -> None:
    """Points the library at a place of its own before it is imported."""
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


def receipts() -> list:
    """Receipts as the store may hold them: each state, and each way one is not to be shown."""
    base = {'state': 'pending', 'agent_id': 'ag', 'trace_id': 't1', 'client_msg_id': 'm1', 'origin': 'user',
            'sender_agent_id': '', 'admitted_at': 1_000, 'user_stop_gen': 1, 'rev': 2, 'seq': 7}
    found: list = [None, [], 'pending', 3]
    for state in ('pending', 'attempted', 'fenced', 'relaunched', 'started', '', None):
        found.append(dict(base, state=state))
    found.append({k: v for k, v in base.items() if k != 'state'})
    for key, values in {
        'rev': (0, -1, 1, 9, None, '2', True, 2.0),
        'seq': (0, -3, 1, 40, None, '7', False, 7.5),
        'agent_id': ('other', '', None),
        'client_msg_id': ('', None, 5, 'u-m1', ' m1 '),
        'trace_id': ('', None, 7, 't2'),
        'origin': ('agent', '', None),
        'sender_agent_id': ('ag2', None, 0, False, []),
        'user_stop_gen': (0, 2, -1, True, '1', 1.0, None),
    }.items():
        for value in values:
            found.append(dict(base, **{key: value}))
    for key in ('rev', 'seq', 'agent_id', 'client_msg_id', 'trace_id', 'origin', 'sender_agent_id', 'user_stop_gen'):
        found.append({k: v for k, v in base.items() if k != key})
    for state in ('attempted', 'fenced'):
        for gen in (None, '1', 0, 5):
            found.append(dict(base, state=state, user_stop_gen=gen))
    return found


def main() -> int:
    isolate()
    from lib import launch_receipts as lr
    vectors = []
    # Each receipt at generations below, at and above its own, with no row (a feed's update),
    # with a user's row and with rows of another origin or sender; and once for another agent.
    rows = ((None, None), ('user', ''), ('user', None), ('agent', None), (None, 'ag2'))
    asked = [(receipt, 'ag', generation, origin, sender)
             for receipt in receipts() for generation in (0, 1, 2) for origin, sender in rows]
    asked += [(receipt, 'other', 1, None, None) for receipt in receipts()]
    for receipt, agent, generation, origin, sender in asked:
        shown = lr.project(receipt, agent_id=agent, user_stop_generation=generation,
                           origin=origin, sender_agent_id=sender)
        vectors.append({'receipt': receipt, 'agent_id': agent, 'generation': generation,
                        'origin': origin, 'sender_agent_id': sender, 'shown': shown})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    states = {}
    for vector in vectors:
        key = vector['shown']['state'] if vector['shown'] else 'unknown'
        states[key] = states.get(key, 0) + 1
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/delivery-projection-vectors.py from server/lib/launch_receipts.py '
                 '(project). Do not edit by hand.',
        'states': [lr.PENDING_LAUNCH, lr.STOPPED, lr.HANDED_OVER],
        'vectors': vectors}, ensure_ascii=False, sort_keys=True) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(vectors)} projections {dict(sorted(states.items()))}, '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
