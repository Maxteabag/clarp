#!/usr/bin/env python3
"""Writes the vectors the Rust Host's artifacts and decisions are tested against.

An artifact is something an agent made for its user: a document, a table, a plan, a question.
The Python Host's rules for them are `server/lib/artifacts.py`. This calls those functions in
sequences, in a database of its own, with a clock and an id source that are fixed, and writes
each call with what it answered or refused to
`crates/clarp-kernel/tests/fixtures/artifact-vectors.json`. The Rust tests make the same calls
with the same clock and ids and must get the same.

    python3 server-rs/tools/artifact-vectors.py            # write the vectors
    python3 server-rs/tools/artifact-vectors.py --show N   # print scenario N's calls instead

Nothing of a live Host is touched: the library is pointed at a directory under /var/tmp that
is made new on every run. Every text is synthetic.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'artifact-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-artifact-vectors')
T0 = 1_794_800_000_000
STEP_MS = 1_000
DAY_MS = 86_400_000


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


class World:
    """One scenario's clock, ids and agents."""

    def __init__(self, number: int):
        import secrets
        from lib import agents, clock, db
        self.now = T0 + number * 10 * DAY_MS
        self.ids = 0
        for module in (clock, db):
            module.now_ms = lambda: self.now
        secrets.token_hex = self.token_hex
        db.reset_for_tests(SCRATCH / f'scenario-{number}.sqlite')
        self.agents = {}
        for index, persona in enumerate(('Vera', 'Omar'), start=1):
            session = f'{persona.lower()}{number}'
            agent_id = f'{number:02x}{index:02x}aaaabbbbcccc'
            real = agents.create_agent(persona=persona, voice_id='', cwd='/var/tmp', session=session)
            db.conn().execute('UPDATE agents SET agent_id=? WHERE agent_id=?', (agent_id, real))
            for table in ('runtimes', 'state_log'):
                with contextlib.suppress(Exception):
                    db.conn().execute(f'UPDATE {table} SET agent_id=? WHERE agent_id=?', (agent_id, real))
            self.agents[session] = {'agent_id': agent_id, 'session': session, 'persona': persona}
        db.conn().commit()
        # The ids made so far are the agents' own; the scenario's count from here.
        self.ids = 0
        self.vera, self.omar = (f'vera{number}', f'omar{number}')
        self.calls: list[dict] = []
        self.kept: dict[str, object] = {}

    def token_hex(self, nbytes: int = 4) -> str:
        self.ids += 1
        return format(self.ids, f'0{nbytes * 2}x')

    def later(self, ms: int) -> None:
        """Lets time pass without a call."""
        self.now += ms

    def call(self, name: str, note: str = '', **args):
        """One call of the library, written down with what it answered or refused."""
        from lib import artifacts, task_plans
        self.now += STEP_MS
        function = {
            'create': artifacts.create, 'get': artifacts.get, 'list': artifacts.list_artifacts,
            'update': lambda artifact_id, data: artifacts.update(artifact_id, data),
            'create_decision': artifacts.create_decision, 'resolve': artifacts.resolve,
            'dismiss': artifacts.dismiss, 'withdraw': artifacts.withdraw, 'archive': artifacts.archive,
            'discard': artifacts.discard, 'pin': artifacts.pin, 'list_pinned': artifacts.list_pinned,
            'attention': artifacts.attention, 'pending_deliveries': self.pending_deliveries,
            'mark_delivered': artifacts.mark_delivered, 'delivery_pending': artifacts.delivery_pending,
            'has_blocking_decision': artifacts.has_blocking_decision,
            'has_pending_decision': artifacts.has_pending_decision,
            'cancel_for_agent': artifacts.cancel_for_agent,
            'plan_create': task_plans.create, 'plan_step': self.plan_step, 'plan_act': self.plan_act,
            'plan_finish': task_plans.finish,
        }[name]
        entry = {'call': name, 'args': args, 'at': self.now}
        ids_before = self.ids
        try:
            result = function(**args)
            if isinstance(result, tuple):
                result = list(result)
            entry['result'] = json.loads(json.dumps(result, default=dict))
        except (ValueError, PermissionError, TypeError, KeyError) as exc:
            entry['error'] = {'kind': type(exc).__name__, 'message': str(exc)}
        except Exception as exc:  # what the route answers 409 with: the database's own refusal
            entry['error'] = {'kind': type(exc).__name__, 'message': str(exc)}
        entry['ids'] = self.ids - ids_before
        if note:
            entry['note'] = note
        self.calls.append(entry)
        return entry.get('result')

    def pending_deliveries(self):
        """What waits to be told to an agent, each with the message it is told with."""
        from lib import artifacts
        return [{**delivery, 'prompt': artifacts.format_delivery_prompt(delivery)}
                for delivery in artifacts.pending_deliveries()]

    def plan_step(self, plan_id: str, item: str, status: str, revision: int, detail=None):
        from lib import task_plans
        return task_plans.update_item(task_plans.item_key(plan_id, item), status, detail, revision=revision)

    def plan_act(self, plan_id: str, revision: int, action: str, data: dict):
        from lib import task_goal_state
        return task_goal_state.mutate(plan_id, revision=revision, action=action, data=data)


