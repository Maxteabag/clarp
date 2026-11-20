#!/usr/bin/env python3
"""Writes the vectors the Rust goal tool is tested against.

The Python Host's goal tool (`scripts/agent_tasks.py`, the program behind `clarp-goal`) is the
reference. This runs sequences of its commands in a database of its own, with a clock and an
id source that are fixed, and writes each command with what it printed and how it exited to
`crates/clarp-host/tests/fixtures/goal-vectors.json`. With every goal it printed goes the
message its owner would be woken with at that moment (`task_goal_recovery._prompt`). The Rust tests replay the commands
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
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-host' / 'tests' / 'fixtures' / 'goal-vectors.json'
T0 = 1_794_700_000_000
STEP_MS = 1_000


def isolate(root) -> None:
    """Points the library at a place of its own before it is imported."""
    os.environ.update({
        'CLAUDE_PWA_DB': str(root / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(root / 'logs'),
        'CLARP_CONFIG_DIR': str(root / 'config'), 'CLARP_SHARE_DIR': str(root / 'share'),
        'CLARP_CACHE_DIR': str(root / 'cache'), 'CLAUDE_PWA_CONFIG': str(root / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    for name in ('CLAUDE_PWA_SESSION', 'CLARP_PROVIDER_TURN', 'CLARP_BACKGROUND_ORIGIN', 'CLARP_HEARTBEATS_DISABLED',
                 'CARTESIA_API_KEY', 'ELEVEN_API_KEY', 'OPENAI_API_KEY', 'CLARP_DELEGATION_TOKEN_FILE'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(REPO / 'server'))
    sys.path.insert(0, str(REPO / 'scripts'))


class World:
    """One scenario's clock, ids and agent."""

    def __init__(self, number: int, root: pathlib.Path):
        import secrets
        from lib import agents, clock, db
        self.now = T0 + number * 10_000_000
        self.ids = 0
        for module in (clock, db):
            module.now_ms = lambda: self.now
        secrets.token_hex = self.token_hex
        # A database of its own for every scenario: none sees what another left.
        db.reset_for_tests(root / f'scenario-{number}.sqlite')
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
        self.native = ''
        self.steps: list[dict] = []

    def bound(self, step: dict) -> dict:
        """A step with the owner's conversation with its tool as it was bound then, where it was."""
        if self.native:
            step['native'] = self.native
        return step

    def bind(self, native: str) -> None:
        """The owner's conversation with its tool is this one from now on."""
        from lib import agents
        agents.bind_backend_session(self.agent_id, native)
        self.native = native

    def carry(self, to: str, reason: str, note: str = '', frm: str | None = None) -> dict:
        """The Host continued the owner's conversation with its tool in a new one
        (`task_plans.carry_native_session`: a tool that lets go of old conversations, and a Host
        that opens the new one with its own record of the old). Written down as `[carry, from, to,
        reason]` under `by: host`, with the plans that moved; the owner's conversation is the new
        one from then on."""
        from lib import task_plans
        self.now += STEP_MS
        was = self.native if frm is None else frm
        moved = task_plans.carry_native_session(self.agent_id, was, to, reason)
        step = {'by': 'host', 'argv': ['carry', was, to, reason], 'at': self.now, 'exit': 0, 'stdout': moved, 'stderr': ''}
        if frm is None:
            self.bind(to)
        if note:
            step['note'] = note
        self.steps.append(self.bound(step))
        return step

    def token_hex(self, nbytes: int = 4) -> str:
        self.ids += 1
        return format(self.ids, f'0{nbytes * 2}x')

    def wake_prompt(self, plan_id: str) -> str:
        """The message the goal's owner would be woken with now, as the Python Host words it."""
        from lib import db, task_goal_recovery
        row = db.conn().execute('SELECT * FROM task_plans WHERE plan_id=?', (plan_id,)).fetchone()
        return task_goal_recovery._prompt(row, json.loads(row['goal_json']))

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
        step = {'argv': filled, 'at': self.now, 'exit': code, 'stdout': printed, 'stderr': err.getvalue().strip()}
        if isinstance(printed, dict) and 'plan_id' in printed and 'items' in printed:
            self.plan, self.revision = printed['plan_id'], printed['revision']
            if printed.get('goal'):
                step['wake_prompt'] = self.wake_prompt(printed['plan_id'])
        if note:
            step['note'] = note
        self.steps.append(self.bound(step))
        return step


    def user(self, action: str, data: dict, note: str = '') -> dict:
        """What an app does to the goal for its user (`POST /task-plan/action`): the same
        rules, with the user as the one who acts. Written down as `[action, plan, revision,
        data]` under `by: user`, with the plan it answers or the words it refuses with."""
        from lib import goal_ledger, task_goal_state
        self.now += STEP_MS
        step = {'by': 'user', 'argv': [action, self.plan, str(self.revision), json.dumps(data)], 'at': self.now}
        try:
            with goal_ledger.acting_as('user'):
                result = task_goal_state.mutate(self.plan, revision=self.revision, action=action, data=data)
        except (ValueError, TypeError) as exc:  # what the route answers 409 with
            step.update(exit=1, stdout=None, stderr=str(exc))
        else:
            step.update(exit=0, stdout=result, stderr='')
            self.plan, self.revision = result['plan_id'], result['revision']
            if result.get('goal'):
                step['wake_prompt'] = self.wake_prompt(result['plan_id'])
        if note:
            step['note'] = note
        self.steps.append(self.bound(step))
        return step


    def keeper(self, steps: dict, progress: str, next_work: str = '', note: str = '', **more) -> dict:
        """What the Goal keeper writes beside the owner's own record (`task_plans.updater_apply`):
        the statuses of steps and its own checkpoint. Written down as `[updater, plan, what it
        asked]` under `by: keeper`, with what the write answered or the words it was refused
        with, and the plan as it is read afterwards."""
        from lib import task_plans
        self.now += STEP_MS
        asked = {'steps': {item.replace('{plan}', self.plan): wanted for item, wanted in steps.items()},
                 'progress': progress, 'next_work': next_work,
                 'seen_revision': more.pop('seen_revision', self.revision), 'model': more.pop('model', 'keeper-model'),
                 'source_refs': more.pop('source_refs', ['message:m-1', 'job:j-1']),
                 'through_ms': more.pop('through_ms', self.now - 500), 'through_event': more.pop('through_event', 3),
                 'janitor_agent_id': 'keeper-agent', **more}
        step = {'by': 'keeper', 'argv': ['updater', self.plan, json.dumps(asked)], 'at': self.now}
        try:
            result = task_plans.updater_apply(self.plan, **asked)
        except (ValueError, TypeError) as exc:  # the store's own refusals, by their kind
            step.update(exit=1, stdout=None, stderr=str(exc), refused=type(exc).__name__)
        else:
            step.update(exit=0, stdout=result, stderr='')
        step['plan'] = task_plans.get(self.plan)
        if note:
            step['note'] = note
        self.steps.append(self.bound(step))
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


