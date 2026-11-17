#!/usr/bin/env python3
"""Writes the vectors the Rust Host's runs of a scheduled janitor are tested against.

A janitor with a scheduled task runs when its schedule says: one run for each time that comes due,
a turn in the janitor's own conversation. The Python Host admits those runs in
`server/lib/janitor_runner.py` (`JanitorRunner._scheduled_tick`) and keeps them in
`server/lib/janitors.py`: what is due, the run and what it is given, whether the Host took its
turn, how the turn ended, what the run said of itself (`worked`, `idle`, `failed`), and from that
when the task runs next. This drives that runner itself, look by look, on a database of its own,
with a clock, an ending of the turn and an answer of the Host's own queue that the story sets, and
writes what the Python Host kept after every step to
`crates/clarp-kernel/tests/fixtures/janitor-run-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/janitor-run-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, and no turn of any tool runs: the runner is handed
a queue that only writes down what it was given.
"""
from __future__ import annotations

import calendar
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-run-vectors.json'

S, MIN, HOUR = 1000, 60_000, 3_600_000
AGENTS = [('Worker', 'worker', 'codex'), ('Second', 'second', 'codex'), ('Daily', 'daily', 'claude'),
          ('Other', 'other', 'codex'), ('Week', 'week', 'codex'), ('Week Two', 'week-two', 'codex')]


def at(year, month, day, hour=0, minute=0, second=0):
    """A moment in UTC, in milliseconds."""
    return calendar.timegm((year, month, day, hour, minute, second)) * 1000


LONG = 'x' * 301
LEAK = ('{"error": "the tool said sk-abc_DEF-123 and Authorization: Bearer abc.def.ghi, then '
        '-----BEGIN OPENSSH PRIVATE KEY-----\\nb3BlbnNzaC1rZXk\\n-----END OPENSSH PRIVATE KEY----- and stopped"}')
REDACTIONS = [
    '', 'nothing to hide', 'sk-abc123', 'a sk-abc_DEF-123, b', 'task-sk-abc', 'risk-abc', 'sk-', 'sk- abc', 'SK-abc',
    'Authorization: Bearer abc.def', 'authorization=bearer  abc;def', '"Authorization": "Bearer abc"', "Authorization':'Bearer a,b",
    'Authorization: Bearer [REDACTED TOKEN]', 'Authorization: Basic abc', 'XAuthorization: Bearer abc', 'Authorization Bearer abc',
    'Authorization:Bearer', 'Authorization: Bearer <abc>',
    '-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY----- after', 'before -----BEGIN RSA PRIVATE KEY-----\nabc and no end',
    '-----BEGIN EC PRIVATE KEY-----a-----END RSA PRIVATE KEY-----b-----END EC PRIVATE KEY-----',
    '-----BEGIN rsa PRIVATE KEY-----abc-----END rsa PRIVATE KEY-----', '-----BEGIN  PRIVATE KEY-----abc',
    '-----BEGIN A B C PRIVATE KEY-----x-----END PRIVATE KEY-----y sk-k1 -----BEGIN PRIVATE KEY-----z',
    'ask-me sk-1 sk-2', 'Authorization: Bearer sk-abc', 'ä sk-ö1 Authorization: Bearer ü',
]


