#!/usr/bin/env python3
"""Writes down what the Python Host does when a call of the Oracle is put through to an agent, and back.

A handoff is a record the Host keeps for a phone: offered to the phone, prepared and activated by it step by step
(each step acknowledged), with a time for every step after which the Host acts by itself. The phone owns the sound
and the Host owns the record. The rule is `server/lib/oracle_handoffs.py` (the contract: docs/oracle-handoff.md),
run here as it is: with a clock, timers, ids and a Host's name of this program's own, its store in a directory of
this program's own, and stand-ins for the calls it acts on (which only write down what they are told), for the
apps' event stream, for the agents a name is looked up among, and for the Host's focus.

For every step of every story it is written down what the step was answered or refused with, what the calls and
the apps were told, which deadlines stand, and what the step changed of what the Host keeps for every phone.

    <the Python Host's interpreter> server-rs/tools/oracle-handoff-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-handoff-vectors.json'
NOW = 1_800_000_000_000
ADMIN = 'administrator'
PHONE, OTHER = 'device_phone', 'device_other'


def H(number: int) -> str:
    """The name of the handoff that was made as this one of a story: the first is 1."""
    return 'hof_' + f'{number:032x}'


def call(name, principal=PHONE, voice=None, *, capable=True, handoff_id=''):
    """A call of the Oracle that is open: of whose phone, under which name of its own, whether its phone takes
    handoffs, and the handoff it is the way back of."""
    return {'name': name, 'principal': principal, 'voice_session_id': voice or f'voice-{name}',
            'provider_session': f'provider-{name}', 'thread_id': f'thread-{principal}', 'handoff_capable': capable,
            'handoff_id': handoff_id}


def ack(caller, number, phase, generation=None, reason=None, **more):
    """A phone says how far it is with a handoff. `generation`: the one the handoff has, unless the story says."""
    body = {'handoff_id': H(number) if isinstance(number, int) else number, 'phase': phase, **more}
    if generation is not None:
        body['generation'] = generation
    if reason is not None:
        body['reason'] = reason
    return ['ack', caller, body, generation is None]


def out(number):
    """Put through to an agent, as far as the phone hears and speaks with it: every step acknowledged."""
    return [ack(PHONE, number, 'preparing'), ack(PHONE, number, 'activating'), ack(PHONE, number, 'ready')]


STORIES = {
    'a call is put through to an agent, step by step, and back': [
        ['register', call('first')],
        ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None],
        ['outcome', H(1), False], ['outcome', H(1), True],
        ['snapshot', PHONE],
        ack(PHONE, 1, 'preparing'), ['outcome', H(1), True],
        ack(PHONE, 1, 'activating'), ['outcome', H(1), True],
        ack(PHONE, 1, 'ready'), ['outcome', H(1), True], ['outcome', H(1), False],
        ['snapshot', PHONE],
        # The phone has left the call of the Oracle, whose socket is closed; the way back opens another.
        ['unregister', 'first'],
        ['snapshot', PHONE],
        ['connect', PHONE, 'ORACLE', None],
        ['snapshot', PHONE],
        ack(PHONE, 2, 'preparing'),
        ['register', call('back', handoff_id=H(2))],
        ack(PHONE, 2, 'activating'),
        ['snapshot', PHONE],
        ack(PHONE, 2, 'ready'),
        ['outcome', H(2), True],
        ['snapshot', PHONE],
        ['pass', 60_000],
    ],
    'every acknowledgement said twice is taken once': [
        ['register', call('first')],
        ['connect', PHONE, 'Theo', None],
        ack(PHONE, 1, 'preparing'), ack(PHONE, 1, 'preparing'),
        ack(PHONE, 1, 'activating'), ack(PHONE, 1, 'activating'), ack(PHONE, 1, 'preparing'),
        ack(PHONE, 1, 'ready'), ack(PHONE, 1, 'ready'), ack(PHONE, 1, 'activating'),
        # What was never said before is no step from here.
        ack(PHONE, 1, 'rolled_back'), ack(PHONE, 1, 'failed'), ack(PHONE, 1, 'rollback_failed'),
        ack(PHONE, 1, 'abandon', reason='no reason of the contract'),
        ack(PHONE, 1, 'abandon', reason='local_stop'), ack(PHONE, 1, 'abandon', reason='local_stop'),
        ack(PHONE, 1, 'ready'), ack(PHONE, 1, 'preparing'),
    ],
    'what is no acknowledgement is refused': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 'hof_1', 'preparing', 1), ack(PHONE, H(1).upper(), 'preparing', 1), ack(PHONE, H(1) + '0', 'preparing', 1),
        ack(PHONE, 'hof_' + 'A' * 32, 'preparing', 1), ack(PHONE, 'hof_' + 'g' * 32, 'preparing', 1),
        ack(PHONE, 'hof_' + 'a' * 32, 'preparing', 1), ack(PHONE, ' ' + H(1), 'preparing', 1),
        ack(PHONE, '', 'preparing', 1), ack(PHONE, 1, 'prepared', 1), ack(PHONE, 1, '', 1),
        ack(PHONE, 1, 'preparing', '1'), ack(PHONE, 1, 'preparing', True), ack(PHONE, 1, 'preparing', 1.0),
        ['ack', PHONE, {'handoff_id': H(1), 'phase': 'preparing'}, False],
        ack(PHONE, 1, 'preparing', 2), ack(PHONE, 1, 'preparing', 0),
        ack(PHONE, 9, 'preparing', 1), ack(OTHER, 1, 'preparing', 1),
        ack(ADMIN, 1, 'preparing', 1), ack(ADMIN, 9, 'preparing', 1),
        ack(PHONE, 1, 'activating', 1, reason=7),
    ],
    'a phone that does not answer an offer, and one that does not get on with it': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None],
        ['pass', 9_999], ['snapshot', PHONE], ['pass', 1], ['outcome', H(1), True],
        ack(PHONE, 1, 'preparing'), ack(PHONE, 1, 'rolled_back'),
        ['connect', PHONE, 'mike', None],
        ack(PHONE, 2, 'preparing'), ['pass', 9_999], ['pass', 1], ['outcome', H(2), True], ['snapshot', PHONE],
        # Late, and still taken: the phone says it has the call of the Oracle again.
        ack(PHONE, 2, 'rolled_back'), ack(PHONE, 2, 'rolled_back'), ack(PHONE, 2, 'ready'),
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 3, 'preparing'), ack(PHONE, 3, 'activating'), ['pass', 29_999], ['pass', 1],
        ['outcome', H(3), True], ['snapshot', PHONE],
        # Late, and still taken: the phone is with the agent after all.
        ack(PHONE, 3, 'ready'), ['snapshot', PHONE],
    ],
    'a handoff that broke is rolled back late, and one whose way back cannot be made': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 1, 'preparing'), ack(PHONE, 1, 'activating'), ['pass', 30_000],
        ack(PHONE, 1, 'rolled_back'), ack(PHONE, 1, 'ready'),
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 2, 'preparing'), ack(PHONE, 2, 'activating'), ack(PHONE, 2, 'rollback_failed'),
        ['outcome', H(2), True], ack(PHONE, 2, 'ready'), ack(PHONE, 2, 'rolled_back'),
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 3, 'preparing'), ack(PHONE, 3, 'activating'), ack(PHONE, 3, 'rolled_back'),
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 4, 'failed'), ack(PHONE, 4, 'failed'), ack(PHONE, 4, 'preparing'),
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 5, 'failed', reason='local_intent_changed'),
        # A phone that is still preparing has the call again; and one that is still preparing is not there yet.
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 6, 'preparing'), ack(PHONE, 6, 'rolled_back'), ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None],
        ack(PHONE, 7, 'preparing'), ack(PHONE, 7, 'ready'), ack(PHONE, 7, 'rollback_failed'),
        ack(PHONE, 7, 'abandon', reason='local_stop'),
    ],
    'the phone is with the agent it was put through to last': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None], *out(1),
        ['connect', PHONE, 'mike', None], *out(2),
        ['snapshot', PHONE],
        # The older talk is not the one the phone is with an agent by: it cannot be given up at its older number.
        ack(PHONE, 1, 'abandon', 1, reason='local_stop'),
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 3, 'preparing'),
        ['register', call('back', handoff_id=H(3))],
        ack(PHONE, 3, 'activating'), ack(PHONE, 3, 'ready'),
        ['snapshot', PHONE],
        # Now it is, and can.
        ack(PHONE, 1, 'abandon', 1, reason='local_stop'),
        ['snapshot', PHONE],
    ],
    'the way back: refused, failed, given up, and made': [
        ['register', call('first')],
        ['connect', PHONE, 'oracle', None],
        ['connect', PHONE, 'theo', None], *out(1),
        ['unregister', 'first'],
        ['connect', PHONE, 'Oracle', None],
        ['connect', PHONE, 'oracle', None],
        # The phone was told to go back and does something else: both are over.
        ack(PHONE, 2, 'failed', reason='local_intent_changed'), ['snapshot', PHONE],
        ['connect', PHONE, 'oracle', None],
        ['register', call('second')],
        ['connect', PHONE, 'theo', None], *out(3),
        ['unregister', 'second'],
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 4, 'failed'), ['snapshot', PHONE],
        # A way back that failed leaves the phone with the agent: it can still end that, at the older number,
        # and say nothing else at it.
        ack(PHONE, 3, 'ready', 3), ack(PHONE, 3, 'rolled_back', 3), ack(PHONE, 3, 'abandon', 3, reason='no reason'),
        ack(PHONE, 3, 'abandon', 3, reason='local_stop'), ['snapshot', PHONE],
        ['register', call('third')],
        ['connect', PHONE, 'theo', None], *out(5),
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 6, 'preparing'), ack(PHONE, 6, 'activating'), ack(PHONE, 6, 'rollback_failed'), ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], *out(7),
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 8, 'preparing'), ['pass', 10_000], ['snapshot', PHONE],
        ack(PHONE, 8, 'rolled_back'), ['snapshot', PHONE],
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 9, 'preparing'), ack(PHONE, 9, 'activating'), ['pass', 30_000], ['snapshot', PHONE],
        ack(PHONE, 9, 'ready'), ['snapshot', PHONE],
    ],
    'a handoff is given up by the phone at every point': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None], ack(PHONE, 1, 'abandon', reason='another reason'), ack(PHONE, 1, 'abandon'),
        ack(PHONE, 1, 'abandon', reason='local_stop'),
        ['connect', PHONE, 'theo', None], ack(PHONE, 2, 'preparing'), ack(PHONE, 2, 'abandon', reason='client_restarted'),
        ['connect', PHONE, 'theo', None], ack(PHONE, 3, 'preparing'), ack(PHONE, 3, 'activating'),
        ack(PHONE, 3, 'abandon', reason='local_intent_changed'), ack(PHONE, 3, 'abandon', reason='local_stop'),
        ['connect', PHONE, 'theo', None], *out(4),
        ['connect', PHONE, 'oracle', None], ack(PHONE, 5, 'abandon', reason='local_stop'), ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], *out(6),
        ['connect', PHONE, 'oracle', None], ack(PHONE, 7, 'preparing'), ack(PHONE, 7, 'abandon', reason='client_restarted'),
        ['snapshot', PHONE],
        # The talk with the agent is given up while a way back is offered, and then the way back.
        ['connect', PHONE, 'theo', None], *out(8),
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 8, 'abandon', 8, reason='local_stop'), ack(PHONE, 9, 'abandon', reason='local_stop'),
        ['snapshot', PHONE],
    ],
    'what a call cannot be put through to, and whose call': [
        ['connect', PHONE, 'theo', None],
        ['connect', ADMIN, 'theo', None], ['connect', ADMIN, 'oracle', None],
        ['register', call('first')],
        ['connect', PHONE, '', None], ['connect', PHONE, '   ', None],
        ['connect', PHONE, 'nobody', None], ['connect', PHONE, 'twins', None], ['connect', PHONE, 'odd', None],
        ['connect', OTHER, 'theo', None],
        ['connect', ADMIN, ' Theo ', None],
        ['register', call('second', OTHER, capable=False)],
        ['connect', OTHER, 'theo', None],
        ['connect', ADMIN, 'mike', None], ['connect', ADMIN, 'mike', OTHER], ['connect', ADMIN, 'mike', PHONE],
        ['connect', ADMIN, 'mike', 'device_unknown'],
        ['snapshot', PHONE], ['snapshot', OTHER], ['snapshot', 'device_unknown'],
        ack(ADMIN, 2, 'preparing'), ack(ADMIN, 2, 'activating'),
        ['connect', PHONE, 'theo', None], ['connect', ADMIN, 'theo', PHONE],
        ack(ADMIN, 2, 'ready'),
        ['connect', ADMIN, 'oracle', None], ['connect', ADMIN, 'oracle', OTHER],
        ['unregister', 'second'], ['register', call('third', OTHER)],
        ['connect', OTHER, 'theo', None], *[[step[0], OTHER, *step[2:]] for step in out(4)],
        ['connect', ADMIN, 'oracle', None],
        ['connect', ADMIN, 'oracle', OTHER],
    ],
    'an offer is over when another is made, and when the phone opens another call': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None],
        ['connect', PHONE, 'mike', None],
        ack(PHONE, 1, 'preparing'), ack(PHONE, 2, 'preparing'),
        ['connect', PHONE, 'theo', None],
        ['register', call('second')],
        # The older call's socket is closed only now: the newer is the phone's call all the same.
        ['unregister', 'first'], ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], ack(PHONE, 4, 'preparing'), ack(PHONE, 4, 'activating'),
        # One that is being activated is not over for another call: nor is another made beside it.
        ['register', call('third')], ['connect', PHONE, 'mike', None],
        ack(PHONE, 4, 'ready'),
        # The phone opens a call that is no way back: it has returned by itself.
        ['register', call('fourth')], ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], *out(5),
        ['connect', PHONE, 'oracle', None],
        # The phone opens a call that is not the way back it was offered: the offer is over, and so is the handoff.
        ['register', call('fifth')], ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], *out(7),
        ['connect', PHONE, 'oracle', None], ack(PHONE, 8, 'preparing'),
        # The way back, opened by its name: nothing is over.
        ['register', call('sixth', handoff_id=H(8))], ['snapshot', PHONE],
        ack(PHONE, 8, 'activating'), ack(PHONE, 8, 'ready'),
        ['connect', PHONE, 'theo', None], *out(9),
        # A call that says it is the way back of another handoff than the one that is offered.
        ['connect', PHONE, 'oracle', None],
        ['register', call('seventh', handoff_id=H(8))], ['snapshot', PHONE],
    ],
    'the way back with its call open, and beside an offer of another agent': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None], *out(1),
        # Another agent is offered while the phone is with the first: the way back is still to be had.
        ['connect', PHONE, 'mike', None],
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 3, 'preparing'),
        ['register', call('back', handoff_id=H(3))],
        # The phone does not get on with it: the call that is the way back is told nothing.
        ['pass', 10_000], ['snapshot', PHONE],
        ack(PHONE, 3, 'rolled_back'),
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 4, 'preparing'), ack(PHONE, 4, 'activating'),
        ['register', call('back-again', handoff_id=H(4))],
        ack(PHONE, 4, 'rolled_back'), ['snapshot', PHONE],
        ['connect', PHONE, 'oracle', None],
        ack(PHONE, 5, 'preparing'), ack(PHONE, 5, 'activating'),
        ['register', call('back-once-more', handoff_id=H(5))],
        ack(PHONE, 5, 'rollback_failed'), ['snapshot', PHONE],
    ],
    'the state of the call of the Oracle, as a handoff says it': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None], ['snapshot', PHONE],
        ack(PHONE, 1, 'preparing'), ['snapshot', PHONE],
        ['unregister', 'first'], ['snapshot', PHONE],
        ['register', call('first')], ['snapshot', PHONE],
        ack(PHONE, 1, 'activating'), ['snapshot', PHONE],
        ack(PHONE, 1, 'ready'), ['snapshot', PHONE],
        ack(PHONE, 1, 'abandon', reason='local_stop'), ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], ack(PHONE, 2, 'preparing'), ['pass', 10_000], ['snapshot', PHONE],
        ['unregister', 'first'],
        ack(PHONE, 2, 'rolled_back'), ['snapshot', PHONE],
        # What the call was when a handoff was over is kept as it was then, whatever the call is later.
        ['register', call('second')],
        ['connect', PHONE, 'theo', None], ['pass', 10_000], ['snapshot', PHONE],
        ['unregister', 'second'], ['snapshot', PHONE], ['outcome', H(3), True],
    ],
    'a Host that starts again in the middle of a handoff': [
        ['register', call('first')],
        ['connect', PHONE, 'theo', None], ack(PHONE, 1, 'preparing'), ['pass', 6_000],
        ['restart'], ['snapshot', PHONE], ['pass', 6_000], ['snapshot', PHONE], ['pass', 4_000], ['snapshot', PHONE],
        ['register', call('second')],
        ['connect', PHONE, 'theo', None], *out(2),
        ['restart'], ['snapshot', PHONE],
        ['connect', PHONE, 'oracle', None], ['pass', 5_000], ['restart'], ['pass', 9_999], ['pass', 1], ['snapshot', PHONE],
        ['register', call('third', OTHER)],
        ['connect', OTHER, 'mike', None], ['restart'], ['restart'], ['pass', 10_000], ['snapshot', OTHER],
    ],
    'no more is kept of what is over than twenty, and all of what is not': [
        ['register', call('first')],
        *[step for number in range(1, 24) for step in (['connect', PHONE, 'theo', None], ack(PHONE, number, 'failed'))],
        ['snapshot', PHONE],
        ['connect', PHONE, 'theo', None], *out(24),
        ['unregister', 'first'],
        *[step for number in range(25, 49) for step in (['connect', PHONE, 'oracle', None], ack(PHONE, number, 'failed'))],
        ['snapshot', PHONE],
        ack(PHONE, 1, 'failed'), ack(PHONE, 24, 'abandon', 24, reason='local_stop'),
    ],
    'what may still be acknowledged late is kept however old, with what it is the way back of': [
        ['register', call('first')],
        # One the phone did not get on with, one that broke, and a way back the phone did not get on with.
        ['connect', PHONE, 'theo', None], ack(PHONE, 1, 'preparing'), ['pass', 10_000],
        ['connect', PHONE, 'theo', None], ack(PHONE, 2, 'preparing'), ack(PHONE, 2, 'activating'), ['pass', 30_000],
        ['connect', PHONE, 'theo', None], *out(3),
        ['connect', PHONE, 'oracle', None], ack(PHONE, 4, 'preparing'), ['pass', 10_000],
        # The talk with the agent is given up: it is over, and is still what the way back was of.
        ack(PHONE, 3, 'abandon', 3, reason='local_stop'),
        *[step for number in range(5, 30) for step in (['connect', PHONE, 'theo', None], ack(PHONE, number, 'failed'))],
        ['snapshot', PHONE],
        ack(PHONE, 1, 'rolled_back', 1), ack(PHONE, 2, 'ready', 2), ack(PHONE, 4, 'rolled_back', 4),
        ['connect', PHONE, 'theo', None], ack(PHONE, 30, 'failed'),
    ],
}

AGENTS = {'theo': ('theo', 'a-theo', 'Theo'), 'mike': ('mike', 'a-mike', 'Mike')}


def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-handoff-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import threading
    from lib import oracle_contact, oracle_handoffs as handoffs, settings_store

    state = {'now': NOW, 'made': 0, 'boots': 1}
    happened: list = []
    timers: list = []

    class Timer:
        def __init__(self, delay, function, args=()):
            self.delay, self.function, self.args, self.daemon, self.live = delay, function, args, False, False

        def start(self):
            self.due, self.live = state['now'] + round(self.delay * 1000), True
            timers.append(self)

        def cancel(self):
            self.live = False

    class Made:
        def __init__(self):
            state['made'] += 1
            self.hex = f'{state["made"]:032x}'

    class Call:
        def __init__(self, said):
            self.__dict__.update(said)
            self.held_for_handoff = False

        def told(self, *what):
            happened.append([self.name, *what])

        def downstream(self, frame):
            self.told('downstream', frame)

        def handoff_offer(self, persona):
            self.told('offer', persona)

        def handoff_mirror(self, record):
            self.told('mirror', record)

        def handoff_hold(self):
            self.held_for_handoff = True
            self.told('hold')

        def handoff_resume(self):
            self.held_for_handoff = False
            self.told('resume')

        def handoff_failed(self, persona, reason, *, note):
            self.told('failed', persona, reason, note)

        def handoff_returned(self, persona):
            self.told('returned', persona)

    def resolve(name):
        if name.strip().casefold() in AGENTS:
            session, agent_id, persona = AGENTS[name.strip().casefold()]
            return {'session': session, 'agent_id': agent_id, 'persona': persona, 'more': 'that is not kept'}
        raise ValueError({'nobody': 'unknown agent', 'twins': 'ambiguous agent'}.get(name, 'something else'))

    def failed(*said, **more):
        raise AssertionError(f'the Host caught an error of its own: {said} {more}')

    handoffs.threading = types.SimpleNamespace(Timer=Timer, RLock=threading.RLock, Condition=threading.Condition)
    handoffs.uuid = types.SimpleNamespace(uuid4=Made)
    handoffs.now_ms = lambda: state['now']
    handoffs.host_id = lambda: 'host-of-the-vectors'
    handoffs.log = lambda *said, **more: None
    handoffs.log_exception = failed
    handoffs._focus = lambda agent: happened.append(['focus', agent])
    handoffs._context['stream'] = object()
    handoffs.events.broadcast = lambda stream, event: happened.append(['apps', dict(event)])
    oracle_contact.resolve_visible = resolve

    def kept():
        """Everything the Host keeps, phone by phone: how many handoffs it has made for the phone, and each record
        as it is kept, without the mark of which run of the Host kept it."""
        whole = {}
        for principal in sorted(handoffs._principals()):
            data = json.loads(settings_store.get(handoffs.KEY + principal))
            records = [{name: value for name, value in record.items() if name != '_boot'} for record in data['records']]
            whole[principal] = {'generation': data['generation'], 'records': records}
        return whole

    def changed(before, after):
        """What a step changed of what is kept: for each phone whose book is not as it was, the number of handoffs
        made, the records there are by their names in their order, and each record that is new or another."""
        change = {}
        for principal, book in after.items():
            was = before.get(principal, {'generation': 0, 'records': []})
            if book == was:
                continue
            old = {record['handoff_id']: record for record in was['records']}
            change[principal] = {
                'generation': book['generation'], 'names': [record['handoff_id'] for record in book['records']],
                'records': [record for record in book['records'] if old.get(record['handoff_id']) != record]}
        return change

    def told(step, calls):
        kind = step[0]
        if kind == 'register':
            calls[step[1]['name']] = made = Call(step[1])
            return handoffs.register(made)
        if kind == 'unregister':
            return handoffs.unregister(calls[step[1]])
        if kind == 'connect':
            return handoffs.connect(step[1], step[2], principal=step[3], wait=None)
        if kind == 'ack':
            return handoffs.ack(step[1], step[2])
        if kind == 'snapshot':
            return handoffs.snapshot(step[1])
        if kind == 'outcome':
            record = next(record for principal in handoffs._principals()
                          for record in handoffs._load(principal)['records'] if record['handoff_id'] == step[1])
            return handoffs._outcome(handoffs.public(record), wait=step[2])
        if kind == 'pass':
            until = state['now'] + step[1]
            while True:
                due = sorted((timer for timer in timers if timer.live and timer.due <= until), key=lambda t: t.due)
                if not due:
                    break
                timer = due[0]
                timer.live, state['now'] = False, timer.due
                happened.append(['deadline', timer.args[1], timer.args[2]])
                timer.function(*timer.args)
            state['now'] = until
            return None
        if kind == 'restart':
            state['boots'] += 1
            handoffs._BOOT = f'boot-{state["boots"]}'
            handoffs._LIVE.clear()
            handoffs._TIMERS.clear()
            timers.clear()
            calls.clear()
            return handoffs.recover()
        raise AssertionError(step)

    stories = []
    for name, steps in STORIES.items():
        for key in list(settings_store.keys_with_prefix(handoffs.KEY)):
            settings_store.delete(key)
        handoffs._LIVE.clear()
        handoffs._TIMERS.clear()
        timers.clear()
        state.update(now=NOW, made=0, boots=1)
        handoffs._BOOT = 'boot-1'
        calls: dict = {}
        played = []
        before: dict = {}
        for step in steps:
            happened.clear()
            state['now'] += 7
            if step[0] == 'ack':
                # Said with the number the handoff has, where the story does not say another.
                body = dict(step[2])
                if step[3]:
                    record = next((record for principal in handoffs._principals()
                                   for record in handoffs._load(principal)['records']
                                   if record['handoff_id'] == body['handoff_id']), None)
                    body['generation'] = record['generation'] if record else 0
                step = ['ack', step[1], body]
            done = {'step': step, 'at': state['now']}
            try:
                done['answered'] = told(step, calls)
            except handoffs.HandoffError as refused:
                done['refused'] = [refused.status, refused.body()]
            done['happened'] = json.loads(json.dumps(happened))
            done['deadlines'] = sorted([timer.args[1], timer.due, timer.args[2]] for timer in timers if timer.live)
            after = kept()
            done['kept'] = changed(before, after)
            before = after
            done['now'] = state['now']
            played.append(done)
        stories.append({'name': name, 'steps': played})
    return {'what': 'server/lib/oracle_handoffs.py', 'now': NOW, 'host_id': 'host-of-the-vectors',
            'ttl_ms': handoffs.TTL_MS, 'keep': handoffs.KEEP_RECORDS,
            'connect_waits_ms': round(handoffs.CONNECT_WAIT_SECONDS * 1000), 'stories': stories}


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
    lines = ['{']
    for name in ('what', 'now', 'host_id', 'ttl_ms', 'keep', 'connect_waits_ms'):
        lines.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    lines.append(' "stories": [')
    for at, story in enumerate(said['stories']):
        lines.append('  {"name": ' + json.dumps(story['name'], ensure_ascii=True) + ', "steps": [')
        lines.extend('   ' + json.dumps(step, ensure_ascii=True) + (',' if n < len(story['steps']) - 1 else '')
                     for n, step in enumerate(story['steps']))
        lines.append('  ]}' + (',' if at < len(said['stories']) - 1 else ''))
    lines.append(' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    steps = [step for story in said['stories'] for step in story['steps']]
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories, {len(steps)} steps, '
          f'{sum(1 for step in steps if "refused" in step)} refused, '
          f'{sum(len(step["happened"]) for step in steps)} things told, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
