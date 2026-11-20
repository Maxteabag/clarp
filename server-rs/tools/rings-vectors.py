#!/usr/bin/env python3
"""Writes the vectors the Rust Host's rings are tested against.

A ring is an agent calling its owner (`docs/agent-rings.md`, `server/lib/agent_rings.py`): the
phone shows an incoming call with the agent's reason, and answering starts a group call in which
the agent's opening line is heard first. The Host records the ring, says who may ring and how
often, and ends every ring one way: answered, declined, cancelled or missed.

This runs that module itself, with agents, a clock, a stream, ids, a voice and phones made up
here, through scripts of requests, and writes what it answered, announced, pushed, noted and put
in its audit trail after each step to `crates/clarp-kernel/tests/fixtures/rings-vectors.json`;
and the push a phone is sent for a ring in each of its states.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/rings-vectors.py

Only the ring this Host's apps answer today is run: the one whose opening line is said
(`opening_mode: say`). A ring held as a live voice call and a ring that listens first are other
contracts, and are not in these vectors.

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. The database it touches is a new one in that directory. No Host is touched or
started, and nothing is pushed or voiced: what would be is written down instead.
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'rings-vectors.json'

# The agents of a Host as it knows them by their ids. One is muted, one is a janitor, one is
# archived: the last two are no contacts, so nobody finds them by a name.
AGENTS = [
    {'session': 'theo-97e5', 'agent_id': 'a-theo', 'persona': 'Theo'},
    {'session': 'mike-86db', 'agent_id': 'a-mike', 'persona': 'Mike'},
    {'session': 'nadia', 'agent_id': 'a-nadia', 'persona': 'Nadia'},
    {'session': 'sam-1', 'agent_id': 'a-sam1', 'persona': 'Sam'},
    {'session': 'sam-2', 'agent_id': 'a-sam2', 'persona': 'Sam'},
    {'session': 'omar-1', 'agent_id': 'a-omar', 'persona': 'Omar'},
    {'session': 'quiet', 'agent_id': 'a-quiet', 'persona': 'Quiet', 'muted': 1},
    {'session': 'janitor-one', 'agent_id': 'a-jan', 'persona': 'Sweeper', 'is_janitor': 1},
    {'session': 'old', 'agent_id': 'a-old', 'persona': 'Old', 'archived_at': 1_700_000_000_000},
]

A, B, ADMIN = 'device_a', 'device_b', 'administrator'
SECOND, MINUTE, HOUR = 1000, 60 * 1000, 60 * 60 * 1000
WHY = 'Possible water leak at pump 01'
LINE = 'Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?'


def ring(turn='', agent='', reason=WHY, say=LINE, voiced=False, **more):
    """`turn`: the agent whose turn asks (proven by the turn); empty: the owner's own tools."""
    return {'do': 'ring', 'turn': turn, 'agent': agent, 'reason': reason, 'opening_line': say, 'voiced': voiced, **more}


def look(principal, at=None):
    return {'do': 'look', 'principal': principal, 'ring': at}


def answer(principal, at=-1, **more):
    return {'do': 'answer', 'principal': principal, 'ring': at, **more}


def decline(principal, at=-1):
    return {'do': 'decline', 'principal': principal, 'ring': at}


def cancel(turn, at=-1):
    return {'do': 'cancel', 'turn': turn, 'ring': at}


def settings(change=None):
    return {'do': 'settings', 'change': change}


def voip(principal, token, environment='', modes=None):
    return {'do': 'voip', 'principal': principal, 'token': token, 'environment': environment, 'modes': modes}


def tick(ms):
    return {'do': 'tick', 'ms': ms}


def timer():
    return {'do': 'timer'}


def call(principal):
    return {'do': 'call', 'principal': principal}


def forget(agent_id):
    return {'do': 'forget', 'agent_id': agent_id}


LONG_WHY = ('  A reason\tthat goes on\n and on  ' * 12).rstrip() + '  '
LONG_LINE = 'An opening line that is longer than a phone would want to hear said.   ' * 12

