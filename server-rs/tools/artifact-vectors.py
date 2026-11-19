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
        # The id of a published file: counted, like every other id of a scenario.
        self.files = 0
        secrets.token_urlsafe = self.token_urlsafe
        # A report's revision that was given no version gets one of sixteen random bytes: the
        # scenario's next id.
        import types
        import uuid
        uuid.uuid4 = lambda: types.SimpleNamespace(hex=self.token_hex(16))
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

    def token_urlsafe(self, nbytes: int = 18) -> str:
        self.files += 1
        return f'{self.files:024d}'

    def publish(self, session: str, blob_hex: str, source_name: str, content_type: str, caption: str = ''):
        """An agent publishes a file (`media_store.publish`): the row it is answered with, and
        what its bytes are, which a podcast names."""
        from lib import media_store
        row = media_store.publish(session=session, blob=bytes.fromhex(blob_hex), source_name=source_name,
                                  content_type=content_type, caption=caption, media_dir=SCRATCH / 'media')
        return {**row, 'sha256': media_store.get(row['asset_id'])['sha256']}

    def revisions(self, artifact_id: str, version=None, limit: int = 50, offset: int = 0):
        """What a report was, version by version (`html_report_revisions.revisions`)."""
        from lib import html_report_revisions
        return html_report_revisions.revisions(artifact_id, version, limit=limit, offset=offset)

    def represent(self, artifact_id: str, representation: str):
        """An artifact as a list shows it (`response_representation`)."""
        from lib import artifacts
        return artifacts.response_representation(artifacts.get(artifact_id), representation)

    def kept_updated_at(self, artifact_id: str) -> int:
        """When an artifact last changed, read without a call that is written down."""
        from lib import artifacts
        return artifacts.get(artifact_id)['updated_at']

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
            'plan_finish': task_plans.finish, 'publish': self.publish, 'revisions': self.revisions,
            'represent': self.represent, 'submit': self.submit, 'pending_submissions': self.pending_submissions,
            'mark_submission_delivered': self.mark_submission_delivered, 'inbox': self.inbox,
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

    def inbox(self, limit=100, cursor: str = '', after: str = '', keep: str = '', representation: str = ''):
        """A page of what the user is shown in Updates (`attention_index.page`). A cursor is each
        Host's own: `after` names the page it goes on from, `keep` the name this page is kept
        under. What is written of a cursor is whether there is one; of the snapshot, that it is
        the same as or another than the one before under the same name."""
        from lib import attention_index
        page = attention_index.page(limit=limit, cursor=self.kept[f'cursor:{after}'] if after else cursor,
                                    representation=representation)
        if keep:
            self.kept[f'cursor:{keep}'] = page['next_cursor'] or ''
        return {**page, 'next_cursor': page['next_cursor'] is not None, 'snapshot_revision': '<revision>'}

    def submit(self, artifact_id: str, data: dict):
        """A user sends answers to a form (`html_forms.submit`)."""
        from lib import html_forms
        return html_forms.submit(artifact_id, data)

    def pending_submissions(self):
        """The answers that wait to be told to their forms' agents, each with what is told."""
        from lib import html_forms
        return [{'submission_id': row['submission_id'], 'artifact_id': row['artifact_id'], 'version': row['version'],
                 'session': row['session'], 'prompt': row['prompt'], 'created_at': row['created_at'],
                 'synthesize_audio': bool(row['synthesize_audio'])} for row in html_forms.pending()]

    def mark_submission_delivered(self, submission_id: str):
        from lib import html_forms
        html_forms.mark_delivered(submission_id)

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


