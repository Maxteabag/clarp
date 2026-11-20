#!/usr/bin/env python3
"""Writes the vectors the Rust Host's fork of an agent is tested against.

An agent forks itself into helpers that start from its conversation and each get a task
(`server/lib/agent_fork.py`, `POST /agents/<session>/fork`). What is asked for is read strictly: a
request that is malformed makes nothing, and says why in a code and in words. Each child's first
message says what it is, carries the part of its parent's conversation it starts from where the tool
could not copy the conversation itself, and says how to report back.

This runs that module itself and writes to `crates/clarp-kernel/tests/fixtures/fork-vectors.json`
what it makes of requests (the children it reads out of each, or the refusal), the first message
of a child in every form it has, and where a child's worktree is put when the request does not say.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/fork-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, no agent is made, and no git is run.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'fork-vectors.json'

TREE = {'repo': '/srv/repo', 'branch': 'feat/x'}
REQUESTS = [
    # What is no request at all.
    None, [], [1], 'children', 7, True,
    # Children, named one by one.
    {}, {'children': []}, {'children': None}, {'children': 'two'}, {'children': 3}, {'children': {}},
    {'children': [1]}, {'children': ['a']}, {'children': [None]}, {'children': [{}]},
    {'children': [{'name': 'a'}]}, {'children': [{'task': 't'}]}, {'children': [{'name': 'a', 'task': 't'}]},
    {'children': [{'name': '  a  ', 'task': '  t \n'}]}, {'children': [{'name': '   ', 'task': 't'}]},
    {'children': [{'name': 'a', 'task': '   '}]}, {'children': [{'name': 7, 'task': 't'}]},
    {'children': [{'name': 'a', 'task': 7}]}, {'children': [{'name': None, 'task': None}]},
    {'children': [{'name': 'x' * 64, 'task': 't'}]}, {'children': [{'name': 'x' * 65, 'task': 't'}]},
    {'children': [{'name': 'ä' * 65, 'task': 't'}]},
    {'children': [{'name': 'a', 'task': 't'}, {'name': 'b', 'task': 'u'}]},
    {'children': [{'name': 'a', 'task': 't'}, {'name': 'a', 'task': 'u'}]},
    {'children': [{'name': 'Alpha', 'task': 't'}, {'name': 'ALPHA', 'task': 'u'}]},
    {'children': [{'name': 'Zoë', 'task': 't'}, {'name': 'ZOË', 'task': 'u'}]},
    {'children': [{'name': 'a', 'task': 't'}, {'name': ' a ', 'task': 'u'}]},
    {'children': [{'name': 'a', 'task': 't'}, {'name': 'b'}]},
    {'children': [{'name': 'a', 'task': 't'}, 'b']},
    {'children': [{'name': f'c{n}', 'task': 't'} for n in range(16)]},
    {'children': [{'name': f'c{n}', 'task': 't'} for n in range(17)]},
    {'children': [{'name': 'a', 'task': 't', 'cwd': ' /srv/work '}]},
    {'children': [{'name': 'a', 'task': 't', 'cwd': 7}]},
    # A worktree of its own.
    {'children': [{'name': 'a', 'task': 't', 'worktree': TREE}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': {**TREE, 'base': ' main ', 'path': ' /srv/trees/a '}}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': 'repo:branch'}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': []}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': {}}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': {'repo': '/srv/repo'}}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': {'branch': 'b'}}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': {'repo': '  ', 'branch': 'b'}}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': {'repo': 7, 'branch': 'b'}}]},
    {'children': [{'name': 'a', 'task': 't', 'worktree': None}]},
    {'children': [{'name': 'a', 'worktree': 'x'}]},
    {'children': [{'task': 't', 'worktree': 'x'}]},
    # The tool, the model and the effort: for all, and for one.
    {'backend': ' codex ', 'model': 'm', 'effort': 'low', 'children': [{'name': 'a', 'task': 't'}]},
    {'backend': 'codex', 'model': 'm', 'children': [{'name': 'a', 'task': 't', 'backend': 'claude', 'effort': 'high'},
                                                     {'name': 'b', 'task': 'u', 'model': '  '}]},
    {'backend': 7, 'model': None, 'children': [{'name': 'a', 'task': 't', 'backend': 7}]},
    # So many of one task.
    {'count': 2, 'task': 't'}, {'count': 2, 'task': 't', 'name': ' part '}, {'count': 1, 'task': 't', 'name': 'solo'},
    {'count': 0, 'task': 't'}, {'count': -1, 'task': 't'}, {'count': 0}, {'count': 2}, {'count': 2, 'task': '  '},
    {'count': 2, 'task': 7}, {'count': '3', 'task': 't'}, {'count': ' 2 ', 'task': 't'}, {'count': 'two', 'task': 't'},
    {'count': '', 'task': 't'}, {'count': 2.9, 'task': 't'}, {'count': True, 'task': 't'}, {'count': False, 'task': 't'},
    {'count': [2], 'task': 't'}, {'count': {}, 'task': 't'}, {'count': 16, 'task': 't'}, {'count': 17, 'task': 't'},
    {'count': 17}, {'count': 10 ** 30, 'task': 't'}, {'count': '1_0', 'task': 't'}, {'count': '+2', 'task': 't'},
    {'count': 2, 'task': 't', 'cwd': '/srv/work'},
    {'count': 2, 'task': 't', 'worktree': TREE},
    {'count': 2, 'task': 't', 'worktree': {**TREE, 'path': '/srv/trees/one', 'base': 'main'}},
    {'count': 2, 'task': 't', 'worktree': {'repo': '/srv/repo'}},
    {'count': 2, 'task': 't', 'worktree': {'repo': '/srv/repo', 'branch': '  '}},
    {'count': 2, 'task': 't', 'worktree': 'repo:branch'},
    {'count': 2, 'task': 't', 'name': 'x' * 62}, {'count': 2, 'task': 't', 'name': 'x' * 63},
    {'count': 2, 'task': 't', 'backend': 'codex', 'effort': 'low'},
    # Both ways at once: the children that are named are the ones.
    {'count': 3, 'task': 't', 'children': [{'name': 'a', 'task': 'u'}]},
    {'count': 3, 'task': 't', 'children': []},
    {'count': None, 'children': None, 'task': 't'},
]

PARENTS = [('maximus', 'probe-1a2b'), ('claire-9f', 'fork-2-77aa')]
SPECS = [
    {'name': 'probe', 'task': 'Read the log and say what failed.', 'index': 1, 'total': 1},
    {'name': 'part-2', 'task': 'Port the second module.\nKeep its tests.', 'index': 2, 'total': 3},
    {'name': 'tree', 'task': 'Fix it.', 'index': 1, 'total': 1, 'branch': 'fix/the-thing'},
    {'name': 'tree-3', 'task': 'Fix it.', 'index': 3, 'total': 4, 'branch': 'fix/the-thing-3'},
]
SEEDS = [None, '', 'User: do the thing\n\nAssistant: on it\n  → Bash: ls']
PLACES = [
    ('/srv/repo', 'feat/x'), ('/srv/repo', 'feat/deep/er'), ('/srv/repo/', 'x'), ('/srv/my repo', 'a b'),
    ('/srv/repo', '..'), ('/srv/repo', '---'), ('/srv/repo', ''), ('/srv/repo', '.hidden'), ('/srv/repo', 'x.'),
    ('/srv/repo', 'ä/ö'), ('/srv/repo', 'a__b--c..d'), ('/srv/repo', 'UPPER/lower_1.2'), ('/repo', 'b'), ('/', 'b'),
    ('/srv/repo.git', 'rel/1.0'),
]


@tool_scratch.case('clarp-fork-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's fork makes of each."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agent_fork

    parsed = []
    for request in REQUESTS:
        try:
            specs = agent_fork.parse(request)
            parsed.append({'request': request, 'children': [dataclasses.asdict(child) for child in specs]})
        except agent_fork.ForkError as refused:
            parsed.append({'request': request, 'refused': [refused.status, refused.code, str(refused)]})
    messages = []
    for parent, helper in PARENTS:
        for fields in SPECS:
            child = agent_fork.ChildSpec(**fields)
            cwd = '/srv/trees/fix-the-thing' if child.branch else '/srv/work'
            for seed in SEEDS:
                messages.append({'parent': parent, 'helper': helper, 'child': dataclasses.asdict(child), 'cwd': cwd,
                                 'seed': seed,
                                 'message': agent_fork.message(parent=parent, helper=helper, spec=child, cwd=cwd, seed=seed)})
    places = [{'repo': repo, 'branch': branch,
               'path': str(agent_fork.default_worktree_path(pathlib.Path(repo), branch))}
              for repo, branch in PLACES]
    return {'most': agent_fork.MAX_CHILDREN, 'seed_most': agent_fork.SEED_MAX_CHARS, 'turn_most': agent_fork._TURN_MAX_CHARS,
            'parsed': parsed, 'messages': messages, 'places': places}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    for name in ('most', 'seed_most', 'turn_most'):
        out.append(f' "{name}": ' + json.dumps(said[name]) + ',')
    parts = ('parsed', 'messages', 'places')
    for at, part in enumerate(parts):
        out.append(f' "{part}": [')
        rows = said[part]
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append(' ]' + (',' if at < len(parts) - 1 else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    refused = {}
    for row in said['parsed']:
        code = row['refused'][1] if 'refused' in row else 'taken'
        refused[code] = refused.get(code, 0) + 1
    print(f'{OUT.relative_to(REPO)}: {len(said["parsed"])} requests ('
          + ', '.join(f'{count} {code}' for code, count in sorted(refused.items()))
          + f'), {len(said["messages"])} first messages, {len(said["places"])} places of a worktree')
    return 0


if __name__ == '__main__':
    sys.exit(main())
