#!/usr/bin/env python3
"""Read the Python Host's actual coordination visibility and run counts (PR 512)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/hidden-coordination-vectors.json'
NOW = 1_795_000_000_000


@tool_scratch.case('clarp-hidden-coordination-vectors-')
def run(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, clock, coordination, db, message_previews

    db.reset_for_tests(root / 'hidden.sqlite')
    names = iter(f'agent-{at}' for at in range(1, 100))
    agents._new_agent_id = lambda: next(names)

    def agent(session):
        return agents.create_agent(persona=session.capitalize(), session=session, voice_id='v',
                                   cwd=str(root), backend='claude')

    ivy = agent('ivy')
    boss = agent('boss')
    choices = []
    for global_choice in ('show', 'hide'):
        coordination.set_global(global_choice)
        for own in ('default', 'show', 'hide'):
            stored = coordination.set_chat(ivy, own)
            for mode in ('auto', 'show', 'hide'):
                choices.append(dict(global_choice=global_choice, own=own, mode=mode,
                                    policy=coordination.read_policy(ivy, mode),
                                    snapshot=coordination.snapshot_fields(
                                        ivy, {ivy: stored}, global_choice)))
    invalid = []
    for raw in ('', 'sometimes', 'DEFAULT', ' Hide ', ' show ', None):
        found = dict(raw=raw, normalized=coordination.normalize_visibility(raw))
        for kind, action in (('global', coordination.set_global),
                             ('chat', lambda text: coordination.set_chat(ivy, text))):
            try:
                found[kind] = action(raw)
            except ValueError as error:
                found[kind] = dict(error=str(error))
        invalid.append(found)
    coordination.set_global('hide')
    coordination.set_chat(ivy, 'default')
    reminder = coordination.reminder(
        agents.get_by_agent_id(ivy), origin='agent', sender_agent_id=boss)

    def gap(which, nearest, tail, *, bounded=False):
        name = f'case-{which}'
        who = agent(name)
        con = db.conn()
        upstream = nearest if tail else nearest[::-1]
        times = list(enumerate(upstream, start=1))
        for n, kind in times:
            stamp = NOW + n
            trace = f't-{n}'
            coordination.mark(con, agent_id=who, trace_id=trace, sender_agent_id=boss, at=stamp)
            con.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id,
                          source_file, seq, role, timestamp, text, tool_name, updated_at,
                          revision, origin, sender_agent_id, trace_id)
                          VALUES (?, ?, 'native', 't.jsonl', ?, 'assistant', ?, ?, ?, ?, ?, 'agent', ?, ?)""",
                        (f'h-{which}-{n}', who, n, clock.iso_from_ms(stamp),
                         'words' if kind == 'm' else '', 'Bash' if kind == 't' else None,
                         stamp, n, boss, trace))
        stamp = NOW if tail else NOW + len(nearest) + 1
        visible = f'visible-{which}'
        shown = [] if bounded and tail else [dict(id=visible)]
        if shown:
            con.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id,
                           source_file, seq, role, timestamp, text, updated_at, revision, origin)
                           VALUES (?, ?, 'native', 't.jsonl', 0, 'user', ?, 'visible', ?, 0, 'user')""",
                        (visible, who, clock.iso_from_ms(stamp), stamp))
        con.commit()
        before, after = message_previews.coordination_gaps(
            agent_id=who, sessions=['native'], rows=shown,
            include_automated=True, contiguous=not bounded or tail, tail=tail)
        return dict(nearest=nearest, tail=tail, answer=after if tail else before.get(visible))

    counts = [gap(n, shape, tail) for n, (shape, tail) in enumerate((
        ('mttm', True),
        ('mttm', False),
        ('t' * 402, True),
        ('t' * 402 + 'm', True),
        ('m' * 100 + 't' * 3, False),
        ('m' * 100 + 't' * 3 + 'm', False),
        ('', True),
    ))]
    look_back = [gap('hidden-tail', 'm' + 't' * 401 + 'mm', True, bounded=True),
                 gap('hidden-delta', 'm' + 't' * 402, False, bounded=True)]
    return dict(choices=choices, inputs=invalid, reminder=reminder, counts=counts,
                look_back=look_back)


def main():
    if sys.argv[1:] == ['--run']:
        print(json.dumps(run()))
        return
    proc = subprocess.run(
        [sys.executable, '-I', __file__, '--run'],
        env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}),
        text=True, check=True, capture_output=True)
    data = json.loads(proc.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(data, ensure_ascii=True, indent=2) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
