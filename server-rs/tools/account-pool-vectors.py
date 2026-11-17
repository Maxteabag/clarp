#!/usr/bin/env python3
"""What the Python Host does with the turns of one account pool (the agents of one tool on one
machine, which share its login) when an account is out: `server/lib/account_failover.py`, the
coordinator, with the plan it asks (`server/lib/policies/failover.py`) and the reading of the
owner's selector (`switch_account`). Its own code is driven through made-up situations, one step
at a time, with a clock, a selector and timers that the script holds, and what it did at each
step is written to `crates/clarp-kernel/tests/fixtures/account-pool-vectors.json`, so that the
Rust Host's pool is held to it.

A situation is a list of steps. What a step can be:

- `register`: a turn of an agent is about to start (`agent`, `trace`, `model`). The answer says
  whether it has to wait; one that does not gets a process.
- `request`: a turn failed on its account (`plan`: the provider said the account does not offer
  its model). The answer says whether the pool takes it up; where it does not, the turn ends.
- `fire`: a timer the coordinator set comes due (`timer`: its number). With `verdict` where the
  pass gets as far as asking the selector: true (an account serves every model), a list (the
  account now active serves these), false (none does), null (no answer), "raise" (the selector
  could not be run). `during`: steps that happen while the selector runs.
- `advance`: the clock goes on (`seconds`).
- `disown`: the turn is no longer the agent's (its user stopped it, the agent was removed).
- `discard`: the turn is over (`agent`, `trace`).
- `served`: someone else changed the machine's login to an account known to serve `models`.
- `carried`: a Host that starts takes up a recovery that was under way (`since`: how long ago
  its turns were first parked, in seconds; `agents`: whose turns were).

What is written down of a step: what the call answered, what the coordinator did in order (a
turn paused, stopped, held, resumed, ended; the selector asked for which models; a timer set
and for when), and how the pool stood afterwards.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network. The selector it runs is this interpreter printing a text it is given.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/account-pool-vectors.py
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import pathlib
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'account-pool-vectors.json'

COMMAND = ['the-owners-selector', '--pool']
# How many steps before the one that went wrong are shown of a situation that does not go as written.
TRACE = next((arg.split('=', 1)[1] for arg in sys.argv[1:] if arg.startswith('--trace=')), '3')
ONLY = [arg for arg in sys.argv[1:] if not arg.startswith('--')]


def reg(agent, model='m1', trace=None, **more):
    return {'do': 'register', 'agent': agent, 'trace': trace or f't-{agent}', 'model': model, **more}


def req(agent, trace=None, **more):
    return {'do': 'request', 'agent': agent, 'trace': trace or f't-{agent}', **more}


def fire(timer, **more):
    return {'do': 'fire', 'timer': timer, **more}


def advance(seconds):
    return {'do': 'advance', 'seconds': seconds}


def disown(agent):
    return {'do': 'disown', 'agent': agent}


def discard(agent, trace=None):
    return {'do': 'discard', 'agent': agent, 'trace': trace or f't-{agent}'}


def served(*models):
    return {'do': 'served', 'models': list(models)}


def minutes(n, first_timer, verdict):
    """A selector that is asked once a minute and answers the same: `n` rounds, the timers
    numbered from `first_timer`."""
    steps = []
    for round_ in range(n):
        steps += [advance(60), fire(first_timer + round_, verdict=verdict)]
    return steps


SITUATIONS = [
    ('one-turn', 'A turn meets the limit, the selector finds an account, the turn goes on',
     [reg('a'), req('a'), fire(1, verdict=True), reg('a'), discard('a')]),
    ('nobody-asked', 'A turn that is not the pool\'s, a turn of another trace, a pool without a selector',
     [req('a'), reg('a'), req('a', trace='t-other'), {**req('a'), 'command': []}, req('a'), fire(1, verdict=True)]),
    ('two-running', 'Two turns run, one meets the limit: both are stopped, and both go on',
     [reg('a'), reg('b', 'm2'), req('a'), fire(1, verdict=True), reg('a'), reg('b', 'm2')]),
    ('joined', 'A second turn meets the limit while the first one\'s recovery is under way',
     [reg('a'), reg('b'), req('a'), req('b'), fire(1, verdict=True)]),
    ('no-account', 'No account serves: asked again every minute, and after half an hour the turns run and fail',
     [reg('a'), reg('b'), req('a'), fire(1, verdict=False), *minutes(29, 2, False),
      advance(60), fire(31, verdict=False),
      # Released: each runs once more, meets the limit, and ends there.
      reg('a'), req('a'), reg('b'), req('b'),
      # A new message to the agent is a new turn, and is the pool's again.
      # The check a minute ago still counts: the next is not due yet.
      reg('a', trace='t-next'), req('a', trace='t-next'), fire(32), advance(60), fire(33, verdict=True)]),
    ('found-at-the-half-hour', 'An account is found at the pass that would have let the turns go: they are taken up',
     [reg('a'), reg('b'), req('a'), fire(1, verdict=False), advance(1800), fire(2, verdict=True), reg('a'), reg('b')]),
    ('no-answer', 'The selector gives no answer, then cannot be run, then finds an account',
     [reg('a'), req('a'), fire(1, verdict=None), advance(60), fire(2, verdict='raise'),
      advance(59), fire(3), advance(1), fire(4, verdict=True)]),
    ('too-early', 'A timer that comes before the next check is due asks nobody',
     [reg('a'), req('a'), fire(1, verdict=False), advance(20), fire(2), advance(40), fire(3, verdict=True)]),
    ('arrives-meanwhile', 'A turn that is to start during a recovery waits with the others',
     [reg('a'), req('a'), reg('b'), fire(1, verdict=True)]),
    ('unchecked-model', 'A turn of another model arrives while the selector runs: one more whole check',
     [reg('a'), req('a'), fire(1, verdict=True, during=[reg('b', 'm2')]),
      advance(60), fire(2, verdict=True)]),
    ('stopped-by-its-user', 'The only waiting turn is stopped by its user: the recovery ends',
     [reg('a'), req('a'), disown('a'), fire(1), reg('a', trace='t-new')]),
    ('stopped-while-asked', 'A waiting turn is stopped while the selector runs: it is not taken up',
     [reg('a'), reg('b'), req('a'), fire(1, verdict=True, during=[disown('a')])]),
    ('over-while-asked', 'A waiting turn is over while the selector runs',
     [reg('a'), reg('b'), req('a'), fire(1, verdict=True, during=[discard('a')])]),
    ('some-models', 'The account now active serves one model of two: those turns go on, the others wait',
     [reg('a', 'm1'), reg('b', 'm2'), reg('c', 'm2'), req('b', ),
      fire(1, verdict=['m1']),
      # The pool works. A turn of the model that is not served waits with its like; one of the
      # served model runs.
      reg('a', 'm1'), reg('d', 'm2'), reg('e', 'm1'),
      # A minute on, the pool still works: nothing is asked, they wait on.
      advance(60), fire(2),
      # The working turns are over. The next look begins a recovery for what waits.
      discard('a'), discard('e'), advance(60), fire(3, verdict=True),
      reg('b', 'm2'), reg('c', 'm2'), reg('d', 'm2')]),
    ('none-of-them', 'The account now active serves none of the models asked for, or others',
     [reg('a', 'm1'), req('a'), fire(1, verdict=[]), advance(60), fire(2, verdict=['m9']),
      advance(60), fire(3, verdict=['m1', 'm9'])]),
    ('held-without-hold', 'A turn that cannot be told it is held is paused',
     [reg('a', 'm1'), reg('b', 'm2', hold=False), req('b'), fire(1, verdict=['m1']),
      reg('c', 'm2', hold=False)]),
    ('its-own-half-hour', 'A turn that waits for its model while the pool works runs after its own half hour, '
                          'and proves the model for the others',
     [reg('a', 'm1'), reg('b', 'm2'), req('b'), fire(1, verdict=['m1']), reg('a', 'm1'),
      advance(900), reg('c', 'm2'), fire(2),
      # b has waited half an hour; c has not. b runs: the trial for c.
      advance(900), fire(3), reg('b', 'm2'),
      # A minute on b still runs and has met nothing: the model is back, c starts.
      advance(60), fire(4), reg('c', 'm2')]),
    ('trial-fails', 'The turn released at its half hour meets the limit again: it ends, the others wait on',
     [reg('a', 'm1'), reg('b', 'm2'), req('b'), fire(1, verdict=['m1']), reg('a', 'm1'),
      advance(900), reg('c', 'm2'), fire(2), advance(900), fire(3), reg('b', 'm2'),
      advance(10), req('b'), discard('b'), advance(50), fire(4), advance(60), fire(5)]),
    ('trial-stopped', 'The turn released at its half hour is stopped by its user: it proves nothing, the others wait on',
     [reg('a', 'm1'), reg('b', 'm2'), req('b'), fire(1, verdict=['m1']), reg('a', 'm1'),
      advance(900), reg('c', 'm2'), fire(2), advance(900), fire(3), reg('b', 'm2'),
      disown('b'), advance(60), fire(4), advance(60), fire(5)]),
    ('trial-too-young', 'A trial that has not run a minute proves nothing yet',
     [reg('a', 'm1'), reg('b', 'm2'), req('b'), fire(1, verdict=['m1']), reg('a', 'm1'),
      advance(1740), reg('c', 'm2'), fire(2), advance(60), fire(3), reg('b', 'm2'),
      advance(30), fire(4)]),
    ('served-by-someone', 'Someone else changes the login to an account that serves the waiting model',
     [reg('a', 'm1'), reg('b', 'm2'), reg('c', 'm3'), req('b'), fire(1, verdict=['m1']), reg('a', 'm1'),
      served('m9'), served('m2'), reg('b', 'm2'), served('m2', 'm3'), reg('c', 'm3')]),
    ('served-during', 'Told of another login while a recovery is under way: nothing is done',
     [reg('a'), req('a'), served('m1'), fire(1, verdict=True)]),
    ('relapse', 'A turn meets the limit again soon after it was taken up: asked later each time, and the fifth ends it',
     [reg('a'), req('a'), fire(1, verdict=True),
      reg('a'), advance(30), req('a'), fire(2), advance(60), fire(3, verdict=True),
      reg('a'), advance(30), req('a'), fire(4), advance(120), fire(5, verdict=True),
      reg('a'), advance(30), req('a'), fire(6), advance(240), fire(7, verdict=True),
      reg('a'), advance(30), req('a'), fire(8), advance(480), fire(9, verdict=True),
      reg('a'), advance(30), req('a')]),
    ('relapse-of-another', 'The pool relapses on a turn that was not the one taken up: it is slowed, and never ended',
     [reg('a'), req('a'), fire(1, verdict=True), reg('a'), discard('a'),
      *itertools.chain.from_iterable(
          [reg(f'n{n}'), advance(20), req(f'n{n}'), fire(2 * n), advance(60 * 2 ** (min(n, 4) - 1)),
           fire(2 * n + 1, verdict=True), reg(f'n{n}'), discard(f'n{n}')] for n in range(1, 7))]),
    ('quiet-again', 'Ten quiet minutes after a recovery: the next limit is a new one',
     [reg('a'), req('a'), fire(1, verdict=True), reg('a'), advance(30), req('a'), fire(2), advance(60),
      fire(3, verdict=True), reg('a'), advance(600), req('a'), fire(4, verdict=True)]),
    ('plan-alone', 'The provider says the account does not offer the model, and nothing else runs: '
                   'the selector is asked three times, then the turn ends with what the provider said',
     [reg('a', 'm7'), req('a', plan=True), fire(1, verdict=False), advance(60), fire(2, verdict=['m1']),
      advance(60), fire(3, verdict=False)]),
    ('plan-served', 'The same, and another account offers the model',
     [reg('a', 'm7'), req('a', plan=True), fire(1, verdict=False), advance(60), fire(2, verdict=True),
      # Taken up onto an account said to offer it, and it does not: the turn ends.
      reg('a', 'm7'), advance(5), req('a', plan=True)]),
    ('plan-while-others-work', 'The same while other turns run: nothing is stopped, the turn ends',
     [reg('a', 'm7'), reg('b', 'm1'), req('a', plan=True)]),
    ('plan-joins', 'The same while a recovery is under way: it waits with the rest, as any turn',
     [reg('a', 'm7'), reg('b', 'm1'), req('b'), req('a', plan=True), fire(1, verdict=['m1']),
      reg('b', 'm1')]),
    ('plan-with-others-waiting', 'A turn parked for its plan among turns parked for the limit: only it ends',
     [reg('a', 'm7'), req('a', plan=True), reg('b', 'm1'), fire(1, verdict=False),
      advance(60), fire(2, verdict=False), advance(60), fire(3, verdict=False),
      advance(60), fire(4, verdict=True)]),
    ('plan-cannot-end', 'A turn parked for its plan that cannot be ended waits like the others',
     [reg('a', 'm7', fail=False), req('a', plan=True), fire(1, verdict=False), advance(60),
      fire(2, verdict=False), advance(60), fire(3, verdict=False), advance(60), fire(4, verdict=True)]),
    ('new-turn-after-expiry', 'A turn released at the half hour is not parked again, a new turn of the agent is',
     [reg('a'), req('a'), fire(1, verdict=False), advance(1800), fire(2, verdict=False),
      reg('b'), req('b'), reg('a'), fire(3), advance(60),
      fire(4, verdict=True, during=[reg('a', trace='t-next')])]),
    ('is-it-parked', 'Whether a turn waits with no process left, asked along the way',
     [reg('a'), {'do': 'parked', 'agent': 'a', 'trace': 't-a'}, req('a'),
      {'do': 'parked', 'agent': 'a', 'trace': 't-a'}, reg('b'), {'do': 'parked', 'agent': 'b', 'trace': 't-b'},
      {'do': 'parked', 'agent': 'b', 'trace': 't-other'}, fire(1, verdict=True),
      {'do': 'parked', 'agent': 'a', 'trace': 't-a'}]),
    ('carried', 'A Host that starts takes up a recovery that was under way, and waits for a turn of it to come back',
     [{'do': 'carried', 'since': 600, 'agents': ['a', 'b']}, fire(1), advance(1), fire(2), advance(1), reg('a'),
      fire(3, verdict=False), advance(1), reg('b'), advance(59), fire(4, verdict=True)]),
    ('carried-one-back', 'The same, and one turn is back when it first looks: it goes on at once, without the other',
     [{'do': 'carried', 'since': 600, 'agents': ['a', 'b']}, reg('a'), fire(1, verdict=True), reg('a'), reg('b')]),
    ('carried-nothing', 'The same, and nothing comes back at all',
     [{'do': 'carried', 'since': 1790, 'agents': ['a']}, fire(1), advance(120), fire(2)]),
    ('carried-late', 'A recovery taken up near its half hour ends at its half hour',
     [{'do': 'carried', 'since': 1770, 'agents': ['a']}, reg('a'), fire(1, verdict=False), advance(60),
      fire(2, verdict=False), {'do': 'carried', 'since': 5, 'agents': []}]),
]

# What a selector printed, how it ended, and the models that were asked for.
SELECTORS = [
    (0, '{"available": true}', ['m1', 'm2']),
    (0, '{"available": true, "models": ["m1", "m2"]}', ['m1', 'm2']),
    (0, '{"available": true, "models": ["m2", "m1", "m9"]}', ['m1', 'm2']),
    (0, '{"available": true, "models": ["m1"]}', ['m1', 'm2']),
    (0, '{"available": true, "models": ["m9"]}', ['m1', 'm2']),
    (0, '{"available": true, "models": []}', ['m1', 'm2']),
    (0, '{"available": true, "models": []}', []),
    (0, '{"available": true, "models": "m1"}', ['m1']),
    (0, '{"available": true, "models": ["m1", 7]}', ['m1']),
    (0, '{"available": true, "models": null}', ['m1']),
    (0, '{"available": true, "profile": "ai2", "models": ["m1"]}', ['m1']),
    (0, '{"available": false}', ['m1']),
    (0, '{"available": false, "error": ""}', ['m1']),
    (0, '{"available": false, "error": null}', ['m1']),
    (0, '{"available": false, "error": "the usage API could not be reached"}', ['m1']),
    (0, '{"available": false, "models": ["m1"]}', ['m1']),
    (0, '{"available": "true"}', ['m1']),
    (0, '{"available": 1}', ['m1']),
    (0, '{"available": null}', ['m1']),
    (0, '{}', ['m1']),
    (0, '[true]', ['m1']),
    (0, 'true', ['m1']),
    (0, '', ['m1']),
    (0, 'not json', ['m1']),
    (0, '  {"available": true}\n', ['m1']),
    (0, 'a line before\n{"available": true}', ['m1']),
    (1, '{"available": true}', ['m1']),
    (1, '{"available": false}', ['m1']),
    (1, '{"available": false, "error": "no saved profile holds the live login"}', ['m1']),
    (3, '', ['m1']),
]


class Handle:
    """The process of a turn, as the coordinator stops it."""
    process_group = None
    drain_thread = None

    def __init__(self, stopped):
        self.stopped = stopped

    def terminate(self):
        self.stopped()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def played(failover, policy, situation):
    name, what, steps = situation
    clock = [1000.0]
    effects, timers, turns = [], {}, {}
    numbered = itertools.count(1)
    asking = []

    def schedule(delay, callback):
        timer = next(numbered)
        timers[timer] = callback
        effects.append({'timer': timer, 'for': 'recovery' if callback == pool.recover else 'held', 'after': delay})

    def switch(command, models):
        step = asking[-1]
        effects.append({'ask': list(models), 'command': list(command)})
        # The coordinator takes whatever its selector raises for "it could not be run": a
        # situation that did not expect to be asked is told so after the step, not in here.
        again, step['asked'] = bool(step.get('asked')), True
        if 'verdict' not in step or again:
            step['unexpected'] = True
            raise RuntimeError('the situation did not say what the selector answers here')
        for inner in step.get('during', ()):
            # What happens while the selector runs: the step, then what the coordinator did at it.
            marked = {'meanwhile': shown(inner)}
            effects.append(marked)
            marked['answered'] = apply(inner)
        verdict = step['verdict']
        if verdict == 'raise':
            raise RuntimeError('the selector could not be run')
        return frozenset(verdict) if isinstance(verdict, list) else verdict

    pool = failover.AccountFailover(threading.RLock(), switch=switch, schedule=schedule, now=lambda: clock[0])

    def shown(step):
        """A step as it is written down: without the generator's own marks."""
        return {key: [shown(inner) for inner in value] if key == 'during' else value
                for key, value in step.items() if key not in ('asked', 'unexpected')}

    def apply(step):
        do = step['do']
        if do == 'register':
            agent, turn, state = step['agent'], {'owned': True}, {}

            def told(what):
                def tell():
                    effect = {what: agent}
                    if what == 'resume' and state.get('relapsed'):
                        effect['relapsed'] = True
                    effects.append(effect)
                return tell
            attempt = failover.Attempt(
                agent_id=agent, trace_id=step['trace'], model=step['model'], state=state,
                owned=lambda: turn['owned'], pause=told('pause'), resume=told('resume'),
                fail=told('end') if step.get('fail', True) else None,
                hold=told('hold') if step.get('hold', True) else None)
            turns[agent] = turn
            waits = pool.register(attempt)
            if not waits:
                # It runs: the dispatcher starts its process and says so.
                attempt.handle = Handle(told('stop'))
                attempt.spawned.set()
            return waits
        if do == 'request':
            return pool.request(step['agent'], step['trace'], tuple(step.get('command', COMMAND)),
                                plan=step.get('plan', False))
        if do == 'fire':
            asking.append(step)
            try:
                timers.pop(step['timer'])()
            finally:
                asking.pop()
            assert not step.get('unexpected'), 'the selector was asked, and the situation does not say what it answers'
            assert ('verdict' in step) == bool(step.get('asked')), 'the selector was not asked'
            return None
        if do == 'advance':
            clock[0] += step['seconds']
            return None
        if do == 'disown':
            turns[step['agent']]['owned'] = False
            return None
        if do == 'discard':
            pool.discard(step['agent'], step['trace'])
            return None
        if do == 'served':
            return pool.served_now(lambda model: model in step['models'])
        if do == 'parked':
            return pool.parked(step['agent'], step['trace'])
        if do == 'carried':
            return pool.resume_recovery(tuple(COMMAND), int((clock[0] - step['since']) * 1000), clock[0],
                                        step['agents'])
        raise AssertionError(step)

    def stood():
        status = pool.status()
        assert status['checking'] is False, name
        return {
            'recovering': status['recovering'],
            'waiting': status['waiting'],
            'relapses': pool.relapses,
            'next_check_in': round(max(0.0, pool.next_check - clock[0]), 3),
            'released': sorted([agent, trace] for agent, trace in pool.park_expired),
            'trials': sorted([agent, trace, model] for (agent, trace), (model, _at) in pool.trials.items()),
            'turns': sorted([attempt.agent_id, attempt.trace_id, attempt.model,
                             bool(attempt.state.get('account_recovery')), attempt.plan, attempt.unserved_checks]
                            for attempt in pool.attempts.values() if attempt.owned()),
            'timers': sorted(timers),
        }

    done = []
    for step in steps:
        effects.clear()
        try:
            answered = apply(step)
        except (AssertionError, KeyError) as wrong:
            # A situation that does not go as it was written: what happened up to here.
            print(f'{name}: {type(wrong).__name__}: {str(wrong)[:160]}', file=sys.stderr)
            for n, was in list(enumerate(done))[-int(TRACE):]:
                print(f"  {n:2} at {was['at']:7.1f} {json.dumps(was['step'])} -> {json.dumps(was['answered'])}\n"
                      f"       did {json.dumps(was['did'])}\n       stood {json.dumps(was['stood'])}", file=sys.stderr)
            print(f'  now {json.dumps(shown(step))}: did {json.dumps(effects)}; timers {sorted(timers)}', file=sys.stderr)
            return None
        done.append({'step': shown(step), 'answered': answered, 'did': list(effects), 'stood': stood(),
                     'at': clock[0] - 1000.0})
    assert not asking
    return {'name': name, 'what': what, 'steps': done}


