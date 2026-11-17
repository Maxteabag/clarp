#!/usr/bin/env python3
"""What a Host's own turns used, as the Python Host counts it for an app's usage screen
(`server/lib/turn_usage.py`: what each turn's tool said it took in and gave out, summed over the
last five hours and the last seven days; and `server/lib/backend_usage_claude.py`,
`_claude_response_row`: the row an app is given of it): what its own functions answer for made-up
turns on a database of their own, written to `crates/clarp-kernel/tests/fixtures/usage-vectors.json`,
so that the Rust Host's counting is held to it.

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


def turn(tool, before, tokens_in=None, tokens_out=None, cost=None, took=None):
    """A turn that ended `before` milliseconds before T0, and what its tool said it used."""
    said = {}
    if tokens_in is not None:
        said['tokens_in'] = tokens_in
    if tokens_out is not None:
        said['tokens_out'] = tokens_out
    if cost is not None:
        said['cost_usd'] = cost
    if took is not None:
        said['duration_ms'] = took
    return {'tool': tool, 'at': T0 - before, 'said': said}


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
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host counts for the stories above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    out = []
    sys.path.insert(0, str(REPO / 'server'))
    for number, (name, turns, asked_at) in enumerate(STORIES):
        # Every story has a database of its own: in a process of its own.
        ran = subprocess.run([sys.executable, '-I', __file__, '--story', str(number)], capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
        if ran.returncode != 0:
            raise SystemExit(f'{name}: {ran.stderr[-3000:]}')
        out.append({'name': name, 'turns': turns, 'asked': json.loads(ran.stdout.splitlines()[-1])})
    return {'stories': out}


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
    db.conn()
    for one in turns:
        clock['now'] = one['at']
        turn_usage.record(backend=one['tool'], agent_id='synthetic-agent', detail=dict(one['said']), trace_id='synthetic-trace')
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
    said = run()
    rows = said['stories']
    out = ['{', ' "stories": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, row in enumerate(rows))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(rows)} stories, {sum(len(r["turns"]) for r in rows)} turns, '
          f'{sum(len(r["asked"]) for r in rows)} askings')
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
