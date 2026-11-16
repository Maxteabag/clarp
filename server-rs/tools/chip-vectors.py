#!/usr/bin/env python3
"""Writes the vectors the Rust Host's "agents working" chip is tested against.

A phone that shows the fleet's work on its lock screen (a Live Activity) is kept current by pushes
from the Host (`docs/live-items.md` §8, `server/lib/live_activity.py`): how many agents work, how
many need the owner, the agent whose turn started last, and up to five rows. The Host folds what
every agent's turn shows into that one state and paces the pushes: routine change at most every
fifteen seconds, a turn that starts or ends or another lead at once, a rising count of what needs
the owner at once and loud.

This runs that module itself and writes to `crates/clarp-kernel/tests/fixtures/chip-vectors.json`:
the name an agent is shown by, the line that says what it does, the state for fleets made up here
(helpers under their agents, agents that wait, what the badge counts), the register of activities a
phone leaves, and what the pacer sends through scripts of events on a clock made up here.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/chip-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, and nothing is pushed: what would be is written
down instead.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'chip-vectors.json'

NAMES = ['Theo', 'theo', 'clarp-memory-expert', 'CLARP-MEMORY-EXPERT', 'ECIT Rachel', 'ECIT', 'NASA', 'ABC', 'AB-CD',
         'rk_integration', 'mike-86db', 'Anna Maria', 'ANNA MARIA', 'anna_maria-3f', 'Zoë', 'ZOË-ÉCOLE', 'a--b__c', '-x-',
         '', 'R2D2', 'R2-D2', '1234', 'ÅSE', 'ÅSEN', 'ǅemal', 'ßtraße-ß']

T0 = 1_800_000_000_000


def tool(name, label='', category=None, started=None):
    made = {'name': name, 'label': label, 'started_at_ms': started}
    if category is not None:
        made['category'] = category
    return made


def doing(state, since=None, turn=None, headline=None, tool_=None, **more):
    return {'state': state, 'since_ms': since, 'turn_started_ms': turn, 'headline': headline, 'tool': tool_, **more}


ACTIVITIES = [
    {}, doing('idle'), doing('thinking', T0 + 5, T0), doing('thinking', T0 + 5, T0, 'Weighing the two designs'),
    doing('responding', T0 + 9, T0), doing('compacting', T0 + 9, T0), doing('waiting', T0 + 9), doing('limited', T0 + 9, T0),
    doing('interrupted'), doing('background', T0 + 2), doing('something-new', T0 + 2), doing('tool', T0 + 9, T0),
    doing('tool', T0 + 9, T0, tool_='not a tool'), doing('tool', T0 + 9, T0, None, tool('Bash', 'npm test', 'exec', T0 + 11)),
    doing('tool', T0 + 9, T0, None, tool('Edit', '/home/u/app/AppModel.swift', 'edit', T0 + 11)),
    doing('tool', T0 + 9, T0, None, tool('Read', 'src/parser.py', 'read')),
    doing('tool', T0 + 9, T0, None, tool('Write', '/tmp/out/', 'write')),
    doing('tool', T0 + 9, T0, None, tool('LS', 'crates/kernel/', 'list')),
    doing('tool', T0 + 9, T0, None, tool('Grep', 'to/kenize', 'search')),
    doing('tool', T0 + 9, T0, None, tool('WebFetch', 'https://example.org/a/b', 'fetch')),
    doing('tool', T0 + 9, T0, None, tool('TodoWrite', '', 'todo')),
    doing('tool', T0 + 9, T0, None, tool('Task', 'look into the leak', 'agent')),
    doing('tool', T0 + 9, T0, None, tool('mcp__files__read', 'read', 'mcp')),
    doing('tool', T0 + 9, T0, None, tool('Something', 'else', 'other')),
    doing('tool', T0 + 9, T0, None, tool('Something', 'else', 'unheard-of')),
    doing('tool', T0 + 9, T0, None, tool('', '', 'exec')), doing('tool', T0 + 9, T0, None, tool('Bash', '', 'exec')),
    doing('tool', T0 + 9, T0, None, tool('Bash', 'cargo test')), doing('tool', T0 + 9, T0, None, tool('Read', 'a/b/c.rs')),
    doing('tool', T0 + 9, T0, None, tool('Glob')), doing('tool', T0 + 9, T0, None, tool('mcp__git__status', 'status')),
    doing('tool', T0 + 9, T0, None, tool('git status --short')), doing('tool', T0 + 9, T0, None, tool('Unheard')),
    doing('tool', T0 + 9, T0, 'A headline that a tool does not take', tool('Bash', 'ls', 'exec', T0 + 30)),
    doing('thinking', None, T0), doing('thinking', None, None), doing('responding', 0, 0),
]


def agent(session, persona=None, activity=None, **row):
    return {'session': session, 'persona': session.capitalize() if persona is None else persona,
            'activity': activity or {}, **row}


def helper(session, parent, activity=None, **row):
    return agent(session, activity=activity, role='helper', parent_agent_id=parent, **row)


def listed(session, persona=None, **row):
    return {'session': session, 'persona': session.capitalize() if persona is None else persona, **row}


WORK, NEWER, OLDER = doing('thinking', T0 + 50, T0 + 40), doing('responding', T0 + 90, T0 + 80), doing('thinking', T0 + 20, T0 + 10)
RUNS = doing('tool', T0 + 60, T0 + 40, None, tool('Bash', 'npm test', 'exec', T0 + 70))
WAITS, IDLE = doing('waiting', T0 + 5), doing('idle')

# A fleet as the live stream showed it (`agents`), what the Host knows of every agent (`roster`),
# and what the badge counts (`attention`), each when the case has one.
FLEETS = [
    {'name': 'nobody', 'agents': {}},
    {'name': 'one agent thinks', 'agents': {'a1': agent('theo', activity=WORK)}},
    {'name': 'one agent is idle', 'agents': {'a1': agent('theo', activity=IDLE)}},
    {'name': 'one agent with nothing told of it', 'agents': {'a1': agent('theo')}},
    {'name': 'two work: the one whose turn started last leads',
     'agents': {'a1': agent('theo', activity=WORK), 'a2': agent('mike', activity=NEWER), 'a3': agent('nadia', activity=OLDER)}},
    {'name': 'the same start: by name', 'agents': {'a1': agent('zed', activity=WORK), 'a2': agent('abe', activity=WORK)}},
    {'name': 'one runs a tool', 'agents': {'a1': agent('theo', activity=RUNS)}},
    {'name': 'one waits for the owner', 'agents': {'a1': agent('theo', activity=WAITS), 'a2': agent('mike', activity=WORK)}},
    {'name': 'every state there is',
     'agents': {f'a{n}': agent(f'agent{n}', activity=doing(state, T0 + n, T0 + n))
                for n, state in enumerate(['thinking', 'responding', 'tool', 'compacting', 'limited', 'waiting', 'idle',
                                           'interrupted', 'background', 'unheard-of'])}},
    {'name': 'more than five rows: who needs the owner first, then the newest work',
     'agents': {**{f'w{n}': agent(f'worker{n}', activity=doing('thinking', T0 + n, T0 + n)) for n in range(6)},
                'n1': agent('needy', activity=WAITS), 'n2': agent('needier', activity=doing('waiting', T0 + 7))}},
    {'name': 'a helper works under an idle agent',
     'agents': {'a1': agent('theo', activity=IDLE), 'h1': helper('theo-helper-1', 'a1', WORK)}},
    {'name': 'two helpers work under an idle agent',
     'agents': {'a1': agent('theo', activity=IDLE), 'h1': helper('h-one', 'a1', WORK), 'h2': helper('h-two', 'a1', NEWER)}},
    {'name': 'a helper works under an agent that works itself',
     'agents': {'a1': agent('theo', activity=OLDER), 'h1': helper('h-one', 'a1', NEWER)}},
    {'name': 'a helper names its parent by session',
     'agents': {'a1': agent('theo', activity=IDLE), 'h1': helper('h-one', 'theo', WORK)}},
    {'name': 'a helper of a helper',
     'agents': {'a1': agent('theo', activity=IDLE), 'h1': helper('h-one', 'a1', IDLE), 'h2': helper('h-two', 'h1', WORK)}},
    {'name': 'a helper waits: its agent needs the owner',
     'agents': {'a1': agent('theo', activity=WORK), 'h1': helper('h-one', 'a1', WAITS)}},
    {'name': 'a helper whose parent nobody knows is its own',
     'agents': {'h1': helper('h-one', 'a-gone', WORK)}},
    {'name': 'a helper whose parent is archived is its own',
     'agents': {'h1': helper('h-one', 'a1', WORK)}, 'roster': {'a1': listed('theo', archived_at=T0 - 5)}},
    {'name': 'a helper whose parent is a janitor is its own',
     'agents': {'h1': helper('h-one', 'j1', WORK)}, 'roster': {'j1': listed('sweeper', is_janitor=1)}},
    {'name': 'helpers that name each other',
     'agents': {'h1': helper('h-one', 'h2', WORK), 'h2': helper('h-two', 'h1', NEWER)}},
    {'name': 'a helper that names itself', 'agents': {'h1': helper('h-one', 'h1', WORK)}},
    {'name': 'a helper without a parent named is an agent', 'agents': {'h1': agent('h-one', activity=WORK, role='helper')}},
    {'name': 'an archived agent and a janitor never show',
     'agents': {'a1': agent('theo', activity=WORK, archived_at=T0 - 1), 'j1': agent('sweeper', activity=WORK, is_janitor=1),
                'a2': agent('mike', activity=WORK)}},
    {'name': 'the roster says an agent seen live is archived: it does not show',
     'agents': {'a1': agent('theo', activity=WORK)}, 'roster': {'a1': listed('theo', archived_at=T0 - 1)}},
    {'name': 'the badge names an agent that is not seen live',
     'agents': {'a1': agent('theo', activity=WORK)}, 'roster': {'a2': listed('mike')},
     'attention': {'count': 1, 'agent_ids': ['a2'], 'items': ['a2']}},
    {'name': 'the badge names a working agent: it works, and needs the owner',
     'agents': {'a1': agent('theo', activity=WORK)}, 'attention': {'count': 2, 'agent_ids': ['a1'], 'items': ['a1', 'a1']}},
    {'name': 'badge items that name nobody on the chip count one each',
     'agents': {'a1': agent('theo', activity=IDLE)},
     'attention': {'count': 4, 'agent_ids': ['a1', 'a-gone'], 'items': ['a1', '', 'a-gone', '']}},
    {'name': 'the badge names a helper: its agent needs the owner, once',
     'agents': {'a1': agent('theo', activity=IDLE), 'h1': helper('h-one', 'a1', IDLE), 'h2': helper('h-two', 'a1', IDLE)},
     'attention': {'count': 3, 'agent_ids': ['h1', 'h2', 'a1'], 'items': ['h1', 'h2', 'a1']}},
    {'name': 'the badge names an archived agent: it counts, and shows nobody',
     'agents': {}, 'roster': {'a1': listed('theo', archived_at=T0 - 1)},
     'attention': {'count': 1, 'agent_ids': ['a1'], 'items': ['a1']}},
    {'name': 'a badge told the old way: a count and the agents it names',
     'agents': {'a1': agent('theo', activity=IDLE)}, 'roster': {'a2': listed('mike')},
     'attention': {'count': 4, 'agent_ids': ['a2', 'a1']}},
    {'name': 'a badge told the old way with fewer than it names',
     'agents': {'a1': agent('theo', activity=IDLE)}, 'attention': {'count': 0, 'agent_ids': ['a1']}},
    {'name': 'an agent shown by a name that is its session', 'agents': {'a1': agent('clarp-memory-expert', persona='', activity=WORK)}},
    {'name': 'an agent with no name at all', 'agents': {'a1': agent('', persona='', activity=WORK)}},
    {'name': 'an agent the roster knows better than the stream',
     'agents': {'h1': agent('h-one', activity=WORK)},
     'roster': {'h1': listed('h-one', role='helper', parent_agent_id='a1'), 'a1': listed('theo')}},
]

# A phone leaves an activity, and takes it back.
REGISTERED = [
    {'do': 'register', 'token': 'AB' * 20, 'activity_id': 'act-1', 'kind': 'agents-working', 'environment': 'sandbox',
     'principal': 'device_a'},
    {'do': 'register', 'token': 'cd' * 20, 'activity_id': 'act-1'},
    {'do': 'register', 'token': 'ef' * 20},
    {'do': 'register', 'token': ' ' + 'ef' * 20 + ' ', 'activity_id': '  act-2  ', 'kind': 'agents', 'environment': 'development'},
    {'do': 'register', 'token': ''}, {'do': 'register', 'token': 'xyz'}, {'do': 'register', 'token': 'ab' * 201},
    {'do': 'register', 'token': 'ab' * 200, 'activity_id': 'x' * 200},
    {'do': 'register', 'token': 'ab', 'kind': 'something'}, {'do': 'register', 'token': 'ab', 'kind': ''},
    {'do': 'register', 'token': 'ab', 'environment': 'moon'}, {'do': 'register', 'token': 'a'},
    {'do': 'unregister', 'activity_id': 'act-1'}, {'do': 'unregister', 'activity_id': 'act-1'},
    {'do': 'unregister', 'token': 'EF' * 20}, {'do': 'unregister'}, {'do': 'unregister', 'token': 'ab' * 200},
]


def status(agent_id, session, activity):
    return {'do': 'observe', 'event': {'type': 'live', 'agent_id': agent_id, 'session': session,
                                       'ops': [{'op': 'status', 'activity': activity}]}}


def wait(seconds):
    return {'do': 'wait', 'seconds': seconds}


def badge(count, items):
    return {'do': 'attention', 'attention': {'count': count, 'agent_ids': sorted({item for item in items if item}),
                                              'items': items}}


def tooling(n):
    return doing('tool', T0 + 60, T0 + 40, None, tool('Bash', f'step {n}', 'exec', T0 + 60 + n))


# What the pacer sends, through events on a clock made up here. `wait` lets the clock run; a timer
# the pacer set goes when its time has come.
PACED = [
    {'name': 'a turn starts and is told at once; its tools are told every fifteen seconds',
     'steps': [status('a1', 'theo', WORK), status('a1', 'theo', WORK), status('a1', 'theo', tooling(1)), wait(1),
               status('a1', 'theo', tooling(2)), wait(5), status('a1', 'theo', tooling(3)), wait(8), wait(2),
               status('a1', 'theo', tooling(4)), wait(20), status('a1', 'theo', tooling(5)), wait(1),
               status('a1', 'theo', IDLE), wait(9), wait(1), wait(30)]},
    {'name': 'a turn that ends soon after it began waits ten seconds from the last push',
     'steps': [status('a1', 'theo', WORK), wait(3), status('a1', 'theo', IDLE), wait(6), wait(1), wait(1),
               status('a1', 'theo', WORK), wait(11), status('a2', 'mike', NEWER), status('a2', 'mike', IDLE), wait(1)]},
    {'name': 'what needs the owner is told at once and loud; what no longer does, in its time',
     'steps': [status('a1', 'theo', WORK), wait(1), status('a1', 'theo', WAITS), status('a2', 'mike', WAITS), wait(1),
               status('a1', 'theo', IDLE), wait(10), badge(2, ['a1', '']), badge(2, ['a1', '']), wait(1), badge(0, []),
               wait(20)]},
    {'name': 'the badge is heard before anybody works, and by itself tells nothing until somebody did',
     'steps': [badge(1, ['a9']), wait(5), status('a1', 'theo', WORK), wait(20), badge(3, ['a9', 'a9', '']), wait(20),
               status('a1', 'theo', IDLE), wait(20)]},
    {'name': 'what is no status is not looked at',
     'steps': [{'do': 'observe', 'event': {'type': 'agent-state', 'agent_id': 'a1'}},
               {'do': 'observe', 'event': {'type': 'live', 'agent_id': 'a1', 'session': 'theo', 'ops': [{'op': 'upsert'}]}},
               {'do': 'observe', 'event': {'type': 'live', 'agent_id': 'a1', 'session': 'theo', 'ops': []}},
               {'do': 'observe', 'event': {'type': 'live', 'agent_id': 'a1', 'session': 'theo',
                                           'ops': [{'op': 'status', 'activity': WORK}, {'op': 'status', 'activity': NEWER}]}},
               {'do': 'observe', 'event': {'type': 'live', 'agent_id': 'a1', 'session': 'theo', 'ops': [{'op': 'status'}]}},
               wait(20)]},
    {'name': 'a janitor is never seen, and a helper is under its agent from the first moment',
     'steps': [status('j1', 'sweeper', WORK), status('h1', 'h-one', WORK), wait(20), status('a1', 'theo', NEWER), wait(20),
               status('h1', 'h-one', IDLE), wait(20), status('j1', 'sweeper', IDLE), wait(20)]},
]



def offer(tokens, state, priority='5'):
    return {'do': 'offer', 'tokens': tokens, 'state': state, 'priority': priority}


def take():
    return {'do': 'due'}


def reserve():
    return {'do': 'reserve'}


def delivered():
    return {'do': 'delivered'}


def limited(retry_after=0, day=False):
    return {'do': 'limited', 'retry_after': retry_after, 'day': day}


def back(count):
    return {'do': 'requeue', 'count': count}


DAY = 86_400
T = [f't{n}' for n in range(14)]

# The share of the push service's budget the chip may use: ten pushes at once and three hundred a
# day, refilled evenly; a push service that says "too many" is left alone for a while, longer each
# time; what is held back is the newest state for each token, and goes when it may. The clock is
# made up here and starts at a midnight; `wait` lets it run, and what the gate asked to be woken
# for goes when its time has come.
BUDGETED = [
    {'name': 'ten at once, then one every 288 seconds',
     'steps': [take(), offer(T[:3], 1), take(), reserve(), delivered(), reserve(), reserve(), offer(T[:12], 2), take(),
               wait(100), take(), wait(188), wait(1), take(), wait(288), offer(T[:12], 3), take(), wait(3000), take(),
               offer(T[:2], 4), take()]},
    {'name': 'the newest state for each token; those that wait keep their places; one that is gone drops out',
     'steps': [offer(T[:12], 1), take(), offer(['t11', 't10', 't1', 't12'], 2, '10'), offer(['t12', 't10', 't0'], 3),
               wait(288), take(), wait(288), take(), wait(600), take()]},
    {'name': 'a push service that says too many is left alone, longer each time, until one is delivered',
     'steps': [offer(T[:2], 1), limited(), take(), wait(59), take(), wait(1), take(), limited(), wait(119), take(), wait(1),
               take(), limited(30), limited(500), offer(T[:2], 2), wait(499), take(), wait(1), take(), delivered(),
               limited(), wait(60), limited(), limited(), limited(), limited(), limited(), limited(), wait(1799),
               offer(T[:1], 3), take(), wait(1), take()]},
    {'name': 'what was taken and did not go is put back, and its place in the budget with it',
     'steps': [offer(T[:12], 1), take(), back(4), take(), back(3), offer(T[:12], 2), take(), limited(40), back(2), take(),
               wait(40), take()]},
    {'name': 'three hundred a day, and the day of the push service is over before the next begins',
     'steps': [reserve()] * 3 + [{'do': 'reserve_many', 'count': 296}, reserve(), reserve(), wait(DAY // 2), reserve(),
                                 wait(DAY // 2 - 1), reserve(), wait(1), reserve(), reserve(), limited(7200, day=True),
                                 reserve(), wait(7199), reserve(), wait(1), reserve(), limited(4000, day=True),
                                 wait(DAY), reserve(), limited(10, day=True), reserve(), offer(T[:1], 9), take()]},
    {'name': 'a clock that went back spends nothing',
     'steps': [reserve(), wait(100), reserve(), wait(-50), reserve(), wait(49), reserve(), wait(1), reserve(), wait(1),
               reserve()]},
]

# The agents the Host knows by their ids, for the pacer.
KNOWN = {
    'a1': {'agent_id': 'a1', 'session': 'theo', 'persona': 'Theo'},
    'a2': {'agent_id': 'a2', 'session': 'mike', 'persona': 'Mike'},
    'h1': {'agent_id': 'h1', 'session': 'h-one', 'persona': 'Helper One', 'role': 'helper', 'parent_agent_id': 'a1'},
    'j1': {'agent_id': 'j1', 'session': 'sweeper', 'persona': 'Sweeper', 'is_janitor': 1},
}


def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-chip-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, identity, live_activity, quiet_agents, settings_store

    def stated(fleet):
        attention = fleet.get('attention')
        if attention is not None:
            attention = {**attention, 'agent_ids': set(attention.get('agent_ids') or ())}
        state = live_activity.content_state(json.loads(json.dumps(fleet['agents'])), T0 + 1000, attention,
                                            json.loads(json.dumps(fleet.get('roster'))) if 'roster' in fleet else None)
        return {**fleet, 'state': state}

    def registry():
        rows = []
        for step in REGISTERED:
            said = {key: value for key, value in step.items() if key != 'do'}
            try:
                if step['do'] == 'register':
                    result, refused = live_activity.register(**said), None
                else:
                    result, refused = live_activity.unregister(**said), None
            except ValueError as why:
                result, refused = None, str(why)
            if isinstance(result, dict):
                result = {**result, 'registered_at': '<now>'}
            kept = sorted(live_activity.tokens(), key=lambda row: row['activity_id'])
            rows.append({'step': step, 'result': result, 'refused': refused,
                         'kept': [{**row, 'registered_at': '<now>'} for row in kept]})
        return rows

    def paced(script):
        state = {'now': 1_800_000_000.0, 'timer': None}
        sent = []

        class Timer:
            def __init__(self, due, fire):
                self.due, self.fire, self.cancelled = due, fire, False

            def cancel(self):
                self.cancelled = True

        def schedule(delay, fire):
            state['timer'] = Timer(state['now'] + delay, fire)
            return state['timer']

        agents.get_by_agent_id = lambda agent_id: dict(KNOWN[agent_id]) if agent_id in KNOWN else None
        identity.lookup = lambda name: next((dict(row) for row in KNOWN.values()
                                             if name in (row['agent_id'], row['session'])), None)
        quiet_agents.is_quiet = lambda row: bool((row or {}).get('is_janitor'))
        pusher = live_activity.LiveActivityPusher(
            lambda payload, priority: sent.append({'payload': json.loads(json.dumps(payload)), 'priority': priority}),
            clock=lambda: state['now'], schedule=schedule,
            send_fcm=lambda content, high: sent[-1].update(high=high))
        rows = []
        for step in script['steps']:
            del sent[:]
            if step['do'] == 'observe':
                pusher.observe(json.loads(json.dumps(step['event'])))
            elif step['do'] == 'attention':
                attention = step['attention']
                pusher.set_attention({**attention, 'agent_ids': set(attention['agent_ids'])})
            else:
                until = state['now'] + step['seconds']
                while True:
                    timer = state['timer']
                    if timer is None or timer.cancelled or timer.due > until:
                        break
                    state['now'], state['timer'] = timer.due, None
                    timer.fire()
                state['now'] = until
            timer = state['timer']
            waits = None if timer is None or timer.cancelled else round(timer.due - state['now'], 3)
            rows.append({'step': step, 'now': state['now'], 'sent': list(sent), 'timer_in': waits})
        return {'name': script['name'], 'steps': rows}

    def budgeted(script):
        from lib import apns
        settings_store.delete(apns._LA_DAY_KEY)
        state = {'now': float(20_000 * DAY), 'timer': None}
        woken, taken = [], []

        class Timer:
            def __init__(self, due):
                self.due, self.cancelled = due, False

            def cancel(self):
                self.cancelled = True

        def schedule(delay, fire):
            state['timer'] = Timer(state['now'] + delay)
            state['fire'] = fire
            return state['timer']

        real = apns.time.time
        apns.time.time = lambda: state['now']
        gate = apns._LiveActivityGate(clock=lambda: state['now'], schedule=schedule)
        flush = lambda: woken.append(state['now'])
        rows = []
        try:
            for step in script['steps']:
                del woken[:]
                do, result = step['do'], None
                if do == 'offer':
                    gate.offer([{'token': token} for token in step['tokens']], {'state': step['state']}, step['priority'])
                elif do == 'due':
                    ready = gate.due(flush)
                    taken[:] = ready
                    result = [[row['token'], payload['state'], priority] for row, payload, priority in ready]
                elif do == 'reserve':
                    result = gate.reserve()
                elif do == 'reserve_many':
                    result = sum(1 for _ in range(step['count']) if gate.reserve())
                elif do == 'delivered':
                    gate.delivered()
                elif do == 'limited':
                    gate.rate_limited(step['retry_after'], day_window=step['day'])
                elif do == 'requeue':
                    gate.requeue(taken[-step['count']:], flush)
                else:
                    until = state['now'] + step['seconds']
                    while step['seconds'] > 0:
                        timer = state['timer']
                        if timer is None or timer.cancelled or timer.due > until:
                            break
                        state['now'], state['timer'] = timer.due, None
                        state['fire']()
                    state['now'] = until
                timer = state['timer']
                waits = None if timer is None or timer.cancelled or gate._timer is None else round(timer.due - state['now'], 6)
                kept = settings_store.get(apns._LA_DAY_KEY)
                rows.append({'step': step, 'now': state['now'], 'result': result, 'woken': len(woken),
                             'timer_in': waits, 'paused': gate.paused(), 'floor': gate.floor,
                             'waiting': list(gate._pending), 'day': json.loads(kept) if kept else None})
        finally:
            apns.time.time = real
        return {'name': script['name'], 'steps': rows}

    for key in settings_store.keys_with_prefix('live_activity.'):
        settings_store.delete(key)
    answered = {
        'names': [{'name': name, 'shown': live_activity.display_name(name)} for name in NAMES],
        'activities': [{'activity': activity, 'headline': live_activity.row_headline(activity),
                        'row': live_activity._row('a1', {'session': 'theo', 'persona': 'Theo', 'activity': activity})}
                       for activity in ACTIVITIES],
        'fleets': [stated(fleet) for fleet in FLEETS],
        'registered': registry(),
        'paced': [paced(script) for script in PACED],
        'budgeted': [budgeted(script) for script in BUDGETED],
        'known': KNOWN,
        'most_rows': live_activity.MAX_ROWS, 'structural_floor_sec': live_activity.STRUCTURAL_FLOOR_SEC,
        'stale_after_sec': live_activity.STALE_AFTER_SEC,
    }
    shutil.rmtree(root, ignore_errors=True)
    return answered


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run(), default=lambda value: sorted(value) if isinstance(value, (set, frozenset)) else str(value)))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    numbers = ('most_rows', 'structural_floor_sec', 'stale_after_sec')
    out = ['{', ' ' + ', '.join(f'"{name}": {json.dumps(said[name])}' for name in numbers) + ',',
           ' "known": ' + json.dumps(said['known'], ensure_ascii=True) + ',']
    for part in ('names', 'activities', 'fleets', 'registered'):
        out.append(f' "{part}": [')
        rows = said[part]
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if at < len(rows) - 1 else '')
                   for at, row in enumerate(rows))
        out.append(' ],')
    for part in ('paced', 'budgeted'):
        out.append(f' "{part}": [')
        for at, script in enumerate(said[part]):
            out.append('  {"name": ' + json.dumps(script['name']) + ', "steps": [')
            rows = script['steps']
            out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                       for index, row in enumerate(rows))
            out.append('  ]}' + (',' if at < len(said[part]) - 1 else ''))
        out.append(' ]' + (',' if part == 'paced' else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = sum(len(script['steps']) for script in said['paced'])
    pushes = sum(len(row['sent']) for script in said['paced'] for row in script['steps'])
    print(f'{OUT.relative_to(REPO)}: {len(said["names"])} names, {len(said["activities"])} activities, '
          f'{len(said["fleets"])} fleets, {len(said["registered"])} steps of the register, '
          f'{len(said["paced"])} paced scripts of {steps} steps ({pushes} pushes), '
          f'{len(said["budgeted"])} scripts of the budget of {sum(len(script["steps"]) for script in said["budgeted"])} steps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
