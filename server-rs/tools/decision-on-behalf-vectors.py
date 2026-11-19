#!/usr/bin/env python3
"""Writes the vectors of who the owner lets answer a request in his place, beside the artifacts' own.

The Python Host lets an agent answer a request for its user only when the owner named its session in
`[agents] decision_on_behalf_sessions` (`lib.config.load`) and the request carries that agent's own turn
identity (what comes of a token is in `turn-identity-vectors.json`). How a resolve on someone's behalf goes is
in `artifact-vectors.json` (the scenario `a_request_answered_on_the_users_behalf`); this writes the owner's list
as `config.load` reads it, from files of every shape.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/decision-on-behalf-vectors.py

It runs in a process of its own over a directory made new under /var/tmp; no Host is started and nothing
leaves the machine. It writes `crates/clarp-kernel/tests/fixtures/decision-on-behalf-vectors.json`.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'decision-on-behalf-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-decision-on-behalf-vectors')

# The owner's files: what stands under [agents], as TOML.
FILES = [
    ('no table of agents', ''),
    ('a table of agents without the list', '[agents]\nclaude_effort = "high"\n'),
    ('one session', '[agents]\ndecision_on_behalf_sessions = ["desk"]\n'),
    ('blanks, a repeat and empties', '[agents]\ndecision_on_behalf_sessions = [" desk ", "desk", "", "  ", "omar"]\n'),
    ('a text, not a list', '[agents]\ndecision_on_behalf_sessions = "desk"\n'),
    ('what is no text is passed over', '[agents]\ndecision_on_behalf_sessions = [7, "desk", true, ["omar"]]\n'),
    ('an empty list', '[agents]\ndecision_on_behalf_sessions = []\n'),
    ('case is kept', '[agents]\ndecision_on_behalf_sessions = ["Desk", "desk", "DESK"]\n'),
    ('a table, not a list', '[agents.decision_on_behalf_sessions]\ndesk = true\n'),
]


def run() -> dict:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'), 'HOME': str(SCRATCH / 'home'),
    })
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config
    files = []
    for number, (what, text) in enumerate(FILES):
        path = SCRATCH / f'config-{number}.toml'
        path.write_text(text)
        files.append({'what': what, 'toml': text,
                      'sessions': list(config.load(path).decision_on_behalf_sessions)})
    return {'files': files}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(said, ensure_ascii=True, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(said["files"])} files of the owner')
    return 0


if __name__ == '__main__':
    sys.exit(main())