def plain_kinds(w: World) -> None:
    w.call('create', session=w.vera, type='document', title='Notes', summary='What was found',
           payload={'content': '# Notes\nA finding.'})
    doc = w.call('create', session=w.vera, type='Document', title='  Spaced title  ', summary=' kept spaces ',
                 status='Active', reference_id='ref-1', payload={'content': 'x', 'extra': {'kept': [1, 2]}})
    w.call('create', session=w.vera, type='research', title='Research', payload={
        'content': 'Findings', 'sources': [{'title': 'A source', 'url': 'https://example.org/a'}],
        'source_count': 1, 'source_url': 'https://example.org/'})
    w.call('create', session=w.vera, type='data', title='Table', payload={
        'columns': ['name', 'count'], 'rows': [['a', 1], ['b', 2.5], ['c', None]], 'row_count': 3,
        'chart': {'kind': 'bar', 'value_column': 'count'}})
    w.call('create', session=w.vera, type='data', title='Chart', payload={
        'columns': ['name', 'count'], 'rows': [['a', 1], ['b', 2.5]], 'chart': {'kind': 'line', 'category_column': 'name'}})
    w.call('create', session=w.vera, type='data', title='Empty table', payload={'columns': ['only'], 'rows': []})
    w.call('create', session=w.vera, type='code_change', title='A change', payload={
        'repository': 'owner/repo', 'branch': 'main', 'commit': 'abc123', 'files_changed': 2, 'additions': 10,
        'deletions': 3, 'diff': '--- a\n+++ b\n'})
    w.call('create', session=w.vera, type='release', title='Release 1.2', payload={
        'version': '1.2.0', 'build': '45', 'environment': 'production'})
    w.call('create', session=w.vera, type='directory', title='The sources', payload={
        'root': 'workspace', 'relative_path': 'src/lib', 'path_label': 'src/lib'})
    run = w.call('create', session=w.omar, type='workflow_run', title='CI', status='active', payload={
        'provider': 'github', 'run_id': '42', 'run_url': 'https://github.com/owner/repo/actions/runs/42',
        'workflow_name': 'Tests', 'total_steps': 5, 'completed_steps': 1, 'current_step': 'build', 'progress': 0.2})
    w.call('create', session=w.omar, type='document', title='Own id', artifact_id='doc.own-id:1',
           payload={'content': 'x'})
    w.call('get', artifact_id=doc['artifact_id'])
    w.call('get', artifact_id='doc.own-id:1')
    w.call('get', artifact_id='nothing')
    w.call('list')
    w.call('list', session=w.vera, order='created')
    w.call('list', agent_id=w.agents[w.omar]['agent_id'])
    w.call('list', type='data')
    w.call('list', search='TITLE')
    w.call('list', search='found')
    w.call('list', search='100%_x')
    w.call('list', limit=2, offset=1)
    w.call('list', limit=0, offset=-5)
    w.call('list', created_from=doc['created_at'], created_to=run['created_at'])
    # Updates.
    w.call('update', artifact_id=run['artifact_id'], data={'payload_patch': {'completed_steps': 3, 'progress': 0.6}})
    w.call('update', artifact_id=run['artifact_id'], data={'status': 'completed'})
    w.call('update', artifact_id=run['artifact_id'], data={'status': 'COMPLETED', 'title': 'CI done', 'summary': 'ok'},
           note='the same status keeps when it was completed')
    w.call('update', artifact_id=run['artifact_id'], data={'status': 'active'}, note='active again: not completed')
    w.call('update', artifact_id=doc['artifact_id'], data={'payload': {'content': 'replaced'}})
    w.call('update', artifact_id=doc['artifact_id'], data={'summary': ''})
    w.call('update', artifact_id=doc['artifact_id'], data={'title': '   '}, note='an empty title keeps the old')
    w.call('update', artifact_id=doc['artifact_id'], data={})
    w.call('list', session=w.vera, limit=3)