MP3 = (b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\x00' * 8).hex()
MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom'.hex()
PDF = b'%PDF-1.7\n1 0 obj\n'.hex()
PNG = (b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + (640).to_bytes(4, 'big') + (480).to_bytes(4, 'big')
       + b'\x08\x06\x00\x00\x00').hex()


def what_names_a_published_file(w: World) -> None:
    """A recording, a film and a document are artifacts that name a file their agent published."""
    voice = w.call('publish', session=w.vera, blob_hex=MP3, source_name='voice note.mp3', content_type='audio/mpeg',
                   caption='A note')
    film = w.call('publish', session=w.vera, blob_hex=MP4, source_name='clip.mp4', content_type='video/mp4')
    paper = w.call('publish', session=w.vera, blob_hex=PDF, source_name='report.pdf', content_type='application/pdf')
    chart = w.call('publish', session=w.vera, blob_hex=PNG, source_name='chart.png', content_type='image/png')
    theirs = w.call('publish', session=w.omar, blob_hex=PDF, source_name='other.pdf', content_type='application/pdf')
    w.call('publish', session='nobody', blob_hex=PDF, source_name='x.pdf', content_type='application/pdf')
    w.call('publish', session=w.vera, blob_hex=b'#!/bin/sh\n'.hex(), source_name='run.sh', content_type='application/x-sh')
    w.call('publish', session=w.vera, blob_hex='', source_name='empty.pdf', content_type='application/pdf')

    def named(asset, **more):
        return {'url': asset['url'], 'mime_type': asset['mime_type'], 'file_name': asset['file_name'], **more}

    audio = w.call('create', session=w.vera, type='audio', title='A voice note', summary='Two minutes',
                   payload=named(voice, duration_ms=120_000, size_bytes=voice['bytes']))
    w.call('create', session=w.vera, type='video', title='A clip', payload=named(film, duration_ms=4_000,
                                                                              thumbnail_url=chart['url']))
    doc = w.call('create', session=w.vera, type='file', title='The report', payload=named(paper))
    w.call('create', session=w.vera, type='file', title='A picture as a file', payload=named(chart))
    w.call('create', session=w.vera, type='file', title='A recording as a file', payload=named(voice))
    w.call('get', artifact_id=audio['artifact_id'])
    w.call('list', session=w.vera, type='audio')
    w.call('list', session=w.vera)
    # What its kind refuses.
    w.call('create', session=w.vera, type='audio', title='T', payload=named(film), note='a film is no recording')
    w.call('create', session=w.vera, type='video', title='T', payload=named(voice), note='a recording is no film')
    w.call('create', session=w.vera, type='audio', title='T', payload=named(voice, mime_type='AUDIO/MPEG'),
           note='the kind is read without its case, the file\'s own is not')
    w.call('create', session=w.vera, type='audio', title='T', payload={'url': voice['url'], 'mime_type': 'audio/mpeg'})
    w.call('create', session=w.vera, type='file', title='T', payload={'url': '', 'mime_type': '', 'file_name': ''})
    w.call('create', session=w.vera, type='file', title='T', payload=named(paper, url=7))
    for url in ('https://example.org/report.pdf', '/media/', '/media/a/b', 'media/' + paper['asset_id'],
                paper['url'] + '?x=1', '/media/' + 'x' * 181, '/media/asset_nothing', ' ' + paper['url']):
        w.call('create', session=w.vera, type='file', title='T', payload=named(paper, url=url))
    w.call('create', session=w.vera, type='file', title='T', payload=named(theirs), note='another agent\'s file')
    w.call('create', session=w.omar, type='file', title='Theirs', payload=named(theirs))
    w.call('create', session=w.vera, type='file', title='T', payload=named(paper, mime_type='text/plain'))
    w.call('create', session=w.vera, type='file', title='T', payload=named(paper, file_name='other.pdf'))
    w.call('create', session=w.vera, type='file', title='T', payload=named(paper, thumbnail_url='http://example.org/t.png'))
    w.call('create', session=w.vera, type='file', title='T', payload=named(paper, size_bytes='17'))
    # A change is held to the same.
    w.call('update', artifact_id=doc['artifact_id'], data={'payload_patch': {'notes': 'Read page 2'}})
    w.call('update', artifact_id=doc['artifact_id'], data={'payload_patch': {'file_name': 'renamed.pdf'}})
    w.call('update', artifact_id=doc['artifact_id'], data={'payload': named(chart)},
           note='the same artifact, another of the agent\'s files')
    w.call('update', artifact_id=doc['artifact_id'], data={'payload': named(theirs)})
    w.call('update', artifact_id=doc['artifact_id'], data={'payload': {'url': paper['url']}})
    w.call('update', artifact_id=audio['artifact_id'], data={'payload': named(film)})
    w.call('update', artifact_id=audio['artifact_id'], data={'status': 'completed', 'summary': 'Heard'})
    w.call('get', artifact_id=doc['artifact_id'])
    w.call('list', type='file')


PAGE_1 = '<h1>Synthetic report</h1><p>First.</p>'
PAGE_2 = ('<h1>Synthetic report</h1><p>Second: æøå, and what a filter of text would cut, '
          '<environment_context>kept</environment_context>.</p><script>document.title = "two"</script>')


def a_report_and_its_revisions(w: World) -> None:
    """A report is a page of HTML that takes no answers. Published again under its id it is revised,
    and what it was is kept, version by version."""
    def page(content, **more):
        return {'content': content, 'read_only': True, **more}

    def report(session, payload, expected=None, note='', **more):
        args = {'session': session, 'type': 'html_form', 'title': 'A report', 'artifact_id': 'report-1',
                'payload': payload, **more}
        if expected is not None:
            args['expected_version'] = expected
        return w.call('create', note=note, **args)

    report(w.vera, page(PAGE_1, version='v1'), summary='Day one')
    w.call('get', artifact_id='report-1')
    w.call('revisions', artifact_id='report-1')
    w.call('represent', artifact_id='report-1', representation='summary-v1')
    w.call('represent', artifact_id='report-1', representation='flat-v1')
    # Published again under the same id: a revision, which says the version it follows.
    report(w.vera, page(PAGE_2, version='v2'), summary='Day two', note='without the version it follows')
    report(w.vera, page(PAGE_2, version='v2'), expected='v0', summary='Day two', note='following a version that is not the current')
    report(w.vera, page(PAGE_2, version='v2'), expected='v1', summary='Day two')
    report(w.vera, page(PAGE_2, version='v2'), expected='v1', summary='Day two', note='the same again: a retry is answered with the report as it is')
    report(w.vera, page(PAGE_2, version='v2'), summary='Day two', note='and once more, without the version it follows')
    report(w.vera, page(PAGE_1, version='v2'), expected='v2', note='a version that was used for other content')
    report(w.vera, page(PAGE_1, version='v1'), expected='v2', note='an old version as it was: answered with the current')
    report(w.vera, page(PAGE_1, version='v1'), note='the same, without the version it follows')
    report(w.omar, page(PAGE_1, version='v9'), expected='v2', note='the report of another agent')
    report(w.vera, page(PAGE_2), expected='v2', note='no version given, nothing changed')
    report(w.vera, page(PAGE_1), expected='v2', title='A report, again', note='no version given, and changed: it is given one')
    w.call('get', artifact_id='report-1')
    # Changed in place.
    w.call('update', artifact_id='report-1', data={'payload_patch': {'content': '<p>Third.</p>'}})
    w.call('update', artifact_id='report-1', data={'payload_patch': {}, 'title': 'A report, renamed'},
           note='nothing of the page changes: no new version')
    w.call('update', artifact_id='report-1', data={'payload': page('<p>Fourth.</p>', version='v4'), 'summary': 'Day four',
                                                   'expected_version': 'nope'})
    w.call('update', artifact_id='report-1', data={'payload': page('<p>Fourth.</p>', version='v4'), 'summary': 'Day four'})
    w.call('update', artifact_id='report-1', data={'payload': page('<p>Fourth.</p>', version='v4'), 'title': 'A fourth'},
           note='the same page under the same version: the title changes')
    w.call('update', artifact_id='report-1', data={'payload': page('<p>x</p>', version='v5'), 'payload_patch': {}})
    w.call('update', artifact_id='report-1', data={'payload': 'text'})
    w.call('update', artifact_id='report-1', data={'payload_patch': ['list']})
    w.call('update', artifact_id='report-1', data={'payload': {'content': '<p>x</p>', 'version': 'v6'}},
           note='a revision stays a report')
    w.call('update', artifact_id='report-1', data={'payload_patch': {'read_only': False}})
    w.call('update', artifact_id='report-1', data={'payload_patch': {'connect_origins': ['https://EXAMPLE.org:443']}})
    w.call('update', artifact_id='report-1', data={'payload_patch': {'connect_origins': ['http://example.org']}})
    w.call('update', artifact_id='report-1', data={'payload_patch': {'version': 'not a version'}})
    w.call('update', artifact_id='report-1', data={'payload_patch': {'version': 'v1'}}, note='back to a version that was another page')
    w.call('update', artifact_id='report-1', data={'payload_patch': {'content': ''}})
    w.call('update', artifact_id='report-1', data={'payload_patch': {'content': 7}})
    w.call('update', artifact_id='report-1', data={'status': 'completed'}, note='its state is no revision')
    w.call('update', artifact_id='report-1', data={'payload_patch': {'notes': 'A note beside the page'}, 'expected_version': 'v4'},
           note='following a version that is no longer the current')
    w.call('revisions', artifact_id='report-1')
    w.call('revisions', artifact_id='report-1', version='v1')
    w.call('revisions', artifact_id='report-1', version='v2')
    w.call('revisions', artifact_id='report-1', version='v4')
    w.call('revisions', artifact_id='report-1', version='nothing')
    w.call('revisions', artifact_id='report-1', limit=2, offset=1)
    w.call('revisions', artifact_id='report-1', limit=0, offset=-3)
    w.call('revisions', artifact_id='report-1', limit=500, offset=50)
    w.call('revisions', artifact_id='nothing')
    w.call('represent', artifact_id='report-1', representation='summary-v1')
    w.call('get', artifact_id='report-1')
    w.call('list', session=w.vera, type='html_form')
    # What is no report.
    w.call('create', session=w.vera, type='html_form', title='T', payload=page(''))
    w.call('create', session=w.vera, type='html_form', title='T', payload=page(PAGE_1))
    w.call('create', session=w.vera, type='html_form', title='T', payload=page(PAGE_1, version='v 1'))
    w.call('create', session=w.vera, type='html_form', title='T', payload=page(7, version='v1'))
    w.call('create', session=w.vera, type='html_form', title='T',
           payload=page(PAGE_1, version='v1', answer_schema={'type': 'object', 'properties': {'a': {'type': 'string'}}}))
    w.call('create', session=w.vera, type='html_form', title='T', payload=page(PAGE_1, version='v1', connect_origins=['https://example.org/x']))
    w.call('create', session=w.vera, type='html_form', title='T', payload=page(PAGE_1, version='v1', read_only='yes'))
    w.call('create', session=w.vera, type='html_form', title='T', payload=None)
    w.call('create', session='nobody', type='html_form', title='T', payload=page(PAGE_1, version='v1'))
    w.call('create', session=w.vera, type='html_form', title='T', status='sleeping', payload=page(PAGE_1, version='v1'))
    # A report under the id of something that is none.
    w.call('create', session=w.vera, type='document', title='Notes', artifact_id='doc-1', payload={'content': 'x'})
    w.call('create', session=w.vera, type='html_form', title='T', artifact_id='doc-1', payload=page(PAGE_1, version='v1'),
           expected_version='v1')
    w.call('revisions', artifact_id='doc-1')
    w.call('represent', artifact_id='doc-1', representation='summary-v1')
    # What a title loses a page keeps, and a page may reach what it names.
    kept = w.call('create', session=w.omar, type='html_form', title='With <environment_context>cut</environment_context> a title',
                  summary='Reaches out', status='active', reference_id='ref-9',
                  payload=page(PAGE_2, version='2026.11.15-a', connect_origins=['https://API.example.org:443', 'https://api.example.org']))
    w.call('revisions', artifact_id=kept['artifact_id'])
    w.call('represent', artifact_id=kept['artifact_id'], representation='summary-v1')
    w.call('list', type='html_form')
    w.call('list', search='report')
    # Put away, pinned and thrown away like anything an agent made.
    w.call('pin', artifact_id='report-1', pinned=True)
    w.call('archive', artifact_id='report-1', archived=True, expected_updated_at=w.kept_updated_at('report-1'))
    w.call('discard', artifact_id=kept['artifact_id'], expected_updated_at=kept['updated_at'])
    w.call('revisions', artifact_id=kept['artifact_id'])
    w.call('get', artifact_id='report-1')


def a_form_and_its_answers(w: World) -> None:
    """A form is a page with a schema of its answers. It is made once. A user sends answers under an
    id of their own choosing; they are held to the schema, kept once, and told to the form's agent."""
    schema = {'type': 'object', 'properties': {
        'when': {'type': 'string', 'enum': ['now', 'later']}, 'budget': {'type': 'number', 'minimum': 0},
        'notes': {'type': 'string', 'maxLength': 40},
        'steps': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3}},
        'required': ['when'], 'additionalProperties': False}

    def form(session, artifact_id, title='A plan to fill in', note='', **more):
        payload = {'content': '<form><input name="when"></form>', 'version': '1', 'answer_schema': schema, **more}
        return w.call('create', note=note, session=session, type='html_form', title=title, artifact_id=artifact_id,
                      summary='What it asks', payload=payload)

    def answers(artifact_id, submission_id, given, note='', **more):
        return w.call('submit', note=note, artifact_id=artifact_id,
                      data={'submission_id': submission_id, 'version': '1', 'answers': given, **more})

    form(w.vera, 'form-1', title='Plan: it\'s "here"')
    form(w.vera, 'form-1', note='a form is made once: its id is taken')
    form(w.omar, 'form-1', note='and by nobody else')
    w.call('get', artifact_id='form-1')
    w.call('represent', artifact_id='form-1', representation='summary-v1')
    w.call('represent', artifact_id='form-1', representation='flat-v1')
    w.call('list', session=w.vera, type='html_form')
    # A form is never changed; what is said of it may be.
    w.call('update', artifact_id='form-1', data={'payload': {'content': '<p>other</p>', 'version': '2', 'answer_schema': schema}})
    w.call('update', artifact_id='form-1', data={'payload_patch': {'version': '2'}})
    w.call('update', artifact_id='form-1', data={'payload_patch': {}})
    w.call('update', artifact_id='form-1', data={'summary': 'What it asks, said better'})
    w.call('revisions', artifact_id='form-1', note='a form has no history')
    # What is no submission.
    w.call('submit', artifact_id='form-1', data={})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'short', 'version': '1', 'answers': {}})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'not_plain_signs-0001', 'version': '1', 'answers': {}})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'x' * 81, 'version': '1', 'answers': {}})
    w.call('submit', artifact_id='form-1', data={'submission_id': 7, 'version': '1', 'answers': {}})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'sub-0000-0000-0001', 'version': '1'})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'sub-0000-0000-0001', 'version': '1', 'answers': ['now']})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now'}, synthesize_audio='yes')
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now'}, synthesize_audio=None)
    answers('form-1', 'sub-0000-0000-0001', {'notes': 'x' * 131072}, note='more than is taken, before it is asked what it is for')
    # What a form does not take.
    answers('nothing-there', 'sub-0000-0000-0001', {'when': 'now'})
    w.call('create', session=w.vera, type='document', title='Notes', artifact_id='notes-1', payload={'content': 'x'})
    answers('notes-1', 'sub-0000-0000-0001', {'when': 'now'}, note='what is no form')
    w.call('create', session=w.vera, type='html_form', title='A report', artifact_id='report-1',
           payload={'content': '<p>Read me.</p>', 'version': '1', 'read_only': True})
    answers('report-1', 'sub-0000-0000-0001', {}, note='a report takes no answers')
    w.call('submit', artifact_id='form-1', data={'submission_id': 'sub-0000-0000-0001', 'version': '2', 'answers': {'when': 'now'}})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'sub-0000-0000-0001', 'version': 1, 'answers': {'when': 'now'}})
    w.call('submit', artifact_id='form-1', data={'submission_id': 'sub-0000-0000-0001', 'answers': {'when': 'now'}})
    answers('form-1', 'sub-0000-0000-0001', {})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'soon'})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'budget': -1})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'budget': '10'})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'notes': 'n' * 41})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'steps': ['a', 'b', 'c', 'd']})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'steps': ['a', 2]})
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'colour': 'red', 'also': 1})
    answers('form-1', 'sub-0000-0000-0001', {'budget': 'x', 'when': 5}, note='the first thing the schema asks is said')
    w.call('pending_submissions', note='nothing was taken yet')
    # Answers that fit: kept once, however often they are sent.
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'budget': 12.5, 'notes': 'blåbær, "quoted" 🎉', 'steps': ['a', 'b']})
    w.call('pending_submissions')
    answers('form-1', 'sub-0000-0000-0001', {'steps': ['a', 'b'], 'notes': 'blåbær, "quoted" 🎉', 'budget': 12.5, 'when': 'now'},
            note='the same answers in another order are the same answers')
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'budget': 12.5, 'notes': 'blåbær, "quoted" 🎉', 'steps': ['a', 'b']},
            synthesize_audio=True, note='and said to be spoken, as they are when nothing is said')
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'budget': 12.5, 'notes': 'blåbær, "quoted" 🎉', 'steps': ['a', 'b']},
            synthesize_audio=False, note='another wish for the same answers')
    answers('form-1', 'sub-0000-0000-0001', {'when': 'later'}, note='other answers under a used id')
    w.call('submit', artifact_id='form-1', data={'submission_id': 'sub-0000-0000-0001', 'version': '2', 'answers': {'when': 'now'}},
           note='a used id, for another version')
    answers('nothing-there', 'sub-0000-0000-0001', {'when': 'now'}, note='a used id, for a form that is not there')
    w.call('mark_submission_delivered', submission_id='sub-0000-0000-0001')
    w.call('pending_submissions')
    answers('form-1', 'sub-0000-0000-0001', {'when': 'now', 'budget': 12.5, 'notes': 'blåbær, "quoted" 🎉', 'steps': ['a', 'b']},
            note='sent again after its agent was told')
    w.call('mark_submission_delivered', submission_id='sub-0000-0000-0001', note='told once')
    w.call('mark_submission_delivered', submission_id='nothing-of-the-kind')
    # A second answer to the same form, not to be spoken; and one to another agent's form.
    answers('form-1', 'sub-0000-0000-0002', {'when': 'later', 'budget': 1e16, 'steps': []}, synthesize_audio=False)
    form(w.omar, 'form-2', title='Omar asks')
    answers('form-2', 'sub-0000-0000-0001', {'when': 'now'}, note='an id is used once on the whole Host')
    answers('form-2', 'sub-0000-0000-0003', {'when': 'now', 'budget': 0})
    w.call('pending_submissions', note='the oldest first')
    w.call('mark_submission_delivered', submission_id='sub-0000-0000-0003')
    w.call('pending_submissions')
    # Put away, a form still takes answers; over, it does not; thrown away, it is not there.
    w.call('archive', artifact_id='form-1', archived=True, expected_updated_at=w.kept_updated_at('form-1'))
    answers('form-1', 'sub-0000-0000-0004', {'when': 'now'}, note='put away')
    w.call('update', artifact_id='form-1', data={'status': 'active'})
    answers('form-1', 'sub-0000-0000-0005', {'when': 'now'}, note='in progress')
    w.call('update', artifact_id='form-1', data={'status': 'completed'})
    answers('form-1', 'sub-0000-0000-0006', {'when': 'now'}, note='over')
    answers('form-1', 'sub-0000-0000-0005', {'when': 'now'}, note='what was taken before it was over is still answered')
    w.call('discard', artifact_id='form-2', expected_updated_at=w.kept_updated_at('form-2'))
    answers('form-2', 'sub-0000-0000-0007', {'when': 'now'}, note='thrown away')
    answers('form-2', 'sub-0000-0000-0003', {'when': 'now', 'budget': 0}, note='what it took before is still answered')
    w.call('get', artifact_id='form-1')


