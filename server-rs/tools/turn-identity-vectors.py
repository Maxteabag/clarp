#!/usr/bin/env python3
"""Writes the vectors of what comes of a turn's token: whose turn it names, or nobody, or that it cannot be said.

The Python Host gives every turn's tool a token (`CLARP_PROVIDER_TURN`, sent back as `X-Clarp-Turn`), keeps it
with its agent while the turn runs (`provider_background_jobs.turn_started`), marks when the turn ended
(`turn_exited`) and forgets it seven days after that (`reap_exited_turns`, `TURN_RETENTION_MS`). A request that
presents it is that agent's (`lib.turn_identity.caller_agent_id`); a lookup that fails is no answer at all (the
server turns it into a retryable 503). This runs those functions as they are over turns that run, that ended
at moments around the seven days, a token never given, a text that is no token, and a store that cannot be
read, and writes what each comes to.

How a token is made is not in the vectors: this Host signs its own (`clarp_kernel::turn_identity`); what is
held is the outcome for each standing of the turn.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/turn-identity-vectors.py

It runs in a process of its own over a directory made new under /var/tmp; no Host is started and nothing
leaves the machine. It writes `crates/clarp-kernel/tests/fixtures/turn-identity-vectors.json`.
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'turn-identity-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-turn-identity-vectors')
NOW = 1_800_000_000_000
DAY = 86_400_000

# Each turn of the agent `id-desk`: what it is, and how long ago it ended (None: it runs).
TURNS = [
    ('a turn that runs', None),
    ('a turn that ended a moment ago', 1),
    ('a turn that ended a day ago', DAY),
    ('a turn that ended seven days ago to the millisecond', 7 * DAY),
    ('a turn that ended seven days and a millisecond ago', 7 * DAY + 1),
    ('a turn that ended eight days ago', 8 * DAY),
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
    from lib import db, provider_background_jobs as turns, turn_identity
    db.reset_for_tests(SCRATCH / 'turns.sqlite')
    db.now_ms = lambda: NOW
    given = []
    for what, ended_ago in TURNS:
        token, env = turns.identity_env('id-desk')
        # This very process is the turn's: one that runs is still running when the sweep looks.
        turns.turn_started(token, agent_id='id-desk', provider='claude', pid=os.getpid())
        if ended_ago is not None:
            turns.turn_exited(token, at=NOW - ended_ago)
        given.append((what, ended_ago, env))
    turns.reap_exited_turns()

    def outcome(environ: dict) -> str:
        try:
            agent = turn_identity.caller_agent_id(environ)
        except Exception:  # noqa: BLE001 - what the server answers 503 for
            return 'cannot say'
        return f'identified {agent}' if agent else 'nobody'

    cases = [{'what': what, 'standing': 'live' if ago is None else 'ended', 'ended_ago_ms': ago,
              'outcome': outcome(env)} for what, ago, env in given]
    never, _ = turns.identity_env('id-desk')
    cases.append({'what': 'a token never given', 'standing': 'unknown', 'ended_ago_ms': None,
                  'outcome': outcome({turns.TURN_ENV: never})})
    cases.append({'what': 'a text that is no token', 'standing': 'no token', 'ended_ago_ms': None,
                  'outcome': outcome({turns.TURN_ENV: 'not a token'})})
    cases.append({'what': 'no token at all', 'standing': 'no token', 'ended_ago_ms': None,
                  'outcome': outcome({})})
    # A store that cannot be read: what the turns are kept in is not there.
    live_env = given[0][2]
    db.conn().execute('DROP TABLE provider_task_turns')
    db.conn().execute('DROP TABLE provider_turns')
    cases.append({'what': 'a turn that runs, when the store cannot be read', 'standing': 'unreadable',
                  'ended_ago_ms': None, 'outcome': outcome(live_env)})
    return {'env': turns.TURN_ENV, 'kept_ms': turns.TURN_RETENTION_MS, 'now_ms': NOW, 'cases': cases}


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
    print(f'{OUT.relative_to(REPO)}: {len(said["cases"])} cases')
    return 0


if __name__ == '__main__':
    sys.exit(main())
