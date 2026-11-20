#!/usr/bin/env python3
"""Writes the vectors the Rust Host's calls are tested against.

A group call is the Host's record of a hands-free conversation with several agents: who is in
it, who is on hold, who has the floor (`docs/group-calls.md`, `server/lib/group_calls.py`). The
phone captures and plays; the record changes only by an explicit request (the app's buttons, an
agent's `clarp-admin call`) and by routing, which moves the floor to whoever was spoken to.

This runs that module itself, with agents, a clock, a stream and ids made up here, through
scripts of requests, and writes what it answered, announced and noted after each step to
`crates/clarp-kernel/tests/fixtures/calls-vectors.json`; and whom a typed or spoken name means,
for names made up here.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/calls-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. The database it touches is a new one in that directory. No Host is touched or
started.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'calls-vectors.json'

# The agents of a Host that may be asked into a call, in the order they were made. A helper is
# another agent's sub-agent: it is found by its session, never by its name.
ROSTER = [
    {'session': 'theo-97e5', 'agent_id': 'a-theo', 'persona': 'Theo'},
    {'session': 'mike-86db', 'agent_id': 'a-mike', 'persona': 'Mike'},
    {'session': 'mikael', 'agent_id': 'a-mikael', 'persona': 'Mikael'},
    {'session': 'nadia', 'agent_id': 'a-nadia', 'persona': 'Nadia'},
    {'session': 'omar-1', 'agent_id': 'a-omar', 'persona': 'Omar'},
    {'session': 'sam-1', 'agent_id': 'a-sam1', 'persona': 'Sam'},
    {'session': 'sam-2', 'agent_id': 'a-sam2', 'persona': 'Sam'},
    {'session': 'mike-helper-1', 'agent_id': 'a-mikeh', 'persona': 'Mike', 'role': 'helper',
     'parent_agent_id': 'a-mike'},
    {'session': 'anna-maria-3f', 'agent_id': 'a-anna', 'persona': 'Anna Maria'},
    {'session': 'zoë', 'agent_id': 'a-zoe', 'persona': 'Zoë'},
    {'session': 'codex-8194', 'agent_id': 'a-codex', 'persona': 'Codex-8194'},
    {'session': 'janitor-one', 'agent_id': 'a-jan', 'persona': 'Sweeper', 'is_janitor': 1},
    {'session': 'b1', 'agent_id': 'a-b1', 'persona': 'Bo'},
    {'session': 'b2', 'agent_id': 'a-b2', 'persona': 'Bob'},
    {'session': 'p1', 'agent_id': 'a-p1', 'persona': 'One'},
    {'session': 'p2', 'agent_id': 'a-p2', 'persona': 'Two'},
    {'session': 'p3', 'agent_id': 'a-p3', 'persona': 'Three'},
    {'session': 'p4', 'agent_id': 'a-p4', 'persona': 'Four'},
    {'session': 'p5', 'agent_id': 'a-p5', 'persona': 'Five'},
    {'session': 'p6', 'agent_id': 'a-p6', 'persona': 'Six'},
    {'session': 'p7', 'agent_id': 'a-p7', 'persona': 'Seven'},
    {'session': 'p8', 'agent_id': 'a-p8', 'persona': 'Eight'},
    {'session': 'p9', 'agent_id': 'a-p9', 'persona': 'Nine'},
]

# A name, and the sessions already in the call it is said in.
NAMES = [
    ('theo-97e5', []), ('THEO-97E5', []), ('Theo', []), ('  theo  ', []), ('theo', []), ('teo', []), ('theodor', []),
    ('the', []), ('th', []), ('Mike', []), ('mike', []), ('mike-86db', []), ('mike-helper-1', []), ('Mik', []),
    ('Mikael', []), ('mikae', []), ('mikel', []), ('michael', []), ('Mike!', []), ('m i k e', []), ('Sam', []),
    ('Sam', ['sam-2']), ('Sam', ['sam-1', 'sam-2']), ('sam-1', []), ('sam', ['theo-97e5']), ('samm', []),
    ('samm', ['sam-1']), ('Nadia', []), ('nadja', []), ('nadi', []), ('Omar', []), ('omar-1', []), ('omar1', []),
    ('omer', []), ('Anna Maria', []), ('annamaria', []), ('anna', []), ('maria', []), ('anna-maria-3f', []),
    ('anna-maria', []), ('Zoë', []), ('zoe', []), ('zo', []), ('ZOË', []), ('Codex-8194', []), ('codex', []),
    ('codex8194', []), ('8194', []), ('Sweeper', []), ('janitor-one', []), ('sweep', []), ('', []), ('   ', []),
    ('!!!', []), ('nobody', []), ('Bo', []), ('bob', []), ('b1', []), ('b', []), ('bo', ['b2']), ('boo', []),
    ('boo', ['b2']), ('One', []), ('on', []), ('one', []), ('p1', []), ('nine', []), ('nin', []), ('straße', []),
]


def start(caller, agents, **more):
    return {'do': 'start', 'caller': caller, 'agents': agents, **more}


def change(caller, action, agent, **more):
    return {'do': 'change', 'caller': caller, 'action': action, 'agent': agent, **more}


def end(caller, **more):
    return {'do': 'end', 'caller': caller, **more}


def route(caller, session, call='live'):
    return {'do': 'route', 'caller': caller, 'session': session, 'call': call}


def routed(caller, session, call='live'):
    return {'do': 'routed', 'caller': caller, 'session': session, 'call': call}


def look(caller, **more):
    return {'do': 'snapshot', 'caller': caller, **more}


def tick(ms):
    return {'do': 'tick', 'ms': ms}


A, B, ADMIN = 'device_a', 'device_b', 'administrator'
HOUR = 60 * 60 * 1000

SCRIPTS = [
    {'name': 'a call starts, others join, one is held, the floor passes, and it ends',
     'steps': [look(A), start(A, ['Theo', 'mike', 'Nadia']), look(A), route(A, 'mike-86db'), route(A, 'mike-86db'),
               change(A, 'hold', 'Mike'), change(A, 'hold', 'Mike'), route(A, 'mike-86db'), change(A, 'resume', 'mike'),
               change(A, 'resume', 'mike'), change(A, 'switch', 'nadia'), change(A, 'switch', 'nadia'),
               change(A, 'remove', 'theo'), change(A, 'remove', 'theo'), change(A, 'add', 'theo'),
               change(A, 'add', 'theo'), end(A), end(A), look(A)]},
    {'name': 'transfer: the one who has the floor is put on hold and another is called',
     'steps': [start(A, ['theo']), change(A, 'transfer', 'omar'), change(A, 'transfer', 'omar'),
               change(A, 'transfer', 'theo'), change(A, 'hold', 'theo'), change(A, 'hold', 'omar'), route(A, 'theo-97e5'),
               change(A, 'transfer', 'nadia'), change(A, 'add', 'mikael'), change(A, 'switch', 'omar'), look(A)]},
    {'name': 'the last one leaves and the call ends; who is asked for is not there',
     'steps': [start(A, ['theo', 'nadia']), change(A, 'remove', 'omar'), change(A, 'hold', 'omar'),
               change(A, 'resume', 'omar'), change(A, 'remove', 'nadia'), change(A, 'hold', 'theo'),
               change(A, 'remove', 'theo'), look(A), change(A, 'add', 'theo'), end(A)]},
    {'name': 'what is no request, and names that mean nobody or two',
     'steps': [start(A, []), start(A, ['', '  ']), start(A, ['nobody']), start(A, ['Sam']), change(A, 'add', 'theo'),
               start(A, ['theo', 'Theo', 'theo-97e5']), change(A, 'dance', 'theo'), change(A, 'add', ''),
               change(A, 'add', 'nobody'), change(A, 'add', 'Sam'), change(A, 'add', 'sam-2'), change(A, 'hold', 'Sam'),
               change(A, 'add', 'Sweeper'), change(A, 'add', 'mike-helper-1'), change(A, 'add', 'mik'), look(A)]},
    {'name': 'a call holds eight',
     'steps': [start(A, ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8', 'p9']),
               start(A, ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8']), change(A, 'add', 'p9'),
               change(A, 'switch', 'p9'), change(A, 'transfer', 'p9'), change(A, 'remove', 'p3'), change(A, 'add', 'p9'),
               change(A, 'add', 'p3'), change(A, 'hold', 'p1'), change(A, 'hold', 'p2'), look(A)]},
    {'name': 'a new call takes the place of the one that is up, unless it is the same request',
     'steps': [start(A, ['theo'], request_id='one'), start(A, ['nadia'], request_id='one'),
               start(A, ['nadia'], request_id='two'), start(A, ['omar']), look(A), end(A, call='first'),
               end(A, call='nothing-of-the-kind'), end(A), end(A)]},
    {'name': 'two phones, each with its call, and what the administrator may do',
     'steps': [look(ADMIN), start(ADMIN, ['theo']), start(A, ['theo']), start(ADMIN, ['nadia']), look(ADMIN),
               start(B, ['mike']), look(ADMIN), look(ADMIN, principal=B), change(ADMIN, 'add', 'omar'),
               change(ADMIN, 'add', 'omar', principal=B, by='mike-86db'), change(ADMIN, 'add', 'omar', call='live-b'),
               change(B, 'hold', 'theo'), route(B, 'theo-97e5'), route(A, 'mike-86db', call='live-b'),
               end(ADMIN), end(ADMIN, principal=A, by='nadia'), look(ADMIN), start(ADMIN, ['nadia'], by='omar-1'),
               look(B), end(ADMIN, principal='device_nobody'), start(ADMIN, ['theo'], principal='device_c'), look(ADMIN)]},
    {'name': 'speech moves the floor, and an invited one who is spoken to is in',
     'steps': [start(A, ['theo', 'mike', 'nadia']), routed(A, 'nadia'), routed(A, 'nadia'), route(A, 'omar-1'),
               change(A, 'hold', 'nadia'), route(A, 'nadia'), change(A, 'hold', 'theo'), change(A, 'hold', 'mike'),
               route(A, 'theo-97e5'), routed(A, 'theo-97e5'), route(A, 'theo-97e5', call='nothing-of-the-kind'),
               change(A, 'resume', 'mike'), route(A, ''), end(A), route(A, 'mike-86db'), routed(A, 'mike-86db')]},
    {'name': 'the floor goes back to the one who had it last',
     'steps': [start(A, ['theo', 'mike', 'nadia', 'omar']), change(A, 'switch', 'nadia'), change(A, 'switch', 'omar'),
               change(A, 'switch', 'mike'), change(A, 'hold', 'mike'), change(A, 'hold', 'omar'),
               change(A, 'remove', 'nadia'), change(A, 'hold', 'theo'), change(A, 'resume', 'omar'),
               change(A, 'resume', 'theo'), change(A, 'add', 'nadia'), change(A, 'remove', 'omar'), look(A)]},
    {'name': 'a call nobody touched for half a day is over',
     'steps': [start(A, ['theo', 'mike']), tick(11 * HOUR), look(A), route(A, 'mike-86db'), tick(12 * HOUR), look(A),
               tick(1), look(A), change(A, 'add', 'nadia'), start(A, ['nadia']), tick(13 * HOUR), start(A, ['omar'])]},
    {'name': 'ended calls are kept, the newest twenty',
     'steps': [step for n in range(23) for step in (start(A, ['theo']), end(A))] + [look(A)], 'kept': True},
]


@tool_scratch.case('clarp-calls-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's call module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import group_calls, oracle_contact, settings_store

    oracle_contact.roster = lambda: [dict(agent) for agent in ROSTER]
    group_calls._host_id = lambda: 'host-1'

    def named(name, participants):
        try:
            agent = group_calls.resolve_name(name, participants)
            return {'name': name, 'in_call': participants, 'session': agent['session']}
        except group_calls.CallError as refused:
            return {'name': name, 'in_call': participants, 'status': refused.status, 'refused': refused.body()}

    def played(script):
        for key in settings_store.keys_with_prefix(group_calls.KEY):
            settings_store.delete(key)
        heard, noted = [], []
        state = {'now': 1_800_000_000_000, 'made': 0, 'first': '', 'last': {}}

        class Stream:
            def broadcast(self, event):
                heard.append(json.loads(json.dumps(dict(event))))

        class Context:
            stream = Stream()

        class Minted:
            def __init__(self, number):
                self.hex = f'{number:032x}'

        def mint():
            state['made'] += 1
            return Minted(state['made'])

        group_calls.bind(Context())
        group_calls.now_ms = lambda: state['now']
        group_calls.uuid.uuid4 = mint
        group_calls._notify = lambda call, notices: noted.append(
            {session: 'Group call: ' + text for session, text in notices.items()
             if group_calls._participant(call, session) is not None})

        def call_of(step, caller):
            which = step.get('call', '')
            if which == 'live':
                return state['last'].get(caller, '')
            if which == 'live-b':
                return state['last'].get(B, '')
            if which == 'first':
                return state['first']
            return which

        def act(step):
            do, caller = step['do'], step.get('caller', '')
            more = {key: step[key] for key in ('principal', 'by') if key in step}
            if do == 'tick':
                state['now'] += step['ms']
                return None
            if do == 'snapshot':
                return group_calls.snapshot(caller, **({'principal': step['principal']} if 'principal' in step else {}))
            if do == 'start':
                return group_calls.start(caller, step['agents'], request_id=step.get('request_id', ''), **more)
            if do == 'change':
                return group_calls.change(caller, step['action'], step['agent'], call_id=call_of(step, caller), **more)
            if do == 'end':
                return group_calls.end(caller, call_id=call_of(step, caller), **more)
            if do == 'route':
                return group_calls.route(caller, call_of(step, caller), step['session'])
            if do == 'routed':
                return group_calls.routed(caller, call_of(step, caller), step['session'])
            raise ValueError(do)

        rows = []
        for step in script['steps']:
            del heard[:], noted[:]
            try:
                result, refused = act(step), None
            except group_calls.CallError as error:
                result, refused = None, {'status': error.status, 'body': error.body()}
            call = (result or {}).get('call') if isinstance(result, dict) else None
            if isinstance(call, dict) and call.get('state') == 'live':
                state['last'][call['principal']] = call['call_id']
                state['first'] = state['first'] or call['call_id']
            rows.append({'step': step, 'now': state['now'], 'result': result, 'refused': refused,
                         'events': list(heard), 'notices': list(noted)})
        kept = None
        if script.get('kept'):
            raw = json.loads(settings_store.get(group_calls.KEY + A))
            kept = [call['call_id'] for call in raw['calls']]
        return {'name': script['name'], 'steps': rows, 'kept': kept}

    answered = {'roster': ROSTER, 'names': [named(name, participants) for name, participants in NAMES],
                'most': group_calls.MAX_PARTICIPANTS, 'kept_ended': group_calls.KEEP_ENDED, 'stale_ms': group_calls.STALE_MS,
                'scripts': [played(script) for script in SCRIPTS]}
    shutil.rmtree(root, ignore_errors=True)
    return answered


# What was said in a call, oldest first: whose conversation it is in, whether the user said it
# (to that agent) or the agent did, and the words. The header an agent is given quotes what was
# said since it last spoke.
LONG = 'A sentence of the long report that goes on. ' * 9
TALK = [
    ('a-theo', 'user', "What's the plan for Friday?"), ('a-theo', 'assistant', 'Ship the beta on Friday and keep the flag off.'),
    ('a-mike', 'user', 'Mike, what do you think?'), ('a-mike', 'assistant', 'I would wait a week.'),
    ('a-nadia', 'user', 'Nadia?'), ('a-nadia', 'assistant', 'The migration is the only risk.'),
    ('a-theo', 'user', 'Theo, a long one please.'), ('a-theo', 'assistant', LONG.strip()),
    ('a-omar', 'assistant', 'Omar, from before he was put on hold.'),
    ('a-mike', 'user', 'And now, Mike?'),
]
MUCH = [('a-theo' if n % 2 else 'a-nadia', 'assistant' if n % 3 else 'user', f'Line number {n} of many, said to fill the header. ' * 2)
        for n in range(30)]


@tool_scratch.case('clarp-calls-headers-')
def headers(root) -> list:
    """Runs in a process of its own: the header the Python Host gives an agent of a call."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import db, group_calls, oracle_contact, settings_store

    oracle_contact.roster = lambda: [dict(agent) for agent in ROSTER]
    group_calls._host_id = lambda: 'host-1'
    group_calls._notify = lambda call, notices: None
    state = {'now': 1_800_000_000_000, 'made': 0, 'talk': []}
    group_calls.now_ms = lambda: state['now']

    class Minted:
        def __init__(self, number):
            self.hex = f'{number:032x}'

    def mint():
        state['made'] += 1
        return Minted(state['made'])

    group_calls.uuid.uuid4 = mint

    class Rows:
        """The conversations of the call's agents, as the header's question finds them."""
        def execute(self, sql, params=()):
            # The question asks for the rows of the call's own agents.
            self.asked_for = {value for value in params if isinstance(value, str)}
            return self

        def fetchall(self):
            return [{'agent_id': agent, 'role': role, 'text': text} for agent, role, text in state['talk']
                    if agent in self.asked_for]

    real = db.conn
    rows = []

    def told(name, talk, sessions):
        state['talk'] = talk
        db.conn = lambda: Rows()
        try:
            said = {session: group_calls.prompt_header(session) for session in sessions}
        finally:
            db.conn = real
        raw = json.loads(settings_store.get(group_calls.KEY + A))
        rows.append({'name': name, 'now': state['now'], 'calls': raw['calls'], 'talk': [list(row) for row in talk],
                     'told': said})

    everyone = ['theo-97e5', 'mike-86db', 'nadia', 'omar-1', 'mikael']
    group_calls.start(A, ['theo', 'mike', 'nadia', 'omar'])
    told('nobody was spoken to yet', TALK, everyone)
    group_calls.route(A, '', 'mike-86db')
    told('the user speaks to one of four', TALK, everyone)
    group_calls.change(A, 'hold', 'omar')
    told('one of them is on hold', TALK, everyone)
    told('nothing was said yet', [], everyone)
    told('much was said', MUCH + [('a-mike', 'user', 'So?')], everyone)
    told('the agent itself spoke last', TALK + [('a-mike', 'assistant', 'Mike answered that.')], everyone)
    state['now'] += 10 * 60 * 1000
    told('ten minutes after the user spoke to it', TALK, everyone)
    state['now'] += 1
    told('a moment later', TALK, everyone)
    group_calls.route(A, '', 'nadia')
    group_calls.change(A, 'remove', 'theo')
    told('another is spoken to, and one has left', TALK, everyone)
    group_calls.end(A)
    told('the call is over', TALK, everyone)
    group_calls.start(A, ['nadia'])
    group_calls.route(A, '', 'nadia')
    told('a call with one agent', TALK, everyone)
    group_calls.start(A, ['theo', 'nadia'], ring={'reason': 'a possible leak at pump 01', 'ring_id': 'rng_1',
                                                   'opening_line': 'Camera 2 shows a possible water leak. Shall I look?'})
    group_calls.route(A, '', 'theo-97e5')
    told('the agent rang the user, who answered', TALK[:2], everyone)
    group_calls.route(A, '', 'nadia')
    told('and the user turns to another', TALK[:2], everyone)
    shutil.rmtree(root, ignore_errors=True)
    return rows


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == '--headers':
        print(json.dumps(headers()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    ran = subprocess.run([sys.executable, '-I', __file__, '--headers'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said['headers'] = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "most": %d, "kept_ended": %d, "stale_ms": %d,' % (said['most'], said['kept_ended'], said['stale_ms']),
           ' "roster": [']
    out.extend('  ' + json.dumps(agent, ensure_ascii=True) + (',' if at < len(said['roster']) - 1 else '')
               for at, agent in enumerate(said['roster']))
    out.append(' ],')
    out.append(' "names": [')
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if at < len(said['names']) - 1 else '')
               for at, row in enumerate(said['names']))
    out.append(' ],')
    out.append(' "scripts": [')
    for at, script in enumerate(said['scripts']):
        out.append('  {"name": ' + json.dumps(script['name']) + ', "kept": ' + json.dumps(script['kept']) + ', "steps": [')
        rows = script['steps']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(said['scripts']) - 1 else ''))
    out.append(' ],')
    out.append(' "headers": [')
    rows = said['headers']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
               for index, row in enumerate(rows))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = sum(len(script['steps']) for script in said['scripts'])
    refused = sum(1 for script in said['scripts'] for row in script['steps'] if row['refused'])
    resolved = sum(1 for row in said['names'] if 'session' in row)
    print(f'{OUT.relative_to(REPO)}: {len(said["scripts"])} scripts of {steps} steps ({refused} refused), '
          f'{len(said["names"])} names ({resolved} mean one agent), {len(said["headers"])} moments of what agents are told')
    return 0


if __name__ == '__main__':
    sys.exit(main())