def what_an_artifact_refuses(w: World) -> None:
    ok = {'content': 'x'}
    w.call('create', session='nobody', type='document', title='T', payload=ok)
    w.call('create', session=w.vera, type='poem', title='T', payload=ok)
    w.call('create', session=w.vera, type='document', title='T', status='sleeping', payload=ok)
    w.call('create', session=w.vera, type='document', title='   ', payload=ok)
    w.call('create', session=w.vera, type='document', title='T', artifact_id='has space', payload=ok)
    w.call('create', session=w.vera, type='document', title='T', artifact_id='x' * 181, payload=ok)
    for reserved in ('decision', 'question', 'plan', ' Plan '):
        w.call('create', session=w.vera, type=reserved, title='T', payload=ok)
    w.call('create', session=w.vera, type='document', title='T', payload=None)
    w.call('create', session=w.vera, type='document', title='T', payload={'content': ''})
    w.call('create', session=w.vera, type='document', title='T', payload=['not', 'an', 'object'])
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 12})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'duration_ms': -1})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'size_bytes': True})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'progress': 2})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'progress': 'half'})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'all_day': 'yes'})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'y' * 140_000})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'source_url': 'http://plain'})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'source_url': '/relative'})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'thumbnail_url': '/media/t'})
    w.call('create', session=w.vera, type='document', title='T', payload={'content': 'x', 'thumbnail_url': '//evil'})
    w.call('create', session=w.vera, type='document', title='T',
           payload={'content': 'x', 'source_url': 'https://user:pw@example.org/'})
    w.call('create', session=w.vera, type='research', title='T', payload={})
    w.call('create', session=w.vera, type='research', title='T', payload={'content': 'x', 'sources': [{'title': 'no url'}]})
    w.call('create', session=w.vera, type='research', title='T',
           payload={'content': 'x', 'sources': [{'title': 't', 'url': 'ftp://example.org'}]})
    w.call('create', session=w.vera, type='data', title='T', payload={'columns': [], 'rows': []})
    w.call('create', session=w.vera, type='data', title='T', payload={'columns': ['a'], 'rows': [['x', 'y']]})
    w.call('create', session=w.vera, type='data', title='T', payload={'columns': ['a', ''], 'rows': []})
    w.call('create', session=w.vera, type='data', title='T', payload={'columns': ['a'], 'rows': [[{'no': 'cell'}]]})
    w.call('create', session=w.vera, type='data', title='T', payload={'columns': ['a'], 'rows': 'none'})
    w.call('create', session=w.vera, type='data', title='T',
           payload={'columns': ['a', 'b'], 'rows': [['x', 'y']], 'chart': {'kind': 'bar'}})
    w.call('create', session=w.vera, type='data', title='T',
           payload={'columns': ['a', 'b'], 'rows': [['x', 1]], 'chart': {'kind': 'pie'}})
    w.call('create', session=w.vera, type='data', title='T',
           payload={'columns': ['a', 'b'], 'rows': [['x', 1]], 'chart': {'kind': 'line', 'value_column': 'c'}})
    w.call('create', session=w.vera, type='code_change', title='T', payload={})
    w.call('create', session=w.vera, type='release', title='T', payload={'version': ''})
    w.call('create', session=w.vera, type='directory', title='T', payload={'root': 'etc', 'relative_path': 'x'})
    w.call('create', session=w.vera, type='directory', title='T', payload={'root': 'home', 'relative_path': '../up'})
    w.call('create', session=w.vera, type='directory', title='T', payload={'root': 'home', 'relative_path': '/abs'})
    w.call('create', session=w.vera, type='workflow_run', title='T', payload={
        'provider': 'gitlab', 'run_id': '1', 'run_url': 'https://github.com/o/r/actions/runs/1', 'workflow_name': 'w'})
    w.call('create', session=w.vera, type='workflow_run', title='T', payload={
        'provider': 'github', 'run_id': '1', 'run_url': 'https://example.org/o/r/actions/runs/1', 'workflow_name': 'w'})
    w.call('create', session=w.vera, type='workflow_run', title='T', payload={
        'provider': 'github', 'run_id': '1', 'run_url': 'https://github.com/o/r/actions/runs/1', 'workflow_name': 'w',
        'total_steps': 2, 'completed_steps': 3})
    made = w.call('create', session=w.vera, type='document', title='Kept', artifact_id='kept', reference_id='ref',
                  payload=ok)
    w.call('create', session=w.vera, type='document', title='Again', artifact_id='kept', payload=ok,
           note='the id is taken')
    w.call('create', session=w.vera, type='document', title='Again', reference_id='ref', payload=ok,
           note='the reference is taken for this agent and kind')
    w.call('create', session=w.omar, type='document', title='Other agent', reference_id='ref', payload=ok)
    w.call('create', session=w.vera, type='research', title='Other kind', reference_id='ref', payload=ok)
    w.call('update', artifact_id='nothing', data={'status': 'ready'})
    w.call('update', artifact_id=made['artifact_id'], data={'status': 'sleeping'})
    w.call('update', artifact_id=made['artifact_id'], data={'payload': ok, 'payload_patch': ok})
    w.call('update', artifact_id=made['artifact_id'], data={'payload': {}})
    w.call('update', artifact_id=made['artifact_id'], data={'payload_patch': {'content': ''}})
    w.call('update', artifact_id=made['artifact_id'], data={'payload_patch': {'progress': 7}})
    w.call('get', artifact_id='kept')


