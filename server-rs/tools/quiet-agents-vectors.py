#!/usr/bin/env python3
"""Writes the vectors of which agents are quiet: those whose activity never claims the owner's attention by itself.

The Python Host has one definition (`lib.quiet_agents.is_quiet`), asked by every path that could reach the owner (the
snapshot's `quiet`, a turn's notification, a decision's push, the attention index, the Live Activity). This runs it
as it is over agent rows of every shape: a janitor, an agent, a helper, one put away, a row without the column, a
column that is empty, and no row at all.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/quiet-agents-vectors.py

It runs in a process of its own; no Host is started and nothing leaves the machine. It writes
`crates/clarp-kernel/tests/fixtures/quiet-agents-vectors.json`.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'quiet-agents-vectors.json'

# Agent rows as the Python Host's `agents` table gives them (the columns that matter), and what each is.
ROWS = [
    ('a janitor', {'agent_id': 'a-1', 'is_janitor': 1, 'role': 'janitor'}),
    ('an agent of the roster', {'agent_id': 'a-2', 'is_janitor': 0, 'role': ''}),
    ('a helper', {'agent_id': 'a-3', 'is_janitor': 0, 'role': 'helper'}),
    ('a janitor put away', {'agent_id': 'a-4', 'is_janitor': 1, 'role': 'janitor', 'archived_at': 5}),
    ('an agent put away', {'agent_id': 'a-5', 'is_janitor': 0, 'archived_at': 5}),
    ('a row without the column', {'agent_id': 'a-6'}),
    ('a column that is empty', {'agent_id': 'a-7', 'is_janitor': None}),
    ('no row', None),
]


def run() -> dict:
    sys.path.insert(0, str(REPO / 'server'))
    from lib import quiet_agents
    return {'rows': [{'what': what, 'row': row, 'quiet': quiet_agents.is_quiet(row)} for what, row in ROWS]}


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
    OUT.write_text(json.dumps(said, ensure_ascii=True, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(said["rows"])} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
