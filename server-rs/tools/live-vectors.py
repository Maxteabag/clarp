#!/usr/bin/env python3
"""Writes the vectors the Rust Host's live items are tested against.

While an agent works, the apps are sent what it is doing as small operations on a list of items
(`docs/live-items.md`): a message grows, a tool starts and prints and ends, the status line
changes, the turn ends. The Python Host makes those in `server/lib/live_core.py` (the hub),
`live_projection.py` (the reducer every client runs), `live_claude.py` (what Claude Code's stream
becomes) and `tool_output.py` (a tool's last lines, the count of an edit). This runs them and
writes what they answered to `crates/clarp-kernel/tests/fixtures/live-vectors.json`:

- the recorded streams of the contract (`contract/live/*.json`), as they are;
- headlines of reasoning, previews of a tool's input, tails of its output, counts of edits, and
  unified diffs of pairs of texts (also long ones, where the matching treats common lines apart);
- scripted turns driven through the Python hub, each step with the events the hub sent for it and
  whether it holds something back: written by hand for every rule, and random walks besides;
- streams of Claude Code read by `ClaudeLiveItems`, with the events they became.

    python3 server-rs/tools/live-vectors.py

Nothing of a Host is touched or started. The hub is made in this process, with a clock and a
timer this script moves by hand; it sends to a list. Every text is made up.
"""
from __future__ import annotations

import copy
import difflib
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'live-vectors.json'

AGENT = 'agent-1'
T = 1_759_480_000_000


class Manual:
    """The hub's timer, fired by hand."""

    def __init__(self, clock):
        self.clock = clock
        self.pending = None

    def __call__(self, interval, fire):
        timer = type('Timer', (), {})()
        timer.at, timer.interval, timer.fire, timer.cancelled = self.clock(), interval, fire, False
        timer.cancel = lambda: setattr(timer, 'cancelled', True)
        self.pending = timer
        return timer

    def due(self):
        timer = self.pending
        if timer is None or timer.cancelled:
            return None
        return timer.at + round(timer.interval * 1000)

    def fire(self):
        timer, self.pending = self.pending, None
        if timer is not None and not timer.cancelled:
            timer.fire()


def at(ms, call, **args):
    return {'at': ms, 'call': call, 'args': args}


def a_hub():
    from lib import live_core
    from lib.live_core import LiveHub
    # Whom a turn is for (`audience`, the Python Host's PR 482) is asked of its database: here a
    # stand-in answers, so that no hub of this script ever opens one.
    live_core._turn_audience = lambda _agent_id, _turn_id: 'user'
    now = {'ms': 0}
    sent: list = []
    manual = Manual(lambda: now['ms'])
    hub = LiveHub(sink=lambda event: sent.append(copy.deepcopy(event)), clock_ms=lambda: now['ms'],
                  schedule=manual)
    hub.epoch = 'epoch-1'
    return hub, now, sent, manual


def hub_case(name, steps):
    hub, now, sent, manual = a_hub()
    told = []
    for step in steps:
        now['ms'] = step['at']
        call, args = step['call'], step['args']
        del sent[:]
        answer = None
        turn = args.get('turn_id')
        if call == 'fire':
            manual.fire()
        elif call == 'snapshot':
            answer = hub.snapshot(agent_id=AGENT)
        elif call == 'begin_turn':
            hub.begin_turn(agent_id=AGENT, session=args['session'], conv=args['conv'], turn_id=args['turn_id'],
                           started_at_ms=args.get('started_at_ms'))
        elif call == 'end_turn':
            hub.end_turn(AGENT, status=args.get('status', 'completed'), error=args.get('error'))
        elif call == 'account_recovery':
            hub.account_recovery(AGENT, args.get('message'))
        elif call == 'message_text':
            hub.message_text(AGENT, args['id'], args['text'], phase=args.get('phase'), row_id=args.get('row_id'),
                             turn_id=turn)
        elif call == 'reasoning_text':
            hub.reasoning_text(AGENT, args['id'], args['text'], turn_id=turn)
        elif call == 'tool_start':
            hub.tool_start(AGENT, args['id'], name=args['name'], call_id=args['call_id'], category=args['category'],
                           label=args['label'], command=args.get('command'),
                           input_preview=args.get('input_preview'), status=args.get('status', 'running'),
                           turn_id=turn)
        elif call == 'tool_output':
            hub.tool_output(AGENT, args['id'], args['lines'], total_lines=args['total_lines'], turn_id=turn)
        elif call == 'patch':
            hub.patch(AGENT, args['id'], args['fields'], turn_id=turn)
        elif call == 'item':
            hub.item(AGENT, args['id'], args['kind'], args['fields'], turn_id=turn)
        elif call == 'done':
            hub.done(AGENT, args['id'], status=args.get('status', 'completed'), patch=args.get('patch'), turn_id=turn)
        else:
            raise ValueError(call)
        step = {**step, 'events': copy.deepcopy(sent), 'due': manual.due()}
        if call == 'snapshot':
            step['answer'] = answer
        told.append(step)
    return {'name': name, 'steps': told}


BEGIN = {'session': 'claire', 'conv': 'conv-1', 'turn_id': 'tr-1'}


def tool(ident, name, category, label, **more):
    return {'id': ident, 'name': name, 'call_id': ident.split(':', 1)[1], 'category': category, 'label': label,
            **more}