def a_plan_is_an_artifact(w: World) -> None:
    goal = {'outcome': 'The thing is shipped', 'criteria': ['It runs'], 'limits': 'No publication'}
    plan = w.call('plan_create', session=w.vera, plan_id='ship', title='Ship the thing',
                  items=[{'id': 'probe', 'title': 'Probe'}, {'id': 'build', 'title': 'Build'}], goal=goal)
    listed = w.call('list', session=w.vera)
    artifact = listed[0]
    w.call('get', artifact_id=artifact['artifact_id'])
    w.call('update', artifact_id=artifact['artifact_id'], data={'status': 'completed'}, note='refused: a plan')
    w.call('archive', artifact_id=artifact['artifact_id'], archived=True, expected_updated_at=artifact['updated_at'])
    plan = w.call('plan_step', plan_id=plan['plan_id'], item='probe', status='completed', revision=plan['revision'])
    w.call('get', artifact_id=artifact['artifact_id'])
    plan = w.call('plan_act', plan_id=plan['plan_id'], revision=plan['revision'], action='pause',
                  data={'reason': 'User paused this goal'})
    w.call('get', artifact_id=artifact['artifact_id'])
    plan = w.call('plan_act', plan_id=plan['plan_id'], revision=plan['revision'], action='block',
                  data={'reason': 'Needs a key'})
    w.call('get', artifact_id=artifact['artifact_id'])
    plan = w.call('plan_act', plan_id=plan['plan_id'], revision=plan['revision'], action='resume',
                  data={'reason': 'The key came'})
    plan = w.call('plan_step', plan_id=plan['plan_id'], item='build', status='completed', revision=plan['revision'])
    plan = w.call('plan_act', plan_id=plan['plan_id'], revision=plan['revision'], action='checkpoint',
                  data={'progress': 'Built', 'next_work': 'Close', 'evidence': {'criterion-1': 'ran it'}})
    w.call('plan_finish', plan_id=plan['plan_id'], revision=plan['revision'])
    w.call('get', artifact_id=artifact['artifact_id'])
    # A plan without a goal, and one that is cancelled.
    legacy = w.call('plan_create', session=w.omar, plan_id='', title='A list', items=[{'id': 'one', 'title': 'One'}])
    w.call('list', session=w.omar)
    other = w.call('plan_create', session=w.omar, plan_id='second', title='Second', items=[], goal=goal)
    w.call('plan_act', plan_id=other['plan_id'], revision=other['revision'], action='cancel',
           data={'reason': 'Not needed'})
    w.call('list', session=w.omar)
    w.call('list', type='plan', order='created')
    del legacy


