#!/usr/bin/env python3
"""What a Host's own turns used, as the Python Host counts it for an app's usage screen
(`server/lib/turn_usage.py`: what each turn's tool said it took in and gave out, summed over the
last five hours and the last seven days; what a turn of Claude Code cost, which tells what its whole
session has cost so far (`claude_cost_delta`); and `server/lib/backend_usage_claude.py`,
`_claude_response_row`: the row an app is given of it): what its own functions answer for made-up
turns on a database of their own, written to `crates/clarp-kernel/tests/fixtures/usage-vectors.json`,
so that the Rust Host's counting is held to it.

The moment from which that Host counts Claude's cost turn by turn is when its database was brought to
that way of counting; here it is set to one moment, so that the same is written at every run.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network. Nothing is asked of any provider: only what turns reported is counted.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/usage-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'usage-vectors.json'

T0 = 1_800_000_000_000
H, M = 3_600_000, 60_000
# From when Claude's cost is counted turn by turn, in these stories.
SINCE = T0 - 30 * 24 * H


def turn(tool, before, tokens_in=None, tokens_out=None, cost=None, took=None, told_a_total=False):
    """A turn that ended `before` milliseconds before T0, and what its tool said it used. `told_a_total`:
    its tool said what its session has cost so far, whatever is known of the turn's own share."""
    said = {}
    if tokens_in is not None:
        said['tokens_in'] = tokens_in
    if tokens_out is not None:
        said['tokens_out'] = tokens_out
    if cost is not None:
        said['cost_usd'] = cost
    if took is not None:
        said['duration_ms'] = took
    return {'tool': tool, 'at': T0 - before, 'said': said, 'told_a_total': told_a_total}


# The stories: a name, the turns that ended (in the order they ended), and the moments it is asked at.
STORIES = [
    ('nothing ran', [], [T0]),
    ('one turn of each tool', [turn('claude', 10 * M, 1200, 300, 0.0125, 4000), turn('codex', 5 * M, 800, 90)], [T0]),
    ('the edges of the windows', [
        turn('claude', 7 * 24 * H + 1, 1, 1, 0.5),         # a moment more than seven days ago
        turn('claude', 7 * 24 * H, 10, 10, 0.25),          # seven days ago to the millisecond
        turn('claude', 7 * 24 * H - 1, 100, 100, 0.125),   # a moment less
        turn('claude', 5 * H + 1, 1000, 1000),
        turn('claude', 5 * H, 10000, 10000, 1.0),
        turn('claude', 5 * H - 1, 100000, 100000),
        turn('claude', 0, 7, 7),                           # this very moment
    ], [T0, T0 + 1, T0 + 5 * H, T0 + 7 * 24 * H]),
    ('what a turn does not say', [
        turn('claude', 50 * M),                            # nothing: not counted at all
        turn('claude', 40 * M, tokens_in=5),               # only what it took in
        turn('claude', 30 * M, tokens_out=7),              # only what it gave out
        turn('claude', 20 * M, cost=0.75),                 # only what it cost
        turn('claude', 15 * M, 0, 0),                      # zeros are said
        turn('claude', 10 * M, took=900),                  # only how long it took: nothing of use
    ], [T0]),
    ('costs', [
        turn('claude', 30 * M, 1, 1, 0.1), turn('claude', 20 * M, 1, 1, 0.2), turn('claude', 10 * M, 1, 1),
        turn('codex', 30 * M, 2, 2), turn('codex', 20 * M, 2, 2),
    ], [T0]),
    ('the newest turn is old', [turn('claude', 61 * M, 3, 4), turn('claude', 9 * 24 * H, 5, 6)], [T0, T0 - 2 * M, T0 + 8 * 24 * H]),
    ('fresh to the millisecond', [turn('claude', 60 * M, 3, 4)], [T0, T0 + 1]),
    ('a turn whose share of the cost is not known is counted all the same', [
        turn('claude', 30 * M, told_a_total=True),         # a total, and nothing known of the turn: counted
        turn('claude', 20 * M, 4, 5, told_a_total=True),   # and with what it took in and gave out
        turn('claude', 10 * M, 1, 1, 0.25, told_a_total=True),
        turn('codex', 10 * M, told_a_total=True),
    ], [T0]),
]

# What a turn of Claude Code cost: what its session's total grew by. Each story is one Host's; each step a turn
# that ended in a session of the tool's, with the total it told, and whether the session began with the turn.
A, B = 'session-a', 'session-b'


def cost(session, total, fresh=False):
    return {'session': session, 'total': total, 'fresh': fresh}


