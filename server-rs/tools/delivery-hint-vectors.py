#!/usr/bin/env python3
"""Writes the vectors the Rust Host's replay of a `delivery-updated` hint is tested against.

A `delivery-updated` event tells a client that a send's delivery may have changed, and nothing
more: on its way out of the store the Python Host keeps only its identity (`type`, `agent_id`,
`session`, `message_id`, `ts`, `event_id`) and leaves out a stored row whose identity is missing
or not a non-empty text (`persistence/sse_events._egress`); every other event leaves as it was
stored. This calls that function for stored hints of every shape and for other events, and
writes what came out to `crates/clarp-kernel/tests/fixtures/delivery-hint-vectors.json`.

    python3 server-rs/tools/delivery-hint-vectors.py

A pure function is called; nothing of a Host is started or written.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'delivery-hint-vectors.json'
SCRATCH = pathlib.Path()  # the scratch place main takes from tools/scratch.py


def isolate() -> None:
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


def stored() -> list[dict]:
    hint = {'type': 'delivery-updated', 'agent_id': 'ag', 'session': 's', 'message_id': 'u-m1',
            'ts': 1_794_700_000_000, 'event_id': 41}
    found = [hint, dict(hint, delivery={'state': 'stopped', 'rev': 2, 'seq': 4}), dict(hint, extra=1, trace_id='t1'),
             dict(hint, _clarp_runtime_event=True)]
    for key in ('agent_id', 'session', 'message_id'):
        for value in ('', None, 5, ['u-m1'], ' '):
            found.append(dict(hint, **{key: value}))
        found.append({k: v for k, v in hint.items() if k != key})
    found += [
        {'type': 'agent-state', 'agent_id': 'ag', 'session': 's', 'kind': 'done', 'ts': 1, 'event_id': 2,
         'delivery': {'state': 'x'}},
        {'type': 'queue-updated', 'agent_id': 'ag', 'session': 's', 'message_id': '', 'ts': 1, 'event_id': 3},
        {'type': 'delivery-updatedx', 'agent_id': 'ag', 'ts': 1, 'event_id': 4},
    ]
    return found


def main() -> int:
    global SCRATCH
    with tool_scratch.directory('clarp-delivery-hint-') as place:
        SCRATCH = pathlib.Path(place)
        return generate()


def generate() -> int:
    isolate()
    from lib.persistence import sse_events
    vectors = []
    for row in stored():
        for internal in (False, True):
            vectors.append({'stored': row, 'internal': internal, 'out': sse_events._egress(dict(row), internal)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/delivery-hint-vectors.py from server/lib/persistence/sse_events.py '
                 '(_egress). Do not edit by hand.',
        'vectors': vectors}, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    dropped = sum(1 for v in vectors if v['out'] is None)
    print(f'{OUT.relative_to(REPO)}: {len(vectors)} rows ({dropped} left out), {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
