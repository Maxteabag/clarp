#!/usr/bin/env python3
"""Writes the vectors of when a janitor's failures reach its owner: the item raised, kept, closed or never made again.

The Python Host raises a `janitor_failure` item (`lib.janitor_attention.reconcile`) for a janitor whose runs of its
current configuration failed twice in a row since its last verified success, keeps it as the streak grows, closes it
when the janitor recovers, is set anew or is put away, and never makes it again for the same episode once the owner
archived or discarded it; `pending` is what `GET /attention` shows of them. This runs those functions as they are,
in a store made new under /var/tmp, over janitors whose runs are written step by step, and writes after each step
the items there are and what `pending` shows.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/janitor-attention-vectors.py

It runs in a process of its own; no Host is started and nothing leaves the machine. It writes
`crates/clarp-kernel/tests/fixtures/janitor-attention-vectors.json`.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-attention-vectors.json'

# The steps of each janitor's story. A run: (status, outcome, error, how it was frozen, its demand result or None).
SCHEDULED = {'template_id': 'scheduled-prompt'}
EPHEMERAL = {'executor': 'ephemeral', 'template_id': 'message-delegator'}
LABELS = {'executor': 'ephemeral', 'template_id': 'label-auditor'}
FAILED = ('failed', 'error', 'The scheduled task failed', SCHEDULED, None)
STORIES = [
    ('a scheduled task', 'Keeper', 'scheduled-prompt', [
        ('fails once', ('run', FAILED)),
        ('fails twice', ('run', FAILED)),
        ('fails a third time, its error with a thought and a key in it',
         ('run', ('failed', 'error', '<think>why it broke</think> token sk-ant-api03-abcdefghijklmnopqrstuv broke',
                  SCHEDULED, None))),
        ('is cancelled', ('run', ('cancelled', 'cancelled', '', SCHEDULED, None))),
        ('fails once after the cancel', ('run', FAILED)),
        ('goes well', ('run', ('completed', 'completed', '', SCHEDULED, None))),
        ('fails twice again', ('runs', [FAILED, FAILED])),
        ('is archived by its owner', ('archive', None)),
        ('fails once more, archived', ('run', FAILED)),
        ('is set anew', ('generation', None)),
        ('fails twice in its new configuration', ('runs', [FAILED, FAILED])),
        ('is turned off', ('enabled', False)),
        ('is turned on again', ('enabled', True)),
    ]),
    ('a janitor that answers requests', 'Router', 'message-delegator', [
        ('fails twice', ('runs', [('failed', 'failed', 'no route', EPHEMERAL, {'summary': 'x'}),
                                  ('failed', 'failed', '', EPHEMERAL, None)])),
        ('answers without a summary', ('run', ('completed', 'completed', '', EPHEMERAL, {'summary': ' '}))),
        ('fails again', ('run', ('failed', 'failed', 'no route', EPHEMERAL, None))),
        ('answers in another way than it was set', ('run', ('completed', 'completed', '',
                                                            {'executor': 'deterministic',
                                                             'template_id': 'message-delegator'},
                                                            {'summary': 'routed'}))),
        ('fails twice more', ('runs', [('failed', 'failed', 'no route', EPHEMERAL, None)] * 2)),
        ('answers well', ('run', ('completed', 'completed', '', EPHEMERAL, {'summary': 'routed'}))),
    ]),
    ('the labels janitor', 'Labels', 'label-auditor', [
        ('fails twice, the second without a word', ('runs', [('failed', 'failed', 'Jev did not answer', LABELS, None),
                                                            ('failed', 'error', '', LABELS, None)])),
        ('is discarded by its owner', ('discard', None)),
        ('fails again, discarded', ('run', ('failed', 'error', 'still', LABELS, None))),
        ('is no janitor any more', ('janitor', False)),
    ]),
    ('a janitor put away', 'Shelved', 'scheduled-prompt', [
        ('fails twice', ('runs', [FAILED, FAILED])),
        ('is put away', ('archived', True)),
    ]),
]


@tool_scratch.case('clarp-janitor-attention-')
def run(root) -> dict:
    os.environ.update({
        'CLAUDE_PWA_DB': str(root / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(root / 'logs'),
        'CLARP_CONFIG_DIR': str(root / 'config'), 'CLARP_SHARE_DIR': str(root / 'share'),
        'CLARP_CACHE_DIR': str(root / 'cache'), 'CLAUDE_PWA_CONFIG': str(root / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'), 'HOME': str(root / 'home'),
    })
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, artifacts, db, janitor_attention, janitors
    db.reset_for_tests(root / 'janitors.sqlite')
    con = db.conn()
    con.execute('PRAGMA foreign_keys=OFF')
    clock = [1_800_000_000_000]

    def tick() -> int:
        clock[0] += 1_000
        return clock[0]

    names = {template: janitors.template(template)['name']
             for template in ('scheduled-prompt', 'message-delegator', 'label-auditor')}
    stories = []
    for number, (what, persona, template, steps) in enumerate(STORIES, start=1):
        session = persona.lower()
        # The same ids every time the vectors are written.
        agents._new_agent_id = lambda number=number: f'{number:016x}'
        agent_id = agents.create_agent(persona=persona, voice_id='', cwd=str(root), session=session)
        con.execute('UPDATE agents SET is_janitor=1 WHERE agent_id=?', (agent_id,))
        con.execute("""INSERT INTO janitor_configs(agent_id,template_id,enabled,revision,generation,created_at,
            updated_at) VALUES(?,?,1,1,1,?,?)""", (agent_id, template, clock[0], clock[0]))
        runs = [0]
        told = []

        def add(run: tuple) -> dict:
            status, outcome, error, frozen, result = run
            runs[0] += 1
            run_id = f'{session}-run-{runs[0]}'
            finished = tick()
            generation = con.execute('SELECT generation FROM janitor_configs WHERE agent_id=?',
                                     (agent_id,)).fetchone()[0]
            con.execute("""INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,
                status,outcome,candidates_json,configuration_json,created_at,started_at,finished_at,error)
                VALUES(?,?,?,?,?,?,?,?,'[]',?,?,?,?,?)""",
                        (run_id, agent_id, session, f'{session}-attachment', generation, run_id, status, outcome,
                         json.dumps(frozen), finished - 500, finished - 400, finished, error))
            if result is not None:
                con.execute('INSERT INTO janitor_demand_results(run_id,result_json,created_at) VALUES(?,?,?)',
                            (run_id, json.dumps(result), finished))
            return {'run_id': run_id, 'status': status, 'outcome': outcome, 'error': error,
                    'configuration': frozen, 'demand_result': result, 'created_at': finished - 500,
                    'finished_at': finished}

        def alerts() -> list[dict]:
            rows = con.execute("""SELECT * FROM artifacts WHERE agent_id=? AND reference_id LIKE ?
                ORDER BY created_at,artifact_id""", (agent_id, janitor_attention.PREFIX + '%')).fetchall()
            return [{'artifact_id': row['artifact_id'], 'reference_id': row['reference_id'], 'type': row['type'],
                     'status': row['status'], 'title': row['title'], 'summary': row['summary'],
                     'payload': json.loads(row['payload_json']), 'archived': row['archived_at'] is not None,
                     'deleted': row['deleted_at'] is not None} for row in rows]

        def shown(include_archived: bool) -> list[dict]:
            items = janitor_attention.pending(include_archived=include_archived)
            mine = [item for item in items if item['agent_id'] == agent_id]
            return [{key: value for key, value in item.items()
                     if key not in ('created_at', 'updated_at', 'archived_at', 'agent_id')}
                    | {'archived': item['archived_at'] is not None} for item in mine]

        for step, (action, value) in steps:
            done = {'step': step, 'action': action}
            if action == 'run':
                done['runs'] = [add(value)]
            elif action == 'runs':
                done['runs'] = [add(one) for one in value]
            elif action in ('archive', 'discard'):
                open_alert = [a for a in alerts() if a['status'] == 'failed'][-1]
                row = artifacts.get(open_alert['artifact_id'])
                if action == 'archive':
                    artifacts.archive(row['artifact_id'], archived=True, expected_updated_at=row['updated_at'])
                else:
                    artifacts.discard(row['artifact_id'], expected_updated_at=row['updated_at'])
                done['artifact_id'] = row['artifact_id']
            elif action == 'generation':
                con.execute('UPDATE janitor_configs SET generation=generation+1 WHERE agent_id=?', (agent_id,))
            elif action == 'enabled':
                con.execute('UPDATE janitor_configs SET enabled=? WHERE agent_id=?', (int(value), agent_id))
                done['enabled'] = value
            elif action == 'janitor':
                con.execute('UPDATE agents SET is_janitor=? WHERE agent_id=?', (int(value), agent_id))
                done['is_janitor'] = value
            elif action == 'archived':
                con.execute('UPDATE agents SET archived_at=? WHERE agent_id=?', (tick(), agent_id))
                done['archived'] = value
            done['changed'] = janitor_attention.reconcile()
            done['alerts'] = alerts()
            done['pending'] = shown(False)
            done['pending_with_archived'] = shown(True)
            told.append(done)
        stories.append({'what': what, 'agent_id': agent_id, 'session': session, 'persona': persona,
                        'template_id': template, 'job_name': names[template], 'steps': told})
    return {'kind': janitor_attention.KIND, 'prefix': janitor_attention.PREFIX, 'stories': stories}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(said, ensure_ascii=True, indent=1) + '\n')
    steps = sum(len(story['steps']) for story in said['stories'])
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} janitors, {steps} steps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