def an_approval_is_asked_and_answered(w: World) -> None:
    asked = w.call('create_decision', session=w.vera, title='Deploy?', question='Deploy version 1.2 to production?',
                   context='Only the web service.', yes_label='Deploy', no_label='Hold', payload={'version': '1.2'},
                   reference_id='deploy-1.2')
    decision = asked['decision']
    w.call('attention')
    w.call('attention', include_questions=True)
    w.call('has_blocking_decision', agent_id=asked['agent_id'])
    w.call('has_pending_decision', agent_id=asked['agent_id'])
    w.call('has_pending_decision', agent_id='')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=decision['revision'] + 1, choice='accepted',
           note='not the current revision')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=0, choice='accepted')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, choice='maybe')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, choice='accepted', answer={'text': 'x'})
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'text': 'x'})
    w.call('resolve', decision_id='no-such', expected_revision=1, choice='accepted')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, choice=' Accepted ')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, choice='accepted',
           note='again, the same: nothing changes')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, choice='rejected',
           note='again, another answer')
    w.call('delivery_pending', decision_id=decision['decision_id'])
    w.call('pending_deliveries')
    w.call('attention')
    w.call('has_blocking_decision', agent_id=asked['agent_id'])
    w.call('mark_delivered', decision_id=decision['decision_id'])
    w.call('pending_deliveries')
    w.call('get', artifact_id=asked['artifact_id'])
    # One that is rejected, with labels left to their defaults and no context.
    asked = w.call('create_decision', session=w.omar, title='Delete?', question='Delete the old branch?',
                   yes_label='', no_label='')
    w.call('resolve', decision_id=asked['decision']['decision_id'], expected_revision=1, choice='rejected')
    w.call('pending_deliveries')
    w.call('list', type='decision')


