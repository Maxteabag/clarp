#!/usr/bin/env python3
"""Writes the vectors the Rust Goal keeper is tested against.

The Python Host's Goal keeper (`server/lib/goal_keeper.py`) is the reference: the janitor that
keeps a durable goal's books beside the agent that does the work. This builds a made-up world in
a database of its own (an owner with a goal, its helpers, what they said, their jobs, what was
done to the goal, how the owner's last turn ended), with a clock and an id source that are fixed,
and writes down for a row of moments what that module's own functions make of it: where the next
update reads from, what happened since, whether the goal has stalled, the text of what was said,
the sheet of facts, the whole prompt; what is made of a model's answer; when a goal is due and how
an update's end is settled. With each moment goes the world as the Rust Host is given it: the
rows, the jobs and the events themselves, read from the same database.

    python3 server-rs/tools/goal-keeper-vectors.py

Nothing of a live Host is touched: the library is pointed at a directory under /var/tmp that is
made new on every run. Every text is synthetic.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'goal-keeper-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-goal-keeper-vectors')
T0 = 1_794_700_000_000
MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000

GOAL = {'outcome': 'The thing is shipped', 'criteria': ['It runs', 'It is documented'], 'limits': 'No publication'}
ITEMS = [{'id': 'probe', 'title': 'Probe the behaviour'},
         {'id': 'build', 'title': 'Build it', 'detail': 'the whole of it', 'subtasks': [{'id': 'part', 'title': 'A part'}]},
         {'id': 'ship', 'title': 'Ship it'}]


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


def iso(at_ms: int) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(at_ms / 1000)) + f'.{at_ms % 1000:03d}Z'


class World:
    """One scenario's clock, ids, owner and goal."""

    def __init__(self, number: int):
        import secrets
        from lib import agents, clock, db
        self.db, self.agents = db, agents
        self.now = T0 + number * 40 * DAY
        self.ids = self.seq = self.jobs = 0
        for module in (clock, db):
            module.now_ms = lambda: self.now
        secrets.token_hex = self.token_hex
        db.reset_for_tests(SCRATCH / f'scenario-{number}.sqlite')
        self.number = number
        self.owner = self.agent('vera', 'Vera')
        self.plan, self.revision = '', 0
        self.steps: list[dict] = []

    def token_hex(self, nbytes: int = 4) -> str:
        self.ids += 1
        return format(self.ids, f'0{nbytes * 2}x')

    def agent(self, session: str, persona: str, *, parent: str = '', state: str | None = None, backend: str = 'claude',
              created: int | None = None, deleted: bool = False, archived: bool = False) -> str:
        """An agent with a name that is the same in every run; a helper where it has a parent."""
        real = self.agents.create_agent(persona=persona, voice_id='', cwd='/var/tmp', session=f'{session}{self.number}',
                                        backend=backend)
        agent_id = f'{self.number:02x}{session:0<14.14}'.replace('-', '0')
        con = self.db.conn()
        con.execute('UPDATE agents SET agent_id=? WHERE agent_id=?', (agent_id, real))
        for table in ('runtimes', 'state_log'):
            with contextlib.suppress(Exception):
                con.execute(f'UPDATE {table} SET agent_id=? WHERE agent_id=?', (agent_id, real))
        con.execute('UPDATE agents SET parent_agent_id=?, helper_state=?, created_at=?, deleted_at=?, archived_at=? WHERE agent_id=?',
                    (parent or None, state, created if created is not None else self.now,
                     self.now if deleted else None, self.now if archived else None, agent_id))
        con.commit()
        return agent_id

    def tool(self, *argv: str) -> dict:
        """One command of the goal tool, as the owner would give it."""
        import agent_tasks
        self.now += 1000
        filled = [a.replace('{plan}', self.plan).replace('{rev}', str(self.revision)).replace('{session}', f'vera{self.number}')
                  for a in argv]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = agent_tasks.main(['agent_tasks.py', *filled])
            except SystemExit as exc:
                code = int(exc.code or 0)
        printed = json.loads(out.getvalue().strip() or 'null')
        assert code == 0, (filled, err.getvalue())
        if isinstance(printed, dict) and 'plan_id' in printed and 'items' in printed:
            self.plan, self.revision = printed['plan_id'], printed['revision']
        return printed

    def user(self, action: str, data: dict | None = None) -> None:
        """What an app does to the goal for its user."""
        from lib import goal_ledger, task_goal_state
        self.now += 1000
        with goal_ledger.acting_as('user'):
            result = task_goal_state.mutate(self.plan, revision=self.revision, action=action, data=data or {})
        self.plan, self.revision = result['plan_id'], result['revision']

    def said(self, agent: str, role: str, text: str, *, at: int, phase: str | None = None, tools=None, origin: str = 'user',
             sender: str | None = None, written: int | None = None) -> str:
        self.seq += 1
        message_id = f'm-{self.seq:04d}'
        tools_json = tools if isinstance(tools, str) else json.dumps(tools or [])
        self.db.conn().execute(
            'INSERT INTO messages (message_id, agent_id, seq, role, timestamp, text, tools_json, updated_at, revision, '
            'display_cells_json, origin, sender_agent_id, phase) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (message_id, agent, self.seq, role, iso(at), text, tools_json, written if written is not None else at, self.seq,
             '[]', origin, sender, phase))
        self.db.conn().commit()
        return message_id

    def job(self, agent: str, title: str, *, started: int, status: str = 'running', ended: int | None = None, reason: str = '',
            kind: str = 'command') -> str:
        self.jobs += 1
        job_id = f'job-{self.jobs:03d}'
        self.db.conn().execute(
            'INSERT INTO background_jobs (job_id, agent_id, session, kind, title, detail, status, started_at, updated_at, '
            'metadata_json, heartbeat_timeout_ms, heartbeat_source, worker_start_token, terminal_at, terminal_reason, revision, '
            'generation, owner_kind, progress_text, log_path, worker_cwd, started_trace_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, agent, 's', kind, title, '', status, started, ended or started, '{}', 0, '', '', ended, reason, 1, 1,
             'agent', '', '', '', ''))
        self.db.conn().commit()
        return job_id

    def state(self, kind: str, *, at: int, reason: str | None = None, detail: str | None = None) -> None:
        """How a turn of the owner ended, as the Host's log of states has it."""
        said = detail if detail is not None else json.dumps({'reason': reason} if reason is not None else {})
        self.db.conn().execute('INSERT INTO state_log (agent_id, ts, kind, detail) VALUES (?,?,?,?)', (self.owner, at, kind, said))
        self.db.conn().commit()

    def continuation(self, fields: dict | None) -> None:
        """Writes what the goal waits for into its record, as the Host's own recovery would have."""
        con = self.db.conn()
        row = con.execute('SELECT goal_json FROM task_plans WHERE plan_id=?', (self.plan,)).fetchone()
        goal = json.loads(row['goal_json'])
        if fields is None:
            goal.pop('continuation', None)
        else:
            goal['continuation'] = {**(goal.get('continuation') or {}), **fields}
        con.execute('UPDATE task_plans SET goal_json=? WHERE plan_id=?', (json.dumps(goal), self.plan))
        con.commit()

    # ---- what the Rust Host is given, read from the same database ----

    def given(self, now: int, since_ms: int, since_event: int) -> dict:
        from lib import goal_keeper, task_plans
        con = self.db.conn()
        row = con.execute('SELECT * FROM task_plans WHERE plan_id=?', (self.plan,)).fetchone()
        owner = con.execute('SELECT * FROM agents WHERE agent_id=?', (self.owner,)).fetchone()
        helpers = goal_keeper._helpers(con, self.owner, int(row['created_at']))
        ids = [self.owner, *(h['agent_id'] for h in helpers)]
        marks = ','.join('?' * len(ids))
        rows = con.execute(
            f'SELECT message_id, agent_id, seq, role, timestamp, text, tools_json, updated_at, origin, sender_agent_id, phase '
            f'FROM messages WHERE agent_id IN ({marks}) AND updated_at > ? ORDER BY updated_at, seq', (*ids, since_ms)).fetchall()
        jobs = con.execute(f'SELECT job_id, agent_id, kind, title, status, started_at, terminal_at, terminal_reason '
                           f'FROM background_jobs WHERE agent_id IN ({marks}) ORDER BY started_at, job_id', ids).fetchall()
        events = con.execute('SELECT event_id, at, kind, delegation_id FROM goal_events WHERE plan_id=? AND event_id > ? '
                             'ORDER BY event_id', (self.plan, since_event)).fetchall()
        ended = con.execute('SELECT ts, kind, detail FROM state_log WHERE agent_id=? ORDER BY state_id DESC LIMIT 1',
                            (self.owner,)).fetchone()
        last = con.execute(f'SELECT MAX(updated_at) FROM messages WHERE agent_id IN ({marks})', ids).fetchone()[0]
        about = []
        for helper in helpers:
            report = con.execute(
                "SELECT text FROM messages WHERE agent_id=? AND sender_agent_id=? AND role='user' "
                'ORDER BY timestamp DESC, seq DESC LIMIT 1', (self.owner, helper['agent_id'])).fetchone()
            final = con.execute(
                "SELECT text FROM messages WHERE agent_id=? AND role='assistant' AND phase='final' "
                'ORDER BY timestamp DESC, seq DESC LIMIT 1', (helper['agent_id'],)).fetchone()
            about.append({'agent_id': helper['agent_id'], 'session': helper['session'], 'backend': helper['backend'],
                          'helper_state': helper['helper_state'], 'archived': helper['archived_at'] is not None,
                          'last_report': report['text'] if report else None, 'last_final': final['text'] if final else None})
        return {
            'now': now, 'since_ms': since_ms, 'since_event': since_event,
            'plan': task_plans.get(self.plan),
            'goal': {'status': row['status'], 'created_at': row['created_at'], 'revision': row['revision'],
                     'goal': json.loads(row['goal_json'] or '{}')},
            'owner': {'agent_id': self.owner, 'session': owner['session'], 'persona': owner['persona']},
            # Every agent the owner made, from which the keeper takes a goal's helpers.
            'children': [dict(child) for child in con.execute(
                'SELECT agent_id, created_at, deleted_at IS NOT NULL AS deleted FROM agents WHERE parent_agent_id=? '
                'ORDER BY created_at, agent_id', (self.owner,))],
            'helpers': about,
            'rows': [{**dict(r), 'tools_json': r['tools_json']} for r in rows],
            'jobs': [dict(j) for j in jobs], 'events': [dict(e) for e in events],
            'last_state': dict(ended) if ended else None, 'last_written': last,
        }

    def look(self, what: str, *, now: int | None = None, full: bool = False) -> dict:
        """One moment: the world as given, and what the keeper's own functions make of it."""
        from lib import goal_keeper
        now = self.now if now is None else now
        con = self.db.conn()
        row = con.execute('SELECT * FROM task_plans WHERE plan_id=?', (self.plan,)).fetchone()
        owner = con.execute('SELECT * FROM agents WHERE agent_id=?', (self.owner,)).fetchone()
        since_ms, since_event = goal_keeper.cursor(row, now, full=full)
        helpers = goal_keeper._helpers(con, self.owner, int(row['created_at']))
        sheet, names, job_refs = goal_keeper.fact_sheet(con, row, owner, helpers, now)
        ids = list(names)
        seen = goal_keeper.activity(con, row, ids, {h['agent_id'] for h in helpers}, since_ms, since_event)
        text, refs = goal_keeper.delta(con, ids, names, since_ms, max(now, seen.newest_ms))
        stalled = goal_keeper.stall(con, row, now)
        from lib import task_plans
        step = {
            'what': what, 'full': full, 'given': self.given(now, since_ms, since_event),
            'cursor': [since_ms, since_event],
            'cursor_whole': list(goal_keeper.cursor(row, now, full=True)),
            'needs_catch_up': goal_keeper._needs_catch_up(row, now),
            'activity': {'triggers': [list(t) for t in seen.triggers], 'status_change': seen.status_change,
                         'newest_ms': seen.newest_ms, 'newest_event': seen.newest_event, 'any_messages': seen.any_messages},
            'delta': {'text': text, 'refs': refs},
            'sheet': {'sheet': sheet, 'names': names, 'refs': job_refs},
            'stall': list(stalled) if stalled else None,
            'stall_line': goal_keeper.stall_line(stalled[0], stalled[1]) if stalled else '',
            'needs_stall_flag': goal_keeper._needs_stall_flag(con, row, now),
            'prompt': goal_keeper.prompt_for(task_plans.get(self.plan), sheet, text),
        }
        self.steps.append(step)
        return step


