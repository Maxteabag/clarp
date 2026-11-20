#!/usr/bin/env python3
"""Writes down what the Python Host answers an agent's note to the user (`POST /agent-notes`, its PR 482).

A note is the one thing an agent can do to put words in front of the user from any turn: a row of its own chat,
always shown, that notifies on its own (reason `agent-note`). It must come from the agent's own running turn,
proven by the token the Host gave that turn. The rule is `server/lib/agent_notes.py` with the route of
`server/lib/routes/agents.py`, the row of `message_writes.record_note` and the notification of
`user_notifications.classify_note`, run here as they are on a store of this program's own: real agents, real
turns, real tokens; a clock of this program's own; stand-ins for the apps' event stream and the push service, which
only write down what they are told. For every step it is written down what the request was answered, the note's
row as `GET /log` gives it, what the apps were told and what would have been pushed.

    <the Python Host's interpreter> server-rs/tools/agent-note-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation, its database there too), with no way out of the machine. The same file at every
run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'agent-note-vectors.json'
NOW = 1_795_000_000_000
WORDS = 'The build is green; nothing waits on you.'

# The ways a request stands to a turn, in words the Rust test stages the same way.
STANDINGS = {
    'the running turn': 'its own agent, in its turn that runs',
    'no token': 'the request presents none, while its turn runs',
    'a token that is none': 'a text that is no token of this Host, while its turn runs',
    "another agent's token": 'the token of a turn of another agent, which runs',
    "a removed agent's token": 'the token of a turn of an agent this Host no longer has',
    'the turn ended': 'its own agent, in a turn that was closed, no newer turn begun',
    'an older turn': 'its own agent, in a turn that ended while a newer one runs',
    'the turn completed normally': 'its own agent, in a turn that completed normally, no newer turn begun',
    'the turn is parked for its account': 'its own agent, its newest turn parked waiting for an account',
}


@tool_scratch.case('clarp-agent-note-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's modules do."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agent_notes, agents, apns, db, provider_background_jobs as provider_jobs, turn_lifecycle
    from lib.routes.agents import AgentRoutes
    db.reset_for_tests(root / 'notes.sqlite')

    clock = {'ms': NOW}
    time.time = lambda: clock['ms'] / 1000
    minted = iter(f'{n:016x}' for n in range(1, 10_000))
    agent_notes.secrets = SimpleNamespace(token_hex=lambda _n: next(minted))
    agent_ids = iter(f'agent-{n}' for n in range(1, 10_000))
    agents._new_agent_id = lambda: next(agent_ids)
    pushed: list = []
    apns.on_user_notification = lambda notification: pushed.append(json.loads(json.dumps(notification)))

    class Stream:
        def __init__(self):
            self.told = []

        def broadcast(self, payload):
            self.told.append(json.loads(json.dumps(payload)))

    class Request(AgentRoutes):
        def __init__(self, body, token=''):
            self.headers = {'X-Clarp-Turn': token} if token else {}
            self.body = body
            self.ctx = SimpleNamespace(stream=Stream())
            self.answer = None

        def _read_json(self):
            return self.body

        def _json_ok(self, payload):
            self.answer = {'status': 200, 'body': payload}

        def _json_error(self, code, message):
            self.answer = {'status': code, 'body': {'error': message}}

    counter = iter(range(1, 10_000))

    def fresh(name, *, bound=True, muted=False, role=None):
        n = next(counter)
        agent = agents.create_agent(persona=f'{name.title()} {n}', voice_id='v', cwd=str(root),
                                    session=f'{name}-{n}', backend='claude')
        if bound:
            agents.bind_backend_session(agent, f'conv-{n}')
        if muted:
            agents.update_agent(agent, muted=True)
        if role:
            db.conn().execute('UPDATE agents SET role = ? WHERE agent_id = ?', (role, agent))
            db.conn().commit()
        return agent, f'{name}-{n}'

    def token_of(agent, trace):
        token = provider_jobs.new_turn_token()
        provider_jobs.turn_started(token, agent_id=agent, provider='claude', pid=os.getpid(), trace_id=trace)
        return token

    def staged(standing, **made):
        """An agent with its turn, and the token the request presents, as the standing has them."""
        agent, session = fresh('ivy', **made)
        turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
        if standing in ('the running turn', 'no token', 'a token that is none'):
            token = {'the running turn': token_of(agent, 'trace-a'), 'no token': '',
                     'a token that is none': 'not-a-token-of-this-host'}[standing]
        elif standing == "another agent's token":
            other, _ = fresh('oak')
            turn_lifecycle.open_turn(agent_id=other, source='pwa', trace_id='trace-o')
            token = token_of(other, 'trace-o')
        elif standing == "a removed agent's token":
            token = token_of('agent-removed', 'trace-r')
        elif standing in ('the turn ended', 'an older turn'):
            token = token_of(agent, 'trace-a')
            turn_lifecycle.close_turns_for_trace(agent, 'trace-a')
            if standing == 'an older turn':
                turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-b')
        elif standing == 'the turn completed normally':
            token = token_of(agent, 'trace-a')
            turn_lifecycle.transition(agent, turn_lifecycle.TurnEvent.SPAWN_STARTED, {'trace_id': 'trace-a'})
            turn_lifecycle.transition(agent, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'trace-a'})
        elif standing == 'the turn is parked for its account':
            token = token_of(agent, 'trace-a')
            turn_lifecycle.transition(agent, turn_lifecycle.TurnEvent.SPAWN_STARTED, {'trace_id': 'trace-a'})
            turn_lifecycle.transition(agent, turn_lifecycle.TurnEvent.ACCOUNT_RECOVERY_WAIT, {
                'dispatch': 'claude', 'trace_id': 'trace-a', 'account_recovery': 'waiting',
                'message': 'waiting for an account'})
        else:
            raise AssertionError(standing)
        return agent, session, token

    def note_rows(agent):
        conv = agents.live_backend_session(agent)
        rows = agents.list_messages(agent_id=agent, backend_session_id=conv, limit=500) if conv else []
        # A text of thousands of the same letter is written as its length and letter.
        return [{**row, 'text': f"<{len(row['text'])} x {row['text'][0]}>"} if len(row.get('text') or '') > 400
                else row for row in rows if row.get('kind') == 'note']

    def ask(agent, body, token, *, step):
        del pushed[:]
        clock['ms'] += 1000
        request = Request(body, token)
        request._handle_agent_note()
        return {'step': step, 'body': body if not isinstance(body, dict) or len(json.dumps(body)) < 400
                else {**body, 'text': f"<{len(body.get('text') or '')} characters>"},
                'at': clock['ms'], 'answer': request.answer, 'told': request.ctx.stream.told,
                'pushed': list(pushed), 'notes': note_rows(agent) if agent else []}

    stories = []

    # Where the request stands to a turn: only the agent's own running turn may leave a note.
    for standing, how in STANDINGS.items():
        agent, session, token = staged(standing)
        stories.append({'name': f'from {standing}', 'how': how, 'steps': [
            ask(agent, {'session': session, 'text': WORDS}, token, step='a note')]})

    # What is asked: each refusal of the body, in the order the reference checks them.
    agent, session, token = staged('the running turn')
    bodies = [
        ('not an object', ['a', 'list']), ('nothing', None), ('no session', {'text': WORDS}),
        ('an empty session', {'session': '  ', 'text': WORDS}), ('no words', {'session': session}),
        ('words of white space', {'session': session, 'text': ' \n\t '}),
        ('words that are a number', {'session': session, 'text': 42, 'note_id': 'number'}),
        ('the most words', {'session': session, 'text': 'w' * 8000, 'note_id': 'most'}),
        ('one word too many', {'session': session, 'text': 'w' * 8001}),
        ('white space around the most words', {'session': session, 'text': '  ' + 'w' * 8000 + '  ',
                                               'note_id': 'padded'}),
        ('an id of capitals', {'session': session, 'text': WORDS, 'note_id': ' Build-Green_1 '}),
        ('the longest id', {'session': session, 'text': WORDS, 'note_id': 'n' * 64}),
        ('an id too long', {'session': session, 'text': WORDS, 'note_id': 'n' * 65}),
        ('an id with a space', {'session': session, 'text': WORDS, 'note_id': 'two words'}),
        ('an id with a dot', {'session': session, 'text': WORDS, 'note_id': 'a.b'}),
        ('an id that is a number', {'session': session, 'text': WORDS, 'note_id': 7}),
        ('no such agent', {'session': 'nobody', 'text': WORDS}),
        ('no words and no such agent', {'session': 'nobody', 'text': ''}),
        ('a bad id and no such agent', {'session': 'nobody', 'text': WORDS, 'note_id': 'a b'}),
        ('too many words and a bad id', {'session': session, 'text': 'w' * 8001, 'note_id': 'a b'}),
    ]
    stories.append({'name': 'what is asked', 'how': 'its own agent, in its turn that runs', 'steps': [
        ask(agent, body, token, step=name) for name, body in bodies]})
    stories.append({'name': 'what is asked, without a token', 'how': 'the request presents none', 'steps': [
        ask(agent, body, '', step=name) for name, body in bodies]})

    # The same note again, the same id with other words, another agent's id.
    agent, session, token = staged('the running turn')
    other, other_session, other_token = staged('the running turn')
    stories.append({'name': 'a note again', 'how': 'two agents, each in its turn that runs', 'steps': [
        ask(agent, {'session': session, 'text': WORDS, 'note_id': 'green'}, token, step='the first'),
        ask(agent, {'session': session, 'text': WORDS, 'note_id': 'green'}, token, step='the same again'),
        ask(agent, {'session': session, 'text': ' ' + WORDS + ' ', 'note_id': 'GREEN'}, token,
            step='the same words padded, the id in capitals'),
        ask(agent, {'session': session, 'text': 'Other words.', 'note_id': 'green'}, token,
            step='the same id, other words'),
        ask(other, {'session': other_session, 'text': 'Words of its own.', 'note_id': 'green'}, other_token,
            step="another agent, the first one's id"),
        ask(agent, {'session': session, 'text': 'A second note.'}, token, step='no id: a new note'),
        ask(agent, {'session': session, 'text': 'A second note.'}, token, step='no id again: a new note again'),
    ]})

    # Who is paged: a muted agent, a helper, an agent with no conversation yet.
    for name, made in (('a muted agent', {'muted': True}), ('a helper', {'role': 'helper'}),
                       ('an agent with no conversation', {'bound': False})):
        agent, session, token = staged('the running turn', **made)
        stories.append({'name': f'a note of {name}', 'how': 'its own agent, in its turn that runs', 'steps': [
            ask(agent, {'session': session, 'text': WORDS}, token, step='a note')]})

    return {'what': 'Written by server-rs/tools/agent-note-vectors.py from server/lib/agent_notes.py, '
                    'routes/agents.py, message_writes.record_note and user_notifications.classify_note. '
                    'Do not edit by hand.',
            'now': NOW, 'max_text': agent_notes.MAX_TEXT, 'command': agent_notes.COMMAND, 'stories': stories}


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
    lines = ['{']
    for name in ('what', 'now', 'max_text', 'command'):
        lines.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    lines.append(' "stories": [')
    for at, story in enumerate(said['stories']):
        lines.append('  {"name": ' + json.dumps(story['name'], ensure_ascii=True) + ', "how": '
                     + json.dumps(story['how'], ensure_ascii=True) + ', "steps": [')
        lines.extend('   ' + json.dumps(step, ensure_ascii=True) + (',' if n < len(story['steps']) - 1 else '')
                     for n, step in enumerate(story['steps']))
        lines.append('  ]}' + (',' if at < len(said['stories']) - 1 else ''))
    lines.append(' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    steps = [step for story in said['stories'] for step in story['steps']]
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories, {len(steps)} steps, '
          f'{sum(1 for step in steps if step["answer"]["status"] != 200)} refused, '
          f'{sum(len(step["pushed"]) for step in steps)} pushed, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