def a_question_with_options_and_typed_answers(w: World) -> None:
    options = [{'id': 'a', 'label': 'The first', 'description': 'Slow and sure'}, {'id': 'b', 'label': 'The second'}]
    asked = w.call('create_decision', session=w.vera, title='Which?', question='Which way?', response_type='single_choice',
                   options=options, recommended_option_id='a', response_effort='quick')
    decision = asked['decision']
    w.call('attention', note='a question is not shown to an app that knows only approvals')
    w.call('attention', include_questions=True)
    w.call('has_blocking_decision', agent_id=asked['agent_id'])
    w.call('has_pending_decision', agent_id=asked['agent_id'])
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, choice='accepted')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'option_id': 'c'})
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'option_id': 7})
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'option_id': 'a', 'text': 'x'})
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'text': '   '})
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'text': 't' * 4001})
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer='a')
    w.call('resolve', decision_id=decision['decision_id'], expected_revision=1, answer={'option_id': ' b '})
    w.call('pending_deliveries')
    w.call('mark_delivered', decision_id=decision['decision_id'])
    asked = w.call('create_decision', session=w.vera, title='Which?', question='Which name?', response_type='single_choice',
                   options=[{'id': 'x', 'label': 'X'}, {'id': 'y', 'label': 'Y'}, {'id': 'z', 'label': 'Z'}],
                   blocks_progress=True, priority_reason='Nothing else can go on', urgency='time_sensitive')
    w.call('has_blocking_decision', agent_id=asked['agent_id'])
    w.call('resolve', decision_id=asked['decision']['decision_id'], expected_revision=1,
           answer={'text': '  My Own  Words <b>kept</b>  '})
    w.call('pending_deliveries')
    w.call('mark_delivered', decision_id=asked['decision']['decision_id'])
    asked = w.call('create_decision', session=w.omar, title='Code', question='The code from the message?',
                   response_type='text_input', input_hint='one_time_code', payload={'purpose': 'login'},
                   expires_at=w.now + 600_000)
    w.call('attention', include_questions=True)
    w.call('resolve', decision_id=asked['decision']['decision_id'], expected_revision=1, answer={'option_id': 'a'})
    w.call('resolve', decision_id=asked['decision']['decision_id'], expected_revision=1, answer={'text': '123456'})
    w.call('pending_deliveries')
    w.call('create_decision', session=w.omar, title='Name', question='What is it called?', response_type='text_input')
    w.call('get', artifact_id=asked['artifact_id'])
    # What is refused when asking.
    ask = dict(session=w.vera, title='T', question='Q?')
    w.call('create_decision', **{**ask, 'question': '   '})
    w.call('create_decision', **{**ask, 'question': 12})
    w.call('create_decision', **{**ask, 'question': 'q' * 4001})
    w.call('create_decision', **{**ask, 'context': 'c' * 16001})
    w.call('create_decision', **{**ask, 'yes_label': 'y' * 81})
    w.call('create_decision', **{**ask, 'response_type': 'essay'})
    w.call('create_decision', **{**ask, 'options': options}, note='an approval takes no options')
    w.call('create_decision', **{**ask, 'allow_custom_text': True})
    w.call('create_decision', **{**ask, 'recommended_option_id': 'a'})
    w.call('create_decision', **{**ask, 'input_hint': 'text'})
    w.call('create_decision', **{**ask, 'blocks_progress': 'yes'})
    w.call('create_decision', **{**ask, 'blocks_progress': True})
    w.call('create_decision', **{**ask, 'urgency': 'time_sensitive'})
    w.call('create_decision', **{**ask, 'urgency': 'now'})
    w.call('create_decision', **{**ask, 'response_effort': 'long'})
    w.call('create_decision', **{**ask, 'expires_at': 'soon'})
    w.call('create_decision', **{**ask, 'expires_at': w.now + 366 * DAY_MS})
    w.call('create_decision', **{**ask, 'deadline_at': -1})
    w.call('create_decision', **{**ask, 'deadline_at': True})
    question = {**ask, 'response_type': 'single_choice'}
    w.call('create_decision', **question)
    w.call('create_decision', **{**question, 'options': options[:1]})
    w.call('create_decision', **{**question, 'options': options + options})
    w.call('create_decision', **{**question, 'options': [{'id': 'a', 'label': 'A'}, {'id': 'a', 'label': 'B'}]})
    w.call('create_decision', **{**question, 'options': [{'id': 'a b', 'label': 'A'}, {'id': 'c', 'label': 'C'}]})
    w.call('create_decision', **{**question, 'options': [{'id': 'a', 'label': ''}, {'id': 'c', 'label': 'C'}]})
    w.call('create_decision', **{**question, 'options': [{'id': 'a', 'label': 'A', 'extra': 1}, {'id': 'c', 'label': 'C'}]})
    w.call('create_decision', **{**question, 'options': [{'id': 'a', 'label': 'A', 'description': 3}, {'id': 'c', 'label': 'C'}]})
    w.call('create_decision', **{**question, 'options': options, 'recommended_option_id': 'q'})
    w.call('create_decision', **{**question, 'options': options, 'allow_custom_text': 'yes'})
    w.call('create_decision', **{**question, 'options': options, 'allow_custom_text': False})
    typed = {**ask, 'response_type': 'text_input'}
    w.call('create_decision', **{**typed, 'options': options})
    w.call('create_decision', **{**typed, 'allow_custom_text': False})
    w.call('create_decision', **{**typed, 'input_hint': 'password'})
    w.call('create_decision', session='nobody', title='T', question='Q?')
    w.call('create_decision', session=w.vera, title='   ', question='Q?')