def what_the_user_is_shown_in_updates(w: World) -> None:
    """Updates is a page at a time of what agents made that a user may want to look at: what failed
    first, then what is ready, then what is being worked on, each oldest first. A cursor goes on
    from where a page ended, as long as nothing that is shown has changed."""
    def made(session, kind, title, status='ready', artifact_id='', **payload):
        return w.call('create', session=session, type=kind, title=title, status=status, artifact_id=artifact_id,
                      payload=payload)

    w.call('inbox', note='nothing was made yet')
    made(w.vera, 'document', 'Notes', content='# Notes')
    made(w.vera, 'research', 'Research, done', status='completed', content='Findings',
         sources=[{'title': 'A source', 'url': 'https://example.org/a'}])
    made(w.vera, 'data', 'A table, in the making', status='active', columns=['a'], rows=[[1]])
    made(w.vera, 'code_change', 'A change that failed', status='failed', repository='owner/repo')
    made(w.vera, 'release', 'A draft', status='draft', version='0.1')
    made(w.vera, 'directory', 'Called off', status='cancelled', root='workspace', relative_path='src')
    made(w.omar, 'document', 'Too late', status='expired', content='x')
    made(w.omar, 'document', "Omar's notes", content='y')
    made(w.vera, 'workflow_run', 'CI', status='active', provider='github', run_id='42',
         run_url='https://github.com/owner/repo/actions/runs/42', workflow_name='Tests')
    made(w.vera, 'html_form', 'A report', artifact_id='report-1', content='<p>Read me.</p>', version='1', read_only=True)
    made(w.vera, 'html_form', 'A form', artifact_id='form-1', content='<form></form>', version='1',
         answer_schema={'type': 'object'})
    made(w.omar, 'document', 'Said to be a janitor\'s', attention_kind='janitor_failure', content='z')
    made(w.omar, 'release', 'Released', status='completed', version='1.0')
    w.call('create_decision', session=w.vera, title='Ship?', question='Ship it?')
    w.call('inbox', note='what failed, what is ready, what is in the making; each oldest first')
    w.call('inbox', representation='summary-v1')
    w.call('inbox', representation='flat-v1')
    w.call('inbox', representation='nothing-of-the-kind')
    # A page at a time.
    w.call('inbox', limit=3, keep='first')
    w.call('inbox', limit=3, after='first', keep='second')
    w.call('inbox', limit=3, after='second', keep='third')
    w.call('inbox', limit=0, keep='one', note='at least one')
    w.call('inbox', limit=1000, note='at most two hundred')
    w.call('inbox', limit=-5)
    # What is no cursor.
    w.call('inbox', cursor='not a cursor')
    w.call('inbox', cursor='bm90IGpzb24=', note='not JSON')
    w.call('inbox', cursor='eyJvZmZzZXQiOiAwfQ==', note='an offset without a snapshot')
    w.call('inbox', cursor='eyJyZXZpc2lvbiI6ICJ4IiwgIm9mZnNldCI6IC0xfQ==', note='an offset before the beginning')
    w.call('inbox', cursor='eyJyZXZpc2lvbiI6ICJ4IiwgIm9mZnNldCI6ICIxIn0=', note='an offset that is text')
    w.call('inbox', cursor='eyJyZXZpc2lvbiI6ICJ4IiwgIm9mZnNldCI6IDAsICJtb3JlIjogMX0=', note='with more than a cursor has')
    w.call('inbox', cursor='eyJyZXZpc2lvbiI6ICJ4IiwgIm9mZnNldCI6IDB9', note='of a snapshot that never was')
    w.call('inbox', cursor='x' * 2049)
    # What a user does with what is shown changes what is shown: a cursor of before is stale.
    w.call('inbox', limit=2, keep='before')
    w.call('archive', artifact_id='report-1', archived=True, expected_updated_at=w.kept_updated_at('report-1'))
    w.call('inbox', limit=2, after='before', note='put away in between')
    w.call('inbox', note='put away, it is still shown, as put away')
    w.call('inbox', limit=2, keep='before')
    w.call('pin', artifact_id='form-1', pinned=True)
    w.call('inbox', limit=2, after='before', note='pinned in between')
    w.call('inbox', limit=2, keep='before')
    w.call('update', artifact_id='form-1', data={'summary': 'Said of it'})
    w.call('inbox', limit=2, after='before', note='changed in between')
    w.call('inbox', limit=2, keep='before')
    w.call('inbox', limit=2, after='before', note='nothing in between')
    # What it becomes moves it: finished work is ready, failed work comes first, what is over is gone.
    first = w.call('list', session=w.vera, type='data')[0]
    w.call('update', artifact_id=first['artifact_id'], data={'status': 'completed'})
    w.call('inbox')
    w.call('update', artifact_id=first['artifact_id'], data={'status': 'failed'})
    w.call('inbox')
    w.call('update', artifact_id=first['artifact_id'], data={'status': 'expired'})
    w.call('inbox')
    w.call('discard', artifact_id='report-1', expected_updated_at=w.kept_updated_at('report-1'))
    w.call('inbox', note='thrown away, it is gone')



