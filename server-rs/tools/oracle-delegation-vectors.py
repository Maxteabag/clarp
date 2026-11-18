#!/usr/bin/env python3
"""Writes down what the Python Host's record of the Oracle's delegations does.

What the Oracle is asked to look into it hands to an agent as a turn of that agent's, and the Host keeps a
record of each such delegation from the moment it is taken until its result was delivered: taken once however
often it is asked (the same id with other work is refused), waiting in the agent's queue or under way, ended by
the turn's answer, its failure or a stop, shown to the phone that asked until that phone says it has it. The
rules are `server/lib/oracle_delegations.py`, run here as they are on a database of this program's own, with a
stand-in for the service that admits a turn and for the queue: nothing else of that Host runs.

    <the Python Host's interpreter> server-rs/tools/oracle-delegation-vectors.py

The same file at every run. Nothing leaves the machine, and nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-delegation-vectors.json'
NOW = 1_800_000_000_000
ADMIN = 'administrator'
PHONE = 'device-phone-1'
# What an agent's live call asks its user's Oracle (the tools of a live call): told only into that call.
ASK = 'Sp' + chr(0xf8) + 'rsm' + chr(0xe5) + 'l fra Sandra i en Live-samtale: is the pump on?'
HEX = '0123456789abcdef01234567'
REFERENCE = '\n\n<oracle-reference-data>'


def story(name, *steps):
    return {'name': name, 'steps': list(steps)}


def op(kind, **said):
    return {'op': kind, **said}


def dispatch(delegation_id, session='theo', request='Look into the pump.', owner=ADMIN, **world):
    """`dispatch` as a route calls it. `world`: has_message (a message of its id is in the conversation), raises
    (the service that admits a turn refuses, in these words), queued and queue (it is told the turn waits, and what
    the queue has for it by then), backend (the session of the agent's tool)."""
    return op('dispatch', delegation_id=delegation_id, session=session, request=request, owner=owner, **world)


LONG = 'x' * 600
STORIES = [
    story('a delegation from the moment it is taken until its result was delivered',
          dispatch('d-1'),
          op('undelivered'),
          dispatch('d-1', has_message=True),
          dispatch('d-1', request='Look into the other pump.'),
          dispatch('d-1', session='mike'),
          dispatch('d-1', owner=PHONE),
          op('completed', trace='oracle-d-1', message='m-1', text='  The pump is on.\n'),
          op('completed', trace='oracle-d-1', message='m-2', text='Another answer.'),
          op('failed', delegation_id='d-1', reason='too late'),
          op('failed_for_trace', trace='oracle-d-1', reason='too late'),
          op('started', trace='oracle-d-1'),
          op('dispatched', delegation_id=' d-1 ', backend='thread-9', queued=False),
          op('dispatched', delegation_id='d-1', backend='thread-9', queued=True, queue='queued'),
          op('dispatched', delegation_id='d-0', backend='thread-9', queued=False),
          dispatch('d-1'),
          dispatch('d-1', request='Look into the other pump.'),
          op('undelivered'),
          op('undelivered', owner=PHONE),
          op('recent'),
          op('acknowledge', delegation_id='d-1', owner=PHONE),
          op('acknowledge', delegation_id='d-1'),
          op('undelivered'),
          op('acknowledge', delegation_id=' d-1 '),
          op('acknowledge', delegation_id='d-0'),
          op('recent'),
          op('recent', all_of=PHONE)),
    story('a delegation that waits in the queue of a busy agent',
          dispatch('d-2', queued=True, queue='queued', backend='thread-7'),
          op('undelivered'),
          op('active', agent='theo', trace='oracle-d-2'),
          op('acknowledge', delegation_id='d-2'),
          op('cancel', delegation_id='d-2'),
          op('started', trace=' oracle-d-2 '),
          op('acknowledge', delegation_id='d-2'),
          op('started', trace='oracle-d-2'),
          op('started', trace='oracle-d-0'),
          op('started', trace=''),
          op('active', agent='theo', trace='oracle-d-2'),
          op('completed', trace=' oracle-d-2 ', message='m-9', text='Done.'),
          op('active', agent='theo', trace='oracle-d-2'),
          dispatch('d-3', queued=True, queue='claimed'),
          dispatch('d-4', queued=True, queue='started'),
          dispatch('d-5', queued=True),
          dispatch('d-6', queued=False, queue='queued'),
          op('recent')),
    story('a delegation taken again while it waits or works',
          dispatch('d-1', queued=True, queue='queued'),
          dispatch('d-1', queued=False, backend='thread-1'),
          dispatch('d-1', has_message=True, queued=True, queue='queued'),
          op('started', trace='oracle-d-1'),
          dispatch('d-1', queued=True, queue='queued', backend='thread-2'),
          dispatch('d-1', raises='the agent is busy elsewhere'),
          dispatch('d-1'),
          op('recent')),
    story('what a delegation cannot be taken as',
          dispatch(''), dispatch('   '), dispatch('a b'), dispatch('d' * 80), dispatch('d' * 81),
          dispatch(' ok-._A9 '), dispatch('semi;colon'), dispatch('d-' + chr(0xe9)), dispatch('line\n'),
          dispatch('d-10', session=''), dispatch('d-10', session='   '), dispatch('d-10', request=''),
          dispatch('d-10', request=' \n '), dispatch('d-10', session='nobody'), dispatch('d-10', session='sweep'),
          dispatch('', session='nobody'), dispatch('d-10', session='nobody', request=''),
          dispatch('d-10', owner=''), dispatch('d-10', owner='  '),
          dispatch('d-10', session=' theo ', request='  Look.  ', owner=' ' + PHONE + ' '),
          dispatch('d-10', session='theo', request='Look.', owner=PHONE),
          dispatch('D-10', session='theo', request='Look.', owner=PHONE),
          op('recent'), op('recent', all_of=PHONE)),
    story('a turn that cannot be admitted, and turns that fail',
          dispatch('d-1', raises='the agent is put away'),
          op('undelivered'),
          dispatch('d-1'),
          dispatch('d-2'),
          op('failed', delegation_id='d-2', reason=''),
          dispatch('d-3'),
          op('failed', delegation_id=' d-3 ', reason=LONG),
          dispatch('d-4'),
          op('failed_for_trace', trace=' oracle-d-4 ', reason='The tool gave up.'),
          op('failed_for_trace', trace='oracle-d-4', reason='again'),
          op('failed_for_trace', trace='oracle-d-0', reason='nobody'),
          dispatch('d-5'),
          op('completed', trace='oracle-d-5', message='', text='An answer.'),
          op('completed', trace='oracle-d-5', message='m-5', text=' \n\t '),
          op('completed', trace='', message='m-5', text='An answer.'),
          op('completed', trace='oracle-d-5', message='m-5', text='An answer.'),
          op('undelivered'), op('undelivered', limit=2), op('undelivered', limit=0), op('undelivered', limit=-3),
          op('recent', limit=1), op('recent', limit=500)),
    story('a delegation is called off by its id only when it is over',
          dispatch('d-1'),
          op('cancel', delegation_id='d-1'),
          op('cancel', delegation_id='d-0'),
          op('cancel', delegation_id='d-1', owner=PHONE),
          dispatch('d-2', queued=True, queue='queued'),
          op('cancel', delegation_id='d-2'),
          op('completed', trace='oracle-d-1', message='m-1', text='Done.'),
          op('cancel', delegation_id='d-1'),
          op('cancel', delegation_id=' d-1 '),
          op('failed', delegation_id='d-2', reason='gave up'),
          op('cancel', delegation_id='d-2'),
          op('recent')),
    story('a stop of an agent calls off what the Oracle handed it',
          dispatch('d-1'),
          dispatch('d-2', queued=True, queue='queued'),
          dispatch('d-3', session='mike'),
          dispatch('d-4', owner=PHONE),
          dispatch('d-5'),
          op('completed', trace='oracle-d-5', message='m-5', text='Done before.'),
          dispatch('d-6'),
          op('failed', delegation_id='d-6', reason='failed before'),
          op('stop', session='theo', does='raises'),
          op('recent'),
          op('stop', session=''), op('stop', session='theo', owner=''), op('stop', session='nobody'),
          op('stop', session=' theo '),
          op('recent'),
          op('stop', session='theo'),
          op('undelivered'), op('undelivered', owner=PHONE),
          op('stop', session='theo', owner=' ' + PHONE + ' '),
          op('recent', all_of=PHONE)),
    story('a stop whose turn failed or ended while it was stopped',
          dispatch('d-1'),
          dispatch('d-2', queued=True, queue='queued'),
          op('stop', session='theo', does='fails', trace='oracle-d-1'),
          dispatch('d-3'),
          dispatch('d-4', queued=True, queue='queued'),
          op('stop', session='theo', does='completes', trace='oracle-d-3'),
          op('recent')),
    story('what a phone is shown, and what is held back for a live call',
          dispatch('sandra-' + HEX, request=ASK),
          dispatch('sandra-' + HEX[:23], request=ASK),
          dispatch('sandra-' + HEX.upper(), request=ASK),
          dispatch('sandro-' + HEX, request=ASK),
          dispatch('sandra-' + HEX[::-1], request=ASK.replace('fra', 'FRA').replace('Live', 'LIVE')),
          dispatch('sandra-' + HEX[1:] + '0', request=ASK.replace(chr(0xf8), chr(0xd8))),
          dispatch('sandra-' + HEX[2:] + '00', request='x ' + ASK),
          dispatch('sandra-' + HEX[3:] + '000', request='Look into the pump.'),
          dispatch('sandra-' + HEX[4:] + '0000', request=ASK.replace('Live-samtale', 'samtale')),
          dispatch('sandra-' + HEX[5:] + '00000', request=ASK[:len(ASK) // 2]),
          dispatch('b-late'), dispatch('a-late'),
          op('completed', trace='oracle-sandra-' + HEX, message='m-1', text='Yes.'),
          op('completed', trace='oracle-sandra-' + HEX[:23], message='m-2', text='Yes.'),
          op('completed', trace='oracle-sandra-' + HEX.upper(), message='m-3', text='Yes.'),
          op('completed', trace='oracle-sandro-' + HEX, message='m-4', text='Yes.'),
          op('completed', trace='oracle-sandra-' + HEX[::-1], message='m-5', text='Yes.'),
          op('completed', trace='oracle-sandra-' + HEX[1:] + '0', message='m-6', text='Yes.'),
          op('completed', trace='oracle-sandra-' + HEX[2:] + '00', message='m-7', text='Yes.'),
          op('failed', delegation_id='sandra-' + HEX[3:] + '000', reason='no'),
          op('completed', trace='oracle-sandra-' + HEX[4:] + '0000', message='m-9', text='Yes.'),
          op('completed', trace='oracle-sandra-' + HEX[5:] + '00000', message='m-10', text='Yes.'),
          op('undelivered'),
          op('recent'),
          op('acknowledge', delegation_id='sandra-' + HEX),
          op('same_moment', ids=['b-late', 'a-late'], message='m-8', text='Together.'),
          op('undelivered'), op('undelivered', limit=3), op('recent', limit=3)),
    story('what an agent is told it still owes the Oracle',
          dispatch('d-1', request='Look into the pump.' + REFERENCE + '{"big": true}</oracle-reference-data>'),
          dispatch('d-2', request='y' * 8100),
          dispatch('d-3', session='mike'),
          dispatch('d-4', request='Second.' + REFERENCE + 'a' + REFERENCE + 'b'),
          op('active', agent='theo', trace='oracle-d-1'),
          op('active', agent='theo', trace='oracle-d-2'),
          op('active', agent='mike', trace='oracle-d-3'),
          op('active', agent='theo', trace='oracle-d-3'),
          op('active', agent='', trace='oracle-d-1'),
          op('active', agent='theo', trace=''),
          op('completed', trace='oracle-d-1', message='m-1', text='Done.'),
          op('active', agent='theo', trace='oracle-d-1')),
    story('delegations whose turn was lost with the process that ran it',
          dispatch('d-1'), dispatch('d-2'), dispatch('d-3'), dispatch('d-4'), dispatch('d-5'), dispatch('d-6'),
          dispatch('d-7', queued=True, queue='queued'), dispatch('d-8'),
          op('completed', trace='oracle-d-8', message='m-8', text='Done in time.'),
          op('orphans', admitted=['d-1', 'd-2', 'd-3', 'd-4', 'd-5'], live=['d-1'],
             said={'d-2': [('final', 'first', 1), ('final', '  The answer.  ', 3), ('live', 'while it ran', 2)],
                   'd-3': [('live', 'as far as it came', 1)],
                   'd-4': [('final', '   ', 2), ('other', 'not of the turn', 9)],
                   'd-5': [('final', '\n\t', 4)]}),
          op('recent'),
          op('orphans', admitted=['d-6', 'd-7'], live=[], said={}),
          op('recent')),
]
AGENTS = [('Theo', 'theo', False), ('Mike', 'mike', False), ('Sweeper', 'sweep', True)]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-delegation-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, message_store, oracle_delegations as book, prompt_admissions, turn_dispatch, turn_queue

    clock = {'now': NOW}
    db.now_ms = lambda: clock['now']
    work = root / 'work'
    work.mkdir()
    ids = {}
    for persona, session, janitor in AGENTS:
        ids[session] = agents.create_agent(persona=persona, voice_id='', cwd=str(work), session=session, backend='claude')
        if janitor:
            db.conn().execute('UPDATE agents SET is_janitor = 1 WHERE agent_id = ?', (ids[session],))
    names = {agent_id: session for session, agent_id in ids.items()}
    world = {}

    class Service:
        """A stand-in for what admits a turn: it says what it was told to say."""

        def __init__(self, ctx):
            pass

        def submit(self, command):
            world['admitted'] = {'text': command.text, 'session': command.forced_session, 'trace': command.trace_id,
                                 'client_msg_id': command.client_msg_id, 'origin': command.origin,
                                 'speaks': command.synthesize_audio, 'waits_if_busy': command.queue_if_busy}
            if world.get('raises'):
                raise RuntimeError(world['raises'])
            if world.get('queue'):
                db.conn().execute(
                    'INSERT OR REPLACE INTO queued_turns (queue_id, agent_id, session, text, trace_id, status, enqueued_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (command.client_msg_id, ids[command.forced_session], command.forced_session, command.text,
                     command.trace_id, world['queue'], clock['now']))

            class Admitted:
                session = command.forced_session
                queued = bool(world.get('queued'))
            return Admitted()

    turn_dispatch.TurnDispatchService = Service
    prompt_admissions.create = lambda **said: {'admission_id': 'a stand-in'}
    agents.live_backend_session = lambda agent_id: world.get('backend', '')
    message_store.has_client_message = lambda client_msg_id: bool(world.get('has_message'))
    taken_from_queue = []
    turn_queue.remove = lambda queue_id: taken_from_queue.append(queue_id)

    def told(row):
        """A row as a client is told it, with the agent by its session: the ids are this run's own."""
        if row is None:
            return None
        row = dict(row)
        row['agent_id'] = names.get(row['agent_id'], row['agent_id'])
        return row

    def table():
        rows = db.conn().execute('SELECT * FROM oracle_delegations ORDER BY delegation_id').fetchall()
        return [told(book._row(row)) for row in rows]

    def tried(act):
        try:
            return {'answer': act()}
        except book.DelegationCollision as error:
            return {'refused': 'collision', 'words': str(error)}
        except book.DelegationNotCancellable as error:
            return {'refused': 'not cancellable', 'words': str(error)}
        except ValueError as error:
            return {'refused': 'value', 'words': str(error)}
        except LookupError as error:
            return {'refused': 'lookup', 'words': str(error)}
        except RuntimeError as error:
            return {'refused': 'raised', 'words': str(error)}

    def played(step):
        kind = step['op']
        clock['now'] += 1000
        world.clear()
        del taken_from_queue[:]
        owner = step.get('owner', ADMIN)
        if kind == 'dispatch':
            world.update({key: step[key] for key in ('has_message', 'raises', 'queued', 'queue', 'backend') if key in step})
            # A queue that has the turn already, from an earlier asking, is as it is told now.
            if step.get('queue') and step.get('has_message'):
                db.conn().execute('UPDATE queued_turns SET status = ? WHERE queue_id = ?',
                                  (step['queue'], 'oracle-' + step['delegation_id'].strip()))
            agent = agents.get_by_session(step['session'].strip())
            said = tried(lambda: told(book.dispatch(ctx=None, delegation_id=step['delegation_id'], session=step['session'],
                                                    request_text=step['request'], authenticated_at_admission=True,
                                                    owner_principal=owner)))
            said['agent'] = None if agent is None else {'session': agent['session'], 'janitor': bool(agent['is_janitor'])}
            said['admitted'] = world.get('admitted')
            return said
        if kind == 'dispatched':
            # What admits a turn says of it, said of a delegation as it is.
            if step.get('queue'):
                db.conn().execute(
                    'INSERT OR REPLACE INTO queued_turns (queue_id, agent_id, session, text, trace_id, status, enqueued_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('oracle-' + step['delegation_id'].strip(), ids['theo'], 'theo', 'x', 'oracle-' + step['delegation_id'].strip(),
                     step['queue'], clock['now']))
            return tried(lambda: told(book.mark_dispatched(step['delegation_id'], backend_session_id=step['backend'],
                                                           queued=step['queued'])))
        if kind == 'completed':
            return tried(lambda: book.complete_for_trace(trace_id=step['trace'], message_id=step['message'], text=step['text']))
        if kind == 'failed':
            return tried(lambda: told(book.fail(step['delegation_id'], step['reason'])))
        if kind == 'failed_for_trace':
            return tried(lambda: book.fail_for_trace(step['trace'], step['reason']))
        if kind == 'started':
            return tried(lambda: book.mark_started_for_trace(step['trace']))
        if kind == 'cancel':
            return tried(lambda: told(book.cancel(step['delegation_id'], owner_principal=owner)))
        if kind == 'acknowledge':
            return tried(lambda: book.acknowledge(step['delegation_id'], owner_principal=owner))
        if kind == 'undelivered':
            return tried(lambda: [told(row) for row in book.undelivered(owner_principal=owner, limit=step.get('limit', 50))])
        if kind == 'recent':
            whose = step.get('all_of', owner)
            return tried(lambda: [told(row) for row in book.recent(owner_principal=whose, limit=step.get('limit', 50))])
        if kind == 'active':
            agent = ids.get(step['agent'], step['agent'])
            return tried(lambda: book.active_requests_for_trace(agent, step['trace']))
        if kind == 'same_moment':
            # Two that end in the same millisecond: shown in the order of their ids.
            for delegation_id in step['ids']:
                book.complete_for_trace(trace_id='oracle-' + delegation_id, message_id=step['message'], text=step['text'])
            return {'answer': True}
        if kind == 'stop':
            released = []

            def stop():
                does = step.get('does')
                if does == 'raises':
                    raise RuntimeError('the agent could not be stopped')
                if does == 'fails':
                    book.fail_for_trace(step['trace'], 'Stopped.')
                if does == 'completes':
                    book.complete_for_trace(trace_id=step['trace'], message_id='m-race', text='Done as it was stopped.')
                return lambda traces: released.append(sorted(traces))
            said = tried(lambda: [told(row) for row in book.cancel_for_session(step['session'], stop=stop,
                                                                               owner_principal=owner)])
            said['taken_from_queue'] = list(taken_from_queue)
            said['released'] = released
            return said
        if kind == 'orphans':
            for delegation_id, rows in step['said'].items():
                for where, text, revision in rows:
                    db.conn().execute(
                        'INSERT INTO messages (message_id, agent_id, backend_session_id, source_file, seq, role, text,'
                        ' updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (f'm-{delegation_id}-{revision}', ids['theo'], 'thread', f'{where}:oracle-{delegation_id}',
                         revision + 100 * int(delegation_id[2:]), 'assistant', text, clock['now'], revision))
            admitted = {'oracle-' + delegation_id for delegation_id in step['admitted']}
            message_store.has_client_message = lambda client_msg_id: client_msg_id in admitted
            live = {'oracle-' + delegation_id for delegation_id in step['live']}
            asked = []

            def is_live(agent_id, trace):
                asked.append(trace)
                return trace in live
            said = tried(lambda: book.reconcile_orphans(is_live=is_live))
            said['asked_whether_live'] = asked
            message_store.has_client_message = lambda client_msg_id: bool(world.get('has_message'))
            return said
        raise SystemExit(f'no such step: {kind}')

    stories = []
    for one in STORIES:
        for name in ('oracle_delegations', 'queued_turns', 'messages'):
            db.conn().execute(f'DELETE FROM {name}')
        clock['now'] = NOW
        steps = []
        for step in one['steps']:
            said = played(step)
            steps.append({**step, 'at': clock['now'], **said, 'rows': table()})
        stories.append({'name': one['name'], 'steps': steps})
    return {'what': 'server/lib/oracle_delegations.py', 'id_most': 80, 'reason_most': 500, 'request_most': 8000,
            'most_shown': 200, 'usual_shown': 50, 'administrator': ADMIN, 'stories': stories}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    parts = list(said)
    for at, part in enumerate(parts):
        end = ',' if at < len(parts) - 1 else ''
        if part != 'stories':
            out.append(f' {json.dumps(part)}: {json.dumps(said[part], ensure_ascii=True)}{end}')
            continue
        out.append(' "stories": [')
        for index, one in enumerate(said[part]):
            out.append('  {"name": ' + json.dumps(one['name'], ensure_ascii=True) + ', "steps": [')
            out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(one['steps']) - 1 else '')
                       for n, row in enumerate(one['steps']))
            out.append('  ]}' + (',' if index < len(said[part]) - 1 else ''))
        out.append(' ]' + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    steps = [step for one in said['stories'] for step in one['steps']]
    refused = sum(1 for step in steps if 'refused' in step)
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories, {len(steps)} steps, {refused} refused, '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