RISES = [
    ('a session from its first turn', [cost(A, 0.125, True), cost(A, 0.375), cost(A, 0.375), cost(A, 0.875)]),
    ('a session that was taken up', [cost(A, 2.5), cost(A, 2.75), cost(A, 3.0)]),
    ('a total that fell', [cost(A, 1.0, True), cost(A, 0.25), cost(A, 0.5), cost(A, 0.5), cost(A, 0.0), cost(A, 0.125)]),
    ('two sessions side by side', [cost(A, 0.5, True), cost(B, 4.0), cost(A, 0.75), cost(B, 4.5), cost(B, 4.5, True),
                                   cost(A, 0.75, True)]),
    ('what is not told', [cost(A, None, True), cost(A, 0.5), cost('', 0.25, True), cost(A, 0.75), cost(A, None),
                          cost(A, 1.0)]),
    ('nothing at first', [cost(A, 0.0, True), cost(A, 0.0), cost(A, 0.5), cost(B, 0.0), cost(B, 0.25)]),
    ('new again', [cost(A, 0.5, True), cost(A, 0.25, True), cost(A, 1.0, True)]),
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host counts for the stories above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    out = []
    sys.path.insert(0, str(REPO / 'server'))
    def alone(kind, number, name):
        # Every story has a database of its own: in a process of its own.
        ran = subprocess.run([sys.executable, '-I', __file__, kind, str(number)], capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
        if ran.returncode != 0:
            raise SystemExit(f'{name}: {ran.stderr[-3000:]}')
        return json.loads(ran.stdout.splitlines()[-1])

    for number, (name, turns, asked_at) in enumerate(STORIES):
        out.append({'name': name, 'turns': turns, 'asked': alone('--story', number, name)})
    rises = [{'name': name, 'steps': alone('--rise', number, name)} for number, (name, _steps) in enumerate(RISES)]
    return {'since': SINCE, 'stories': out, 'rises': rises}


def isolated(prefix):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))


def rise(number: int) -> list:
    name, steps = RISES[number]
    isolated('clarp-usage-rise-vectors-')
    from lib import db, log, turn_usage

    db.conn()
    turn_usage.log = lambda *said: None
    log.log = lambda *said: None
    answered = []
    for step in steps:
        answered.append({**step, 'share': turn_usage.claude_cost_delta(step['session'], step['total'], fresh=step['fresh'])})
    return answered


def story(number: int) -> list:
    name, turns, asked_at = STORIES[number]
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-usage-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import backend_usage, db, turn_usage

    clock = {'now': T0}
    turn_usage.now_ms = lambda: clock['now']
    db.conn().execute("UPDATE settings SET value=? WHERE key='usage.claude_cost_activation_ms'", (str(SINCE),))
    for one in turns:
        clock['now'] = one['at']
        turn_usage.record(backend=one['tool'], agent_id='synthetic-agent', detail=dict(one['said']), trace_id='synthetic-trace',
                          reported_cost=one['told_a_total'])
    asked = []
    for now in asked_at:
        clock['now'] = now
        asked.append({'now': now,
                      'totals': {tool: turn_usage.totals(tool, now=now) for tool in ('claude', 'codex')},
                      'row': backend_usage._claude_response_row(now)})
    return asked


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == '--story':
        print(json.dumps(story(int(sys.argv[2]))))
        return 0
    if len(sys.argv) > 2 and sys.argv[1] == '--rise':
        print(json.dumps(rise(int(sys.argv[2]))))
        return 0
    said = run()
    rows = said['stories']
    out = ['{', f' "since": {said["since"]},', ' "stories": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, row in enumerate(rows))
    out.append(' ],')
    out.append(' "rises": [')
    rises = said['rises']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rises) - 1 else '') for n, row in enumerate(rises))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    known = sum(1 for r in rises for step in r['steps'] if step['share'] is not None)
    print(f'{OUT.relative_to(REPO)}: {len(rows)} stories, {sum(len(r["turns"]) for r in rows)} turns, '
          f'{sum(len(r["asked"]) for r in rows)} askings; {len(rises)} stories of cost, '
          f'{sum(len(r["steps"]) for r in rises)} turns ({known} with a share that is known)')
    for row in rows:
        for asked in row['asked']:
            claude = asked['totals']['claude']['windows']
            print(f'  {row["name"]} at +{(asked["now"] - T0) // M} min: 5h {claude["five_hour"]["turns"]} turns '
                  f'{claude["five_hour"]["tokens_in"]}/{claude["five_hour"]["tokens_out"]} cost {claude["five_hour"]["cost_usd"]}; '
                  f'7d {claude["seven_day"]["turns"]} turns cost {claude["seven_day"]["cost_usd"]}; '
                  f'row: {asked["row"]["freshness"]} {asked["row"]["fetched_at"]} {asked["row"]["error"]!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