def the_user_acts_on_a_goal(w: World) -> None:
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('act', '{plan}', '{rev}', 'waive', j({'criterion_id': 'criterion-2', 'reason': 'Not needed'}),
          note='refused: only the user sets a criterion aside')
    w.user('waive', {'criterion_id': 'criterion-9', 'reason': 'x'}, note='no such criterion')
    w.user('waive', {'criterion_id': 'criterion-2'}, note='no reason')
    w.user('waive', {'criterion_id': 'criterion-2', 'reason': 'The document is not needed after all'})
    w.run('get', '{plan}')
    w.user('unwaive', {'criterion_id': 'criterion-1'}, note='not waived')
    w.user('unwaive', {'criterion_id': 'criterion-2'})
    w.run('act', '{plan}', '{rev}', 'unwaive', j({'criterion_id': 'criterion-2'}), note='refused: only the user')
    w.user('waive', {'criterion_id': 'criterion-2', 'reason': 'Set aside again'})
    w.user('pause', {}, note='a control needs a reason from the user too')
    w.user('pause', {'reason': 'User paused this goal'})
    w.user('resume', {'reason': 'User explicitly resumed this goal'})
    w.user('checkpoint', {'progress': 'Seen by the user', 'next_work': 'Finish', 'evidence': {'criterion-1': 'ran it'}})
    w.user('frobnicate', {}, note='no such action')
    w.user('add_step', {'id': 'tell', 'title': 'Tell the owner', 'reason': 'The user wants to hear of it', 'required': False})
    w.run('step', '{plan}', '{rev}', 'probe', 'completed', 'It behaves')
    w.run('step', '{plan}', '{rev}', 'part', 'completed')
    w.run('step', '{plan}', '{rev}', 'build', 'completed')
    w.run('complete', '{plan}', '{rev}', note='one criterion has evidence, the other was set aside')
    w.run('get', '{plan}')
    w.user('cancel', {'reason': 'Too late'}, note='refused: closed')


