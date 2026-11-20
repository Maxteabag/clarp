#!/usr/bin/env python3
"""Writes the vectors of the Quota keeper (the janitor job quota-monitor) of the Python Host.

Its own functions are run as they are (`server/lib/janitor_autonomy.py`: `quota_window_key`,
`seed_quota_window_state`, `quota_crossing`, and the backends' `quota_identity`), with the settings they keep
their memory in standing in a dictionary; and whole stories of the keeper's lane (`AutonomyJanitors.quota_once`),
each in a process of its own on a database of its own, with a usage reading, a clock and a push of the story's:
what each look told the phones, the receipts it kept, the run it made, when its lane runs next and what it
remembers of each window.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/quota-keeper-vectors.py

It writes `crates/clarp-kernel/tests/fixtures/quota-keeper-vectors.json`, in processes whose every home, data,
config and cache directory is a new empty directory (the replay harness's isolation), with no way out to any
network: no provider is asked, no login read, no push sent, no account changed. The same file at every run.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'quota-keeper-vectors.json'
T0 = 1_800_000_000_000
STAMP = '2027-01-15T08:00:00Z'


def window(window_id='w-5h', used=80, observed=STAMP, freshness='fresh', label='5h', **more):
    row = {'window_id': window_id, 'used_percentage': used, 'freshness': freshness, 'observed_at': observed}
    if label is not None:
        row['label'] = label
    return {**row, **more}


def provider(windows, instance='host-1:codex'):
    return {'provider_instance_id': instance, 'windows': windows}


def at(minutes, seconds=0):
    """An observed_at so many minutes after the first."""
    total = minutes * 60 + seconds
    return f'2027-01-15T{8 + total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}Z'


# ---- the window's identity and the keys of its memory
WINDOWS = [
    ('codex', 'host-1:codex', window()),
    ('codex', 'host-1:codex', window(kind='five_hour', resets_at='2027-01-15T12:00:29Z')),
    ('claude', 'host-1:claude', window(kind='five_hour', resets_at='2027-01-15T12:00:29.6Z')),
    ('claude', 'host-1:claude', window(kind='five_hour', resets_at='2027-01-15T12:00:30.4Z')),
    ('claude', 'host-1:claude', window(kind='five_hour', resets_at='2027-01-15T11:59:30.4Z')),
    ('claude', 'host-1:claude', window(kind='seven_day', resets_at='2027-01-20T09:15:00+02:00')),
    ('claude', 'host-1:claude', window(kind='five_hour', resets_at='2027-01-15T12:00:00')),
    ('claude', 'host-1:claude', window(kind='five_hour', resets_at='not a time')),
    ('claude', 'host-1:claude', window(kind='five_hour')),
    ('claude', 'host-1:claude', window(resets_at='2027-01-15T12:00:00Z')),
    ('claude', 'host-1:claude', window(kind='', resets_at='2027-01-15T12:00:00Z')),
    ('claude', 'host-1:claude', window(kind='five_hour', resets_at='')),
    ('agy', 'host-1:agy', window(kind='daily', resets_at='2027-01-16T00:00:00Z')),
    ('openai', 'host-1:openai', window(kind='daily', resets_at='2027-01-16T00:00:00Z')),
    ('grok', 'host-1:grok', window(window_id='g-1')),
    ('codex', 'acct-é', window(window_id='w-é')),
]

# ---- crossings: one window's readings in order, against a threshold; and the memory kept before them
CROSSINGS = [
    ('down through the threshold', 25, None, [(70, 1), (74, 2), (75, 3), (76, 4), (80, 5), (90, 6)]),
    ('from far below to far above', 25, None, [(10, 1), (99, 2), (100, 3), (100, 4), (50, 5), (80, 6)]),
    ('first reading already under', 25, None, [(90, 1), (95, 2)]),
    ('to nothing left, and nothing again', 25, None, [(80, 1), (100, 2), (100, 3), (60, 4), (100, 5)]),
    ('at the threshold exactly from above', 25, None, [(74.9, 1), (75, 2)]),
    ('fractions', 25, None, [(74.5, 1), (75.5, 2), (99.99, 3), (100, 4)]),
    ('a stamp not newer is no reading', 25, None, [(70, 5), (80, 5), (80, 4), (80, 6)]),
    ('outside of what can be used', 25, None, [(-1, 1), (101, 2), (100.5, 3), (80, 4)]),
    ('a threshold of nothing', 0, None, [(50, 1), (99, 2), (100, 3), (100, 4)]),
    ('a threshold of everything', 100, None, [(0, 1), (10, 2), (100, 3)]),
    ('memory from before, over the threshold', 25, {'stamp': 0, 'remaining': 40}, [(80, 1)]),
    ('memory from before, already under', 25, {'stamp': 0, 'remaining': 10}, [(80, 1), (100, 2)]),
    ('memory from before at nothing', 25, {'stamp': 0, 'remaining': 0}, [(100, 1), (80, 2)]),
]

# ---- the old key carried over
SEEDS = [
    ('an old key and no new one', 'claude', window(kind='five_hour', resets_at='2027-01-15T12:00:29Z'), True, False),
    ('an old key and a new one', 'claude', window(kind='five_hour', resets_at='2027-01-15T12:00:29Z'), True, True),
    ('no old key', 'claude', window(kind='five_hour', resets_at='2027-01-15T12:00:29Z'), False, False),
    ('a window whose key is its id', 'codex', window(), True, False),
]

# ---- whole stories of the lane
CODEX = 'codex'
STORIES = [
    {'name': 'a window that falls under the threshold is told once',
     'steps': [{'do': 'usage', 'providers': {CODEX: provider([window(used=70, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 100}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 200}, {'do': 'usage', 'providers': {CODEX: provider([window(used=80, observed=at(5))])}},
               {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'usage', 'providers': {CODEX: provider([window(used=85, observed=at(10))])}},
               {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(15))])}},
               {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'}]},
    {'name': 'what is not fresh, not a number or not dated is passed over',
     'steps': [{'do': 'usage', 'providers': {
         CODEX: provider([window('a', used=90, freshness='stale', observed=at(0)), window('b', used='90', observed=at(0)),
                          window('c', used=True, observed=at(0)), window('d', used=None, observed=at(0)),
                          window('e', used=90, observed='yesterday'),
                          window('g', used=90.5, observed=at(0), label=''), window('h', used=90, observed='2027-01-15T08:00:00+01:00'),
                          {'window_id': 'i', 'used_percentage': 90, 'freshness': 'fresh'}]),
         'claude': {'windows': [window('cl', used=99, observed=at(0), kind='five_hour', resets_at='2027-01-15T12:00:29Z')]},
         'agy': provider([], instance='host-1:agy')}},
         {'do': 'tick'}]},
    {'name': 'a look whose push raises is closed with what it read kept; a threshold changed cancels what was not delivered',
     'steps': [{'do': 'usage', 'providers': {CODEX: provider([window(used=80, observed=at(0))])}},
               {'do': 'push', 'answer': 'raise'}, {'do': 'tick'},
               {'do': 'options', 'options': {'remaining_threshold': 50}}, {'do': 'on'},
               {'do': 'push', 'answer': {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}},
               {'do': 'wait', 'seconds': 300}, {'do': 'usage', 'providers': {CODEX: provider([window(used=81, observed=at(5))])}},
               {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'usage', 'providers': {CODEX: provider([window(used=40, observed=at(10))])}},
               {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'usage', 'providers': {CODEX: provider([window(used=60, observed=at(15))])}},
               {'do': 'tick'}]},
    {'name': 'a push that answers nothing is delivered as nothing',
     'steps': [{'do': 'push', 'answer': None}, {'do': 'usage', 'providers': {CODEX: provider([window(used=90, observed=at(0))])}},
               {'do': 'tick'}]},
    {'name': 'a keeper turned off looks at nothing, and on again goes on',
     'steps': [{'do': 'usage', 'providers': {CODEX: provider([window(used=60, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'off'}, {'do': 'wait', 'seconds': 300},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=90, observed=at(5))])}}, {'do': 'tick'},
               {'do': 'on'}, {'do': 'tick'}, {'do': 'wait', 'seconds': 300}, {'do': 'tick'}]},
    {'name': 'an automatic switcher owns the notices of Claude and Codex, and of no other tool',
     'steps': [{'do': 'switcher'},
               {'do': 'usage', 'providers': {
                   CODEX: provider([window(used=80, observed=at(0))]),
                   'claude': provider([window('cl', used=90, observed=at(0), kind='five_hour', resets_at='2027-01-15T12:00:29Z')],
                                      instance='host-1:claude'),
                   'agy': provider([window('ag', used=95, observed=at(0))], instance='host-1:agy')}},
               {'do': 'tick'}]},
    {'name': 'an interval set longer, and many windows in one look',
     'steps': [{'do': 'options', 'options': {'interval_seconds': 900, 'remaining_threshold': 30}}, {'do': 'on'},
               {'do': 'usage', 'providers': {
                   CODEX: provider([window('w-5h', used=75, observed=at(0)), window('w-7d', used=40, observed=at(0), label='7d')]),
                   'agy': provider([window('ag', used=71, observed=at(0), label=None)], instance='host-1:agy')}},
               {'do': 'tick'}, {'do': 'wait', 'seconds': 600}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300},
               {'do': 'usage', 'providers': {CODEX: provider([window('w-5h', used=75, observed=at(15)),
                                                              window('w-7d', used=71, observed=at(15), label='7d')])}},
               {'do': 'tick'}]},
    {'name': 'a window dated with nothing is passed over, and the rest are read',
     'steps': [{'do': 'usage', 'providers': {CODEX: provider([window('a', used=80, observed=at(0)),
                                                              window('b', used=90, observed=None),
                                                              window('c', used=95, observed=at(0))])}},
               {'do': 'tick'}, {'do': 'wait', 'seconds': 300}, {'do': 'tick'}]},
    {'name': 'a push that reaches no phone stays undelivered, and is tried again past the interval',
     'steps': [{'do': 'push', 'answer': {'enabled': True, 'sent': 0, 'failed': 1, 'disabled': 0}},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=80, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'push', 'answer': {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'usage', 'providers': {CODEX: provider([window(used=85, observed=at(10))])}},
               {'do': 'tick'}]},
    {'name': 'a notice that waits is replaced by a newer crossing of its window',
     'steps': [{'do': 'push', 'answer': {'enabled': True, 'sent': 0, 'failed': 1, 'disabled': 0}},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=80, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(5))])}}, {'do': 'tick'},
               {'do': 'push', 'answer': {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'}]},
    {'name': 'two notices of one window written at the same moment: the newest is the larger id, though it was '
             'written first', 'tie': True,
     # Claude rounds a window's reset to the minute (its quota_identity), so two windows of one kind whose resets
     # differ by jitter are one window to the keeper: both cross in one look (80 used, then nothing left, read
     # later), and both notices are written at the same moment. The ids are chosen so that the larger is written
     # first (the story checks it): the reference takes the newest by (created_at, receipt_id), not by the order
     # written.
     'steps': [{'do': 'usage', 'providers': {'claude': provider([
                   window('cl-a', used=80, observed=at(0), kind='five_hour', resets_at='2027-01-15T12:00:29.6Z'),
                   window('cl-b', used=100, observed=at(0, 30), kind='five_hour', resets_at='2027-01-15T12:00:29.9Z')],
                   instance='host-1:claude')}},
               {'do': 'tick'}]},
    {'name': 'a notice that waits is let go once its window has reset',
     'steps': [{'do': 'push', 'answer': {'enabled': True, 'sent': 0, 'failed': 1, 'disabled': 0}},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=80, observed=at(0), kind='five_hour',
                                                                   resets_at=at(7))])}}, {'do': 'tick'},
               {'do': 'push', 'answer': {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}},
               {'do': 'wait', 'seconds': 600}, {'do': 'tick'}]},
    {'name': 'nothing left with a recovery asked: what the recovery answers where none runs',
     'steps': [{'do': 'options', 'options': {'recovery_mode': 'automatic'}}, {'do': 'on'}, {'do': 'selector', 'value': None},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(0))]),
                                             'agy': provider([window('ag', used=100, observed=at(0))], instance='host-1:agy')}},
               {'do': 'tick'}, {'do': 'wait', 'seconds': 300}, {'do': 'selector', 'value': 'cycler'},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=50, observed=at(5)),
                                                              window('w-7d', used=100, observed=at(5), label='7d')])}},
               {'do': 'tick'}, {'do': 'wait', 'seconds': 300}, {'do': 'options', 'options': {'recovery_mode': 'notify'}},
               {'do': 'on'},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(10))])}}, {'do': 'tick'}]},
    # ---- recovery under a selector: the runtime's own function, its coordinator stood in
    {'name': 'nothing left, recovered at once: the first turn the coordinator owns is taken up', 'recover': True,
     'steps': [{'do': 'options', 'options': {'recovery_mode': 'automatic'}}, {'do': 'on'},
               {'do': 'selector', 'value': 'cycler'},
               {'do': 'coordinator', 'attempts': [{'agent_id': 'a-gone', 'trace_id': 't-0', 'owned': False},
                                                  {'agent_id': 'a-1', 'trace_id': 't-1', 'owned': True},
                                                  {'agent_id': 'a-2', 'trace_id': 't-2', 'owned': True}], 'accepts': True},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'coordinator', 'attempts': [], 'accepts': True},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=40, observed=at(5))])}}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300},
               {'do': 'coordinator', 'attempts': [{'agent_id': 'a-3', 'trace_id': 't-3', 'owned': True}], 'accepts': False},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(10))]),
                                             'claude': provider([window('cl', used=100, observed=at(10), kind='five_hour',
                                                                        resets_at='2027-01-15T12:00:29Z')],
                                                                instance='host-1:claude')}}, {'do': 'tick'}]},
    {'name': 'nothing left and nothing to take up: the coordinator owns no turn', 'recover': True,
     'steps': [{'do': 'options', 'options': {'recovery_mode': 'automatic'}}, {'do': 'on'},
               {'do': 'selector', 'value': 'cycler'}, {'do': 'coordinator', 'attempts': [], 'accepts': True},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(0))])}}, {'do': 'tick'}]},
    {'name': 'nothing left, the owner asked: a card, and the recovery once it is accepted, once', 'recover': True,
     'steps': [{'do': 'options', 'options': {'recovery_mode': 'ask'}}, {'do': 'on'},
               {'do': 'selector', 'value': 'cycler'},
               {'do': 'coordinator', 'attempts': [{'agent_id': 'a-1', 'trace_id': 't-1', 'owned': True}], 'accepts': True},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'},
               {'do': 'resolve', 'choice': 'accepted'},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'}]},
    {'name': 'a card turned down, and one accepted after the keeper was set anew, recover nothing', 'recover': True,
     'steps': [{'do': 'options', 'options': {'recovery_mode': 'ask'}}, {'do': 'on'},
               {'do': 'selector', 'value': 'cycler'},
               {'do': 'coordinator', 'attempts': [{'agent_id': 'a-1', 'trace_id': 't-1', 'owned': True}], 'accepts': True},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(0))])}}, {'do': 'tick'},
               {'do': 'resolve', 'choice': 'rejected'},
               {'do': 'wait', 'seconds': 300},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=50, observed=at(5))])}}, {'do': 'tick'},
               {'do': 'wait', 'seconds': 300},
               {'do': 'usage', 'providers': {CODEX: provider([window(used=100, observed=at(10))])}}, {'do': 'tick'},
               {'do': 'options', 'options': {'remaining_threshold': 30}}, {'do': 'on'},
               {'do': 'resolve', 'choice': 'accepted'},
               {'do': 'wait', 'seconds': 300}, {'do': 'tick'}]},
]


def isolated(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    return root


@tool_scratch.case('clarp-quota-keeper-units-')
def units(root) -> dict:
    """The functions themselves, their memory a dictionary."""
    isolated(root)
    from lib import janitor_autonomy as A, settings_store
    kept = {}
    settings_store.get_text = lambda key, default='': kept.get(key, default)
    settings_store.set_text = lambda key, value: kept.__setitem__(key, value)
    keys = []
    for prov, account, row in WINDOWS:
        legacy = 'quota-keeper.window.' + A.digest([prov, account, row['window_id']])
        keys.append({'provider': prov, 'account': account, 'window': row, 'key': A.quota_window_key(prov, account, row),
                     'legacy': legacy})
    crossings = []
    for name, threshold, before, readings in CROSSINGS:
        kept.clear()
        prov, account, row = 'codex', 'host-1:codex', window()
        key = A.quota_window_key(prov, account, row)
        if before is not None:
            kept[key] = json.dumps(before)
        owner = {'options': {'remaining_threshold': threshold}}
        steps = []
        for used, stamp in readings:
            told = A.quota_crossing(prov, account, row, used, owner, stamp)
            steps.append({'used': used, 'stamp': stamp, 'told': told, 'kept': json.loads(kept[key]) if key in kept else None})
        crossings.append({'name': name, 'threshold': threshold, 'before': before, 'steps': steps})
    seeds = []
    for name, prov, row, old, new in SEEDS:
        kept.clear()
        account = 'host-1:' + prov
        key = A.quota_window_key(prov, account, row)
        legacy = 'quota-keeper.window.' + A.digest([prov, account, row['window_id']])
        if old:
            kept[legacy] = json.dumps({'stamp': 7, 'remaining': 20})
        if new:
            kept[key] = json.dumps({'stamp': 9, 'remaining': 60})
        A.seed_quota_window_state(prov, account, row)
        seeds.append({'name': name, 'provider': prov, 'window': row, 'old': old, 'new': new,
                      'kept': {('key' if k == key else 'legacy'): json.loads(v) for k, v in sorted(kept.items())}})
    return {'keys': keys, 'crossings': crossings, 'seeds': seeds}


@tool_scratch.case('clarp-quota-keeper-story-')
def story(root, index) -> dict:
    """One story of the lane, on a database of its own."""
    isolated(root)
    from lib import agents, artifacts, db, janitors, janitor_builtins, janitor_autonomy as A, settings_store, turn_dispatch
    from lib import log as host_log
    host_log.log = host_log.log_exception = lambda *said, **more: None
    A.log_exception = lambda *said, **more: None
    one = STORIES[index]
    clock = {'now': T0}
    db.now_ms = lambda: clock['now']
    work = root / 'work'
    work.mkdir()
    janitor_builtins.ensure_builtins(str(work))
    session = 'clarp-quota-monitor'
    row = janitors.get(session)
    janitors.set_enabled(session, row['revision'], True)
    reading = {'providers': {}}
    selector = {'value': None}
    turn_dispatch.account_selector = lambda backend: selector['value']
    asked = []
    coordinated = {'attempts': [], 'accepts': True}

    class Attempt:
        def __init__(self, row):
            self.agent_id, self.trace_id, self._owned = row['agent_id'], row['trace_id'], row['owned']

        def owned(self):
            return self._owned

    class Coordinator:
        """The account pool's coordinator, stood in: its turns, and whether it takes a request."""
        def __init__(self, provider):
            self.provider = provider
            self.lock = threading.Lock()

        @property
        def attempts(self):
            return {row['agent_id']: Attempt(row) for row in coordinated['attempts']}

        def request(self, agent_id, trace_id, command):
            asked.append({'provider': self.provider, 'agent_id': agent_id, 'trace_id': trace_id, 'command': command})
            return coordinated['accepts']
    turn_dispatch.account_failover = Coordinator
    pushed = []
    answer = {'value': {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}}

    def push(payload):
        pushed.append(payload)
        if answer['value'] == 'raise':
            raise RuntimeError('the push service is down')
        return answer['value']
    recover = A.runtime_recover if one.get('recover') else None
    keeper = A.AutonomyJanitors(lambda *said, **more: True, push, usage_read=lambda: json.loads(json.dumps(reading)),
                                recover=recover)
    A.setup()
    steps = []
    for step in one['steps']:
        kind = step['do']
        more = {}
        if kind == 'usage':
            reading['providers'] = step['providers']
        elif kind == 'wait':
            clock['now'] += step['seconds'] * 1000
        elif kind == 'selector':
            selector['value'] = step['value']
        elif kind == 'push':
            answer['value'] = step['answer']
        elif kind == 'options':
            current = janitors.get(session)
            options = {**current['options'], **step['options']}
            janitors.configure(session, current['revision'], options=options)
        elif kind in ('off', 'on'):
            current = janitors.get(session)
            janitors.set_enabled(session, current['revision'], kind == 'on')
        elif kind == 'switcher':
            agents.create_agent(persona='Seats', voice_id='', cwd=str(work), session='seats', backend='claude')
            made = janitors.create('seats', template_id='account-hotseat')
            janitors.set_enabled('seats', made['revision'], True)
        elif kind == 'coordinator':
            coordinated['attempts'], coordinated['accepts'] = step['attempts'], step['accepts']
        elif kind == 'resolve':
            card = db.conn().execute(
                'SELECT d.decision_id, d.revision FROM artifact_decisions d JOIN artifacts a ON a.artifact_id=d.artifact_id '
                'WHERE a.session=? ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1', (session,)).fetchone()
            artifacts.resolve(card['decision_id'], expected_revision=card['revision'], choice=step['choice'])
        elif kind == 'tick':
            asked_before = len(asked)
            before = len(pushed)
            runs_before = {run['run_id'] for run in janitors.list_runs(session)}
            try:
                keeper.quota_once()
            except Exception as broke:  # noqa: BLE001 - the Python Host's own break, told as it is
                more['broke'] = type(broke).__name__
            more['pushed'] = pushed[before:]
            if one.get('recover'):
                more['asked'] = asked[asked_before:]
            new_runs = [run for run in janitors.list_runs(session) if run['run_id'] not in runs_before]
            more['runs'] = [{key: run[key] for key in ('status', 'outcome', 'activity', 'activity_summary', 'created_at',
                                                         'finished_at', 'generation')}
                            | {'result': run.get('demand_result')} for run in new_runs]
        if one.get('tie') and kind == 'tick':
            # What the story is for: two notices of one window, the same moment, the larger id written first.
            written = db.conn().execute('SELECT receipt_id, created_at, payload_json FROM janitor_quota_receipts '
                                        'ORDER BY rowid').fetchall()
            keys = {json.loads(r['payload_json'])['window_key'] for r in written}
            assert len(written) == 2 and len(keys) == 1 and written[0]['created_at'] == written[1]['created_at'] \
                and written[0]['receipt_id'] > written[1]['receipt_id'], ('the tie this story is for did not happen', [dict(r) for r in written])
        current = janitors.get(session)
        receipts = [{'receipt_id': r['receipt_id'], 'payload': json.loads(r['payload_json']),
                     'delivery': json.loads(r['delivery_json']) if r['delivery_json'] is not None else None,
                     'created_at': r['created_at']}
                    for r in db.conn().execute('SELECT * FROM janitor_quota_receipts ORDER BY created_at, receipt_id')]
        memory = {r['key']: json.loads(r['value']) for r in db.conn().execute(
            "SELECT key, value FROM settings WHERE key LIKE 'quota-keeper.window.%' ORDER BY key")} \
            if db.conn().execute("SELECT name FROM sqlite_master WHERE name='settings'").fetchone() else {}
        owner = {'agent_id': current['agent_id'], 'generation': current['generation'], 'options': current['options']}
        lane = A.lane_state('quota-keeper', owner)
        if one.get('recover'):
            more['cards'] = [{'artifact_id': row['artifact_id'], 'title': row['title'], 'question': row['question'],
                              'context': row['context'], 'reference_id': row['reference_id'],
                              'payload': json.loads(row['payload_json'] or 'null'), 'status': row['status'],
                              'resolved_choice': row['resolved_choice']}
                             for row in db.conn().execute(
                                 'SELECT a.artifact_id, a.title, d.question, d.context, a.reference_id, a.payload_json, '
                                 'd.status, d.resolved_choice FROM artifacts a JOIN artifact_decisions d '
                                 'ON d.artifact_id=a.artifact_id WHERE a.session=? ORDER BY a.created_at, a.rowid',
                                 (session,))]
            more['approvals'] = sorted(r['key'] for r in db.conn().execute(
                "SELECT key FROM settings WHERE key LIKE 'quota-keeper.approval.%'"))
        steps.append({**step, **more, 'now': clock['now'], 'enabled': current['enabled'], 'generation': current['generation'],
                      'options': current['options'], 'next_run_at': A.lane_next_run('quota-keeper', owner,
                                                                                    current['options']['interval_seconds'],
                                                                                    'quota-keeper.last-check'),
                      'lane': lane, 'receipts': receipts, 'memory': memory})
    agent = janitors.get(session)
    # A card's id is drawn anew at every run: it is written as the card it is, in the order they were made.
    named = []
    for step in steps:
        for card in step.get('cards', []):
            if card['artifact_id'] not in named:
                named.append(card['artifact_id'])
    written = json.dumps(steps)
    for number, artifact_id in enumerate(named, 1):
        written = written.replace(artifact_id, f'card-{number}')
    steps = json.loads(written)
    return {'name': one['name'], 'agent_id': agent['agent_id'], 'session': session, 'persona': agent['name'], 'steps': steps}


def child(args):
    ran = subprocess.run([sys.executable, '-I', __file__, *args], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC'}), check=False)
    if ran.returncode != 0:
        raise SystemExit(f'{args}: {ran.stderr[-4000:]}')
    return json.loads(ran.stdout.splitlines()[-1])


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--units':
        print(json.dumps(units()))
        return 0
    if len(sys.argv) > 2 and sys.argv[1] == '--story':
        print(json.dumps(story(int(sys.argv[2]))))
        return 0
    told = child(['--units'])
    told['stories'] = [child(['--story', str(index)]) for index in range(len(STORIES))]
    out = ['{']
    parts = list(told)
    for number, part in enumerate(parts):
        rows = told[part]
        out.append(f' "{part}": [')
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append(' ]' + (',' if number < len(parts) - 1 else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    json.loads(OUT.read_text())
    print(f'{OUT.relative_to(REPO)}: {len(told["keys"])} keys, {len(told["crossings"])} crossings, '
          f'{len(told["seeds"])} seeds, {len(told["stories"])} stories of '
          f'{sum(len(s["steps"]) for s in told["stories"])} steps, '
          f'{sum(len(st.get("pushed", [])) for s in told["stories"] for st in s["steps"])} pushed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