SCRIPTS = [
    {'name': 'an agent rings, the phone answers, and the call is a group call',
     'steps': [settings(), look(A), ring('a-theo', voiced=True), look(A), look(A, 0), look(B), look(ADMIN),
               ring('a-mike'), answer(A), answer(A), answer(B), answer(A, opening_mode='say'),
               answer(A, opening_mode='listen_first'), look(A), look(A, 0), call(A), decline(A), cancel('a-theo'),
               cancel('a-mike'), settings()]},
    {'name': 'a ring is declined, cancelled or missed, once',
     'steps': [ring('a-theo'), decline(A), decline(A), decline(B), answer(A), look(A, 0),
               tick(61 * SECOND), ring('a-theo'), cancel('a-mike'), cancel('a-theo'), cancel('a-theo'), cancel('a-mike'),
               cancel(''), decline(A),
               tick(61 * SECOND), ring('a-theo'), cancel(''), look(A, 2),
               tick(61 * SECOND), ring('a-theo', voiced=True), tick(44_999), look(A), tick(1), look(A), look(A, 3),
               tick(61 * SECOND), ring('a-theo'), tick(45_000), timer(), timer(), look(A, 4),
               tick(61 * SECOND), ring('a-theo'), tick(46_000), answer(A), answer(A),
               tick(61 * SECOND), ring('a-theo'), tick(46_000), decline(A), decline(A),
               tick(61 * SECOND), ring('a-theo'), tick(46_000), cancel('a-mike'), cancel('a-mike'), cancel('a-theo'),
               settings()]},
    {'name': 'who may ring, what a ring must say, and how often',
     'steps': [ring('', ''), ring('', '   '), ring('', 'nobody'), ring('', 'Sam'), ring('a-nope'), ring('a-nope', 'theo'),
               ring('a-theo', 'Mike'), ring('a-theo', 'nobody'), ring('a-theo', 'Sam'), ring('a-theo', 'theo', voiced=True),
               decline(A),
               ring('a-mike', reason=''), ring('a-mike', reason='  \n\t '), ring('a-mike', say=''), ring('a-mike', say=' \n '),
               ring('a-mike', opening_mode='bogus'), ring('a-mike', opening_mode=5), ring('a-mike', opening_mode=None),
               ring('a-mike', conversation_mode='bogus'), ring('a-mike', conversation_mode=5),
               ring('a-mike', event_context={'a': 1}), ring('a-mike', reason='', opening_mode='bogus'),
               ring('a-mike', say='', opening_mode='bogus'), ring('a-mike', say='', event_context={'a': 1}),
               ring('a-mike', reason='', conversation_mode='bogus'), ring('', '', conversation_mode='bogus'),
               ring('a-quiet'), ring('', 'quiet'), ring('a-jan'), ring('', 'Sweeper'), ring('', 'janitor-one'), ring('a-old'),
               ring('', 'old'),
               ring('a-theo', conversation_mode='classic', opening_mode='say'),
               ring('', 'nadia', reason=LONG_WHY, say=LONG_LINE), ring('a-mike'), ring('', 'theo'), decline(A),
               ring('a-nadia'), tick(59_001), ring('a-nadia'), tick(998), ring('a-nadia'), tick(1), ring('a-nadia'),
               cancel('a-nadia'),
               settings({'enabled': False}), ring('a-mike'), settings({'enabled': 'yes'}), settings({'show_reason': 1}),
               settings({'enabled': True, 'show_reason': True}), settings({}), settings({'enabled': False, 'show_reason': 'x'}),
               settings({'enabled': True, 'other': 1}),
               ring('a-mike'), cancel(''), ring('a-sam1'), cancel(''), ring('a-sam2'), cancel(''), ring('a-omar'), cancel(''),
               tick(61 * SECOND), ring('a-theo'), cancel(''), ring('a-mike'), cancel(''), ring('a-sam1'), cancel(''),
               ring('a-sam2'), ring('a-sam2', reason=''), ring('a-quiet'),
               tick(57 * MINUTE), ring('a-sam2'), tick(MINUTE), ring('a-sam2'), ring('a-omar'), cancel(''), ring('a-omar'),
               settings()]},
    {'name': 'who answers: a phone, the administrator, and an agent that is gone',
     'steps': [ring('a-theo'), answer(ADMIN), look(A), answer(A, conversation_mode='bogus'),
               answer(A, conversation_mode='agent_live_v1'), answer(A, 'rng_nothing', conversation_mode='agent_live_v1'),
               answer(A, 'rng_nothing'), decline(A, 'rng_nothing'), cancel('a-theo', 'rng_nothing'), cancel('', ''),
               look(A, 'rng_nothing'), answer(A, conversation_mode='classic'), call(A),
               tick(61 * SECOND), ring('a-mike', voiced=True), answer(ADMIN), answer(A), answer(ADMIN), call(A), call(B),
               tick(61 * SECOND), ring('a-nadia'), forget('a-nadia'), answer(B), look(B), decline(B),
               tick(61 * SECOND), ring('a-omar'), answer(B, opening_mode=None), call(B), call(A), settings()]},
    {'name': 'the newest fifty rings are kept, and the newest two hundred lines of the trail',
     'steps': [step for n in range(70) for step in (tick(7 * MINUTE), ring('a-theo', reason=f'Reason {n}'), ring('a-mike'),
                                                    cancel('a-theo'))]
              + [look(A, 0), look(A, 19), look(A, 20), look(A, 69), settings()], 'kept': True},
    {'name': 'a phone leaves its token for calls',
     'steps': [voip(A, 'AB' * 32, 'sandbox'), voip(A, 'zz' * 20), voip(A, 'ab' * 10), voip(A, ' ' + 'ab' * 16 + ' '),
               voip(A, 'ab' * 100, 'Sandbox'), voip(A, 'ab' * 101), voip(B, 'cd' * 20, '', ['agent_live_v1', 'agent_live_v1']),
               voip(B, 'cd' * 20, 'production', ['bogus']), voip(B, 'cd' * 20, '', 'agent_live_v1'),
               voip(B, 'cd' * 20, '', ['classic', 'agent_live_v1', 'classic']), voip(B, 'cd' * 20, 'other', []),
               voip(ADMIN, 'ef' * 20, '', ['classic']), voip(A, ''), voip(A, ''), voip(B, 'zz', '', ['bogus'])], 'kept': True},
]