def what_becomes_of_a_request_nobody_answers(w: World) -> None:
    one = w.call('create_decision', session=w.vera, title='One', question='The first?')
    two = w.call('create_decision', session=w.vera, title='Two', question='The second?', response_type='single_choice',
                 options=[{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}])
    three = w.call('create_decision', session=w.vera, title='Three', question='The third?',
                   expires_at=w.now + 60_000)
    four = w.call('create_decision', session=w.omar, title='Four', question='The fourth?', response_type='text_input',
                  expires_at=w.now + 60_000)
    w.call('dismiss', decision_id=one['decision']['decision_id'], expected_revision=2, note='not the current revision')
    w.call('dismiss', decision_id=one['decision']['decision_id'], expected_revision=1)
    w.call('dismiss', decision_id=one['decision']['decision_id'], expected_revision=1, note='again: nothing changes')
    w.call('resolve', decision_id=one['decision']['decision_id'], expected_revision=2, choice='accepted',
           note='a discarded request is not answered any more')
    w.call('dismiss', decision_id='no-such', expected_revision=1)
    w.call('withdraw', decision_id=two['decision']['decision_id'], session=w.omar, note='not the one who asked')
    w.call('withdraw', decision_id=two['decision']['decision_id'], session='   ')
    w.call('withdraw', decision_id=two['decision']['decision_id'], session=w.vera)
    w.call('withdraw', decision_id=two['decision']['decision_id'], session=w.vera, note='again: nothing changes')
    w.call('dismiss', decision_id=two['decision']['decision_id'], expected_revision=2,
           note='a withdrawn request is no longer pending')
    w.call('withdraw', decision_id=one['decision']['decision_id'], session=w.vera)
    w.call('withdraw', decision_id='no-such', session=w.vera)
    w.call('pending_deliveries', note='neither is told to the agent')
    w.call('attention', include_questions=True)
    w.later(120_000)
    w.call('resolve', decision_id=three['decision']['decision_id'], expected_revision=1, choice='accepted',
           note='it expired a minute ago')
    w.call('attention', include_questions=True)
    w.call('dismiss', decision_id=four['decision']['decision_id'], expected_revision=2)
    w.call('pending_deliveries', note='an expired request is told to its agent')
    w.call('get', artifact_id=three['artifact_id'])
    w.call('list', session=w.vera)
    # An agent that goes takes its open requests with it.
    five = w.call('create_decision', session=w.omar, title='Five', question='The fifth?')
    w.call('create', session=w.omar, type='document', title='Being written', status='active', payload={'content': 'x'})
    w.call('cancel_for_agent', agent_id=five['agent_id'])
    w.call('list', session=w.omar)
    w.call('pending_deliveries')


