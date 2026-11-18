#!/usr/bin/env python3
"""Writes down what the Python Host's tools of the fleet answer the Oracle's router.

The voice of a call holds no tools. What a router makes of the user's words is done with the tools of the
fleet: the agents there are, work handed to one of them or to the Oracle's contact, an agent stopped (and other
work put in the place of what was stopped), what an agent is doing, and what it said, read without asking it.
The rules are `server/lib/oracle_calls_stable.py` (`AgentTools` and the readers beside it), run here as they
are, with the agents, their messages and their states made up, and with a stand-in for the book of delegations
that says what it was asked to do. Nothing else of that Host runs.

    <the Python Host's interpreter> server-rs/tools/oracle-tools-vectors.py

Moments are written as how long before the run they are, so that the same file is written at every run.
Nothing leaves the machine, and nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-tools-vectors.json'
PHONE = 'device-phone-1'
AGENTS = [
    {'agent_id': 'a-theo', 'persona': 'Theo', 'session': 'theo', 'backend': 'claude', 'personality': 'Calm. ' * 120},
    {'agent_id': 'a-mike', 'persona': 'Mike', 'session': 'mike', 'backend': 'codex', 'personality': None},
    {'agent_id': 'a-old', 'persona': 'Old', 'session': 'old', 'backend': 'claude', 'archived_at': 5},
    {'agent_id': 'a-jan', 'persona': 'Janitor', 'session': 'jan', 'backend': 'claude', 'is_janitor': 1},
    {'agent_id': 'a-twin1', 'persona': 'Twin', 'session': 'twin-1', 'backend': 'claude'},
    {'agent_id': 'a-twin2', 'persona': 'TWIN', 'session': 'twin-2', 'backend': 'claude'},
    {'agent_id': 'a-ess', 'persona': 'Stra' + chr(0xdf) + 'e', 'session': 'strasse-1', 'backend': 'agy'},
]
# How long before now a moment is, in seconds: as it is told (`None`: a moment that cannot be read).
AGES = [0, 1, 30, 50, 70, 110, 130, 3590, 3610, 7190, 7210, 86_390, 86_410, 172_810, 86_400 * 400, -60, None,
        # Exactly a minute, an hour and a day: each is one of its kind from its first second on.
        60, 3600, 86_400]


def said(role, text, seconds_ago, ident=None):
    return {'role': role, 'text': text, 'seconds_ago': seconds_ago, 'id': ident or f'm-{seconds_ago}'}


MESSAGES = {
    'none': [],
    'a few': [said('user', 'What happened at station four?', 600), said('assistant', 'The pump ran dry.', 540),
              said('tool', 'not a message of the talk', 500), said('assistant', '', 480), said('user', 'And now?', 90),
              said('assistant', '   ', 60), said('assistant', 'It runs again.', 30)],
    'many': [said('user' if n % 2 else 'assistant', f'Message {n}. ' + 'words ' * (5 * n), 4000 - 60 * n) for n in range(40)],
    'long': [said('assistant', 'x' * 7000, 300), said('user', 'y' * 2500, 200), said('assistant', 'z' * 9000, 100),
             said('user', 'w' * 3000, 50)],
    'only tools': [said('tool', 'a', 30), said('system', 'b', 20)],
    'the agent said nothing yet': [said('user', 'Hello?', 30)],
    'many long': [said('user' if n % 2 else 'assistant', chr(97 + n) * 2500, 1000 - 60 * n) for n in range(10)],
    'the agent ended on a blank': [said('assistant', 'It runs.', 120), said('assistant', '   ', 30)],
}
STATES = {
    'none': None,
    'thinking': {'kind': 'thinking', 'detail': {'summary': 'Reading the valve log'}, 'seconds_ago': 125},
    'tool': {'kind': 'tool', 'detail': {'tool': 'Bash', 'message': 'not shown'}, 'seconds_ago': 5},
    'spawned': {'kind': 'spawned', 'detail': {'message': 'm' * 300}, 'seconds_ago': 7300},
    'done': {'kind': 'done', 'detail': 'no record', 'seconds_ago': 90_000},
    'no kind': {'kind': '', 'detail': {}, 'seconds_ago': None},
    'no time': {'kind': 'waiting', 'detail': {'summary': None, 'tool': '', 'message': 'A message.'}, 'ts': 'x'},
    'a tool and what it is for': {'kind': 'tool', 'detail': {'tool': 'Bash', 'summary': 'Reading the log'}, 'seconds_ago': 5},
}
# What a router wants done: the tool, what it is handed, and the name of the action.
CALLS = [
    ('list_agents', {}, 'c-1'), ('list_agents', {'agent': 'nobody'}, 'c-1'),
    ('investigate_with_oracle', {'request': 'Look into the pump.'}, 'c-2'),
    ('investigate_with_oracle', {'request': '  with blanks around  '}, 'c-2'),
    ('investigate_with_oracle', {'request': ''}, 'c-2'), ('investigate_with_oracle', {}, 'c-2'),
    ('investigate_with_oracle', {'request': '   '}, 'c-2'), ('investigate_with_oracle', {'request': 'r' * 16000}, 'c-2'),
    ('investigate_with_oracle', {'request': 'r' * 16001}, 'c-2'),
    ('investigate_with_oracle', {'request': ' ' + 'r' * 16000 + ' '}, 'c-2'),
    ('investigate_with_oracle', {'request': 7}, 'c-2'),
    ('delegate_to_agent', {'agent': 'mike', 'request': 'Check the valve.'}, 'c-3'),
    ('delegate_to_agent', {'agent': ' MIKE ', 'request': 'By its name, whatever the case.'}, 'c-3'),
    ('delegate_to_agent', {'agent': 'Mike', 'request': 'By its persona.'}, 'c-4'),
    ('delegate_to_agent', {'agent': 'STRASSE', 'request': 'By its persona, folded.'}, 'c-4'),
    ('delegate_to_agent', {'agent': 'nobody', 'request': 'x'}, 'c-5'), ('delegate_to_agent', {'request': 'x'}, 'c-5'),
    ('delegate_to_agent', {'agent': '', 'request': 'x'}, 'c-5'), ('delegate_to_agent', {'agent': 'old', 'request': 'x'}, 'c-5'),
    ('delegate_to_agent', {'agent': 'jan', 'request': 'x'}, 'c-5'), ('delegate_to_agent', {'agent': 'twin', 'request': 'x'}, 'c-5'),
    ('delegate_to_agent', {'agent': 'twin-1', 'request': 'x'}, 'c-5'), ('delegate_to_agent', {'agent': 'mike'}, 'c-5'),
    ('delegate_to_agent', {'agent': 7, 'request': 'x'}, 'c-5'),
    ('get_agent_status', {'agent': 'theo'}, 'c-6'), ('get_agent_status', {'agent': 'nobody'}, 'c-6'),
    ('read_agent_messages', {'agent': 'theo'}, 'c-7'), ('read_agent_transcript', {'agent': 'theo'}, 'c-8'),
    ('read_agent_transcript', {'agent': 'theo', 'limit': 2}, 'c-8'),
    ('cancel_agent', {'agent': 'theo'}, 'c-9'), ('cancel_agent', {'agent': 'theo', 'request': '   '}, 'c-9'),
    ('cancel_agent', {'agent': 'theo', 'request': ' Do this instead. '}, 'c-9'),
    ('cancel_agent', {'agent': 'theo', 'request': 'r' * 16001}, 'c-9'),
    ('cancel_agent', {'agent': 'theo', 'request': ' ' + 'r' * 16000 + ' '}, 'c-9'),
    ('cancel_agent', {'agent': 'nobody', 'request': 'x'}, 'c-9'), ('cancel_agent', {}, 'c-9'),
    ('fly_to_the_moon', {'agent': 'theo'}, 'c-10'), ('fly_to_the_moon', {}, 'c-10'), ('', {'agent': 'theo'}, 'c-10'),
    # A name with a letter that is folded to two, as it is written; a request within its bound in signs and over it
    # in bytes; and what an agent said read for a sentence about it where there is much of it (the last word of
    # each: the messages the agent has then).
    ('delegate_to_agent', {'agent': 'Stra' + chr(0xdf) + 'e', 'request': 'By its persona as it is written.'}, 'c-4'),
    ('investigate_with_oracle', {'request': chr(0xe9) * 9000}, 'c-2'),
    ('read_agent_messages', {'agent': 'theo'}, 'c-7', 'many'), ('read_agent_messages', {'agent': 'theo'}, 'c-7', 'long'),
    ('read_agent_messages', {'agent': 'theo'}, 'c-7', 'many long'),
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-tools-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import copy
    import time
    from datetime import datetime, timezone

    from lib import agents, message_store, oracle_calls_stable as tools, oracle_delegations, turn_queue

    began = time.time()

    def moment(seconds_ago):
        """A moment so long before the run, as a message has it."""
        return datetime.fromtimestamp(began - seconds_ago, timezone.utc).isoformat()

    world = {'messages': [], 'state': None, 'asked': []}

    def rows():
        return [{'id': row['id'], 'role': row['role'], 'text': row['text'], 'timestamp': moment(row['seconds_ago'])}
                for row in world['messages']]

    def listed(*, agent_id, limit, include_automated):
        world['asked'].append({'messages_of': agent_id, 'limit': limit, 'include_automated': include_automated})
        return rows()[-limit:]
    message_store.list_messages = listed

    def latest(agent_id):
        state = copy.deepcopy(world['state'])
        if state and 'seconds_ago' in state:
            ago = state.pop('seconds_ago')
            state['ts'] = None if ago is None else int((began - ago) * 1000)
        return state
    agents.latest_state = latest
    agents.list_agents = lambda: copy.deepcopy(AGENTS)

    def aged(output, names):
        """What was answered, with its moments as how long before the run they are."""
        output = copy.deepcopy(output)
        for message in output.get('messages', []) if isinstance(output, dict) else []:
            then = datetime.fromisoformat(message['timestamp'])
            message['seconds_ago'] = round(began - then.timestamp())
            del message['timestamp']
        return output

    ages = []
    for ago in AGES:
        written = 'not a moment' if ago is None else moment(ago)
        ages.append({'seconds_ago': ago, 'age': tools._age_text(written)})
    ages.append({'written': '2026-01-01T00:00:00Z', 'ends_in_z': True,
                 'age_is_days': tools._age_text('2026-01-01T00:00:00Z').endswith('days ago')})
    ages.append({'written': None, 'age': tools._age_text(None)})
    ages.append({'written': '', 'age': tools._age_text('')})
    ages.append({'written': '2027-01-15T09:00:00', 'no_zone': True, 'age': 'cannot be told'})
    ages.pop()

    agent = AGENTS[0]
    readings = []
    for name, messages in MESSAGES.items():
        world['messages'] = messages
        for limit, chars, budget, mark in ((10, 2000, 12000, False), (10, 6000, 16000, True), (3, 6000, 16000, True),
                                           (30, 6000, 16000, True), (10, 50, 120, True), (10, 50, 120, False),
                                           (10, 6000, 0, True), (2, 10, 15, True)):
            world['asked'] = []
            got, cut = tools._recent_messages(agent, limit=limit, message_chars=chars, budget=budget, mark_cuts=mark)
            readings.append({'messages': name, 'limit': limit, 'message_chars': chars, 'budget': budget, 'mark_cuts': mark,
                             'asked': world['asked'], 'read': aged({'messages': got}, None)['messages'], 'cut': cut})
    transcripts = []
    for name in MESSAGES:
        world['messages'] = MESSAGES[name]
        for limit in (None, 0, -1, 1, 5, 30, 31, 400, 'x', True, 2.0):
            output = tools._agent_transcript(agent, limit)
            newest = output.pop('newest_message_age')
            transcripts.append({'messages': name, 'limit': limit, 'output': aged(output, None), 'newest_message_age': newest})
    statuses = []
    for state_name in STATES:
        for name in ('none', 'a few', 'only tools', 'the agent said nothing yet', 'long', 'the agent ended on a blank'):
            world['state'], world['messages'], world['asked'] = STATES[state_name], MESSAGES[name], []
            statuses.append({'state': state_name, 'messages': name, 'asked': world['asked'],
                             'output': tools._agent_status(agent)})

    def handed(fallback, name, arguments, call_id, messages='a few'):
        """One thing a router wants done, with what the book of delegations and the queues were asked meanwhile."""
        asked = []

        def dispatch(**said):
            said.pop('ctx')
            asked.append({'dispatch': said})
            return {'status': 'queued' if 'instead' in said['request_text'] else 'accepted', 'delegation_id': said['delegation_id']}

        def cancel_for_session(session, *, stop, owner_principal):
            asked.append({'cancel_for_session': session, 'owner_principal': owner_principal, 'stopped': stop()})
            return [{'delegation_id': 'rtc-old', 'status': 'cancelled'}]
        oracle_delegations.dispatch, oracle_delegations.cancel_for_session = dispatch, cancel_for_session
        turn_queue.set_paused = lambda agent_id, paused: asked.append({'set_paused': agent_id, 'paused': paused})
        fleet = tools.AgentTools(None, PHONE, fallback, lambda session: asked.append({'stop': session}) or 'stopped')
        fleet.supersede = lambda session: asked.append({'supersede': session})
        world['state'], world['messages'] = STATES['thinking'], MESSAGES[messages]
        row = {'fallback': fallback, 'name': name, 'arguments': arguments, 'call_id': call_id, 'messages': messages}
        try:
            output = fleet.execute(name, copy.deepcopy(arguments), call_id)
            for key in ('newest_message_age', 'since', 'last_said_age'):
                if isinstance(output, dict) and key in output:
                    output[key] = '<an age>'
            row['output'] = aged(output, None)
        except (ValueError, TypeError, AttributeError) as refused:
            row['raises'] = str(refused) if isinstance(refused, ValueError) else type(refused).__name__
        row['asked'] = asked
        row['owns'] = sorted(fleet.delegations)
        return row
    calls = [handed(fallback, *call) for fallback in ('theo', '', 'nobody') for call in CALLS]
    return {'what': 'server/lib/oracle_calls_stable.py: AgentTools, _recent_messages, _agent_transcript, _agent_status, _age_text',
            'phone': PHONE, 'agents': AGENTS, 'messages': MESSAGES, 'states': STATES,
            'transcript_default': tools.TRANSCRIPT_DEFAULT, 'transcript_max': tools.TRANSCRIPT_MAX,
            'transcript_message_chars': tools.TRANSCRIPT_MESSAGE_CHARS, 'transcript_budget': tools.TRANSCRIPT_BUDGET,
            'ages': ages, 'readings': readings, 'transcripts': transcripts, 'statuses': statuses, 'calls': calls}


def shrunk(value):
    """What is long and says nothing here is written down short: one sign many times over as the sign and how
    many (`{"runs": [...]}`: texts as they are, and `[sign, count]` for a run of it). Whoever reads the file
    writes them out again."""
    import re
    if isinstance(value, str):
        parts = []
        for found in re.finditer(r'(.)\1{63,}|(?:(.)(?!\2{63}))+', value, flags=re.S):
            parts.append([found.group(1), len(found.group(0))] if found.group(1) else found.group(0))
        return {'runs': parts} if any(isinstance(part, list) for part in parts) else value
    if isinstance(value, list):
        return [shrunk(item) for item in value]
    if isinstance(value, dict):
        return {key: shrunk(item) for key, item in value.items()}
    return value


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = shrunk(json.loads(ran.stdout.splitlines()[-1]))
    out = ['{']
    parts = list(said)
    for at, part in enumerate(parts):
        end = ',' if at < len(parts) - 1 else ''
        if not isinstance(said[part], list) or part == 'agents':
            out.append(f' {json.dumps(part)}: {json.dumps(said[part], ensure_ascii=True)}{end}')
            continue
        out.append(f' {json.dumps(part)}: [')
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(said[part]) - 1 else '')
                   for n, row in enumerate(said[part]))
        out.append(' ]' + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(said["ages"])} ages, {len(said["readings"])} readings, '
          f'{len(said["transcripts"])} transcripts, {len(said["statuses"])} statuses, {len(said["calls"])} calls, '
          f'{sum(1 for call in said["calls"] if "raises" in call)} refused, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