def decided(policy):
    """The plan by itself, over every kind of answer and of work."""
    verdicts = {'not checked': policy.NOT_CHECKED, 'failed': policy.FAILED, 'yes': True, 'no': False, 'nothing': None}
    works = [[], [('m1', True)], [('m1', False)], [('m1', True), ('m2', True)], [('m2', True), ('m1', False)],
             [('m1', True), ('m1', True)]]
    settings = policy.FailoverSettings(recheck_seconds=60.0, max_park_seconds=1800.0)
    out = []
    for work in works:
        pending = [policy.PendingWork(model=model, stopped=stopped) for model, stopped in work]
        for said, verdict in verdicts.items():
            for since in (0.0, 1799.5, 1800.0, 4000.0):
                for due_in in (-5.0, 0.0, 12.5):
                    for checked in ((), ('m1',), ('m1', 'm2')):
                        now = 10_000.0
                        plan = policy.FailoverPlan(parked_since=now - since, next_check=now + due_in,
                                                   checked_models=checked)
                        decision = plan.decide(pending, verdict, now, settings)
                        told = {'kind': type(decision).__name__.lower()}
                        for field in ('reason', 'delay', 'models'):
                            if hasattr(decision, field):
                                value = getattr(decision, field)
                                told[field] = list(value) if isinstance(value, tuple) else value
                        out.append({'work': [list(item) for item in work], 'verdict': said, 'parked_for': since,
                                    'due_in': due_in, 'checked': list(checked), 'decision': told})
    return out