def a_request_answered_on_the_users_behalf(w: World) -> None:
    """The owner named a session that may answer for him (`designated_sessions`, his `[agents]
    decision_on_behalf_sessions`), and the Host knows which agent's turn asks (`verified_session`): Omar is
    the owner's front desk, Vera asks, and nobody has the session `ghost`."""
    asked = w.call('create_decision', session=w.vera, title='Deploy?', question='Deploy version 1.3?',
                   reference_id='deploy-1.3')
    decision_id = asked['decision']['decision_id']
    answer = lambda note, **more: w.call('resolve', decision_id=decision_id, expected_revision=1,  # noqa: E731
                                         choice='accepted', note=note, **more)
    answer('named by nobody', on_behalf_session=w.omar)
    answer('named, but its turn is not known', on_behalf_session=w.omar, designated_sessions=[w.omar])
    answer('named, and another agent\'s turn', on_behalf_session=w.omar, designated_sessions=[w.omar],
           verified_session=w.vera)
    answer('an agent\'s turn that says for whom it answers nothing', verified_session=w.omar)
    answer('a blank session', on_behalf_session='  ', designated_sessions=[w.omar], verified_session=w.omar)
    answer('the asking agent for itself', on_behalf_session=w.vera, designated_sessions=[w.vera],
           verified_session=w.vera)
    answer('a session nobody has', on_behalf_session='ghost', designated_sessions=['ghost'],
           verified_session='ghost')
    answer('the front desk, in its own turn, with blanks around the names', on_behalf_session=f' {w.omar} ',
           designated_sessions=[f' {w.omar} ', 7, ''], verified_session=f'{w.omar} ')
    answer('again, the same: nothing changes', on_behalf_session=w.omar, designated_sessions=[w.omar],
           verified_session=w.omar)
    answer('again, by the user: answered by another', )
    w.call('get', artifact_id=asked['artifact_id'])
    w.call('pending_deliveries')
    options = [{'id': 'a', 'label': 'The first'}, {'id': 'b', 'label': 'The second'}]
    asked = w.call('create_decision', session=w.vera, title='Which?', question='Which way?',
                   response_type='single_choice', options=options)
    w.call('resolve', decision_id=asked['decision']['decision_id'], expected_revision=1, answer={'option_id': 'b'},
           on_behalf_session=w.omar, designated_sessions=[w.omar], verified_session=w.omar)
    w.call('pending_deliveries')
    w.call('list', type='decision')

SCENARIOS = [plain_kinds, what_an_artifact_refuses, a_plan_is_an_artifact, an_approval_is_asked_and_answered,
             a_question_with_options_and_typed_answers, what_becomes_of_a_request_nobody_answers,
             the_inbox_and_what_a_user_does_with_an_artifact, what_names_a_published_file,
             a_report_and_its_revisions, a_form_and_its_answers, what_the_user_is_shown_in_updates,
             a_request_answered_on_the_users_behalf]


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        # The Python Host builds some objects from sets, whose fields then come in the order
        # of the run's hash seed. One seed, one order: the vectors change when the rules do.
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
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