# A ring in each of its states, and the push a phone is sent for it.
PUSHED = [
    {'kind': kind, 'state': state, 'show_reason': show, 'avatar_url': face, 'avatar_custom': own, 'server_instance_id': host,
     'persona': persona, 'reason': reason}
    for kind, state in (('ring', 'ringing'), ('answered', 'answered'), ('declined', 'declined'), ('missed', 'missed'),
                        ('cancelled', 'cancelled'), ('other', 'ringing'))
    for show, face, own, host, persona, reason in (
        (False, '', False, '', 'Theo', WHY), (True, 'https://host.example/avatars/a-theo?v=1', True, 'host-1', 'Theo', WHY),
        (True, 'https://host.example/static/avatars/theo.png', False, 'host-1', '  ', 'r' * 320))
]


@tool_scratch.case('clarp-rings-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's ring module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import (agent_ring_openings, agent_rings, agent_rings_base, agents, apns, group_calls, oracle_contact,
                     settings_store)

    group_calls._host_id = lambda: 'host-1'

    def played(script):
        for prefix in (group_calls.KEY, 'agent_ring.'):
            for key in settings_store.keys_with_prefix(prefix):
                settings_store.delete(key)
        heard, noted, pushed, trail = [], [], [], []
        state = {'now': 1_800_000_000_000, 'made': 0, 'rings': [], 'voiced': False, 'gone': set()}

        class Stream:
            def broadcast(self, event):
                heard.append(json.loads(json.dumps(dict(event))))

        class Context:
            stream = Stream()
            tts = None

        class Minted:
            def __init__(self, number):
                self.hex = f'{number:032x}'

        def mint():
            state['made'] += 1
            return Minted(state['made'])

        def known():
            return [dict(agent) for agent in AGENTS if agent['agent_id'] not in state['gone']]

        oracle_contact.roster = lambda: [agent for agent in known() if not agent.get('archived_at')]
        agents.get_by_agent_id = lambda agent_id: next((agent for agent in known() if agent['agent_id'] == agent_id), None)
        group_calls.bind(Context())
        agent_rings.bind(Context())
        for module in (agent_rings, agent_rings_base, agent_ring_openings, group_calls):
            module.now_ms = lambda: state['now']
        group_calls.uuid.uuid4 = mint
        group_calls._notify = lambda record, notices: noted.extend(
            {'agent_id': group_calls._participant(record, session)['agent_id'],
             'id': f"{record['call_id']}:{record['revision']}:{group_calls._participant(record, session)['agent_id']}",
             'text': 'Group call: ' + text}
            for session, text in notices.items() if group_calls._participant(record, session) is not None)
        agent_rings._voice = lambda text, agent: (
            f"/audio/{state['now']}__{agent['session']}.mp3" if state['voiced'] else '')
        agent_rings._schedule_expiry = lambda record: None
        agent_rings._push_voip = lambda record: {'enabled': False, 'sent': 0, 'failed': 0}

        def push(record, kind):
            pushed.append({'ring_id': record['ring_id'], 'kind': kind, 'state': record['state']})
            return {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}

        agent_rings._push = push
        agent_rings._notice = lambda record, text: noted.append(
            {'agent_id': record['agent_id'], 'id': f"{record['ring_id']}:{record['state']}", 'text': 'Call: ' + text})
        write = agent_rings_base._audit

        def audit(entry):
            trail.append(json.loads(json.dumps({'at': state['now'], **entry})))
            write(entry)

        agent_rings._audit = audit

        def which(step):
            at = step.get('ring')
            if isinstance(at, int):
                return state['rings'][at] if -len(state['rings']) <= at < len(state['rings']) else 'rng_none'
            return at or ''

        def act(step):
            do = step['do']
            if do == 'tick':
                state['now'] += step['ms']
                return None
            if do == 'forget':
                state['gone'].add(step['agent_id'])
                return None
            if do == 'timer':
                return agent_rings.expire_due()
            if do == 'call':
                return group_calls.snapshot(step['principal'])
            if do == 'settings':
                change = step['change']
                kept = agent_rings.settings() if change is None else agent_rings.update_settings(change)
                return {'ok': True, 'settings': kept, 'audit': agent_rings.audit()[:50]}
            if do == 'voip':
                return agent_rings.register_voip_token(step['principal'], step['token'], environment=step['environment'],
                                                       supported_conversation_modes=step['modes'])
            if do == 'ring':
                state['voiced'] = step['voiced']
                more = {key: step[key] for key in ('conversation_mode', 'event_context', 'opening_mode') if key in step}
                return agent_rings.ring(caller_agent_id=step['turn'], agent=step['agent'], reason=step['reason'],
                                        opening_line=step['opening_line'], **more)
            ring_id = which(step)
            if do == 'look':
                # What the route itself does with the module (`server/lib/routes/calls.py`).
                principal = step['principal']
                record = agent_rings.get(ring_id) if ring_id else agent_rings.current(principal)
                if record is not None and not agent_rings.event_visible(record, principal) and \
                        record.get('answered_by') != principal:
                    record = None
                if ring_id and record is None:
                    raise agent_rings.CallError(404, 'unknown call')
                return {'ring': record, 'settings': agent_rings.settings()}
            if do == 'answer':
                more = {key: step[key] for key in ('conversation_mode', 'opening_mode') if key in step}
                return agent_rings.answer(step['principal'], ring_id, **more)
            if do == 'decline':
                return agent_rings.decline(step['principal'], ring_id)
            if do == 'cancel':
                return agent_rings.cancel(step['turn'], ring_id)
            raise ValueError(do)

        rows = []
        for step in script['steps']:
            del heard[:], noted[:], pushed[:], trail[:]
            made = state['made']
            try:
                result, refused = act(step), None
            except agent_rings.CallError as error:
                result, refused = None, {'status': error.status, 'body': error.body()}
            placed = (result or {}).get('ring') if isinstance(result, dict) and step['do'] == 'ring' else None
            if isinstance(placed, dict):
                state['rings'].append(placed['ring_id'])
            row = {'step': dict(step), 'now': state['now'], 'result': result, 'refused': refused, 'events': list(heard),
                   'notices': list(noted), 'pushes': list(pushed), 'audit': list(trail), 'minted': state['made'] - made}
            if 'ring' in step:
                row['step']['ring_id'] = which(step)
            rows.append(json.loads(json.dumps(row)))
        kept = None
        if script.get('kept'):
            kept = {'rings': [record['ring_id'] for record in agent_rings_base._load()],
                    'audit': len(json.loads(settings_store.get(agent_rings_base.AUDIT_KEY) or '[]')),
                    'oldest_audit': (json.loads(settings_store.get(agent_rings_base.AUDIT_KEY) or '[null]'))[0],
                    'voip': agent_rings.voip_tokens()}
        return {'name': script['name'], 'steps': rows, 'kept': kept}

    def payload(case):
        record = {'ring_id': 'rng_' + 'ab' * 16, 'session': 'theo-97e5', 'persona': case['persona'], 'agent_id': 'a-theo',
                  'reason': case['reason'], 'opening_line': LINE, 'opening_audio_url': '/audio/1800000000000__theo-97e5.mp3',
                  'state': case['state'], 'created_at': 1_800_000_000_000, 'expires_at': 1_800_000_045_000, 'ended_at': None,
                  'answered_by': '', 'call_id': ''}
        return {**case, 'ring': record, 'collapse_id': apns.agent_ring_collapse_id(record),
                'payload': apns.agent_ring_payload(
                    record, case['kind'], avatar_url=case['avatar_url'] or None, avatar_custom=case['avatar_custom'],
                    server_instance_id=case['server_instance_id'], show_reason=case['show_reason'])}

    answered = {'agents': AGENTS, 'scripts': [played(script) for script in SCRIPTS],
                'pushed': [payload(case) for case in PUSHED],
                'ring_ms': agent_rings.RING_TIMEOUT_MS, 'between_ms': agent_rings.AGENT_INTERVAL_MS,
                'hourly_most': agent_rings.HOURLY_LIMIT, 'kept': agent_rings.KEEP_RECORDS, 'audit_kept': agent_rings.KEEP_AUDIT,
                'reason_most': agent_rings.REASON_CHARS, 'opening_most': agent_rings.OPENING_CHARS}
    shutil.rmtree(root, ignore_errors=True)
    return answered


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    numbers = ('ring_ms', 'between_ms', 'hourly_most', 'kept', 'audit_kept', 'reason_most', 'opening_most')
    out = ['{', ' ' + ', '.join(f'"{name}": {said[name]}' for name in numbers) + ',', ' "agents": [']
    out.extend('  ' + json.dumps(agent, ensure_ascii=True) + (',' if at < len(said['agents']) - 1 else '')
               for at, agent in enumerate(said['agents']))
    out.append(' ],')
    out.append(' "scripts": [')
    for at, script in enumerate(said['scripts']):
        out.append('  {"name": ' + json.dumps(script['name']) + ', "kept": ' + json.dumps(script['kept'], ensure_ascii=True)
                   + ', "steps": [')
        rows = script['steps']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(said['scripts']) - 1 else ''))
    out.append(' ],')
    out.append(' "pushed": [')
    rows = said['pushed']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
               for index, row in enumerate(rows))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = sum(len(script['steps']) for script in said['scripts'])
    refused = sum(1 for script in said['scripts'] for row in script['steps'] if row['refused'])
    placed = sum(1 for script in said['scripts'] for row in script['steps']
                 if row['step']['do'] == 'ring' and row['result'])
    print(f'{OUT.relative_to(REPO)}: {len(said["scripts"])} scripts of {steps} steps ({refused} refused, {placed} rings placed), '
          f'{len(said["pushed"])} pushes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