def the_keeper_writes_beside_the_owner(w: World) -> None:
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('step', '{plan}', '{rev}', 'probe', 'in_progress')
    w.keeper({'{plan}:probe': {'status': 'completed', 'note': 'Evidence: PR #12 merged'},
              '{plan}:build': {'status': 'in_progress', 'note': 'n' * 600},
              '{plan}:part': {'status': 'blocked'},
              '{plan}:no-such-step': {'status': 'completed'},
              'probe': {'status': 'completed', 'note': 'a short id is no id here'}},
             '  Probe merged; the build is under way.  ', '  Finish the build.  ')
    w.run('get', '{plan}')
    w.keeper({'{plan}:probe': {'status': 'in_progress', 'note': 'a step the keeper closed stays closed'},
              '{plan}:build': {'status': 'in_progress'},
              '{plan}:part': {'status': 'done'},
              '{plan}:ship': None},
             'Nothing changed, said again.', note='no step changes: what is closed stays closed, the same status is no change')
    w.keeper({}, '', 'x', note='refused: a checkpoint needs its progress')
    w.keeper({}, '   ', note='refused: space is no progress')
    w.keeper({'{plan}:part': {'status': 'pending'}}, 'P' * 2100, 'N' * 1100, source_refs=[f'message:m-{n}' for n in range(50)],
             through_ms=0, through_event=0, model='', note='long texts are cut, and no more than forty sources kept')
    w.run('step', '{plan}', '{rev}', 'part', 'in_progress')
    w.keeper({'{plan}:part': {'status': 'completed', 'note': 'Evidence: built'}}, 'Stale.', seen_revision=1,
             note='refused: the owner changed the plan since the keeper read it')
    w.keeper({'{plan}:part': {'status': 'completed', 'note': 'Evidence: built'}}, 'The part is built.',
             note='a step that ran is closed: the time it ran is counted')
    w.user('pause', {'reason': 'Waiting for a review'})
    w.keeper({'{plan}:build': {'status': 'pending'}}, 'Paused by the user.',
             note='a paused goal: the plan keeps its state; a step that ran has its time counted')
    w.keeper({'{plan}:build': {'status': 'in_progress'}}, 'Still paused.',
             note='a step that begins while the goal is paused has no start')
    w.run('get', '{plan}')
    w.user('resume', {'reason': 'Reviewed'})
    for n in range(6):
        w.keeper({'{plan}:build': {'status': 'pending' if n % 2 == 0 else 'in_progress'}}, f'Round {n}.')
    w.run('get', '{plan}')
    w.user('cancel', {'reason': 'Not needed'})
    w.keeper({'{plan}:ship': {'status': 'completed'}}, 'Too late.', note='refused: the goal is closed')
    w.run('create', '{session}', 'list', 'A plan without a goal', j(ITEMS[:1]))
    w.keeper({}, 'No goal here.', note='refused: a plan that has no goal')


CARRIED = ('Antigravity pruned the conversation; Clarp continued it in a new one that opens with its '
           'record of the old one')


def the_host_carries_goals_to_a_new_conversation(w: World) -> None:
    w.bind('native-1')
    w.run('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    w.run('step', '{plan}', '{rev}', 'probe', 'in_progress')
    due = w.now + 3_600_000
    w.run('checkpoint', '{plan}', '{rev}', j({'progress': 'Build launched', 'next_work': 'Inspect its result',
                                             'continuation': {'kind': 'dependency', 'key': 'build-1', 'reason': 'Waiting for build',
                                                              'due_at': due}}),
          note='a goal that waits: its wake is of the plan as it stands')
    first = w.plan
    w.run('create', '{session}', 'other', 'Another goal', j(ITEMS[:1]), j(GOAL))
    w.run('cancel', '{plan}', '{rev}', j({'reason': 'Not needed'}))
    w.run('create', '{session}', 'list', 'A plan without a goal', j(ITEMS[:1]))
    w.carry('native-9', 'from a conversation no goal is bound to', frm='native-0', note='nothing moves')
    w.run('get', first)
    w.carry('native-2', CARRIED, note='the open goal follows; the cancelled one and the plan without a goal stay')
    w.run('get', first, note='bound to the new conversation, one revision on, and its wake still of the plan as it stands')
    w.run('list', '{session}')
    w.run('step', first, '{rev}', 'probe', 'completed', 'It behaves', note="the owner's next command, with the plan as it now reads")
    w.run('step', first, '{rev}', 'build', 'in_progress')
    w.carry('native-3', 'carried once more')
    w.run('get', first)
    w.carry('native-3', 'to the conversation it is in', note='carried from where it is to where it is: written down all the same')
    w.run('get', first)


SCENARIOS = [a_goal_from_start_to_completion, what_the_tool_refuses, a_goal_is_paused_resumed_blocked_and_cancelled,
             a_plan_changes_and_keeps_its_notes, a_goal_waits_for_something, the_user_acts_on_a_goal,
             the_keeper_writes_beside_the_owner, the_host_carries_goals_to_a_new_conversation]


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        # The Python Host counts a plan's steps by status in the order a set gives them, which
        # is another one in every process. With the seed fixed, the vectors are the same file
        # every time they are written.
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    with tool_scratch.directory('clarp-goal-vectors-') as name:
        return generate(pathlib.Path(name), argv)


def generate(root, argv):
    isolate(root)
    scenarios = []
    for number, scenario in enumerate(SCENARIOS, start=1):
        world = World(number, root)
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
