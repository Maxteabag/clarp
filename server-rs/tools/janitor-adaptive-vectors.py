#!/usr/bin/env python3
"""Writes the vectors the Rust Host's adaptive intervals of a janitor are tested against.

A janitor that runs at intervals waits longer while it finds nothing to do: every run ends as
`worked`, `idle` or `failed`; idle doubles the wait from the janitor's base up to its longest,
worked puts it back to the base, failed leaves it. The Python Host holds the whole rule in
`server/lib/janitor_adaptive.py`, as functions of what they are given. This calls them, one run
after another over lives of janitors made for it, and writes what they answered to
`crates/clarp-kernel/tests/fixtures/janitor-adaptive-vectors.json`: what a finished run counts as,
the bounds of an interval, and for each life the state after every run, the interval that follows,
and what the janitor is shown as.

    python3 server-rs/tools/janitor-adaptive-vectors.py

The module reads nothing and keeps nothing: no Host is touched or started.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-adaptive-vectors.json'

LONG = 'x' * 320
RUNS = [
    {}, {'activity': 'worked'}, {'activity': 'idle', 'activity_summary': 'nothing to do'},
    {'activity': 'failed', 'activity_summary': 'could not read'}, {'activity': 'napped'}, {'activity': ''},
    {'activity': None, 'activity_summary': None}, {'activity': 'idle', 'activity_summary': LONG},
    {'status': 'failed'}, {'status': 'error', 'error': 'the tool died'}, {'outcome': 'failed', 'activity': 'worked'},
    {'outcome': 'error', 'activity': 'idle', 'activity_summary': 'said idle', 'error': 'and broke'},
    {'status': 'completed', 'outcome': 'changed', 'activity': 'worked', 'activity_summary': 'two labels'},
    {'status': 'failed', 'error': LONG}, {'status': 'failed', 'activity_summary': '', 'error': None},
    {'status': 'completed'}, {'activity': 'WORKED'}, {'activity': 7, 'activity_summary': 7},
]
BOUNDS = [(3600, None), (3600, 86400), (3600, 0), (3600, 60), (3600, 3600), (3600, 7200), (3600, 604800), (3600, 604801),
          (3600, 10 ** 9), (0, None), (-5, 100), (1, 1), (90000, 86400), (700000, None), (60, -1), (59.9, 120.7)]
# Lives: the base, the longest, and what each run ended as (with what it said, and when).
LIVES = [
    (3600, 86400, ['idle', 'idle', 'idle', 'idle', 'idle', 'idle', 'idle', 'worked', 'idle', 'failed', 'idle', 'worked']),
    (3600, None, ['idle'] * 7 + ['failed', 'failed', 'worked']),
    (300, 0, ['idle', 'idle', 'worked', 'failed', 'idle']),
    (900, 3600, ['worked', 'idle', 'idle', 'idle', 'idle', 'failed', 'idle', 'worked', 'worked']),
    (86400, 604800, ['idle', 'idle', 'idle', 'idle', 'idle', 'worked']),
    (60, 100, ['idle', 'idle', 'idle']),
    (1, 7, ['idle', 'idle', 'idle', 'idle', 'failed', 'worked']),
]
# A janitor that is set anew in the middle of its life: the base or the longest changes.
CHANGED = [
    ((3600, 86400), ['idle', 'idle', 'idle'], (1800, 86400), ['idle', 'idle', 'worked']),
    ((3600, 86400), ['idle', 'idle', 'idle'], (3600, 7200), ['idle', 'failed', 'idle']),
    ((3600, 86400), ['idle', 'idle'], (3600, 86400), ['idle']),
    ((900, 0), ['idle', 'idle'], (900, 3600), ['idle', 'idle', 'idle']),
]


def main() -> int:
    spec = importlib.util.spec_from_file_location('janitor_adaptive', REPO / 'server' / 'lib' / 'janitor_adaptive.py')
    adaptive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adaptive)

    def lived(state, base, longest, endings, at):
        steps = []
        for number, activity in enumerate(endings):
            summary = '' if activity == 'worked' else f'{activity} {number}' + (LONG if number == 1 else '')
            before = adaptive.current(state, base_seconds=base, max_seconds=longest)
            now = at + number * 1000 if number % 2 == 0 else None
            interval = adaptive.advance(state, activity=activity, summary=summary, base_seconds=base,
                                        max_seconds=longest, now=now)
            steps.append({'activity': activity, 'summary': summary, 'now': now, 'waited': before, 'interval': interval,
                          'state': copy.deepcopy(state),
                          'shown': adaptive.cadence(state, base_seconds=base, max_seconds=longest,
                                                    next_run_at=at + interval * 1000, lane='schedule:one')})
        return steps

    said = {
        'activities': list(adaptive.ACTIVITIES), 'longest': adaptive.DEFAULT_MAX_INTERVAL_SECONDS,
        'limit': adaptive.MAX_INTERVAL_LIMIT_SECONDS, 'summary_most': adaptive.SUMMARY_LIMIT,
        'outcomes': [{'run': run, 'is': list(adaptive.outcome(run))} for run in RUNS],
        'bounds': [{'base': base, 'longest': longest, 'is': list(adaptive.bounds(base, longest))} for base, longest in BOUNDS],
        'fresh': [{'base': base, 'longest': longest,
                   'waits': adaptive.current({}, base_seconds=base, max_seconds=longest),
                   'shown': adaptive.cadence({}, base_seconds=base, max_seconds=longest, next_run_at=None, lane='')}
                  for base, longest in BOUNDS if base >= 1],
        'lives': [], 'changed': [],
    }
    for base, longest, endings in LIVES:
        said['lives'].append({'base': base, 'longest': longest, 'runs': lived({}, base, longest, endings, 1_800_000_000_000)})
    for (base, longest), first, (then_base, then_longest), second in CHANGED:
        state = {}
        before = lived(state, base, longest, first, 1_800_000_000_000)
        said['changed'].append({'base': base, 'longest': longest, 'runs': before, 'then_base': then_base,
                                'then_longest': then_longest,
                                'waits': adaptive.current(state, base_seconds=then_base, max_seconds=then_longest),
                                'shown': adaptive.cadence(state, base_seconds=then_base, max_seconds=then_longest,
                                                          next_run_at=7, lane='active-interval:two'),
                                'then': lived(state, then_base, then_longest, second, 1_800_000_100_000)})
    try:
        adaptive.advance({}, activity='napped', base_seconds=60, max_seconds=None)
        said['unknown'] = None
    except ValueError as refused:
        said['unknown'] = str(refused)
    out = ['{']
    parts = list(said)
    for at, part in enumerate(parts):
        rows = said[part]
        end = ',' if at < len(parts) - 1 else ''
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            out.append(f' "{part}": [')
            out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                       for index, row in enumerate(rows))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(rows, ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    runs = sum(len(life['runs']) for life in said['lives']) + sum(len(life['runs']) + len(life['then']) for life in said['changed'])
    print(f'{OUT.relative_to(REPO)}: {len(said["outcomes"])} finished runs, {len(said["bounds"])} bounds, '
          f'{len(said["lives"])} lives and {len(said["changed"])} that were set anew: {runs} runs in all')
    return 0


if __name__ == '__main__':
    sys.exit(main())
