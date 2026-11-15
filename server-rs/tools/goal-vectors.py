#!/usr/bin/env python3
"""Writes the vectors the Rust goal tool is tested against.

The Python Host's goal tool (`scripts/agent_tasks.py`, the program behind `clarp-goal`) is the
reference. This runs sequences of its commands in a database of its own, with a clock and an
id source that are fixed, and writes each command with what it printed and how it exited to
`crates/clarp-host/tests/fixtures/goal-vectors.json`. The Rust tests replay the commands
against the Rust Host's tool with the same clock and ids and must get the same.

    python3 server-rs/tools/goal-vectors.py            # write the vectors
    python3 server-rs/tools/goal-vectors.py --show N   # print scenario N's steps instead

Nothing of a live Host is touched: the library is pointed at a directory under /var/tmp that
is made new on every run. Every text is synthetic.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-host' / 'tests' / 'fixtures' / 'goal-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-goal-vectors')
T0 = 1_794_700_000_000
STEP_MS = 1_000


def isolate() -> None:
    """Points the library at a place of its own before it is imported."""
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    for name in ('CLAUDE_PWA_SESSION', 'CLARP_PROVIDER_TURN', 'CLARP_BACKGROUND_ORIGIN', 'CLARP_HEARTBEATS_DISABLED',
                 'CARTESIA_API_KEY', 'ELEVEN_API_KEY', 'OPENAI_API_KEY', 'CLARP_DELEGATION_TOKEN_FILE'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(REPO / 'server'))
    sys.path.insert(0, str(REPO / 'scripts'))


class World:
    """One scenario's clock, ids and agent."""

    def __init__(self, number: int):
        import secrets
        from lib import agents, clock, db
        self.now = T0 + number * 10_000_000
        self.ids = 0
        for module in (clock, db):
            module.now_ms = lambda: self.now
        secrets.token_hex = self.token_hex
        # A database of its own for every scenario: none sees what another left.
        db.reset_for_tests(SCRATCH / f'scenario-{number}.sqlite')
        # The agent's own id is made before the ids are counted: it is given to the replay.
        self.session = f'vera{number}'
        self.agent_id = f'{number:04x}aaaabbbbcccc'
        real = agents.create_agent(persona='Vera', voice_id='', cwd='/var/tmp', session=self.session)
        db.conn().execute('UPDATE agents SET agent_id=? WHERE agent_id=?', (self.agent_id, real))
        for table in ('runtimes', 'state_log'):
            with contextlib.suppress(Exception):
                db.conn().execute(f'UPDATE {table} SET agent_id=? WHERE agent_id=?', (self.agent_id, real))
        db.conn().commit()
        self.plan, self.revision = '', 0
        self.steps: list[dict] = []

    def token_hex(self, nbytes: int = 4) -> str:
        self.ids += 1
        return format(self.ids, f'0{nbytes * 2}x')

    def run(self, *argv: str, note: str = '') -> dict:
        """One command, with {plan} and {rev} filled in from what the last one printed."""
        import agent_tasks
        self.now += STEP_MS
        filled = [a.replace('{plan}', self.plan).replace('{rev}', str(self.revision)).replace('{session}', self.session)
                  for a in argv]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = agent_tasks.main(['agent_tasks.py', *filled])
            except SystemExit as exc:
                code = int(exc.code or 0)
            except Exception as exc:  # what the real tool would die of, with a traceback
                code, _ = 1, print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        text = out.getvalue().strip()
        try:
            printed = json.loads(text) if text else None
        except json.JSONDecodeError:
            printed = text
        if isinstance(printed, dict) and 'plan_id' in printed and 'items' in printed:
            self.plan, self.revision = printed['plan_id'], printed['revision']
        step = {'argv': filled, 'at': self.now, 'exit': code, 'stdout': printed, 'stderr': err.getvalue().strip()}
        if note:
            step['note'] = note
        self.steps.append(step)
        return step


def j(value) -> str:
    return json.dumps(value)


GOAL = {'outcome': 'The thing is shipped', 'criteria': ['It runs', 'It is documented'], 'limits': 'No publication'}
ITEMS = [{'id': 'probe', 'title': 'Probe the behaviour'},
         {'id': 'build', 'title': 'Build it', 'detail': 'the whole of it', 'subtasks': [{'id': 'part', 'title': 'A part'}]}]