# ---- the scenarios ----------------------------------------------------------------------------

def j(value) -> str:
    return json.dumps(value)


LONG = 'L' * 1700


def a_goal_with_its_helpers_and_jobs(w: World) -> None:
    """A goal that was just made, then one whose owner and helpers worked: every kind of row."""
    old = w.agent('vera-old', 'Old helper', parent=w.owner, state='done', created=w.now - 3 * HOUR)
    w.tool('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    made = w.now
    w.look('a goal nobody has worked on yet')
    one = w.agent('vera-h1', 'Helper one', parent=w.owner, created=made + 10_000, backend='codex')
    two = w.agent('vera-h2', 'Helper two', parent=w.owner, state='done', created=made + 20_000)
    gone = w.agent('vera-gone', 'Gone helper', parent=w.owner, created=made + 30_000, deleted=True)
    kept = w.agent('vera-arch', 'Archived helper', parent=w.owner, created=made + 40_000, archived=True)
    other = w.agent('other', 'Somebody else')
    w.said(old, 'assistant', 'What the old helper said last.', at=made + 30_000, phase='final')
    w.said(gone, 'assistant', 'What the deleted helper said.', at=made + 40_000, phase='final')
    w.said(w.owner, 'user', '  Start with the probe.  ', at=made + MINUTE)
    w.said(w.owner, 'assistant', 'Starting the probe.', at=made + 2 * MINUTE, phase='commentary',
           tools=[{'name': 'Bash', 'summary': 'Running the probe'}, {'name': 'Read', 'file_path': '/srv/app/README.md'},
                  {'description': 'a tool with no name ' + 'd' * 200}, 'not a tool', {'name': 'Bare'}])
    w.said(w.owner, 'assistant', 'Probe done: 3 of 3 checks pass. PR #12 is opened.', at=made + 5 * MINUTE, phase='final')
    w.said(one, 'user', 'Build the part.', at=made + 6 * MINUTE, origin='agent', sender=w.owner)
    w.said(one, 'assistant', '', at=made + 7 * MINUTE, tools=[{'name': 'Edit', 'summary': 'part.rs'}])
    w.said(one, 'assistant', 'Part built, commit abc123.', at=made + 8 * MINUTE, phase='final')
    w.said(w.owner, 'user', 'Report: the part is built (abc123).', at=made + 9 * MINUTE, origin='agent', sender=one)
    w.said(w.owner, 'user', 'A word from somebody else.', at=made + 10 * MINUTE, origin='agent', sender=other)
    w.said(w.owner, 'user', 'From nobody known.', at=made + 11 * MINUTE, origin='agent', sender='no-such-agent')
    w.said(w.owner, 'user', 'With no origin written.', at=made + 12 * MINUTE, origin='')
    w.said(w.owner, 'assistant', LONG, at=made + 13 * MINUTE)
    w.said(w.owner, 'user', LONG, at=made + 14 * MINUTE)
    w.said(w.owner, 'assistant', '   ', at=made + 15 * MINUTE)
    w.said(w.owner, 'assistant', 'Its tools are not written as a list.', at=made + 16 * MINUTE, tools='{"not": "a list"}')
    w.said(w.owner, 'assistant', 'Its tools are no JSON.', at=made + 17 * MINUTE, tools='not json')
    w.said(two, 'assistant', 'Helper two is done: docs written.', at=made + 18 * MINUTE, phase='final')
    w.said(w.owner, 'user', 'Second report of helper one.', at=made + 19 * MINUTE, origin='agent', sender=one)
    w.said(kept, 'assistant', 'The archived helper finished too.', at=made + 20 * MINUTE, phase='final')
    w.said(w.owner, 'assistant', 'Written late, said early.', at=made + 3 * MINUTE, written=made + 21 * MINUTE, phase='final')
    w.job(w.owner, 'A job that still runs, with a title that is a good deal longer than the eighty signs a sheet shows of it',
          started=made + 2 * MINUTE)
    w.job(w.owner, 'Finished well', started=made + 3 * MINUTE, status='completed', ended=made + 4 * MINUTE)
    w.job(one, 'Failed with a reason', started=made + 4 * MINUTE, status='failed', ended=made + 5 * MINUTE, reason='exit 2')
    w.job(w.owner, 'The watcher of a helper', started=made + 5 * MINUTE, status='failed', ended=made + 6 * MINUTE,
          reason='worker_vanished', kind='sub-agent')
    w.job(other, 'A job of somebody else', started=made + 5 * MINUTE)
    w.job(w.owner, 'Before the goal', started=made - DAY, status='completed', ended=made - DAY + MINUTE)
    w.now = made + 25 * MINUTE
    w.look('after the owner and its helpers worked')
    w.look('the same read whole, from the goal\'s own start', full=True)
    w.user('pause', {'reason': 'Waiting for a review'})
    w.user('resume', {'reason': 'Reviewed'})
    w.now += MINUTE
    w.look('after the user paused it and resumed it')
    w.tool('step', '{plan}', '{rev}', 'probe', 'in_progress')
    w.tool('checkpoint', '{plan}', '{rev}', j({'progress': 'Probe under way', 'next_work': 'Build it'}))
    w.now += MINUTE
    w.look('after the owner wrote a checkpoint of its own: read from there')
    for n in range(12):
        w.job(w.owner, f'One of many jobs {n}', started=w.now + n * 1000, status='completed', ended=w.now + n * 1000 + 500)
    for n in range(9):
        w.agent(f'vera-m{n}', f'Many {n}', parent=w.owner, created=w.now + n * 1000)
    w.now += MINUTE
    w.look('with more jobs and more helpers than a sheet holds')


def a_goal_that_stalls(w: World) -> None:
    """When an active goal counts as stalled, by what the database holds alone."""
    w.tool('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    made = w.now
    w.said(w.owner, 'assistant', 'Working on it.', at=made + MINUTE, phase='final')
    w.now = made + 2 * DAY
    w.look('quiet for two days: not yet')
    w.now = made + 3 * DAY + MINUTE
    w.look('quiet for three days: stalled, idle', now=made + 3 * DAY + MINUTE + 59_999)
    job = w.job(w.owner, 'Still running', started=made + 2 * MINUTE)
    w.look('a job of the owner still runs: it waits')
    w.db.conn().execute("UPDATE background_jobs SET status='queued' WHERE job_id=?", (job,))
    w.db.conn().commit()
    w.look('a job that is queued: it waits too')
    w.db.conn().execute("UPDATE background_jobs SET status='completed', terminal_at=? WHERE job_id=?", (made + 3 * DAY, job))
    w.db.conn().commit()
    w.look('the job ended a minute ago: the quiet counts from there')
    w.now = made + 6 * DAY + 2 * MINUTE
    w.look('three days after the job ended: stalled')
    w.user('pause', {'reason': 'Holiday'})
    w.look('a paused goal is never stalled')
    w.user('resume', {'reason': 'Reviewed'})
    w.said(w.owner, 'assistant', 'Back.', at=w.now, phase='final')
    w.state('interrupted', at=w.now + MINUTE, reason='usage_limit')
    w.now += 30 * MINUTE
    w.look('the owner\'s turn stopped on a usage limit half an hour ago: not yet')
    w.now += 31 * MINUTE
    w.look('an hour ago: stalled on a usage limit')
    for reason in ('account_plan', 'auth', 'transient', 'connection', 'runner_exit', 'timeout', 'unknown', 'user_stop', ''):
        w.state('interrupted', at=w.now - 2 * HOUR, reason=reason)
        w.look(f'stopped two hours ago, reason {reason!r}')
    w.state('interrupted', at=w.now - 2 * HOUR, detail='not json')
    w.look('stopped with a detail that is no JSON')
    w.state('interrupted', at=w.now - 2 * HOUR, detail='null')
    w.look('stopped with no detail')
    w.state('done', at=w.now - 2 * HOUR, reason='usage_limit')
    w.look('the last state is no stop')
    w.state('interrupted', at=w.now - 2 * HOUR, reason='usage_limit')
    w.tool('checkpoint', '{plan}', '{rev}', j({
        'progress': 'Sent for review', 'next_work': 'Read the review',
        'continuation': {'kind': 'dependency', 'key': 'review-1', 'reason': 'Waiting for the reviewer'}}))
    w.look('it waits for something, with no deadline: no stall')
    w.now += 4 * DAY
    w.look('four days later it still waits: no stall')
    w.tool('dependency', '{plan}', '{rev}', j({'key': 'review-1', 'outcome': 'succeeded', 'evidence': 'review read'}))
    w.look('what it waited for came: the quiet counts again')
    w.tool('checkpoint', '{plan}', '{rev}', j({
        'progress': 'Build launched', 'next_work': 'Inspect its result',
        'continuation': {'kind': 'dependency', 'key': 'build-1', 'reason': 'Waiting for build', 'due_at': w.now + HOUR}}))
    w.look('it waits for a build that is due in an hour: no stall')
    w.now += 2 * HOUR
    w.look('past that hour it still waits: what it waits for has no time of its own in its continuation')
    w.tool('dependency', '{plan}', '{rev}', j({'key': 'build-1', 'outcome': 'succeeded', 'evidence': 'built'}))
    w.tool('checkpoint', '{plan}', '{rev}', j({
        'progress': 'Built', 'next_work': 'Look again in an hour', 'continuation': {'kind': 'timer', 'due_at': w.now + HOUR}}))
    w.look('a timer nobody has taken up has no time in its continuation: the quiet counts')
    # As the Host's own recovery writes a continuation that it has taken up: with the time it is due.
    for what, fields in (
            ('due in an hour: it waits, no stall', {'state': 'scheduled', 'due_at': w.now + HOUR}),
            ('due a second ago: no longer a wait', {'state': 'scheduled', 'due_at': w.now - 1000}),
            ('waiting, and due in an hour: no stall', {'state': 'waiting', 'due_at': w.now + HOUR}),
            ('waiting, and overdue: no longer a wait', {'state': 'waiting', 'due_at': w.now - 1000}),
            ('waiting with a time that is no number: a wait with no deadline', {'state': 'waiting', 'due_at': 'soon'}),
            ('waiting with the time 0: a wait with no deadline', {'state': 'waiting', 'due_at': 0}),
            ('due at a time written as a fraction, to come', {'state': 'scheduled', 'due_at': w.now + 0.5}),
            ('no continuation at all', None)):
        w.continuation(fields)
        w.look(what)


def what_is_made_of_an_answer(w: World) -> None:
    """What the keeper takes from what a model answered."""
    from lib import goal_keeper, task_plans
    w.tool('create', '{session}', 'ship', 'Ship the thing', j(ITEMS), j(GOAL))
    plan = task_plans.get(w.plan)
    answers = [
        {'steps': {'probe': {'status': 'completed', 'evidence': 'PR #12 merged', 'note': 'three checks'},
                   'build': {'status': 'In Progress', 'note': '  half way  '},
                   'part': {'status': 'completed', 'evidence': '   ', 'note': 'looks done'},
                   'ship': 'blocked', 'unknown-step': {'status': 'completed', 'evidence': 'x'}},
         'checkpoint': {'progress': 'Probe merged.', 'next_work': 'Build.'}, 'changed': True},
        {'steps': {'probe': {'status': 'completed', 'evidence': 'done', 'note': ''}, 'build': 7, 'part': None,
                   'ship': {'status': None, 'note': None, 'evidence': None}},
         'checkpoint': {'progress': 7, 'next_work': None}},
        {'steps': {'probe': {'status': 'completed'}, 'part': {'status': ' COMPLETED ', 'evidence': 0, 'note': 0}},
         'checkpoint': None},
        {'steps': None, 'checkpoint': {'progress': '', 'next_work': 'x'}},
        {},
        {'steps': {w.plan + ':probe': {'status': 'completed', 'evidence': 'a whole id is no short one'}},
         'checkpoint': {'progress': 'p'}},
    ]
    for answer in answers:
        steps, progress, next_work = goal_keeper.decide(plan, answer)
        w.steps.append({'what': 'decide', 'plan': plan, 'answer': answer,
                        'decided': {'steps': steps, 'progress': progress, 'next_work': next_work}})


SCENARIOS = [a_goal_with_its_helpers_and_jobs, a_goal_that_stalls, what_is_made_of_an_answer]

# What the attachments of a keeper say it watches.
CONFIGS = [
    {},
    {'attachments': None},
    {'attachments': [
        {'attachment_id': 'a1', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'agent-work-completed',
         'config': {'coalesce_seconds': 120}},
        {'attachment_id': 'a2', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'goal-activity-observed', 'config': None},
        {'attachment_id': 'a3', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'schedule',
         'config': {'cron': '*/30 * * * *', 'timezone': 'Europe/Oslo'}}]},
    {'attachments': [
        {'attachment_id': 'b1', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'agent-work-completed'},
        {'attachment_id': 'b2', 'enabled': False, 'trigger_version': 1, 'trigger_id': 'goal-activity-observed'},
        {'attachment_id': 'b3', 'enabled': True, 'trigger_version': 2, 'trigger_id': 'schedule', 'config': {'cron': '0 * * * *'}},
        {'attachment_id': 'b4', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'schedule', 'config': {'cron': '0 4 * * *'}},
        {'attachment_id': 'b5', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'something-else'}]},
    {'attachments': [
        {'attachment_id': 'c1', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'schedule', 'config': {}},
        {'attachment_id': 'c2', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'agent-work-completed',
         'config': {'coalesce_seconds': '45'}},
        {'attachment_id': 'c3', 'enabled': True, 'trigger_version': 1, 'trigger_id': 'agent-work-completed',
         'config': {'coalesce_seconds': 0}}]},
]


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    isolate()
    from lib import goal_keeper
    scenarios = []
    for number, scenario in enumerate(SCENARIOS, start=1):
        world = World(number)
        scenario(world)
        scenarios.append({'name': scenario.__name__, 'what': scenario.__doc__, 'steps': world.steps})
    watched = []
    for config in CONFIGS:
        found = goal_keeper.triggers(config)
        watched.append({'config': config, 'watch': {
            'owner_turns': found.owner_turns, 'goal_activity': found.goal_activity, 'fold_ms': found.fold_ms,
            'sweep': found.sweep, 'progress_attachment': found.progress_attachment}})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/goal-keeper-vectors.py from server/lib/goal_keeper.py. Do not edit by hand.',
               'system': goal_keeper.SYSTEM, 'scenarios': scenarios, 'watched': watched}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    looks = sum(1 for s in scenarios for step in s['steps'] if 'given' in step)
    decided = sum(1 for s in scenarios for step in s['steps'] if step['what'] == 'decide')
    stalled = sum(1 for s in scenarios for step in s['steps'] if step.get('stall'))
    print(f'{OUT.relative_to(REPO)}: {len(scenarios)} scenarios, {looks} looks ({stalled} stalled), {decided} answers decided, '
          f'{len(watched)} configurations, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