def hub_cases():
    msg, think = 'cl:msg_1:0', 'cl:msg_1:1'
    yield hub_case('a message grows, is sent in pieces, and settles', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'message_text', id=msg, text='', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 20, 'message_text', id=msg, text='Hel', turn_id='tr-1'),
        at(T + 30, 'message_text', id=msg, text='Hello', turn_id='tr-1'),
        at(T + 120, 'fire'),
        at(T + 130, 'message_text', id=msg, text='Hello', turn_id='tr-1'),
        at(T + 140, 'message_text', id=msg, text='Hello there', turn_id='tr-1'),
        at(T + 150, 'done', id=msg, turn_id='tr-1'),
        at(T + 160, 'done', id=msg, turn_id='tr-1'),
        at(T + 170, 'message_text', id=msg, text='Hello there again', turn_id='tr-1'),
        at(T + 900, 'end_turn', status='completed'),
        at(T + 901, 'snapshot'),
        at(T + 902, 'fire'),
    ])
    yield hub_case('reasoning has a headline, which changes with what is thought', [
        at(T, 'begin_turn', **BEGIN, started_at_ms=T - 400),
        at(T + 5, 'reasoning_text', id=think, text='', turn_id='tr-1'),
        at(T + 10, 'reasoning_text', id=think, text='**Looking', turn_id='tr-1'),
        at(T + 20, 'reasoning_text', id=think, text='**Looking at the files**\nThere are two.', turn_id='tr-1'),
        at(T + 30, 'reasoning_text', id=think, text='**Looking at the files**\nThere are two. And more.', turn_id='tr-1'),
        at(T + 200, 'fire'),
        at(T + 210, 'reasoning_text', id='cl:msg_1:2', text='First sentence. Second one', turn_id='tr-1'),
        at(T + 220, 'reasoning_text', id='cl:msg_1:2', text='Rewritten whole', turn_id='tr-1'),
        at(T + 230, 'done', id=think, turn_id='tr-1'),
        at(T + 240, 'message_text', id=msg, text='Done thinking.', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 250, 'end_turn', status='completed'),
        at(T + 260, 'snapshot'),
    ])
    yield hub_case('tools that look at things are one group; another tool or a message ends it', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'message_text', id=msg, text='I will look.', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 20, 'tool_start', **tool('cl:t1', 'Read', 'read', 'src/a.rs'), turn_id='tr-1'),
        at(T + 30, 'tool_start', **tool('cl:t2', 'Grep', 'search', 'needle'), turn_id='tr-1'),
        at(T + 40, 'tool_start', **tool('cl:t1', 'Read', 'read', 'src/a.rs', input_preview={'file_path': 'src/a.rs'}),
           turn_id='tr-1'),
        at(T + 50, 'done', id='cl:t1', turn_id='tr-1', patch={'tool': {'output': {'exit_code': 0}}}),
        at(T + 60, 'tool_start', **tool('cl:t3', 'Bash', 'exec', 'npm test', command='npm test'), turn_id='tr-1'),
        at(T + 70, 'tool_start', **tool('cl:t4', 'LS', 'list', 'src'), turn_id='tr-1'),
        at(T + 80, 'message_text', id='cl:msg_2:0', text='Between.', turn_id='tr-1'),
        at(T + 90, 'tool_start', **tool('cl:t5', 'Glob', 'search', '*.rs'), turn_id='tr-1'),
        at(T + 100, 'done', id='cl:t2', status='failed', turn_id='tr-1'),
        at(T + 110, 'done', id='cl:t5', turn_id='tr-1'),
        at(T + 120, 'done', id='cl:t4', turn_id='tr-1'),
        at(T + 130, 'done', id='cl:t3', turn_id='tr-1'),
        at(T + 140, 'done', id='cl:msg_2:0', turn_id='tr-1'),
        at(T + 150, 'end_turn', status='completed'),
        at(T + 160, 'snapshot'),
    ])
    yield hub_case('a tool prints: its lines are held back, the last fifty kept, long ones cut', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'tool_start', **tool('cl:t1', 'Bash', 'exec', 'make', command='make'), turn_id='tr-1'),
        at(T + 20, 'tool_output', id='cl:t1', lines=['one', 'two'], total_lines=2, turn_id='tr-1'),
        at(T + 30, 'tool_output', id='cl:t1', lines=['three'], total_lines=3, turn_id='tr-1'),
        at(T + 280, 'fire'),
        at(T + 290, 'tool_output', id='cl:t1', lines=[f'line {n}' for n in range(70)], total_lines=73, turn_id='tr-1'),
        at(T + 300, 'tool_output', id='cl:t1', lines=['x' * 500, 'ü' * 401], total_lines=75, turn_id='tr-1'),
        at(T + 310, 'tool_output', id='cl:unknown', lines=['lost'], total_lines=1, turn_id='tr-1'),
        at(T + 320, 'patch', id='cl:t1', fields={'tool': {'explain': {'text': 'Builds it', 'level': 2, 'status': 'ready'}}},
           turn_id='tr-1'),
        at(T + 330, 'tool_output', id='cl:t1', lines=['last'], total_lines=76, turn_id='tr-1'),
        at(T + 340, 'done', id='cl:t1', turn_id='tr-1', patch={'tool': {'output': {'exit_code': 0}}}),
        at(T + 350, 'tool_output', id='cl:t1', lines=['after the end'], total_lines=77, turn_id='tr-1'),
        at(T + 360, 'snapshot'),
        at(T + 370, 'end_turn', status='completed'),
    ])
    yield hub_case('two tools at once: the status line names the newest that still runs', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'tool_start', **tool('cl:t1', 'Bash', 'exec', 'sleep 5', command='sleep 5'), turn_id='tr-1'),
        at(T + 20, 'tool_start', **tool('cl:t2', 'WebFetch', 'fetch', 'https://example.org'), status='pending',
           turn_id='tr-1'),
        at(T + 30, 'tool_start', **tool('cl:t3', 'Task', 'agent', 'look around'), turn_id='tr-1'),
        at(T + 40, 'done', id='cl:t3', turn_id='tr-1'),
        at(T + 50, 'done', id='cl:t1', turn_id='tr-1'),
        at(T + 60, 'tool_start', **tool('cl:t4', 'TodoWrite', 'todo', 'plan'), turn_id='tr-1'),
        at(T + 70, 'tool_start', **tool('cl:t5', 'mcp__x__do', 'mcp', 'do'), turn_id='tr-1'),
        at(T + 80, 'tool_start', **tool('cl:t6', 'Odd', 'other', 'Odd'), turn_id='tr-1'),
        at(T + 90, 'end_turn', status='completed'),
    ])
    yield hub_case('a turn stopped in the middle of a tool', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'message_text', id=msg, text='Running it.', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 20, 'tool_start', **tool('cl:t1', 'Bash', 'exec', 'sleep 60', command='sleep 60'), turn_id='tr-1'),
        at(T + 30, 'tool_output', id='cl:t1', lines=['waiting'], total_lines=1, turn_id='tr-1'),
        at(T + 40, 'end_turn', status='interrupted'),
        at(T + 50, 'done', id='cl:t1', turn_id='tr-1'),
        at(T + 60, 'message_text', id=msg, text='Running it. More.', turn_id='tr-1'),
        at(T + 70, 'tool_start', **tool('cl:t2', 'Read', 'read', 'late.rs'), turn_id='tr-1'),
        at(T + 80, 'snapshot'),
    ])
    yield hub_case('a turn that failed says why', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'reasoning_text', id=think, text='Hm', turn_id='tr-1'),
        at(T + 20, 'end_turn', status='failed',
           error={'reason': 'account_plan', 'message': 'This plan has no such model.', 'detail': 'the words'}),
        at(T + 30, 'snapshot'),
        at(T + 40, 'begin_turn', session='claire', conv='conv-1', turn_id='tr-2'),
        at(T + 50, 'end_turn', status='failed'),
        at(T + 60, 'begin_turn', session='claire', conv='conv-1', turn_id='tr-3'),
        at(T + 70, 'end_turn', status='failed', error={}),
    ])
    yield hub_case('a new turn over one that never settled; the same turn begun twice', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 5, 'begin_turn', **BEGIN),
        at(T + 10, 'tool_start', **tool('cl:t1', 'Bash', 'exec', 'make', command='make'), turn_id='tr-1'),
        at(T + 20, 'message_text', id=msg, text='Half a', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 30, 'message_text', id=msg, text='Half a sentence', turn_id='tr-1'),
        at(T + 40, 'begin_turn', session='', conv='conv-1', turn_id='tr-2'),
        at(T + 50, 'message_text', id=msg, text='Half a sentence, late', turn_id='tr-1'),
        at(T + 60, 'tool_output', id='cl:t1', lines=['late'], total_lines=1, turn_id='tr-1'),
        at(T + 70, 'done', id='cl:t1', turn_id='tr-1'),
        at(T + 80, 'message_text', id='cl:msg_2:0', text='New turn.', phase='final', row_id='r-2', turn_id='tr-2'),
        at(T + 90, 'end_turn', status='completed'),
        at(T + 100, 'begin_turn', session='claire', conv='conv-1', turn_id='tr-2'),
        at(T + 110, 'end_turn', status='completed'),
        at(T + 120, 'end_turn', status='completed'),
    ])
    yield hub_case('a turn waits for an account', [
        at(T, 'account_recovery', message='Too early'),
        at(T + 1, 'begin_turn', **BEGIN),
        at(T + 10, 'account_recovery', message='Waiting for an account with available usage'),
        at(T + 20, 'account_recovery', message='Waiting for an account with available usage'),
        at(T + 5000, 'account_recovery', message='Still waiting'),
        at(T + 6000, 'message_text', id=msg, text='Back.', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 6010, 'account_recovery', message=None),
        at(T + 6020, 'account_recovery', message=None),
        at(T + 6030, 'end_turn', status='completed'),
        at(T + 6040, 'account_recovery', message='After the end'),
        at(T + 6050, 'account_recovery', message=None),
    ])
    yield hub_case('before any turn nothing is shown', [
        at(T, 'snapshot'),
        at(T + 1, 'message_text', id=msg, text='x', turn_id=None),
        at(T + 2, 'tool_start', **tool('cl:t1', 'Read', 'read', 'a.rs')),
        at(T + 3, 'done', id='cl:t1'),
        at(T + 4, 'end_turn', status='completed'),
        at(T + 5, 'fire'),
        at(T + 6, 'patch', id='cl:t1', fields={'x': 1}),
        at(T + 7, 'item', id='cl:tr-1:plan', kind='plan', fields={'plan': {'steps': []}}),
    ])
    yield hub_case('what names no turn is taken while any turn is held; what names another is not', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'message_text', id=msg, text='No turn named.', turn_id=None),
        at(T + 20, 'message_text', id='cl:msg_9:0', text='Another turn named.', turn_id='tr-0'),
        at(T + 30, 'tool_start', **tool('cl:t1', 'Read', 'read', 'a.rs')),
        at(T + 40, 'end_turn', status='completed'),
        at(T + 50, 'tool_start', **tool('cl:t2', 'Read', 'read', 'b.rs')),
        at(T + 60, 'message_text', id='cl:msg_3:0', text='After the end, no turn named.'),
        at(T + 70, 'done', id='cl:t2'),
        at(T + 80, 'snapshot'),
    ])
    yield hub_case('a plan is replaced whole; a compaction runs and ends', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'item', id='cl:tr-1:plan', kind='plan',
           fields={'plan': {'steps': [{'text': 'read', 'status': 'in_progress'}, {'text': 'write', 'status': 'pending'}]}},
           turn_id='tr-1'),
        at(T + 20, 'item', id='cl:tr-1:plan', kind='plan',
           fields={'plan': {'steps': [{'text': 'read', 'status': 'completed'}]}}, turn_id='tr-1'),
        at(T + 30, 'item', id='cl:tr-1:compaction', kind='compaction', fields={'status': 'running'}, turn_id='tr-1'),
        at(T + 40, 'message_text', id=msg, text='While compacting.', turn_id='tr-1'),
        at(T + 50, 'done', id='cl:tr-1:compaction', turn_id='tr-1'),
        at(T + 60, 'item', id='cl:tr-1:diff', kind='diff',
           fields={'diff': {'added': 2, 'removed': 1, 'files': [{'path': 'a.rs', 'added': 2, 'removed': 1}], 'preview': '+a'}},
           turn_id='tr-1'),
        at(T + 70, 'patch', id='cl:tr-1:diff', fields={'diff': {'added': 3, 'files': None}}, turn_id='tr-1'),
        at(T + 80, 'end_turn', status='completed'),
        at(T + 90, 'snapshot'),
    ])
    yield hub_case('another conversation counts from one again', [
        at(T, 'begin_turn', **BEGIN),
        at(T + 10, 'message_text', id=msg, text='In the first.', phase='final', row_id='r-1', turn_id='tr-1'),
        at(T + 20, 'end_turn', status='completed'),
        at(T + 30, 'begin_turn', session='claire-2', conv='conv-2', turn_id='tr-2'),
        at(T + 40, 'message_text', id='cl:msg_2:0', text='In the second.', phase='commentary', row_id='r-2',
           turn_id='tr-2'),
        at(T + 50, 'snapshot'),
        at(T + 60, 'begin_turn', session='', conv='conv-3', turn_id='tr-3'),
        at(T + 70, 'snapshot'),
    ])
    for seed in range(1, 7):
        yield hub_case(f'a random walk, seed {seed}', walk(seed))


