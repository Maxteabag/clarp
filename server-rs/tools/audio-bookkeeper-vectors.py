#!/usr/bin/env python3
"""Writes the vectors the Rust Host's audio bookkeeper is tested against.

One of a Host's own janitors keeps the books of what is said aloud, and asks no model for it
(`server/lib/audio_bookkeeper.py`): the first time a clip is seen in a stage (what its producer
says of it: `producer:streaming`, `producer:complete`, `producer:failed`; what was done with it
for its listener: `client:synthesized`, `client:broadcast`, `client:queued`, `client:play-ok`
and the like), the database writes that down by itself, once for a clip and a stage; and a pass
(`drain`) writes each such fact as a completed run of the janitor that keeps the books for the
clip's agent, when one is on and has the agent in its scope. What nobody keeps the books for
waits. This makes clips and moves them through their stages with the Python Host's own
functions, on a database made for it, drains, and writes down after every step what was seen
and what the janitor's runs say, to
`crates/clarp-kernel/tests/fixtures/audio-bookkeeper-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/audio-bookkeeper-vectors.py

One thing is written down otherwise than the Python Host has it. Its database numbers what it
sees, and gives a number also to a stage a clip comes back to (played a second time: the row is
not written, its number is spent), so its numbers have gaps. The Rust Host numbers what it
writes down. The numbers here are therefore the reference's order without its gaps: the first
thing seen is 1, the next 2, in everything that names one (a fact, its run, the run's context).

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, and nothing is spoken: a clip is a row.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'audio-bookkeeper-vectors.json'

T0 = 1_800_000_000_000
S = 1000
AGENTS = [('Worker', 'worker', 'codex'), ('Other', 'other', 'codex'), ('Third', 'third', 'codex')]
ROLE = 'audio-bookkeeper'


def stories():
    """Each step: what it shows, when, what is done, and with what. `{worker}` and the like is the
    id of that agent; a clip is known by the name its step gives it."""
    def clip(now, name, agent, producer='streaming', turn=3, **more):
        given = {'name': name, 'agent': '{' + agent + '}', 'producer': producer, 'turn': turn, **more}
        return (f'a clip of {agent} is made', T0 + now * S, 'clip', given)

    def producer(now, name, to):
        return (f'its producer says {to}', T0 + now * S, 'producer', {'name': name, 'to': to})

    def status(now, name, to):
        return (f'it is {to}', T0 + now * S, 'status', {'name': name, 'to': to})

    def drain(now, what='the books are kept', **more):
        return (what, T0 + now * S, 'drain', more)

    a_clips_life = [
        clip(1, 'one', 'worker'),
        drain(2, 'the two things seen of a new clip are written down'),
        producer(3, 'one', 'complete'),
        status(4, 'one', 'broadcast'),
        status(5, 'one', 'queued'),
        status(6, 'one', 'play-start'),
        status(7, 'one', 'play-ok'),
        drain(8, 'each stage once'),
        status(9, 'one', 'play-start'),
        status(10, 'one', 'play-ok'),
        producer(11, 'one', 'complete'),
        drain(12, 'played again: nothing new was seen'),
        clip(13, 'whole', 'worker', producer=None, turn=None),
        clip(14, 'broken', 'worker'),
        producer(15, 'broken', 'failed'),
        status(16, 'broken', 'play-fail'),
        status(17, 'whole', 'held'),
        drain(18, 'a clip that came whole, and one that failed'),
        drain(19, 'nothing waits'),
    ]
    whose_books = [
        ('the keeper is paused', T0 + 100 * S, 'enabled', {'on': False}),
        clip(101, 'unkept', 'worker'),
        producer(102, 'unkept', 'complete'),
        drain(103, 'paused: what was seen waits'),
        ('the keeper is on again', T0 + 104 * S, 'enabled', {'on': True}),
        drain(105, 'what waited is written down'),
        ('the keeper is told to leave an agent out', T0 + 110 * S, 'configure',
         {'scope': {'exclude_agent_ids': ['{other}']}}),
        ('and turned on', T0 + 111 * S, 'enabled', {'on': True}),
        clip(112, 'theirs', 'other'),
        clip(113, 'mine', 'worker'),
        drain(114, 'the one left out waits; the other is written down'),
        ('the keeper is told to keep for one agent only', T0 + 120 * S, 'configure',
         {'scope': {'agent_ids': ['{third}']}}),
        ('and turned on', T0 + 121 * S, 'enabled', {'on': True}),
        clip(122, 'third', 'third'),
        status(123, 'mine', 'broadcast'),
        drain(124, 'only that agent'),
        ('the keeper keeps for everyone again', T0 + 130 * S, 'configure', {'scope': {}}),
        ('and turned on', T0 + 131 * S, 'enabled', {'on': True}),
        drain(132, 'everything that waited'),
    ]
    many = [clip(200 + n, f'm{n}', 'worker', producer=None) for n in range(20)]
    many += [
        drain(230, 'no more than a pass takes', limit=32),
        drain(231, 'the rest'),
        drain(232, 'a pass of one', limit=1),
        clip(233, 'last', 'worker'),
        drain(234, 'a pass of one takes the oldest', limit=1),
        drain(235, 'and the next', limit=1),
    ]
    return [("a clip's life", a_clips_life), ('whose books are kept', whose_books), ('many at once', many)]


@tool_scratch.case('clarp-audio-bookkeeper-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's functions leave at each step."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, audio_bookkeeper, clip_records, db, janitor_builtins, janitors

    clock = {'now': T0}
    db.now_ms = lambda: clock['now']
    clip_records.now_ms = lambda: clock['now']
    work = root / 'work'
    work.mkdir()
    ids = {session: agents.create_agent(persona=persona, voice_id='', cwd=str(work), session=session, backend=backend)
           for persona, session, backend in AGENTS}
    janitor_builtins.ensure_builtins(str(work))
    own = janitor_builtins.get_builtin(ROLE)
    session = own['session']
    clips: dict[str, int] = {}

    def named(value):
        text = json.dumps(value)
        for name, agent_id in ids.items():
            text = text.replace(agent_id, f'id-{name}')
        return json.loads(text.replace(str(work), '/srv/work'))

    def filled(value):
        text = json.dumps(value)
        for name, agent_id in ids.items():
            text = text.replace('{' + name + '}', agent_id)
        return json.loads(text)

    def seen():
        """Everything that was seen, in the order it was: the clip by its name here. When a
        stage was seen is the database's own clock, and is left out."""
        by_id = {clip_id: name for name, clip_id in clips.items()}
        rows = db.conn().execute('SELECT * FROM audio_bookkeeping_events ORDER BY event_id').fetchall()
        return [{'event_id': row['event_id'], 'clip': by_id[row['clip_id']], 'clip_id': row['clip_id'],
                 'agent_id': row['agent_id'], 'turn_id': row['turn_id'], 'runtime_id': row['runtime_id'],
                 'stage': row['stage'], 'run_id': row['run_id'], 'completed_at': row['completed_at']}
                for row in rows]

    def keeper():
        row = janitors.get(session)
        return {key: row[key] for key in ('enabled', 'revision', 'generation', 'scope', 'last_run_at', 'last_error')}

    told = []
    first = dict(janitors.get(session))
    first.pop('avatar_url', None)
    first = named(first)
    for name, steps in stories():
        rows = []
        for what, now, ask, given in steps:
            clock['now'] = now
            args = filled(given)
            if ask == 'clip':
                clips[args['name']] = clip_records.record_clip(
                    agent_id=args['agent'], path=f"/srv/clips/{args['name']}.mp3", trace_id=f"t-{args['name']}",
                    turn_id=args['turn'], producer_status=args['producer'], runtime_id=lambda _agent: None)
                answer = None
            elif ask == 'producer':
                answer = clip_records.mark_clip_producer_status(clip_id=clips[args['name']], producer_status=args['to'])
            elif ask == 'status':
                answer = clip_records.mark_clip_status(clip_id=clips[args['name']], status=args['to'])
            elif ask == 'drain':
                answer = audio_bookkeeper.drain(**args)
            elif ask == 'enabled':
                janitors.set_enabled(session, janitors.get(session)['revision'], args['on'])
                answer = None
            elif ask == 'configure':
                janitors.configure(session, janitors.get(session)['revision'], scope=args['scope'])
                answer = None
            else:
                raise ValueError(ask)
            row = {'what': what, 'now': now, 'ask': ask, 'given': given, 'answer': answer, 'seen': named(seen()),
                   'keeper': named(keeper())}
            if ask == 'drain':
                row['runs'] = named(janitors.list_runs(session, 100))
            rows.append(row)
        told.append({'name': name, 'steps': rows})
    # The reference's numbers, without their gaps.
    spent = sorted({fact['event_id'] for story in told for step in story['steps'] for fact in step['seen']})
    dense = {number: place for place, number in enumerate(spent, 1)}

    def run_name(run_id):
        return f"audio-bookkeeping-{dense[int(run_id.rsplit('-', 1)[1])]}" if run_id else run_id

    for story in told:
        for step in story['steps']:
            for fact in step['seen']:
                fact['event_id'] = dense[fact['event_id']]
                fact['run_id'] = run_name(fact['run_id'])
            for kept in step.get('runs', []):
                for key in ('run_id', 'trace_id', 'queue_id', 'client_msg_id'):
                    kept[key] = run_name(kept[key])
                context = kept['configuration']['context']
                context['event_id'] = dense[context['event_id']]
            # Listed as the reference lists them, by the names they have here.
            if 'runs' in step:
                step['runs'].sort(key=lambda kept: (kept['created_at'], kept['run_id']), reverse=True)
    return {'gaps_left_out': len(range(1, spent[-1] + 1)) - len(spent),
            'agents': [{'agent_id': f'id-{name}', 'session': name, 'persona': persona, 'backend': backend}
                       for persona, name, backend in AGENTS],
            'first_day': first,
            'stories': told}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
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
    drains = [step for step in steps if step['ask'] == 'drain']
    last = steps[-1]
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories of {len(steps)} steps, {len(drains)} passes that wrote '
          f'{sum(step["answer"] for step in drains)} facts down; {len(last["seen"])} things seen in all')
    return 0


if __name__ == '__main__':
    sys.exit(main())