def stories(base):
    """The lives that are told. `base` is the first moment; every step says its own.

    A step is an asking of the book (`create`, `configure`, `enabled`, `remove`, `release`), a look of
    the runner (`tick`: with how the run's turn ended, if it has, and what the Host's queue answers
    when it is handed a run), or something a run or its turn does (`report`, `started`, `context`,
    `valid`, `runs`). `{run}` stands for the run that was handed over last, `{old}` for the one
    before it.
    """
    half = {'attachment_id': 'half-hourly', 'trigger_id': 'schedule', 'config': {'cron': '*/30 * * * *', 'timezone': 'UTC'}}
    done, failed = {'kind': 'done', 'detail': '{}'}, {'kind': 'error', 'detail': LEAK}
    on, off = {'enabled': True}, {'enabled': False}
    t = base  # 2027-03-26 10:07:00 UTC
    first = [
        ('make', t, 'create', 'worker', {'template_id': 'scheduled-prompt', 'attachments': [half],
                                          'options': {'instructions': '  Sweep the yard.\nThen say what you found.  ',
                                                      'max_interval_seconds': 7200}}),
        ('a look while it is off', t + 1 * S, 'tick', {}),
        ('turn on', t + 2 * S, 'enabled', 'worker', {'expected_revision': 1, **on}),
        ('the first look: when it runs next', t + 4 * S, 'tick', {}),
        ('a look that changes nothing', t + 6 * S, 'tick', {}),
        ('a look a moment before it is due', at(2027, 3, 26, 10, 29, 59), 'tick', {}),
        ('due: a run is made and handed over', at(2027, 3, 26, 10, 30, 1), 'tick', {'delivery': 'accepted'}),
        ('a look while its turn waits', at(2027, 3, 26, 10, 30, 3), 'tick', {}),
        ('the run is read by its turn', at(2027, 3, 26, 10, 30, 4), 'context', {'run': '{run}'}),
        ('the Host asks whether the turn may start', at(2027, 3, 26, 10, 30, 4), 'valid', {'session': 'worker', 'run': '{run}', 'trace': '{run}'}),
        ('asked of another session', at(2027, 3, 26, 10, 30, 4), 'valid', {'session': 'second', 'run': '{run}', 'trace': '{run}'}),
        ('asked under another trace', at(2027, 3, 26, 10, 30, 4), 'valid', {'session': 'worker', 'run': '{run}', 'trace': 'janitor-other'}),
        ('its turn starts', at(2027, 3, 26, 10, 30, 5), 'started', {'run': '{run}'}),
        ('it starts once more', at(2027, 3, 26, 10, 30, 9), 'started', {'run': '{run}'}),
        ('a look while its turn runs', at(2027, 3, 26, 10, 30, 11), 'tick', {}),
        ('it says something that is no ending', at(2027, 3, 26, 10, 31), 'report', {'run': '{run}', 'activity': 'napped', 'summary': 'zzz'}),
        ('it says too much', at(2027, 3, 26, 10, 31), 'report', {'run': '{run}', 'activity': 'idle', 'summary': LONG}),
        ('it says it found nothing', at(2027, 3, 26, 10, 31), 'report', {'run': '{run}', 'activity': 'idle', 'summary': '  nothing \n to   sweep '}),
        ('a run nobody has says something', at(2027, 3, 26, 10, 31), 'report', {'run': 'janitor-nobody', 'activity': 'idle', 'summary': ''}),
        ('the turn ended: the run is done, and the wait is doubled', at(2027, 3, 26, 10, 31, 30), 'tick', {'ended': done}),
        ('it says something after it ended', at(2027, 3, 26, 10, 31, 40), 'report', {'run': '{run}', 'activity': 'worked', 'summary': 'late'}),
        ('read after it ended', at(2027, 3, 26, 10, 31, 40), 'context', {'run': '{run}'}),
        ('started after it ended', at(2027, 3, 26, 10, 31, 40), 'started', {'run': '{run}'}),
        ('asked after it ended', at(2027, 3, 26, 10, 31, 40), 'valid', {'session': 'worker', 'run': '{run}', 'trace': '{run}'}),
        ('the half hour it now skips', at(2027, 3, 26, 11, 0, 1), 'tick', {}),
        ('due: the queue does not take it', at(2027, 3, 26, 11, 30, 2), 'tick', {'delivery': 'refused'}),
        ('a look before it is offered again', at(2027, 3, 26, 11, 30, 20), 'tick', {'delivery': 'accepted'}),
        ('offered again: the queue breaks', at(2027, 3, 26, 11, 30, 33), 'tick', {'delivery': 'broke'}),
        ('offered a third time: taken', at(2027, 3, 26, 11, 31, 4), 'tick', {'delivery': 'accepted'}),
        ('the turn ended and the run said nothing: it counts as work', at(2027, 3, 26, 11, 33), 'tick', {'ended': done}),
        ('due at the next half hour again', at(2027, 3, 26, 12, 0, 0), 'tick', {'delivery': 'accepted'}),
        ('it says it worked', at(2027, 3, 26, 12, 1), 'report', {'run': '{run}', 'activity': 'worked', 'summary': 'swept'}),
        ('it says it failed, which is what holds', at(2027, 3, 26, 12, 2), 'report', {'run': '{run}', 'activity': 'failed', 'summary': 'no broom'}),
        ('the turn ended: the wait stays', at(2027, 3, 26, 12, 3), 'tick', {'ended': {'kind': 'idle', 'detail': ''}}),
        ('due: a run whose turn fails', at(2027, 3, 26, 12, 30, 0), 'tick', {'delivery': 'accepted'}),
        ('it said it worked before its turn failed', at(2027, 3, 26, 12, 30, 30), 'report', {'run': '{run}', 'activity': 'worked', 'summary': 'half'}),
        ('the turn failed: the run is an error, told without what is secret', at(2027, 3, 26, 12, 31), 'tick', {'ended': failed}),
        ('read it', at(2027, 3, 26, 12, 31, 1), 'get', 'worker', {}),
        ('due: idle', at(2027, 3, 26, 13, 0, 0), 'tick', {'delivery': 'accepted'}),
        ('nothing', at(2027, 3, 26, 13, 0, 30), 'report', {'run': '{run}', 'activity': 'idle', 'summary': ''}),
        ('ended', at(2027, 3, 26, 13, 1), 'tick', {'ended': done}),
        ('due after an hour: idle again', at(2027, 3, 26, 14, 0, 0), 'tick', {'delivery': 'accepted'}),
        ('nothing again', at(2027, 3, 26, 14, 0, 30), 'report', {'run': '{run}', 'activity': 'idle', 'summary': 'still nothing'}),
        ('ended: two hours is its longest', at(2027, 3, 26, 14, 1), 'tick', {'ended': done}),
        ('due after two hours: idle a third time', at(2027, 3, 26, 16, 0, 0), 'tick', {'delivery': 'accepted'}),
        ('nothing a third time', at(2027, 3, 26, 16, 0, 30), 'report', {'run': '{run}', 'activity': 'idle', 'summary': ''}),
        ('ended: it waits no longer than its longest', at(2027, 3, 26, 16, 1), 'tick', {'ended': done}),
        ('due: a run whose turn never ends', at(2027, 3, 26, 18, 0, 0), 'tick', {'delivery': 'accepted'}),
        ('an hour and fifty-nine minutes on', at(2027, 3, 26, 19, 59, 59), 'tick', {}),
        ('two hours on: the run is given up', at(2027, 3, 26, 20, 0, 1), 'tick', {}),
        ('its runs, the newest three', at(2027, 3, 26, 20, 0, 2), 'runs', 'worker', {'limit': 3}),
        ('its runs, asked for none', at(2027, 3, 26, 20, 0, 2), 'runs', 'worker', {'limit': 0}),
        ('the runs of an agent nobody has', at(2027, 3, 26, 20, 0, 2), 'runs', 'nobody', {'limit': 3}),
        ('paused at the end', at(2027, 3, 26, 20, 0, 3), 'enabled', 'worker', {'expected_revision': 2, **off}),
    ]
    t = at(2027, 3, 26, 10, 7)
    # Two triggers of one janitor, made together. The runner looks at triggers in the order they were
    # made; these are named so that the order of their names is that order too.
    quarter = {'attachment_id': 'by-the-quarter', 'trigger_id': 'schedule', 'config': {'cron': '*/15 * * * *', 'timezone': 'UTC'}}
    hourly = {'attachment_id': 'on-the-hour', 'trigger_id': 'schedule', 'config': {'cron': '0 * * * *', 'timezone': 'UTC'}}
    second = [
        ('make with two schedules', t, 'create', 'second', {'template_id': 'scheduled-prompt', 'attachments': [quarter, hourly],
                                                            'options': {'instructions': 'Count the doors.'}}),
        ('turn on', t + 1 * S, 'enabled', 'second', {'expected_revision': 1, **on}),
        ('the first look', t + 2 * S, 'tick', {}),
        ('a quarter past: one is due', at(2027, 3, 26, 10, 15, 0), 'tick', {'delivery': 'accepted'}),
        ('paused while its run is out', at(2027, 3, 26, 10, 16), 'enabled', 'second', {'expected_revision': 2, **off}),
        ('a look while it is off', at(2027, 3, 26, 10, 16, 2), 'tick', {}),
        ('the run that was cut off says something', at(2027, 3, 26, 10, 16, 3), 'report', {'run': '{run}', 'activity': 'worked', 'summary': 'x'}),
        ('the Host asks whether its turn may start', at(2027, 3, 26, 10, 16, 3), 'valid', {'session': 'second', 'run': '{run}', 'trace': '{run}'}),
        ('turn on again', at(2027, 3, 26, 10, 20), 'enabled', 'second', {'expected_revision': 3, **on}),
        ('the first look of its new life', at(2027, 3, 26, 10, 20, 2), 'tick', {}),
        ('half past: a run of the new life', at(2027, 3, 26, 10, 30, 0), 'tick', {'delivery': 'accepted'}),
        ('set anew while its run is out', at(2027, 3, 26, 10, 31), 'configure', 'second', {'expected_revision': 4, 'options': {'instructions': 'Count the windows.'}}),
        ('the run that was cut off is read', at(2027, 3, 26, 10, 31, 1), 'context', {'run': '{run}'}),
        ('turn on as set', at(2027, 3, 26, 10, 40), 'enabled', 'second', {'expected_revision': 5, **on}),
        ('the first look as set', at(2027, 3, 26, 10, 40, 2), 'tick', {}),
        ('a quarter to: one is due', at(2027, 3, 26, 10, 45, 0), 'tick', {'delivery': 'accepted'}),
        ('the hour: the other is due while the first is out', at(2027, 3, 26, 11, 0, 0), 'tick', {}),
        ('the first ended: only then the other runs', at(2027, 3, 26, 11, 2), 'tick', {'ended': done}),
        ('the next look: the other is handed over', at(2027, 3, 26, 11, 2, 2), 'tick', {'delivery': 'accepted'}),
        ('the other said it worked', at(2027, 3, 26, 11, 3), 'report', {'run': '{run}', 'activity': 'worked', 'summary': 'nine doors'}),
        ('the other ended', at(2027, 3, 26, 11, 4), 'tick', {'ended': done}),
        ('a look at a quarter past', at(2027, 3, 26, 11, 15, 0), 'tick', {'delivery': 'accepted'}),
        ('one schedule is taken away while its run is out', at(2027, 3, 26, 11, 16), 'configure', 'second', {'expected_revision': 6, 'attachments': [hourly]}),
        ('turn on with one schedule', at(2027, 3, 26, 11, 17), 'enabled', 'second', {'expected_revision': 7, **on}),
        ('the first look with one', at(2027, 3, 26, 11, 17, 2), 'tick', {}),
        ('the hour', at(2027, 3, 26, 12, 0, 0), 'tick', {'delivery': 'accepted'}),
        ('removed while its run is out', at(2027, 3, 26, 12, 1), 'remove', 'second', {'expected_revision': 8}),
        ('a look after it was removed', at(2027, 3, 26, 12, 1, 2), 'tick', {}),
        ('its runs', at(2027, 3, 26, 12, 1, 3), 'runs', 'second', {'limit': 30}),
    ]
    # Schedules whose runs are not evenly apart, each with a longest wait of nothing: a task that keeps
    # its pace. Nothing is said by any run, which counts as work.
    def paced(session, attachment, made, days):
        """One life: made and turned on at `made`; then for each of `days` a look a second after
        the moment, and, where a run was handed over, the end of its turn two minutes on."""
        steps = [
            ('make', made, 'create', session, {'template_id': 'scheduled-prompt', 'attachments': [attachment],
                                               'options': {'instructions': 'Open the shop.', 'max_interval_seconds': 0}}),
            ('turn on', made + 1 * S, 'enabled', session, {'expected_revision': 1, **on}),
            ('the first look', made + 2 * S, 'tick', {}),
        ]
        for what, moment in days:
            # `named`: whether the schedule names this moment. A task that keeps its pace runs at each.
            named_now = not what.endswith(('at ten', 'there is none'))
            steps.append((what, moment + 1 * S, 'tick', {'delivery': 'accepted', 'named': named_now}))
            steps.append((what + ': the end of its turn, if it ran', moment + 2 * MIN, 'tick', {'ended': done}))
        steps.append(('paused at the end', days[-1][1] + 3 * MIN, 'enabled', session, {'expected_revision': 2, **off}))
        return steps
    nine = {'attachment_id': 'at-nine', 'trigger_id': 'schedule', 'config': {'cron': '0 9 * * *', 'timezone': 'Europe/Oslo'}}
    # Every morning at nine by Oslo's clocks, over the night they are put forward (28 March 2027:
    # nine comes 23 hours after nine).
    spring = paced('daily', nine, at(2027, 3, 24, 7, 0), [
        ('Wednesday at nine', at(2027, 3, 24, 8)), ('Thursday at nine', at(2027, 3, 25, 8)), ('Friday at nine', at(2027, 3, 26, 8)),
        ('Saturday at nine', at(2027, 3, 27, 8)), ('Sunday at nine, an hour earlier by the sun', at(2027, 3, 28, 7)),
        ('Sunday at ten', at(2027, 3, 28, 8)), ('Monday at nine', at(2027, 3, 29, 7)), ('Tuesday at nine', at(2027, 3, 30, 7))])
    # The same over the night they are put back (31 October 2027: nine comes 25 hours after nine),
    # by a task that first ran two days before.
    late = dict(nine, attachment_id='at-nine-in-autumn')
    autumn = paced('other', late, at(2027, 10, 29, 6, 0), [
        ('Friday at nine', at(2027, 10, 29, 7)), ('Saturday at nine', at(2027, 10, 30, 7)),
        ('Sunday at nine, an hour later by the sun', at(2027, 10, 31, 8)), ('Monday at nine', at(2027, 11, 1, 8)),
        ('Tuesday at nine', at(2027, 11, 2, 8)), ('Wednesday at nine', at(2027, 11, 3, 8)), ('Thursday at nine', at(2027, 11, 4, 8))])
    # Nine on working days, by a task that first ran on a Thursday: the two runs after it are
    # three days apart.
    working = {'attachment_id': 'working-days', 'trigger_id': 'schedule', 'config': {'cron': '0 9 * * 1-5', 'timezone': 'UTC'}}
    week = paced('week', working, at(2027, 3, 24, 12, 0), [
        ('Thursday', at(2027, 3, 25, 9)), ('Friday', at(2027, 3, 26, 9)), ('Monday', at(2027, 3, 29, 9)),
        ('Tuesday', at(2027, 3, 30, 9)), ('Wednesday', at(2027, 3, 31, 9)), ('Thursday again', at(2027, 4, 1, 9)),
        ('Friday again', at(2027, 4, 2, 9)), ('Monday again', at(2027, 4, 5, 9))])
    # The same by a task that first ran on a Monday.
    monday = dict(working, attachment_id='working-days-from-monday')
    week_two = paced('week-two', monday, at(2027, 3, 21, 12, 0), [
        ('Monday', at(2027, 3, 22, 9)), ('Tuesday', at(2027, 3, 23, 9)), ('Friday', at(2027, 3, 26, 9)),
        ('Saturday, when there is none', at(2027, 3, 27, 9)), ('Monday after', at(2027, 3, 29, 9))])
    return [('a task of every half hour: idle, at work, failing, given up', first, False),
            ('two schedules of one janitor, paused, set anew and removed while a run is out', second, False),
            ('every morning at nine, over the night the clocks are put forward', spring, True),
            ('every morning at nine, over the night the clocks are put back', autumn, True),
            ('nine on working days, first run on a Thursday', week, True),
            ('nine on working days, first run on a Monday', week_two, True)]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's runner makes of each step."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-janitor-run-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, janitors
    from lib.janitor_context import redact
    from lib.janitor_runner import RECHECK_MS, RUN_TTL_MS, JanitorRunner

    base = at(2027, 3, 26, 10, 7)
    clock = {'now': base}
    db.now_ms = lambda: clock['now']
    work = root / 'work'
    work.mkdir()
    ids = {session: agents.create_agent(persona=persona, voice_id='', cwd=str(work), session=session, backend=backend)
           for persona, session, backend in AGENTS}

    def named(value):
        text = json.dumps(value)
        for session, agent_id in ids.items():
            text = text.replace(agent_id, f'id-{session}')
        return json.loads(text.replace(str(work), '/srv/work'))

    def attempt(call):
        try:
            return {'taken': named(call())}
        except janitors.JanitorError as refused:
            return {'refused': [refused.status, refused.code, named(str(refused))]}

    class Source:
        """What the runner reads of the Host: here, only how the turn of a run ended."""
        ended = None

        def terminal(self, run):
            return self.ended

        def application_active(self, idle_timeout_seconds):
            return True

    handed: list = []
    queue = {'answer': 'accepted'}

    def dispatch(run, prompt):
        handed.append([run['run_id'], run['session'], prompt])
        if queue['answer'] == 'broke':
            raise RuntimeError('the queue is down')
        return {'ok': queue['answer'] == 'accepted', 'queued': False}

    source = Source()
    runner = JanitorRunner(dispatch, source=source, clock=lambda: clock['now'])

    def shown(session):
        """A janitor as it is read, without its picture (told elsewhere)."""
        row = janitors.get(session)
        row.pop('avatar_url')
        return row

    told = []
    for name, steps, paced in stories(base):
        sessions: list = []
        last = {'run': '', 'old': ''}
        rows = []
        for what, now, ask, *rest in steps:
            clock['now'] = now
            of, given = (rest[0], rest[1]) if len(rest) == 2 else ('', rest[0])
            given = json.loads(json.dumps(given).replace('{run}', last['run']).replace('{old}', last['old']))
            row = {'what': what, 'now': now, 'ask': ask, 'of': of, 'given': given}
            if of and of in ids and of not in sessions:
                sessions.append(of)
            if ask == 'tick':
                source.ended = given.get('ended')
                queue['answer'] = given.get('delivery', 'accepted')
                del handed[:]
                row['count'] = runner.tick()
                row['handed'] = named(list(handed))
                if handed:
                    if handed[-1][0] != last['run']:
                        last['old'], last['run'] = last['run'], handed[-1][0]
            else:
                args = dict(given)
                calls = {
                    'get': lambda: shown(of),
                    'create': lambda: (janitors.create(of, **args), shown(of))[1],
                    'configure': lambda: (janitors.configure(of, args.pop('expected_revision'), **args), shown(of))[1],
                    'enabled': lambda: (janitors.set_enabled(of, args['expected_revision'], args['enabled']), shown(of))[1],
                    'remove': lambda: janitors.remove(of, args['expected_revision']),
                    'release': lambda: (janitors.release(of, args.pop('expected_revision'), **args), shown(of))[1],
                    'report': lambda: janitors.report_activity(args['run'], args['activity'], args['summary']),
                    'started': lambda: janitors.mark_started(args['run']),
                    'context': lambda: janitors.run_context(args['run']),
                    'valid': lambda: janitors.validate_dispatch(args['session'], args['run'], args['trace']),
                    'runs': lambda: janitors.list_runs(of, args['limit']),
                }
                try:
                    row.update(attempt(calls[ask]))
                except ValueError as broke:
                    # Not a refusal of the book: the Python Host answers 400 with these words.
                    row['refused'] = [400, '', str(broke)]
            # What is kept after the step: each trigger's progress, the runs, and the janitor as read.
            row['after'] = named({
                'progress': {r['attachment_id']: janitors.get_progress(r['attachment_id'])
                             for r in db.conn().execute('SELECT attachment_id FROM janitor_attachments ORDER BY attachment_id')
                             if janitors.get_progress(r['attachment_id'])},
                'runs': {session: [[run['run_id'], run['status'], run['outcome'], run['error'], run['activity'],
                                    run['activity_summary'], run['created_at'], run['started_at'], run['finished_at']]
                                   for run in janitors.list_runs(session, 100)] for session in sessions},
                'janitors': {session: shown(session) for session in sessions},
            })
            rows.append(row)
        # A task that keeps its pace: how many of the moments its schedule names it ran at.
        named_looks = [row for row in rows if row['given'].get('named')]
        told.append({'name': name, 'paced': paced, 'named': len(named_looks),
                     'ran': sum(len(row['handed']) for row in named_looks), 'steps': rows})
    # Every run as it is read whole, once: what it was given when it was made.
    whole = named([janitors.get_run(r['run_id']) for r in db.conn().execute('SELECT run_id FROM janitor_runs ORDER BY created_at,run_id')])
    return {'agents': [{'agent_id': f'id-{session}', 'session': session, 'persona': persona, 'backend': backend}
                       for persona, session, backend in AGENTS],
            'offered_again_after_ms': RECHECK_MS, 'given_up_after_ms': RUN_TTL_MS,
            'redactions': [[text, redact(text)] for text in REDACTIONS],
            'stories': told, 'runs': whole}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-4000:], file=sys.stderr)
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
                head = {key: value for key, value in story.items() if key != 'steps'}
                out.append('  {' + json.dumps(head)[1:-1] + ', "steps": [')
                out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(story['steps']) - 1 else '')
                           for n, row in enumerate(story['steps']))
                out.append('  ]}' + (',' if number < len(rows) - 1 else ''))
            out.append(' ]' + end)
        elif isinstance(rows, list) and rows and isinstance(rows[0], (dict, list)):
            out.append(f' "{part}": [')
            out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '')
                       for n, row in enumerate(rows))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(rows, ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = [step for story in said['stories'] for step in story['steps']]
    looks = [step for step in steps if step['ask'] == 'tick']
    refused = sum(1 for step in steps if 'refused' in step)
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories of {len(steps)} steps ({len(looks)} looks of the runner, '
          f'{sum(len(step["handed"]) for step in looks)} runs handed over, {refused} askings refused), {len(said["runs"])} runs, '
          f'{len(said["redactions"])} redactions')
    for story in said['stories']:
        if story['paced']:
            print(f'  {story["name"]}: ran at {story["ran"]} of the {story["named"]} moments its schedule names')
    return 0


if __name__ == '__main__':
    sys.exit(main())