def walk(seed, count=150):
    """Calls in an order nobody would write: what the hub does with each is what is held to."""
    rng = random.Random(seed)
    now, turn, steps = T, 0, []
    messages, thoughts, tools = ['cl:m1:0', 'cl:m1:1', 'cl:m2:0'], ['cl:m1:9', 'cl:m2:9'], ['cl:t1', 'cl:t2', 'cl:t3', 'cl:t4']
    words = ['alpha', ' beta', '. Gamma', '\n**Bold** line', ' delta!', ' epsilon? Zeta', '\n# Heading', ' eta', '']
    kinds = [('Read', 'read', 'a.rs', None), ('Grep', 'search', 'needle', None), ('Bash', 'exec', 'npm test', 'npm test'),
             ('LS', 'list', 'src', None), ('Edit', 'edit', 'b.rs', None), ('Task', 'agent', 'look', None)]
    texts: dict[str, str] = {}
    printed: dict[str, int] = {}
    running = False
    for _ in range(count):
        now += rng.choice([0, 1, 5, 40, 130, 300])
        roll = rng.random()
        named = rng.choice([f'tr-{turn}'] * 6 + [None, 'tr-old'])
        # A turn that ended is mostly followed by the next one soon, so that most steps meet a running turn.
        if turn == 0 or roll < 0.04 or (not running and rng.random() < 0.6):
            turn += 1
            running = True
            steps.append(at(now, 'begin_turn', session=rng.choice(['claire', 'claire', '']), conv='conv-1',
                            turn_id=f'tr-{rng.choice([turn, turn, turn - 1])}',
                            started_at_ms=rng.choice([None, 0, now - 500])))
        elif roll < 0.08:
            running = False
            steps.append(at(now, 'end_turn', status=rng.choice(['completed', 'interrupted', 'failed']),
                            error=rng.choice([None, {'reason': 'failed', 'message': 'It broke', 'detail': 'words'}])))
        elif roll < 0.19:
            steps.append(at(now, 'fire'))
        elif roll < 0.23:
            steps.append(at(now, 'snapshot'))
        elif roll < 0.27:
            steps.append(at(now, 'account_recovery', message=rng.choice([None, 'Waiting', 'Waiting', 'Waiting more'])))
        elif roll < 0.49:
            ident = rng.choice(messages)
            texts[ident] = texts.get(ident, '') + rng.choice(words) if rng.random() < 0.9 else rng.choice(words)
            steps.append(at(now, 'message_text', id=ident, text=texts[ident], phase=rng.choice([None, 'final', 'commentary']),
                            row_id=rng.choice([None, 'r-1']), turn_id=named))
        elif roll < 0.63:
            ident = rng.choice(thoughts)
            texts[ident] = texts.get(ident, '') + rng.choice(words) if rng.random() < 0.9 else rng.choice(words)
            steps.append(at(now, 'reasoning_text', id=ident, text=texts[ident], turn_id=named))
        elif roll < 0.77:
            ident = rng.choice(tools)
            name, category, label, command = rng.choice(kinds)
            steps.append(at(now, 'tool_start', **tool(ident, name, category, label), command=command,
                            input_preview=rng.choice([None, {'file_path': 'a.rs', 'limit': 5}]),
                            status=rng.choice(['running', 'running', 'pending']), turn_id=named))
        elif roll < 0.87:
            ident = rng.choice(tools)
            more = rng.choice([1, 2, 2, 55])
            printed[ident] = printed.get(ident, 0) + more
            steps.append(at(now, 'tool_output', id=ident, lines=[f'{ident[3:]}.{n}' for n in range(more)],
                            total_lines=printed[ident], turn_id=named))
        elif roll < 0.93:
            steps.append(at(now, 'done', id=rng.choice(messages + thoughts + tools),
                            status=rng.choice(['completed', 'completed', 'failed']),
                            patch=rng.choice([None, {'tool': {'output': {'exit_code': 0}}}]), turn_id=named))
        elif roll < 0.97:
            steps.append(at(now, 'patch', id=rng.choice(messages + thoughts + tools),
                            fields=rng.choice([{'tool': {'diff': {'added': 1, 'removed': 0, 'files': [], 'preview': '+x'}}},
                                               {'title': 'Patched'}, {'tool': {'explain': None}}]), turn_id=named))
        else:
            ident, kind = rng.choice([('cl:plan', 'plan'), ('cl:compaction', 'compaction')])
            steps.append(at(now, 'item', id=ident, kind=kind,
                            fields=rng.choice([{'status': 'running'}, {'plan': {'steps': [{'text': 'a', 'status': 'pending'}]}}]),
                            turn_id=named))
    steps.append(at(now + 1000, 'fire'))
    steps.append(at(now + 1001, 'snapshot'))
    return steps