def read(failover):
    """What the coordinator makes of what a selector printed: the selector is this interpreter,
    which says what it was given on its own standard input and prints the text it is told to."""
    program = ('import json, sys\n'
               'asked = sys.stdin.read()\n'
               'open(sys.argv[3], "w").write(asked)\n'
               'sys.stdout.write(sys.argv[1])\n'
               'sys.exit(int(sys.argv[2]))\n')
    out = []
    with tempfile.TemporaryDirectory(prefix='clarp-account-pool-selector-', dir='/var/tmp') as scratch:
        given = pathlib.Path(scratch) / 'given'
        for code, printed, models in SELECTORS:
            verdict = failover.switch_account((sys.executable, '-I', '-c', program, printed, str(code), str(given)),
                                              list(models))
            told = sorted(verdict) if isinstance(verdict, frozenset) else verdict
            out.append({'exit': code, 'printed': printed, 'models': models, 'given': given.read_text(),
                        'verdict': {'serves': told} if isinstance(verdict, frozenset) else told})
    return out


def run() -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-account-pool-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import account_failover as failover
    from lib.policies import failover as policy
    return {
        'rules': {'recheck_seconds': failover.RECHECK_SECONDS, 'max_park_seconds': failover.MAX_PARK_SECONDS,
                  'quiet_seconds': failover.QUIET_SECONDS, 'max_relapses': failover.MAX_RELAPSES,
                  'plan_checks': failover.PLAN_CHECKS, 'carried_wait_seconds': failover.CARRIED_WORK_WAIT_SECONDS,
                  'carried_poll_seconds': failover.CARRIED_WORK_POLL_SECONDS},
        'command': COMMAND,
        'situations': [played(failover, policy, situation) for situation in SITUATIONS
                       if not ONLY or situation[0] in ONLY],
        'decisions': decided(policy),
        'selectors': read(failover),
    }


def main() -> int:
    vectors = run()
    wrong = sum(1 for situation in vectors['situations'] if situation is None)
    if wrong or ONLY:
        print(f'{wrong} situation(s) do not go as written; nothing is written down' if wrong else 'as written')
        for situation in vectors['situations'] if ONLY and not wrong else ():
            for n, was in enumerate(situation['steps']):
                print(f"  {n:2} at {was['at']:7.1f} {json.dumps(was['step'])} -> {json.dumps(was['answered'])}\n"
                      f"       did {json.dumps(was['did'])}\n       stood {json.dumps(was['stood'])}")
        return 1 if wrong else 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    steps = sum(len(situation['steps']) for situation in vectors['situations'])
    did = sum(len(step['did']) for situation in vectors['situations'] for step in situation['steps'])
    print(f"{len(vectors['situations'])} situations, {steps} steps, {did} things done, "
          f"{len(vectors['decisions'])} decisions, {len(vectors['selectors'])} selectors -> {OUT.relative_to(REPO)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