def a_goal_from_start_to_completion(w: World) -> None:
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('get', '{plan}')
    w.run('list', '{session}')
    w.run('show', '{session}')
    w.run('step', '{plan}', '{rev}', 'probe', 'in_progress')
    w.run('step', '{plan}', '{rev}', 'probe', 'completed', 'It behaves')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Probe done', 'next_work': 'Build it'}))
    w.run('step', '{plan}', '{rev}', 'build', 'in_progress')
    w.run('step', '{plan}', '{rev}', 'part', 'completed')
    w.run('step', '{plan}', '{rev}', 'build', 'completed')
    w.run('complete', '{plan}', '{rev}', note='refused: the criteria have no evidence')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Built', 'next_work': 'Close',
                                             'evidence': {'criterion-1': 'ran it', 'criterion-2': 'README written'}}))
    w.run('complete', '{plan}', '{rev}')
    w.run('get', '{plan}')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'More', 'next_work': 'None'}), note='refused: closed')
    w.run('list', '{session}')
    w.run('show', '{session}')


def what_the_tool_refuses(w: World) -> None:
    w.run('create', 'nobody', 'ship', 'Ship', j(ITEMS), j(GOAL), note='no such session')
    w.run('create', '{session}', 'ship', '  ', j(ITEMS), j(GOAL), note='no title')
    w.run('create', '{session}', 'ship', 'Ship', j(ITEMS), j({**GOAL, 'criteria': []}), note='no criteria')
    w.run('create', '{session}', 'ship', 'Ship', j(ITEMS), j({'outcome': 'x', 'criteria': ['a']}), note='no limits')
    w.run('create', '{session}', 'ship', 'Ship', j(ITEMS), j({**GOAL, 'enroll': True}), note='no native conversation to enroll')
    w.run('create', '{session}', 'ship', 'Ship', '{"not": "a list"}', j(GOAL))
    w.run('get', 'no-such-plan')
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('step', '{plan}', '99', 'probe', 'in_progress', note='a stale revision')
    w.run('step', '{plan}', '{rev}', 'nothing', 'in_progress', note='no such step')
    w.run('step', '{plan}', '{rev}', 'probe', 'dancing', note='no such status')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x'}), note='no next work')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y', 'evidence': {'criterion-9': 'z'}}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y', 'continuation': {'kind': 'someday'}}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y', 'continuation': {'kind': 'dependency', 'key': 'k'}}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y', 'continuation': {'kind': 'blocked'}}))
    w.run('pause', '{plan}', '{rev}', j({}), note='a control needs a reason')
    w.run('checkpoint', '{plan}', '{rev}', 'not json')
    w.run('frobnicate', '{plan}')
    w.run()


