#!/usr/bin/env python3
"""Writes the vectors the Rust Host's runs that answer one request are tested against.

Two of a Host's own janitors do not run at set times: the one that explains the use of a tool and
the one that finds whom a spoken message is for answer one request each. The Python Host keeps
such a request as a run of its janitor (`server/lib/janitor_builtins.py`): which janitor answers
(`resolve`), the run of a request under an id made from the request's own (`begin_run`: the same
request is the same run, never a second call of a model), the claim that is taken once before a
model is called (`claim_run`), the result that is kept once (`complete_run`), and the three minutes
after which a run nobody finished is given up (`recover_expired_runs`). This calls those functions,
step by step over stories, on a database made for it, and writes what they answered to
`crates/clarp-kernel/tests/fixtures/janitor-demand-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/janitor-demand-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, and no model is called: a claim and a result are
what a caller says they are.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-demand-vectors.json'

T0 = 1_800_000_000_000
S = 1000
AGENTS = [('Worker', 'worker', 'codex'), ('Other', 'other', 'codex'), ('Custom', 'custom', 'codex')]
EXPLAIN, ROUTE = 'tool-explainer', 'message-delegator'
LONG = 'x' * 501


def stories():
    """Each step: what it shows, when, what is asked, and with what. `{run}` is the run that was begun
    last, `{worker}` and the like the id of that agent."""
    seen = {'input_hash': 'h1', 'item_count': 2}
    first = [
        ('who explains for an agent', T0, 'resolve', {'role': EXPLAIN, 'target': '{worker}'}),
        ('who explains for nobody in particular', T0, 'resolve', {'role': EXPLAIN}),
        ('who explains for an agent nobody has', T0, 'resolve', {'role': EXPLAIN, 'target': 'nobody-has-this-id'}),
        ('who routes, where routing is off', T0, 'resolve', {'role': ROUTE}),
        ('who does a job there is not', T0, 'resolve', {'role': 'no-such-job'}),
        ('a request with an id of nothing', T0, 'begin', {'role': EXPLAIN, 'request': '', 'context': seen}),
        ('a request with an id too long', T0, 'begin', {'role': EXPLAIN, 'request': 'r' * 161, 'context': seen}),
        ('a request that says more than counts', T0, 'begin', {'role': EXPLAIN, 'request': 'q0', 'context': {'text': 'the command'}}),
        ('a request that says too much', T0, 'begin', {'role': EXPLAIN, 'request': 'q0', 'context': {'summary': LONG}}),
        ('a request that says a number too large', T0, 'begin', {'role': EXPLAIN, 'request': 'q0', 'context': {'item_count': 10 ** 12 + 1}}),
        ('a request that says a list', T0, 'begin', {'role': EXPLAIN, 'request': 'q0', 'context': {'summary': ['x']}}),
        ('a request where routing is off', T0, 'begin', {'role': ROUTE, 'request': 'q0', 'context': seen}),
        ('a request', T0 + 1 * S, 'begin', {'role': EXPLAIN, 'request': 'q1', 'context': seen, 'target': '{worker}'}),
        ('the janitor while its run is out', T0 + 1 * S, 'get', {'role': EXPLAIN}),
        ('the same request again', T0 + 2 * S, 'begin', {'role': EXPLAIN, 'request': 'q1', 'context': seen, 'target': '{worker}'}),
        ('the same id for other work', T0 + 2 * S, 'begin', {'role': EXPLAIN, 'request': 'q1', 'context': {'input_hash': 'h2'}, 'target': '{worker}'}),
        ('the same id for another agent', T0 + 2 * S, 'begin', {'role': EXPLAIN, 'request': 'q1', 'context': seen, 'target': '{other}'}),
        ('another request while the first is out', T0 + 3 * S, 'begin', {'role': EXPLAIN, 'request': 'q2', 'context': seen}),
        ('is it current', T0 + 3 * S, 'current', {'run': '{run}'}),
        ('the claim, taken', T0 + 4 * S, 'claim', {'run': '{run}'}),
        ('the claim, asked for again', T0 + 5 * S, 'claim', {'run': '{run}'}),
        ('a claim on a run nobody has', T0 + 5 * S, 'claim', {'run': 'janitor-demand-nobody'}),
        ('a result that is no ending', T0 + 6 * S, 'complete', {'run': '{run}', 'outcome': 'changed'}),
        ('a result that says too much', T0 + 6 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'summary': LONG}}),
        ('an error too long', T0 + 6 * S, 'complete', {'run': '{run}', 'outcome': 'failed', 'error': LONG}),
        ('the result', T0 + 7 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'status': 'explained', 'item_count': 2}}),
        ('the same result again', T0 + 8 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'status': 'explained', 'item_count': 2}}),
        ('another result for the same run', T0 + 8 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'status': 'other'}}),
        ('a result for a run nobody has', T0 + 8 * S, 'complete', {'run': 'janitor-demand-nobody', 'outcome': 'completed'}),
        ('the request, when its run is over', T0 + 9 * S, 'begin', {'role': EXPLAIN, 'request': 'q1', 'context': seen, 'target': '{worker}'}),
        ('the run of a request by its id', T0 + 9 * S, 'request-run', {'role': EXPLAIN, 'request': 'q1'}),
        ('the run of a request nobody made', T0 + 9 * S, 'request-run', {'role': EXPLAIN, 'request': 'q-nobody'}),
        ('the janitor after its run', T0 + 9 * S, 'get', {'role': EXPLAIN}),
        ('the next request', T0 + 10 * S, 'begin', {'role': EXPLAIN, 'request': 'q3', 'context': None}),
        ('a run that failed, with what it said of itself', T0 + 11 * S, 'complete',
         {'run': '{run}', 'outcome': 'failed', 'error': 'the model said nothing', 'activity': 'failed', 'summary': ' no \n answer '}),
        ('the janitor after a run that failed', T0 + 11 * S, 'get', {'role': EXPLAIN}),
    ]
    second = [
        ('a request', T0 + 100 * S, 'begin', {'role': EXPLAIN, 'request': 'e1', 'context': {'item_count': 1}}),
        ('claimed', T0 + 101 * S, 'claim', {'run': '{run}'}),
        ('three minutes less a second on: still current', T0 + 100 * S + 179_999, 'current', {'run': '{run}'}),
        ('three minutes on: no more', T0 + 100 * S + 180_000, 'current', {'run': '{run}'}),
        ('a request while the old run is past its time', T0 + 281 * S, 'begin', {'role': EXPLAIN, 'request': 'e2', 'context': {'item_count': 1}}),
        ('the run that was given up', T0 + 281 * S, 'request-run', {'role': EXPLAIN, 'request': 'e1'}),
        ('its result, come too late', T0 + 282 * S, 'complete', {'run': '{old}', 'outcome': 'completed', 'result': {'status': 'late'}}),
        ('the janitor then', T0 + 282 * S, 'get', {'role': EXPLAIN}),
        ('the new run finished', T0 + 283 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'status': 'explained'}}),
        ('a run nobody claims, and three minutes', T0 + 300 * S, 'begin', {'role': EXPLAIN, 'request': 'e3', 'context': None}),
        ('given up by the look that finds it', T0 + 481 * S, 'recover', {}),
        ('nothing left to give up', T0 + 482 * S, 'recover', {}),
        ('the run then', T0 + 482 * S, 'request-run', {'role': EXPLAIN, 'request': 'e3'}),
    ]
    third = [
        ('a request', T0 + 1000 * S, 'begin', {'role': EXPLAIN, 'request': 'p1', 'context': {'item_count': 3}}),
        ('claimed', T0 + 1001 * S, 'claim', {'run': '{run}'}),
        ('the janitor is paused while its run is out', T0 + 1002 * S, 'enabled', {'role': EXPLAIN, 'enabled': False}),
        ('is it current', T0 + 1002 * S, 'current', {'run': '{run}'}),
        ('its result, when its janitor is paused', T0 + 1003 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'status': 'explained'}}),
        ('the run then', T0 + 1003 * S, 'request-run', {'role': EXPLAIN, 'request': 'p1'}),
        ('a request while it is paused', T0 + 1004 * S, 'begin', {'role': EXPLAIN, 'request': 'p2', 'context': None}),
        ('turned on again', T0 + 1005 * S, 'enabled', {'role': EXPLAIN, 'enabled': True}),
        ('a request', T0 + 1006 * S, 'begin', {'role': EXPLAIN, 'request': 'p3', 'context': None, 'target': '{worker}'}),
        ('a claim that is not taken', T0 + 1006 * S, 'current', {'run': '{run}'}),
        ('set anew while its run is out', T0 + 1007 * S, 'configure', {'role': EXPLAIN, 'options': {'detail_level': 3}}),
        ('the claim, after it was set anew', T0 + 1008 * S, 'claim', {'run': '{run}'}),
        ('turned on as set', T0 + 1009 * S, 'enabled', {'role': EXPLAIN, 'enabled': True}),
        ('the old request again', T0 + 1010 * S, 'begin', {'role': EXPLAIN, 'request': 'p3', 'context': None, 'target': '{worker}'}),
        ('a new request as set', T0 + 1011 * S, 'begin', {'role': EXPLAIN, 'request': 'p4', 'context': None, 'target': '{worker}'}),
        ('finished', T0 + 1012 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {}}),
        # Routing is turned on: it answers for whom it watches.
        ('routing turned on', T0 + 1100 * S, 'enabled', {'role': ROUTE, 'enabled': True}),
        ('who routes', T0 + 1100 * S, 'resolve', {'role': ROUTE}),
        ('who routes for an agent', T0 + 1100 * S, 'resolve', {'role': ROUTE, 'target': '{other}'}),
        ('a message to route', T0 + 1101 * S, 'begin', {'role': ROUTE, 'request': 'm1', 'context': {'request_hash': 'm', 'candidate_count': 3}}),
        ('claimed', T0 + 1102 * S, 'claim', {'run': '{run}'}),
        ('routed', T0 + 1103 * S, 'complete', {'run': '{run}', 'outcome': 'completed', 'result': {'decision_id': 'd1', 'status': 'routed'},
                                                'activity': 'worked', 'summary': 'to Other'}),
        ('the janitor that routes, after', T0 + 1103 * S, 'get', {'role': ROUTE}),
        ('the runs of the one that explains', T0 + 1104 * S, 'runs', {'role': EXPLAIN}),
    ]
    return [('a request, its claim and its result', first), ('a run that nobody finishes', second),
            ('paused and set anew while a run is out, and routing', third)]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's functions answer at each step."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-janitor-demand-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, janitor_builtins, janitors

    clock = {'now': T0}
    db.now_ms = lambda: clock['now']
    work = root / 'work'
    work.mkdir()
    ids = {session: agents.create_agent(persona=persona, voice_id='', cwd=str(work), session=session, backend=backend)
           for persona, session, backend in AGENTS}
    janitor_builtins.ensure_builtins(str(work))
    own = {role: janitor_builtins.get_builtin(role) for role in (EXPLAIN, ROUTE)}

    def named(value):
        text = json.dumps(value)
        for session, agent_id in ids.items():
            text = text.replace(agent_id, f'id-{session}')
        return json.loads(text.replace(str(work), '/srv/work'))

    def filled(value):
        text = json.dumps(value)
        for session, agent_id in ids.items():
            text = text.replace('{' + session + '}', agent_id)
        return json.loads(text)

    def shown(row):
        """A janitor as it is read, without its picture (told elsewhere)."""
        if isinstance(row, dict):
            row = dict(row)
            row.pop('avatar_url', None)
        return row

    def attempt(call):
        try:
            return {'taken': named(call())}
        except janitors.JanitorError as refused:
            return {'refused': [refused.status, refused.code, named(str(refused))]}

    told = []
    first_day = named({role: shown(row) for role, row in own.items()})
    for name, steps in stories():
        last = {'run': '', 'old': ''}
        rows = []
        for what, now, ask, given in steps:
            clock['now'] = now
            given = json.loads(json.dumps(given).replace('{run}', last['run']).replace('{old}', last['old']))
            args = filled(given)
            session = own.get(args.get('role'), {}).get('session', '') if isinstance(own.get(args.get('role')), dict) else ''

            def revision():
                return janitors.get(session)['revision']
            calls = {
                'resolve': lambda: shown(janitor_builtins.resolve(args['role'], target_agent_id=args.get('target'))),
                'begin': lambda: janitor_builtins.begin_run(args['role'], args['request'], context=args.get('context'),
                                                            target_agent_id=args.get('target')),
                'request-run': lambda: janitor_builtins.get_request_run(args['role'], args['request']),
                'current': lambda: janitor_builtins.is_current(args['run']),
                'claim': lambda: janitor_builtins.claim_run(args['run']),
                'complete': lambda: janitor_builtins.complete_run(
                    args['run'], args['outcome'], result=args.get('result'), error=args.get('error', ''),
                    **({'activity': args['activity'], 'summary': args.get('summary', '')} if 'activity' in args else {})),
                'recover': lambda: janitor_builtins.recover_expired_runs(),
                'get': lambda: shown(janitors.get(session)),
                'enabled': lambda: shown(janitors.set_enabled(session, revision(), args['enabled'])),
                'configure': lambda: shown(janitors.configure(session, revision(), options=args['options'])),
                'runs': lambda: janitors.list_runs(session, 100),
            }
            row = {'what': what, 'now': now, 'ask': ask, 'given': given, **attempt(calls[ask])}
            if ask == 'begin' and isinstance(row.get('taken'), dict) and row['taken'].get('run_id') != last['run']:
                last['old'], last['run'] = last['run'], row['taken']['run_id']
            rows.append(row)
        told.append({'name': name, 'steps': rows})
    return {'agents': [{'agent_id': f'id-{session}', 'session': session, 'persona': persona, 'backend': backend}
                       for persona, session, backend in AGENTS],
            'given_up_after_ms': janitor_builtins.DEMAND_RUN_TTL_MS,
            'said': sorted(janitor_builtins._METADATA_KEYS), 'first_day': first_day, 'stories': told}


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
                out.append('  {"name": ' + json.dumps(story['name']) + ', "steps": [')
                out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(story['steps']) - 1 else '')
                           for n, row in enumerate(story['steps']))
                out.append('  ]}' + (',' if number < len(rows) - 1 else ''))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(rows, ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = [step for story in said['stories'] for step in story['steps']]
    refused = sum(1 for step in steps if 'refused' in step)
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories of {len(steps)} steps ({refused} refused)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