# --- Claude Code's stream ---

def stream(event):
    return {'type': 'stream_event', 'event': event, 'parent_tool_use_id': None}


def message_start(ident):
    return stream({'type': 'message_start', 'message': {'id': ident, 'role': 'assistant', 'content': []}})


def block_start(index, block):
    return stream({'type': 'content_block_start', 'index': index, 'content_block': block})


def delta(index, kind, **fields):
    return stream({'type': 'content_block_delta', 'index': index, 'delta': {'type': kind, **fields}})


def block_stop(index):
    return stream({'type': 'content_block_stop', 'index': index})


def result_of(call, content, error=False):
    part = {'type': 'tool_result', 'tool_use_id': call, 'content': content}
    if error:
        part['is_error'] = True
    return {'type': 'user', 'message': {'role': 'user', 'content': [part]}}


def claude_streams():
    edit = json.dumps({'file_path': '/src/a.rs', 'old_string': 'one\ntwo\nthree\n', 'new_string': 'one\n2\nthree\nfour\n'})
    yield 'a turn that thinks, says, reads and edits', [
        {'begin': 'tr-1'},
        {'event': {'type': 'system', 'subtype': 'init', 'session_id': 's-1', 'model': 'm'}},
        {'event': message_start('msg_01A')},
        {'event': block_start(0, {'type': 'thinking', 'thinking': ''})},
        {'event': delta(0, 'thinking_delta', thinking='**Finding the file**\n')},
        {'event': delta(0, 'thinking_delta', thinking='It is in src.')},
        {'event': delta(0, 'signature_delta', signature='abc')},
        {'event': block_stop(0)},
        {'event': block_start(1, {'type': 'text', 'text': ''})},
        {'event': delta(1, 'text_delta', text='I will ')},
        {'event': delta(1, 'text_delta', text='look.')},
        {'fire': True},
        {'event': block_stop(1)},
        {'event': block_start(2, {'type': 'tool_use', 'id': 'toolu_01R', 'name': 'Read', 'input': {}})},
        {'event': delta(2, 'input_json_delta', partial_json='{"file_path": "/src')},
        {'event': delta(2, 'input_json_delta', partial_json='/a.rs", "limit": 20}')},
        {'event': block_stop(2)},
        {'event': block_start(3, {'type': 'tool_use', 'id': 'toolu_01G', 'name': 'Grep', 'input': {}})},
        {'event': delta(3, 'input_json_delta', partial_json='{"pattern": "needle", "path": "src"}')},
        {'event': block_stop(3)},
        {'event': stream({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}})},
        {'event': stream({'type': 'message_stop'})},
        {'event': {'type': 'assistant', 'message': {'id': 'msg_01A', 'content': [{'type': 'text', 'text': 'I will look.'}]}}},
        {'event': result_of('toolu_01R', 'fn main() {}\n// end\n')},
        {'event': result_of('toolu_01G', [{'type': 'text', 'text': 'src/a.rs:1:needle'}, {'type': 'text', 'text': 'src/b.rs:9:needle\nmore'}])},
        {'event': message_start('msg_01B')},
        {'event': block_start(0, {'type': 'text', 'text': 'Now the edit.'})},
        {'event': block_stop(0)},
        {'event': block_start(1, {'type': 'tool_use', 'id': 'toolu_01E', 'name': 'Edit', 'input': {}})},
        {'event': delta(1, 'input_json_delta', partial_json=edit[:25])},
        {'event': delta(1, 'input_json_delta', partial_json=edit[25:])},
        {'event': block_stop(1)},
        {'event': stream({'type': 'message_stop'})},
        {'event': result_of('toolu_01E', 'The file was edited.')},
        {'event': message_start('msg_01C')},
        {'event': block_start(0, {'type': 'text', 'text': ''})},
        {'event': delta(0, 'text_delta', text='Done: one line changed, one added.')},
        {'event': block_stop(0)},
        {'event': stream({'type': 'message_stop'})},
        {'event': {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Done.'}},
        {'end': 'completed'},
    ]
    yield 'a command that fails, one that prints much, and a file written', [
        {'begin': 'tr-1'},
        {'event': message_start('msg_02A')},
        {'event': block_start(0, {'type': 'tool_use', 'id': 'toolu_02B', 'name': 'Bash', 'input': {}})},
        {'event': delta(0, 'input_json_delta', partial_json='{"command": "cat a.rs", "description": "Read it"}')},
        {'event': block_stop(0)},
        {'event': block_start(1, {'type': 'tool_use', 'id': 'toolu_02C', 'name': 'Bash', 'input': {'command': 'npm test'}})},
        {'event': delta(1, 'input_json_delta', partial_json='{"command": "npm test", "timeout": 5000, "run_in_background": false}')},
        {'event': block_stop(1)},
        {'event': stream({'type': 'message_stop'})},
        {'event': result_of('toolu_02B', 'cat: a.rs: No such file or directory', error=True)},
        {'event': result_of('toolu_02C', '\n'.join(f'test {n} ... ok' for n in range(130)))},
        {'event': message_start('msg_02B')},
        {'event': block_start(0, {'type': 'tool_use', 'id': 'toolu_02W', 'name': 'Write', 'input': {}})},
        {'event': delta(0, 'input_json_delta', partial_json=json.dumps({'file_path': '/notes/new.md', 'content': '# Title\n\nA line.\n'}))},
        {'event': block_stop(0)},
        {'event': block_start(1, {'type': 'tool_use', 'id': 'toolu_02M', 'name': 'MultiEdit', 'input': {}})},
        {'event': delta(1, 'input_json_delta', partial_json=json.dumps({'file_path': 'b.rs', 'edits': [
            {'old_string': 'a\nb', 'new_string': 'a\nc'}, {'old_string': 'x', 'new_string': ''}, 'not an edit']}))},
        {'event': block_stop(1)},
        {'event': stream({'type': 'message_stop'})},
        {'event': result_of('toolu_02W', '')},
        {'event': result_of('toolu_02M', None)},
        {'event': result_of('toolu_unknown', 'nobody asked')},
        {'end': 'completed'},
    ]
    yield 'what is not understood is left alone', [
        {'begin': 'tr-1'},
        {'event': {'type': 'stream_event', 'event': 'not an object'}},
        {'event': {'type': 'stream_event'}},
        {'event': {'type': 'other'}},
        {'event': message_start('msg_03A')},
        {'event': block_start(0, {'type': 'redacted_thinking', 'data': 'xxxx'})},
        {'event': block_stop(0)},
        {'event': block_start(1, {'type': 'server_tool_use', 'id': 'srvtoolu_03S', 'name': 'web_search', 'input': {}})},
        {'event': delta(1, 'input_json_delta', partial_json='{"query": "rust live items"}')},
        {'event': block_stop(1)},
        {'event': block_start(2, {'type': 'tool_use', 'name': 'NoId', 'input': {}})},
        {'event': block_stop(2)},
        {'event': block_start(3, {'type': 'tool_use', 'id': 'toolu_03J', 'name': 'Read', 'input': {}})},
        {'event': delta(3, 'input_json_delta', partial_json='{"file_path": ')},
        {'event': block_stop(3)},
        {'event': block_start(4, {'type': 'tool_use', 'id': 'toolu_03L', 'name': 'Read', 'input': {}})},
        {'event': delta(4, 'input_json_delta', partial_json='["a", "list"]')},
        {'event': block_stop(4)},
        {'event': block_start(5, {'type': 'image', 'source': {}})},
        {'event': delta(5, 'text_delta', text='to no block')},
        {'event': block_stop(5)},
        {'event': block_start('six', {'type': 'text', 'text': 'no index'})},
        {'event': delta(9, 'text_delta', text='to a block that never began')},
        {'event': block_start(6, {'type': 'text', 'text': 'Still here.'})},
        {'event': stream({'type': 'message_stop'})},
        {'event': {'type': 'user', 'message': {'role': 'user', 'content': 'a text, no parts'}}},
        {'event': {'type': 'user', 'message': None}},
        {'event': {'type': 'user', 'message': {'content': ['a text part', {'type': 'text', 'text': 'x'},
                                                       {'type': 'tool_result', 'tool_use_id': 'srvtoolu_03S',
                                                        'content': [{'type': 'text', 'text': 'found'}, 'bare', 5, None]}]}}},
        {'end': 'interrupted'},
    ]
    yield 'a turn the hub was never told of shows nothing', [
        {'event': message_start('msg_04A')},
        {'event': block_start(0, {'type': 'text', 'text': 'Unseen.'})},
        {'event': block_stop(0)},
        {'begin': 'tr-other'},
        {'event': message_start('msg_04B')},
        {'event': block_start(0, {'type': 'text', 'text': 'Of another turn.'})},
        {'event': block_stop(0)},
    ]


def claude_case(name, steps):
    from lib import live_core, live_hub
    from lib.live_claude import ClaudeLiveItems
    hub, now, sent, manual = a_hub()
    live_core.install(hub)
    # The turn is begun by this script; the reader is told it is there, as the Host's own check would say.
    live_hub.ensure_turn = lambda _hub, _agent_id, trace='': {'backend': 'claude'}
    reader = ClaudeLiveItems(agent_id=AGENT, trace_id='tr-1', row_id=lambda key: 'r-' + key.removeprefix('msg_'))
    told = []
    clock = T
    for step in steps:
        clock += 7
        now['ms'] = clock
        del sent[:]
        if 'begin' in step:
            hub.begin_turn(agent_id=AGENT, session='claire', conv='conv-1', turn_id=step['begin'])
        elif 'end' in step:
            hub.end_turn(AGENT, status=step['end'])
        elif 'fire' in step:
            manual.fire()
        else:
            reader.on_event(step['event'])
        told.append({**step, 'at': clock, 'events': copy.deepcopy(sent), 'due': manual.due()})
    now['ms'] = clock + 500
    del sent[:]
    snapshot = hub.snapshot(agent_id=AGENT)
    live_core.install(None)
    return {'name': name, 'steps': told, 'after': {'at': clock + 500, 'events': copy.deepcopy(sent), 'snapshot': snapshot}}


# --- what is said of one thing ---

TITLES = [
    '', '   ', '\n\n', 'One sentence.', 'First sentence. Second one.', 'No end of sentence', 'Why? Because.',
    'Stop! Now.', 'A dot.no space after', 'Dots... and more. End', '**Bold title**\nBody', '  **Spaced bold** rest',
    '**Unclosed bold\nBody', '****', '**a**', '***x***', '** **', '**Two** **bolds**', 'Text **bold** inside. Next.',
    '# Heading\nBody', '### Deep heading here', '#NoSpace', '#', '##', '# ', '#   ', '#\tTabbed', '   ## Indented',
    'x' * 200, '**' + 'y' * 120 + '**', '# ' + 'z' * 100, 'Word ' * 30, 'Ends with space.   ', 'Tab\tafter. dot',
    'Line one\rLine two', 'Line one\x0bLine two', 'Line one Line two', ' Non-breaking start. Next',
    'Unicode ünïcödé. Näxt', 'é combining. Next', '!', '?', '.', '. ', '... ', 'a.　b', '**bold **',
    '\x1c**x**', '*single*', '_under_', '1. A list item. More', '- dash item', '> quote',
]

PREVIEWS = [
    None, 'text', 5, [], {}, {'a': 'x'}, {'a': 1, 'b': 2.5, 'c': True, 'd': None, 'e': [1], 'f': {'g': 1}, 'h': False},
    {str(n): n for n in range(12)}, {'long': 'x' * 400, 'longer': 'x' * 401, 'after': 'y'},
    {'uni': 'ü' * 400, 'uni2': 'ü' * 401}, {'n': None, 'l': [], 'o': {}, **{f'k{n}': 'v' for n in range(9)}},
    {'zero': 0, 'empty': '', 'neg': -3, 'big': 10 ** 15, 'float': 1e-7},
]

TAILS = [
    None, '', 'one line', 'two\nlines', 'ends with newline\n', '\n\nleading', 'a\r\nb\rc', 'x' * 450,
    '\n'.join(f'line {n}' for n in range(120)), '\n'.join('ü' * 410 for _ in range(3)), [],
    [{'type': 'text', 'text': 'part one'}, {'type': 'text', 'text': 'part two\nmore'}],
    [{'type': 'text'}, {'type': 'text', 'text': None}, {'type': 'text', 'text': ''}], ['bare', 'texts'],
    [{'type': 'image', 'source': {}}, {'type': 'text', 'text': 'after an image'}], [1, None, True, 2.5],
    [{'text': 5}, {'text': ['a']}], 0, 7, False, True, 'a\x0cb\x1cc\x85d',
]


def diff_inputs():
    yield 'Edit', {'file_path': '/a/b.rs', 'old_string': 'one\ntwo\nthree', 'new_string': 'one\n2\nthree\nfour'}
    yield 'Edit', {'file_path': 'b.rs', 'old_string': 'same', 'new_string': 'same'}
    yield 'Edit', {'old_string': 'gone', 'new_string': ''}
    yield 'Edit', {'filePath': 'camel.rs', 'old_string': '', 'new_string': 'new\nlines'}
    yield 'edit', {'path': 'p.rs', 'old_string': '+++plus\n---minus', 'new_string': '+++plus changed\n---minus'}
    yield 'Edit', {}
    yield 'Edit', None
    yield 'Edit', 'not an object'
    yield 'Edit', {'file_path': 5, 'old_string': 'a', 'new_string': 'b'}
    yield 'str_replace', {'path': 'x', 'old_string': 'a\nb\nc\nd\ne\nf\ng', 'new_string': 'a\nB\nc\nd\ne\nF\ng'}
    yield 'MultiEdit', {'file_path': 'm.rs', 'edits': [{'old_string': 'a', 'new_string': 'b'},
                                                      {'old_string': 'c\nd', 'new_string': 'c'}, 'skip', None, 5]}
    yield 'MultiEdit', {'file_path': 'm.rs', 'edits': []}
    yield 'MultiEdit', {'file_path': 'm.rs'}
    yield 'MultiEdit', {'file_path': 'm.rs', 'edits': 'not a list'}
    yield 'Write', {'file_path': 'w.md', 'content': 'a\nb\nc\n'}
    yield 'Write', {'file_path': 'w.md', 'content': ''}
    yield 'Write', {'file_path': 'w.md', 'content': 5}
    yield 'Write', {'file_path': 'w.md', 'content': 5, 'diff': '+x\n-y\n context'}
    yield 'create', {'path': 'c.md', 'content': '\n'.join(f'line {n}' for n in range(60))}
    yield 'write_file', {'path': 'c.md', 'content': 'x' * 450 + '\nshort'}
    yield 'apply_patch', {'patch': '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n context\n+++not a header\n'}
    yield 'file_change', {'diff': '+only added', 'path': 'f.rs'}
    yield 'anything', {'diff': '', 'patch': '-removed'}
    yield 'anything', {'diff': 5}
    yield 'Read', {'file_path': 'r.rs'}
    yield 'Bash', {'command': 'ls'}
    yield '', {}
    rng = random.Random(11)
    lines = [f'line {n}' for n in range(40)]
    for _ in range(40):
        old = [rng.choice(lines) for _ in range(rng.randint(0, 14))]
        new = list(old)
        for _ in range(rng.randint(0, 5)):
            where = rng.randint(0, len(new))
            action = rng.choice(['insert', 'delete', 'replace'])
            if action == 'insert' or not new:
                new.insert(where, rng.choice(lines))
            elif action == 'delete':
                del new[min(where, len(new) - 1)]
            else:
                new[min(where, len(new) - 1)] = rng.choice(lines)
        yield 'Edit', {'file_path': 'r.rs', 'old_string': '\n'.join(old), 'new_string': '\n'.join(new)}


def text_pairs():
    """Pairs of texts for the diff itself: short ones, and long ones where a line that is a
    hundredth of the text and more starts no match."""
    yield '', ''
    yield 'a', 'a'
    yield '', 'a\nb'
    yield 'a\nb', ''
    yield 'a\nb\nc', 'a\nc'
    yield 'a\nb\nc\nd\ne\nf\ng\nh', 'a\nb\nX\nd\ne\nf\nY\nh'
    yield 'x\nx\nx\nx', 'x\nx\ny\nx\nx'
    rng = random.Random(5)
    for size in (5, 30, 150, 199, 200, 201, 260, 400):
        alphabet = [f'w{n}' for n in range(max(3, size // 6))] + ['', '}', '    }', 'end']
        for _ in range(4):
            old = [rng.choice(alphabet) for _ in range(size)]
            new = list(old)
            for _ in range(rng.randint(1, 12)):
                where = rng.randint(0, max(0, len(new) - 1))
                action = rng.choice(['insert', 'delete', 'replace', 'move'])
                if action == 'insert' or not new:
                    new.insert(where, rng.choice(alphabet))
                elif action == 'delete':
                    del new[where]
                elif action == 'replace':
                    new[where] = rng.choice(alphabet)
                else:
                    new.insert(rng.randint(0, len(new) - 1), new.pop(where))
            yield '\n'.join(old), '\n'.join(new)
    # Many of the same line, in a text long enough for them to be "popular".
    block = ['{', '    x', '}', ''] * 80
    changed = list(block)
    changed[101] = '    y'
    del changed[200:204]
    changed.insert(17, 'new')
    yield '\n'.join(block), '\n'.join(changed)


# What the Rust Host does not have yet: a turn's `audience` (coordination between agents, the
# Python Host's PR 482, its contract 108). Left out of what is written, here and nowhere else;
# the port of PR 482 puts it back.
NOT_YET_KEYS = frozenset({'audience'})


def without_not_yet(value):
    """`value` without the keys of NOT_YET_KEYS on a turn (a dict that names its `turn_id`)."""
    if isinstance(value, list):
        return [without_not_yet(item) for item in value]
    if isinstance(value, dict):
        turn = 'turn_id' in value
        return {key: without_not_yet(item) for key, item in value.items() if not (turn and key in NOT_YET_KEYS)}
    return value


def written(vectors: dict) -> str:
    """The vectors as a file: one case on a line, so that a change to one case is a change to one line."""
    out = ['{']
    names = list(vectors)
    for name in names:
        value, comma = vectors[name], '' if name == names[-1] else ','
        if not isinstance(value, list):
            out.append(f' {json.dumps(name)}: {json.dumps(value, ensure_ascii=False)}{comma}')
            continue
        out.append(f' {json.dumps(name)}: [')
        out.extend('  ' + json.dumps(case, ensure_ascii=False) + (',' if at_place < len(value) - 1 else '')
                   for at_place, case in enumerate(value))
        out.append(' ]' + comma)
    out.append('}')
    return '\n'.join(out) + '\n'


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib.live_core import reasoning_title
    from lib.live_hub import _preview
    from lib.tool_output import diff_stats, output_tail

    fixtures = []
    for path in sorted((REPO / 'contract' / 'live').glob('*.json')):
        fixtures.append({'name': path.stem, **json.loads(path.read_text())})
    diffs = []
    for name, tool_input in diff_inputs():
        try:
            diffs.append({'name': name, 'input': tool_input, 'diff': diff_stats(name, tool_input)})
        except (AttributeError, TypeError):
            # What the library itself breaks on: the Host shows no diff then.
            diffs.append({'name': name, 'input': tool_input, 'diff': None})
    tails = []
    for content in TAILS:
        tail, total = output_tail(content)
        tails.append({'content': content, 'tail': tail, 'total': total})
    vectors = {
        'about': 'Written by server-rs/tools/live-vectors.py from server/lib/live_core.py, live_projection.py, '
                 'live_claude.py, tool_output.py and contract/live. Do not edit by hand.',
        'fixtures': fixtures,
        'titles': [{'text': text, 'title': reasoning_title(text)} for text in TITLES],
        'previews': [{'input': given, 'preview': _preview(given)} for given in PREVIEWS],
        'tails': tails,
        'diffs': diffs,
        'unified': [{'a': a, 'b': b, 'lines': list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm='', n=1))}
                    for a, b in text_pairs()],
        'hub': list(hub_cases()),
        'claude': [claude_case(name, steps) for name, steps in claude_streams()],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = without_not_yet(vectors)
    OUT.write_text(written(vectors))
    events = sum(len(step['events']) for case in vectors['hub'] + vectors['claude'] for step in case['steps'])
    print(f'{OUT.relative_to(REPO)}: {len(fixtures)} recorded streams, {len(vectors["titles"])} titles, '
          f'{len(vectors["previews"])} previews, {len(tails)} tails, {len(diffs)} diffs, '
          f'{len(vectors["unified"])} unified diffs, {len(vectors["hub"])} hub cases and '
          f'{len(vectors["claude"])} Claude streams with {events} events; {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