def the_inbox_and_what_a_user_does_with_an_artifact(w: World) -> None:
    plain = w.call('create_decision', session=w.vera, title='Plain', question='A plain approval?')
    w.call('create_decision', session=w.omar, title='Blocking', question='A blocking question?',
           response_type='single_choice', options=[{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}],
           blocks_progress=True, priority_reason='Stops the work')
    w.call('create_decision', session=w.vera, title='Urgent', question='An urgent approval?', urgency='time_sensitive',
           priority_reason='Before noon', deadline_at=w.now + 3_600_000)
    w.call('create_decision', session=w.omar, title='Urgent sooner', question='An urgent approval, due sooner?',
           urgency='time_sensitive', priority_reason='Before ten', deadline_at=w.now + 600_000)
    w.call('create_decision', session=w.vera, title='Both', question='Blocking and urgent?', blocks_progress=True,
           urgency='time_sensitive', priority_reason='Everything waits')
    w.call('create_decision', session=w.omar, title='Typed', question='A typed answer?', response_type='text_input')
    w.call('attention', include_questions=True)
    w.call('attention')
    w.call('archive', artifact_id=plain['artifact_id'], archived=True, expected_updated_at=plain['updated_at'] + 1,
           note='not the time it was last changed at')
    w.call('archive', artifact_id=plain['artifact_id'], archived='yes', expected_updated_at=plain['updated_at'])
    w.call('archive', artifact_id=plain['artifact_id'], archived=False, expected_updated_at=plain['updated_at'],
           note='it is not archived: nothing changes')
    hidden = w.call('archive', artifact_id=plain['artifact_id'], archived=True, expected_updated_at=plain['updated_at'])
    w.call('attention', include_questions=True, note='an archived request is out of the inbox')
    w.call('attention', include_questions=True, include_archived=True)
    w.call('archive', artifact_id=plain['artifact_id'], archived=False, expected_updated_at=hidden[0]['updated_at'])
    w.call('archive', artifact_id='nothing', archived=True, expected_updated_at=1)
    doc = w.call('create', session=w.vera, type='document', title='A document', payload={'content': 'x'})
    other = w.call('create', session=w.omar, type='document', title='Another', payload={'content': 'y'})
    w.call('pin', artifact_id=doc['artifact_id'], pinned=True)
    w.call('pin', artifact_id=doc['artifact_id'], pinned=True, note='again: nothing changes')
    w.call('pin', artifact_id=other['artifact_id'], pinned=True)
    w.call('pin', artifact_id=doc['artifact_id'], pinned='yes')
    w.call('pin', artifact_id='nothing', pinned=True)
    w.call('list_pinned')
    w.call('list_pinned', limit=1)
    w.call('pin', artifact_id=other['artifact_id'], pinned=False)
    w.call('discard', artifact_id=plain['artifact_id'], expected_updated_at=1, note='a request is discarded its own way')
    w.call('discard', artifact_id=doc['artifact_id'], expected_updated_at=1, note='not the time it was last changed at')
    w.call('discard', artifact_id=doc['artifact_id'], expected_updated_at=doc['updated_at'])
    w.call('discard', artifact_id=doc['artifact_id'], expected_updated_at=doc['updated_at'], note='again')
    w.call('discard', artifact_id='nothing', expected_updated_at=1)
    w.call('get', artifact_id=doc['artifact_id'], note='a discarded artifact is gone')
    w.call('pin', artifact_id=doc['artifact_id'], pinned=True)
    w.call('list_pinned')
    w.call('list', type='document')
    w.call('update', artifact_id=doc['artifact_id'], data={'status': 'ready'})


SCENARIOS = [plain_kinds, what_an_artifact_refuses, a_plan_is_an_artifact, an_approval_is_asked_and_answered,
             a_question_with_options_and_typed_answers, what_becomes_of_a_request_nobody_answers,
             the_inbox_and_what_a_user_does_with_an_artifact]


def main(argv: list[str]) -> int:
    isolate()
    scenarios = []
    for number, scenario in enumerate(SCENARIOS, start=1):
        world = World(number)
        scenario(world)
        scenarios.append({'name': scenario.__name__, 'agents': list(world.agents.values()), 'calls': world.calls})
    if len(argv) == 3 and argv[1] == '--show':
        chosen = scenarios[int(argv[2]) - 1]
        width = int(os.environ.get('WIDTH', '300'))
        for index, call in enumerate(chosen['calls']):
            said = json.dumps(call.get('result', call.get('error')), ensure_ascii=False)
            print(f"{index} {call['call']} {json.dumps(call['args'], ensure_ascii=False)[:160]}"
                  f"{'  # ' + call['note'] if call.get('note') else ''}\n   -> {said[:width]}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/artifact-vectors.py from server/lib/artifacts.py. '
                        'Do not edit by hand.',
               't0': T0, 'step_ms': STEP_MS, 'scenarios': scenarios}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    calls = sum(len(s['calls']) for s in scenarios)
    refused = sum(1 for s in scenarios for c in s['calls'] if 'error' in c)
    print(f'{OUT.relative_to(REPO)}: {len(scenarios)} scenarios, {calls} calls ({refused} refused), '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