def a_goal_is_paused_resumed_blocked_and_cancelled(w: World) -> None:
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('step', '{plan}', '{rev}', 'probe', 'in_progress')
    w.run('pause', '{plan}', '{rev}', j({'reason': 'User paused this goal'}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Noted while paused', 'next_work': 'Wait'}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y', 'continuation': {'kind': 'timer'}}),
          note='refused: a paused goal takes no continuation')
    w.run('step', '{plan}', '{rev}', 'build', 'in_progress', note='steps of a paused goal')
    w.run('resume', '{plan}', '{rev}', j({'reason': 'User explicitly resumed this goal'}))
    w.run('get', '{plan}')
    w.run('block', '{plan}', '{rev}', j({'reason': 'Needs a key only the user has'}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y'}), note='refused: resume first')
    w.run('resume', '{plan}', '{rev}', j({'reason': 'The key arrived'}))
    w.run('cancel', '{plan}', '{rev}', j({'reason': 'User cancelled the outcome'}))
    w.run('get', '{plan}')
    w.run('resume', '{plan}', '{rev}', j({'reason': 'again'}), note='refused: closed')


def a_plan_changes_and_keeps_its_notes(w: World) -> None:
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('replan', '{plan}', '{rev}', j({'reason': 'An existing path makes the build unnecessary',
                                         'steps': [{'id': 'reuse', 'title': 'Validate and reuse the existing path'}]}))
    w.run('add_step', '{plan}', '{rev}', j({'id': 'tell', 'title': 'Tell the owner'}), note='refused: no reason')
    w.run('add_step', '{plan}', '{rev}', j({'id': 'tell', 'title': 'Tell the owner', 'reason': 'The owner must know'}))
    w.run('document', '{plan}', '{rev}', j({'name': 'working-notes.md', 'format': 'markdown',
                                           'content': '# Notes\nFirst.', 'document_revision': 0, 'reason': 'Initial findings'}))
    w.run('read', '{plan}', 'working-notes.md')
    w.run('get', '{plan}')
    w.run('document', '{plan}', '{rev}', j({'name': 'working-notes.md', 'format': 'markdown',
                                           'content': '# Notes\nSecond.', 'document_revision': 0, 'reason': 'Stale write'}),
          note='refused: not the current revision')
    w.run('document', '{plan}', '{rev}', j({'name': 'working-notes.md', 'format': 'markdown',
                                           'content': '# Notes\nSecond.', 'document_revision': 1, 'reason': 'More'}))
    w.run('read', '{plan}', 'working-notes.md', '1')
    w.run('read', '{plan}', 'working-notes.md')
    w.run('read', '{plan}', 'nothing.md')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Notes kept', 'next_work': 'Go on', 'documents': [
        {'name': 'facts.json', 'format': 'json', 'content': {'a': [1, 2]}, 'document_revision': 0, 'reason': 'Facts'}]}))
    w.run('get', '{plan}')
    w.run('ledger', '{plan}')
    w.run('subgoal', '{plan}', 'add', j({'subgoal_id': 'phone', 'title': 'Works on the phone', 'intent': 'The original acceptance',
                                        'criteria': ['Real events']}))
    w.run('subgoal', '{plan}', 'update', j({'subgoal_id': 'phone', 'expected_revision': 1,
                                           'fields': {'current_action': 'Waiting for the build'}, 'reason': 'Install not verified'}))
    w.run('get', '{plan}')


def a_goal_waits_for_something(w: World) -> None:
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    due = w.now + 3_600_000
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Build launched', 'next_work': 'Inspect its result',
                                             'continuation': {'kind': 'dependency', 'key': 'build-1', 'reason': 'Waiting for build', 'due_at': due}}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Still waiting', 'next_work': 'Inspect its result'}),
          note='no continuation: the wake stands')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'x', 'next_work': 'y', 'continuation': {'kind': 'timer', 'due_at': due}}),
          note='refused: a timer would replace the dependency')
    w.run('dependency', '{plan}', '{rev}', j({'key': 'other', 'outcome': 'succeeded', 'evidence': 'x'}), note='not the awaited key')
    w.run('dependency', '{plan}', '{rev}', j({'key': 'build-1', 'outcome': 'succeeded', 'evidence': 'log at /var/tmp/x'}))
    w.run('get', '{plan}')
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Result read', 'next_work': 'Go on',
                                             'continuation': {'kind': 'timer', 'due_at': due}}))
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Stuck', 'next_work': 'Ask',
                                             'continuation': {'kind': 'blocked', 'reason': 'Needs the owner'}}))
    w.run('get', '{plan}')


SCENARIOS = [a_goal_from_start_to_completion, what_the_tool_refuses, a_goal_is_paused_resumed_blocked_and_cancelled,
             a_plan_changes_and_keeps_its_notes, a_goal_waits_for_something]


def main(argv: list[str]) -> int:
    isolate()
    scenarios = []
    for number, scenario in enumerate(SCENARIOS, start=1):
        world = World(number)
        scenario(world)
        scenarios.append({'name': scenario.__name__, 'session': world.session, 'agent_id': world.agent_id,
                          'steps': world.steps})
    if len(argv) == 3 and argv[1] == '--show':
        chosen = scenarios[int(argv[2]) - 1]
        for step in chosen['steps']:
            printed = json.dumps(step['stdout'])
            print(f"$ {' '.join(a if len(a) < 60 else a[:57] + '...' for a in step['argv'])}\n  exit {step['exit']}"
                  f"{'  # ' + step['note'] if step.get('note') else ''}\n  stderr: {step['stderr'][-300:]}\n  stdout: {printed[:int(os.environ.get('WIDTH', '400'))]}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/goal-vectors.py from scripts/agent_tasks.py. Do not edit by hand.',
               't0': T0, 'step_ms': STEP_MS, 'scenarios': scenarios}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    steps = sum(len(s['steps']) for s in scenarios)
    print(f'{OUT.relative_to(REPO)}: {len(scenarios)} scenarios, {steps} steps, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
