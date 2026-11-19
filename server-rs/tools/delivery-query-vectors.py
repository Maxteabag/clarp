#!/usr/bin/env python3
"""Writes the vectors the Rust Host's reading of a `GET /log` delivery query is tested against.

The Python Host adds the delivery fields to a `GET /log` answer in `Handler._add_delivery_state`
(server/server.py): it checks the query's `delivery_*` parameters (a 400 in its own words for
one that is not plain ASCII digits), reads `delivery_bootstrap` and the runtime scope a client
carries, and, owning its turns itself (no external runtime: the in-process mode, which is the
Rust Host's), puts the page's rows, the scope, the generation and a page of the feed into the
answer, expiring a cursor taken under another scope. This calls that method with a stand-in
handler on a store of its own, for every way the parameters can be written, and writes the
store the answer was made from, the query and what the method answered and added, to
`crates/clarp-kernel/tests/fixtures/delivery-query-vectors.json`.

    python3 server-rs/tools/delivery-query-vectors.py

Nothing of a Host is started; the library writes only under /var/tmp.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import shutil
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'delivery-query-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-delivery-query-vectors')
T0 = 1_794_700_000_000
SERVER_ID, INSTANCE = 'host-1', 'instance-1'


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
    for name in ('CLAUDE_PWA_SESSION', 'CLARP_PROVIDER_TURN', 'CLARP_BACKGROUND_ORIGIN'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(REPO / 'server'))


NUMBERS = ['', '0', '1', '3', '7', '99', ' 4 ', '-1', '+3', '3.0', 'x', '²', '٣', '１', '1e3', '0x1',
           '00', '18446744073709551616']


def queries() -> list[dict]:
    """Each parameter written every way, alone and with what a client sends beside it."""
    asked: list[dict] = [{}]
    for name in ('delivery_since', 'delivery_until', 'delivery_stop_generation'):
        for raw in NUMBERS:
            asked.append({name: raw})
            asked.append({'delivery_since': '0', name: raw} if name != 'delivery_since' else {name: raw,
                                                                                            'delivery_stop_generation': '1'})
    for raw in ('1', 'true', 'TRUE', 'yes', '0', '', ' 1 ', 'false'):
        asked.append({'delivery_bootstrap': raw})
        asked.append({'delivery_bootstrap': raw, 'delivery_since': '0'})
        asked.append({'delivery_bootstrap': raw, 'delivery_since': '2', 'delivery_until': '4',
                      'delivery_stop_generation': '1'})
        asked.append({'delivery_bootstrap': raw, 'delivery_since': '2', 'delivery_until': '4',
                      'delivery_stop_generation': '1', 'delivery_runtime_release': 'in-process',
                      'delivery_runtime_instance': INSTANCE})
    scopes = [('in-process', INSTANCE), ('in-process', 'another'), ('other', INSTANCE), ('', ''), (None, None),
              (' in-process ', f' {INSTANCE} ')]
    for release, instance in scopes:
        carried = {k: v for k, v in (('delivery_runtime_release', release),
                                     ('delivery_runtime_instance', instance)) if v is not None}
        asked.append({'delivery_since': '2', 'delivery_stop_generation': '1', **carried})
        asked.append({'delivery_since': '0', **carried})
        asked.append({'delivery_since': '0', 'delivery_stop_generation': '1', **carried})
        asked.append({'delivery_bootstrap': '1', 'delivery_since': '0', **carried})
        asked.append({'delivery_bootstrap': '1', 'delivery_since': '2', 'delivery_until': '4',
                      'delivery_stop_generation': '1', **carried})
    asked.append({'delivery_since': '1', 'delivery_until': '9', 'delivery_stop_generation': '1',
                  'delivery_runtime_release': 'in-process', 'delivery_runtime_instance': INSTANCE})
    return asked


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    isolate()
    import server
    from lib import clock, db, launch_receipts as lr, server_identity, settings_store as store, turn_queue
    now = [T0]
    for module in [clock, db, *sys.modules.values()]:
        if getattr(module, '__name__', '').startswith('lib.') and hasattr(module, 'now_ms'):
            module.now_ms = lambda: now[0]
    db.reset_for_tests(SCRATCH / 'query.sqlite')
    lr.reset_for_tests()
    lr.IN_PROCESS_INSTANCE = INSTANCE
    server_identity.server_id = lambda: SERVER_ID
    server.agents_db.get_by_session = lambda session: {'agent_id': 'ag', 'session': 's'} if session == 's' else None

    def send(trace: str, cmid: str) -> None:
        now[0] += 1000
        lr.record_pending(agent_id='ag', trace_id=trace, client_msg_id=cmid, origin='user',
                          user_stop_generation=turn_queue.user_stop_generation('ag'))

    send('t1', 'm1')
    now[0] += 1000
    lr.mark_attempted('t1')
    send('t2', 'm2')
    now[0] += 1000
    turn_queue.set_paused('ag', True, user_stop=True)
    lr.note_stop('ag')
    lr.fence_agent('ag')
    send('t3', 'm3')
    send('t4', 'm4')

    prefix = f'{lr.POINTER_PREFIX}ag:'
    pointers = store.items_in_range(prefix, prefix[:-1] + ';', limit=100_000)
    raws = store.get_many([lr._key(trace) for _key, trace in pointers])
    given = {'pointers': [[lr._pointer_seq(key), trace] for key, trace in pointers],
             'receipts': {trace: raws.get(lr._key(trace)) for _key, trace in pointers},
             'current': store.get_int(lr.SEQ_KEY), 'watermark': store.get_int(lr.PRUNE_SEQ_KEY),
             'generation': turn_queue.user_stop_generation('ag'),
             'server_id': SERVER_ID, 'agent_id': 'ag', 'release': lr.IN_PROCESS_RELEASE, 'instance': INSTANCE}
    body = {'session': 's', 'turns': [
        {'role': 'user', 'id': 'u-m1', 'trace_id': 't1'}, {'role': 'assistant', 'id': 'r-1', 'trace_id': 't1'},
        {'role': 'user', 'id': 'u-m2', 'trace_id': 't2'}, {'role': 'user', 'id': 'u-m3', 'trace_id': 't3'},
        {'role': 'user', 'id': 'u-m9', 'trace_id': 't9'}]}

    handler = types.SimpleNamespace(ctx=types.SimpleNamespace(runtime_client=None))
    for name in ('_delivery_fields', '_delivery_bracket', '_first_observation', '_delivery_observation'):
        setattr(handler, name, getattr(server.Handler, name).__get__(handler))

    vectors = []
    for session in ('s', 'nobody'):
        for qs in (queries() if session == 's' else [{}, {'delivery_since': 'x'}]):
            answer = copy.deepcopy(body)
            said = server.Handler._add_delivery_state(handler, answer, session,
                                                       {k: [v] for k, v in qs.items()})
            vectors.append({'session': session, 'query': qs, 'said': said, 'answer': answer})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/delivery-query-vectors.py from server/server.py '
                 '(Handler._add_delivery_state, its bracket in the in-process mode). Do not edit by hand.',
        'given': given, 'body': body, 'vectors': vectors}, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    refused = sum(1 for v in vectors if v['said'])
    paged = sum(1 for v in vectors if 'delivery_updates' in v['answer'])
    expired = sum(1 for v in vectors if v['answer'].get('delivery_cursor_expired'))
    print(f'{OUT.relative_to(REPO)}: {len(vectors)} queries ({refused} refused, {paged} with a page, {expired} expired), '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
