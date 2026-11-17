#!/usr/bin/env python3
"""Writes the vectors the Rust Host's router of spoken messages is tested against: a message's way
through it.

The Python Host's router is `OrchestratorService.handle_send` (`server/lib/orchestrator.py`): a
message that is dictated hands-free is shown to a model with what the agents were last told and
said (`build_context_packet`), the model's decision is applied (the message is given to an agent,
or held while the user is asked whom it was for, or answered with an agent's status, or a
command is carried out, or it is left where it was), and what was decided is kept. This drives
that service on a database made for it, with a model that answers what the story says, a clock
the story sets, and a Host around it that only writes down what it was asked to do (to give a
message to an agent, to say something aloud, to tell the apps). It writes, for every message of
every story, what the model was shown, what the Host was asked to do, what was answered, and what
was kept, to `crates/clarp-kernel/tests/fixtures/router-flow-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/router-flow-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, no model is asked anything, nothing is said.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'router-flow-vectors.json'

T0 = 1_800_000_000_000
S = 1000
# The agents: (persona, session, tool, voice). The last is put away before anything is said.
AGENTS = [('Mike', 'mike', 'codex', 'voice-mike'), ('Anna', 'anna', 'claude', 'voice-anna'),
          ('Zoë', 'zoe', 'codex', 'voice-zoe'), ('Gone', 'gone', 'codex', 'voice-gone')]
# What each was told and said before, oldest first: (session, role, text, seconds before T0).
SAID = [
    ('mike', 'user', 'Please fix the failing build on main.', 900), ('mike', 'assistant', 'The build is green again: one flaky test was pinned.', 880),
    ('mike', 'user', 'Now bump the version.', 600), ('mike', 'assistant', 'Version bumped to 2.4.1 and tagged.', 590),
    ('anna', 'user', 'Draft the release notes   for\n2.4.', 500), ('anna', 'assistant', 'x' * 300, 480),
    ('zoe', 'user', 'Look into the login bug.', 4000),
]


def message(what, text, decide, **more):
    """One message of a story: what it shows, what was said, what the model answers (a dict, a list of
    dicts for more than one asking, a text to answer as it is, or an exception's words to fail with),
    and how it is sent (`session`, `hands_free`, `fallback`, `id`, `at`, `dispatch`: a status to
    fail with, `only`: the sessions of a call)."""
    return {'what': what, 'text': text, 'decide': decide, **more}


def to(session, confidence=0.93, **more):
    return {'kind': 'agent_message', 'target_session': session, 'confidence': confidence, 'addressing': True,
            'reason': 'named', **more}


STORIES = [
    ('the router is off, or the message is none of its kind', [
        message('off', 'Anna, ship it.', to('anna'), janitor=None),
        message('typed, where only what is dictated is routed', 'Anna, ship it.', to('anna'), hands_free=False,
                janitor={'enabled': True, 'options': {'fallback_only': False}}),
        message('not asked for by an app, where only that is routed', 'Anna, ship it.', to('anna'),
                janitor={'enabled': True, 'options': {'fallback_only': True}}),
    ]),
    ('a message finds its agent, or the user is asked', [
        message('for another agent, surely', 'Anna, are the notes done?', to('anna', text_to_send='are the notes done?'),
                janitor={'enabled': True, 'options': {'fallback_only': False}}, id='m-1'),
        message('the same message again', 'Anna, are the notes done?', to('mike'), id='m-1'),
        message('the same id for other words', 'Mike, are the notes done?', to('mike'), id='m-1'),
        message('for the agent that is open, surely, in its own words', 'bump it once more', to('mike', 0.8), id='m-2'),
        message('for another agent, not surely enough', 'ship the notes', to('anna', 0.6), id='m-3'),
        message('for an agent nobody has', 'tell Bob hello', to('bob'), id='m-4'),
        message('for an agent that was put away', 'tell Gone hello', to('gone'), id='m-5'),
        message('the answer to what was asked: the held message goes', 'yes, Anna',
                to('anna', 0.95, pending_id='{pending:m-3}', text_to_send='yes, Anna'), id='m-6'),
        message('the model asks itself', 'do the thing', {'kind': 'clarify', 'confidence': 0.4, 'spoken_text': ' Which thing? ',
                                                           'phrase_key': 'which', 'reason': 'unclear'}, id='m-7'),
        message('the model cannot tell two apart', 'fix it', {'kind': 'ambiguous', 'target_session': 'anna', 'confidence': 0.5}, id='m-8'),
        message('noise', 'uh the the', {'kind': 'ignored', 'confidence': 0.9, 'reason': 'noise'}, id='m-9'),
        message('noise the model is not sure of: it is asked again with everyone in view', 'uh maybe',
                [{'kind': 'ignored', 'confidence': 0.5}, to('zoe', 0.9)], id='m-10'),
        message('a correction of whom the last was for', 'no, that was for Zoe',
                {'kind': 'recipient_correction', 'target_session': 'zoe', 'confidence': 0.9}, id='m-11'),
    ]),
    ('status and commands', [
        message('how an agent is doing, in the model\'s words', 'how is Anna doing?',
                {'kind': 'status_query', 'target_session': 'anna', 'confidence': 0.9, 'status_text': ' Anna is writing. '}, id='s-1'),
        message('how an agent is doing, by what it last said', 'how is Mike doing?',
                {'kind': 'status_query', 'target_session': 'mike', 'confidence': 0.9}, id='s-2'),
        message('how an agent is doing that has said nothing', 'how is Zoe doing?',
                [{'kind': 'status_query', 'target_session': 'zoe', 'confidence': 0.9}] * 2, id='s-3'),
        message('how an agent nobody has is doing', 'how is Bob doing?',
                [{'kind': 'status_query', 'target_session': 'bob', 'confidence': 0.9}] * 2, id='s-4'),
        message('say that again, with nothing to say again', 'say that again',
                {'kind': 'control', 'control_action': 'repeat_last', 'confidence': 0.9}, id='s-5'),
        message('who am I talking to', 'who is this?',
                {'kind': 'control', 'control_action': 'who_am_i_talking_to', 'confidence': 0.9}, id='s-6'),
        message('a command nobody knows', 'do a barrel roll',
                {'kind': 'control', 'control_action': 'barrel_roll', 'confidence': 0.9}, id='s-7'),
        message('a command the model is not sure of', 'say again maybe',
                {'kind': 'control', 'control_action': 'repeat_last', 'confidence': 0.5}, id='s-8'),
        message('switch to another agent', 'switch to Anna',
                [{'kind': 'agent_control', 'control_action': 'switch', 'target_session': 'anna', 'confidence': 0.9}] * 2, id='s-9'),
        message('fork an agent, which the router never does', 'fork Mike',
                {'kind': 'agent_control', 'control_action': 'fork', 'target_session': 'mike', 'confidence': 0.95}, id='s-10', session='mike'),
        message('a command for an agent that is none of the call', 'switch to Zoe',
                [{'kind': 'agent_control', 'control_action': 'switch', 'target_session': 'zoe', 'confidence': 0.95}] * 2,
                id='s-11', session='mike', only=['mike', 'anna']),
        message('something nobody knows how to do', 'paint Mike blue',
                {'kind': 'agent_control', 'control_action': 'paint', 'target_session': 'mike', 'confidence': 0.95}, id='s-12', session='mike'),
    ]),
    ('what an app could place by no name', [
        message('the model finds whom it is for', 'are the notes done?', to('anna', 0.9), fallback=True, id='f-1',
                janitor={'enabled': True, 'options': {'fallback_only': True}}),
        message('the model is not sure: nothing is held, the app asks', 'is it done?', to('anna', 0.6), fallback=True, id='f-2'),
        message('the model answers what was not asked', 'how is Anna?',
                {'kind': 'status_query', 'target_session': 'anna', 'confidence': 0.9}, fallback=True, id='f-3'),
        message('the model says it is noise', 'uh', {'kind': 'ignored', 'confidence': 0.95}, fallback=True, id='f-4'),
    ]),
    ('what goes wrong', [
        message('the model answers no JSON', 'Anna, hello', 'I think this is for Anna.', id='w-1',
                janitor={'enabled': True, 'options': {'fallback_only': False}}),
        message('the model fails', 'Anna, hello', RuntimeError('codex: not logged in'), id='w-2'),
        message('the model answers a kind there is not', 'Anna, hello', {'kind': 'teleport'}, id='w-3'),
        message('the second asking fails: the first answer stands', 'uh maybe', [{'kind': 'ignored', 'confidence': 0.5},
                                                                                  RuntimeError('timeout')], id='w-4'),
        message('the agent does not take the message, for now', 'Anna, hello', to('anna'), id='w-5', dispatch=503),
        message('the same message again: it is given again', 'Anna, hello', to('mike'), id='w-5'),
        message('the agent refuses the message for good', 'Anna, hello again', to('anna'), id='w-6', dispatch=413),
        message('the same message again: it stays refused', 'Anna, hello again', to('anna'), id='w-6'),
        message('paused while a message is with the model', 'Anna, one more', to('anna'), id='w-7', pause_at_model=True),
        message('a message while it is paused', 'Anna, and another', to('anna'), id='w-8'),
    ]),
]


def run() -> dict:
    """Runs in a process of its own: a message's way through the Python Host's router."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-router-flow-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, janitor_builtins, janitors, orchestrator, prompt_admissions
    from lib.turn_command import DispatchError

    clock = {'now': T0 - 5000 * S}
    db.now_ms = lambda: clock['now']
    orchestrator.now_ms = db.now_ms
    from lib import orchestrator_context
    orchestrator_context.now_ms = db.now_ms
    work = root / 'work'
    work.mkdir()
    ids = {}
    for persona, session, backend, voice in AGENTS:
        ids[session] = agents.create_agent(persona=persona, voice_id=voice, cwd=str(work), session=session, backend=backend)
    agents.set_archived(ids['gone'], True)
    janitor_builtins.ensure_builtins(str(work))
    router = janitor_builtins.get_builtin('message-delegator')
    for session, role, text, before in SAID:
        clock['now'] = T0 - before * S
        orchestrator.record_routing_message(session=session, role=role, text=text, trace_id=f'before-{before}', source='seed')
    clock['now'] = T0
    agents.set_focus(ids['mike'])

    pending: dict = {}

    def named(value):
        text = json.dumps(value)
        for session, agent_id in ids.items():
            text = text.replace(agent_id, f'id-{session}')
        for raw, name in pending.items():
            text = text.replace(raw, name)
        return json.loads(text.replace(str(work), '/srv/work'))

    class Stream:
        """The apps, as far as the router tells them anything."""
        def __init__(self):
            self.told = []

        def broadcast(self, event):
            self.told.append(dict(event))

        broadcast_ephemeral = broadcast

    class Ctx:
        """The Host around the router: it writes down what it is asked to say."""
        def __init__(self):
            self.stream = Stream()
            self.said = []
            self.herald = None

        def speak_announcement(self, text, voice_id, session=None):
            self.said.append({'text': text, 'voice_id': voice_id, 'session': session})

    ctx = Ctx()
    shown: list = []
    answers: list = []
    state = {'pause': False}

    def model(packet, settings):
        shown.append({'packet': json.loads(json.dumps(packet)), 'provider': settings.provider, 'model': settings.model,
                      'effort': settings.effort})
        if state['pause']:
            state['pause'] = False
            current = janitors.get(router['session'])
            janitors.set_enabled(router['session'], current['revision'], False)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return orchestrator._extract_json(answer)
        return json.loads(json.dumps(answer))

    service = orchestrator.OrchestratorService(ctx, model_call=model, now=lambda: clock['now'])
    given: list = []

    @dataclasses.dataclass
    class Dispatched:
        session: str
        backend: str

    told = []
    at = T0
    for name, messages in STORIES:
        rows = []
        for one in messages:
            at += 10 * S
            clock['now'] = at
            if 'janitor' in one:
                current = janitors.get(router['session'])
                wanted = one['janitor'] or {'enabled': False}
                if wanted.get('options'):
                    current = janitors.configure(router['session'], current['revision'], options=wanted['options'])
                if current['enabled'] != wanted['enabled']:
                    janitors.set_enabled(router['session'], current['revision'], wanted['enabled'])
            decide = one['decide']
            del answers[:]
            answers.extend(decide if isinstance(decide, list) else [decide])
            # `{pending:m-3}`: the id under which the message of that id is held.
            for index, answer in enumerate(answers):
                if isinstance(answer, dict) and str(answer.get('pending_id', '')).startswith('{pending:'):
                    wanted = answer['pending_id'][len('{pending:'):-1]
                    held = db.conn().execute('SELECT pending_id FROM orchestrator_pending_utterances WHERE trace_id = ?',
                                             (f'trace-{wanted}',)).fetchone()
                    answers[index] = {**answer, 'pending_id': held['pending_id'] if held else ''}
            del shown[:], given[:], ctx.said[:], ctx.stream.told[:]
            state['pause'] = bool(one.get('pause_at_model'))
            fails_with = one.get('dispatch')

            def dispatch(**asked):
                admission = asked.pop('prompt_admission', None)
                given.append({**asked, 'unheard_audio_sessions': list(asked.get('unheard_audio_sessions') or ()),
                              'prompt_admission': json.loads(admission.to_json()) if admission else None})
                if fails_with:
                    raise DispatchError(fails_with, f'the agent answered {fails_with}')
                target = agents.get_by_session(asked['forced_session'])
                return Dispatched(session=target['session'], backend=target['backend'])

            message_id = one.get('id', f'auto-{len(told)}-{len(rows)}')
            requested = one.get('session', 'mike')
            admission = prompt_admissions.PromptAdmission(
                admission_id=f'adm-{message_id}', admission_version=1, authenticated_at_admission=True,
                cooperative_principal='administrator', principal_id='', origin='user', sender_agent_id='',
                channel='http', observed_at=at, client_admission_id=message_id, trace_id=f'trace-{message_id}',
                original_text=one['text'])
            try:
                outcome = service.handle_send(
                    text=one['text'], requested_session=requested, trace_id=f'trace-{message_id}',
                    prompt_admission=admission, hands_free=one.get('hands_free', True), synthesize_audio=True,
                    unheard_audio_sessions=(), dispatch=dispatch, fallback_request=bool(one.get('fallback')),
                    only_sessions=tuple(one['only']) if one.get('only') else None)
                answered = dataclasses.asdict(outcome) if outcome is not None else None
            except Exception as broke:  # noqa: BLE001 - told by its kind: the Python Host breaks here
                answered = {'broke': type(broke).__name__, 'said': str(broke)[:200]}
            for held in db.conn().execute('SELECT pending_id FROM orchestrator_pending_utterances ORDER BY created_at, pending_id'):
                pending.setdefault(held['pending_id'], f'<pending:{len(pending) + 1}>')
            decisions = [dict(row) for row in db.conn().execute(
                'SELECT * FROM orchestrator_decisions WHERE trace_id = ? ORDER BY decision_id', (f'trace-{message_id}',))]
            held_now = [dict(row) for row in db.conn().execute(
                'SELECT * FROM orchestrator_pending_utterances ORDER BY created_at, pending_id')]
            run_of = janitor_builtins.get_request_run(
                'message-delegator', 'routing-' + __import__('hashlib').sha256(f'u-{message_id}'.encode()).hexdigest())
            decide_shown = [str(d) if isinstance(d, Exception) else d for d in (decide if isinstance(decide, list) else [decide])]
            row = {
                'what': one['what'], 'now': at, 'text': one['text'], 'id': message_id, 'session': requested,
                'hands_free': one.get('hands_free', True), 'fallback': bool(one.get('fallback')), 'only': one.get('only'),
                'decide': decide_shown, 'fails': [isinstance(d, Exception) for d in (decide if isinstance(decide, list) else [decide])],
                'dispatch_fails_with': fails_with, 'pause_at_model': bool(one.get('pause_at_model')),
                'janitor': one.get('janitor', 'as it was'),
                'shown': shown[:], 'given': given[:], 'said': ctx.said[:], 'told': ctx.stream.told[:],
                'answered': answered, 'decisions': decisions, 'held': held_now,
                'run': None if run_of is None else {key: run_of[key] for key in ('status', 'outcome', 'error', 'demand_result')},
                'focus': agents.get_focus(),
                'routing_messages': [dict(r) for r in db.conn().execute(
                    'SELECT session, role, text, trace_id, source, created_at FROM agent_routing_messages '
                    'WHERE trace_id = ? ORDER BY rowid', (f'trace-{message_id}',))],
            }
            rows.append(named(row))
        told.append({'name': name, 'messages': rows})
    def state_of(session):
        # What the agent was last doing, as the router reads it: written when the agent was made, by
        # the clock of the machine and not the story's.
        row = orchestrator_context._latest_state(ids[session])
        return dict(row) if row else None

    return {'agents': [{'agent_id': f'id-{session}', 'session': session, 'persona': persona, 'backend': backend,
                        'voice_id': voice, 'cwd': '/srv/work', 'archived': session == 'gone',
                        'created_at': T0 - 5000 * S, 'state': state_of(session),
                        'last_activity': agents.last_activity(ids[session])}
                       for persona, session, backend, voice in AGENTS],
            'said_before': [{'session': session, 'role': role, 'text': text, 'at': T0 - before * S, 'trace_id': f'before-{before}'}
                            for session, role, text, before in SAID],
            'focus': 'id-mike', 'router': named({key: router[key] for key in ('agent_id', 'session', 'backend', 'model', 'effort',
                                                                              'execution', 'options', 'attachments')}),
            'stories': told}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    parts = list(said)
    for index, part in enumerate(parts):
        rows = said[part]
        end = ',' if index < len(parts) - 1 else ''
        if part == 'stories':
            out.append(f' "{part}": [')
            for number, story in enumerate(rows):
                out.append('  {"name": ' + json.dumps(story['name']) + ', "messages": [')
                out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(story['messages']) - 1 else '')
                           for n, row in enumerate(story['messages']))
                out.append('  ]}' + (',' if number < len(rows) - 1 else ''))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(rows, ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    messages = [one for story in said['stories'] for one in story['messages']]
    broke = [one['what'] for one in messages if isinstance(one['answered'], dict) and 'broke' in one['answered']]
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories of {len(messages)} messages; the model was asked '
          f'{sum(len(one["shown"]) for one in messages)} times, {sum(len(one["given"]) for one in messages)} messages were given to an agent, '
          f'{sum(len(one["said"]) for one in messages)} things were said aloud; broke: {broke}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
