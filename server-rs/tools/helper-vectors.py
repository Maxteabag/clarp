#!/usr/bin/env python3
"""Writes the vectors the Rust Host's rules for helper agents are tested against.

A helper is an agent another agent made to do one piece of work. How its work stands
(running, reported, done, failed, abandoned), what moves it, who may be whose parent and
which finished helpers are put away is decided in the Python Host by pure functions in
`server/lib/policies/helper_state.py`. This calls them for every state and event, and for a
set of other inputs, and writes what they decided to
`crates/clarp-kernel/tests/fixtures/helper-vectors.json`.

    python3 server-rs/tools/helper-vectors.py

Nothing of a Host is touched or started: pure functions are called.
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'helper-vectors.json'

DAY = 24 * 60 * 60 * 1000
NOW = 1_795_000_000_000


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib.policies import helper_state as policy

    states = [None, '', 'bogus', *[state.value for state in policy.HelperState]]
    events = ['bogus', '', *[event.value for event in policy.HelperEvent]]
    moves = []
    for state in states:
        for event in events:
            moved = policy.next_state(state, event)
            moves.append({'state': state, 'event': event,
                          'to': moved and {'state': moved.state.value, 'completed': moved.completed}})
    roles = [{'given': given, 'role': (lambda role: role and role.value)(policy.parse_role(given)),
              'initial_state': (lambda state: state and state.value)(
                  policy.initial_state(policy.parse_role(given) or ''))}
             for given in [None, '', 'agent', 'helper', 'janitor', ' Helper ', 'AGENT', 'boss', 0, False]]
    parents = [{'child': child, 'parent': parent, 'ancestors': ancestors,
                'refusal': policy.parent_refusal(child, parent, ancestors)}
               for child, parent, ancestors in [
                   ('', 'p', []), ('c', '', ['c']), ('c', 'c', []), ('c', 'p', []), ('c', 'p', ['g', 'c']),
                   ('c', 'p', ['g']), ('', 'p', ['p']), ('', '', [])]]
    rows = [
        {'agent_id': 'done-long-ago', 'role': 'helper', 'helper_state': 'done', 'archived_at': None,
         'helper_completed_at': NOW - 2 * DAY},
        {'agent_id': 'done-just-now', 'role': 'helper', 'helper_state': 'done', 'archived_at': None,
         'helper_completed_at': NOW - 1000},
        {'agent_id': 'done-at-the-edge', 'role': 'helper', 'helper_state': 'done', 'archived_at': None,
         'helper_completed_at': NOW - DAY},
        {'agent_id': 'done-without-a-time', 'role': 'helper', 'helper_state': 'done', 'archived_at': None,
         'helper_completed_at': None},
        {'agent_id': 'put-away-already', 'role': 'helper', 'helper_state': 'done', 'archived_at': NOW - 5,
         'helper_completed_at': NOW - 2 * DAY},
        {'agent_id': 'failed-long-ago', 'role': 'helper', 'helper_state': 'failed', 'archived_at': None,
         'helper_completed_at': NOW - 2 * DAY},
        {'agent_id': 'reported-long-ago', 'role': 'helper', 'helper_state': 'reported', 'archived_at': None,
         'helper_completed_at': None},
        {'agent_id': 'no-helper', 'role': 'agent', 'helper_state': 'done', 'archived_at': None,
         'helper_completed_at': NOW - 2 * DAY},
    ]
    due = [{'grace_ms': grace, 'now_ms': NOW,
            'due': policy.archive_due(rows, now_ms=NOW, **({} if grace is None else {'grace_ms': grace}))}
           for grace in [None, 0, 500, 3 * DAY, -5]]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/helper-vectors.py from server/lib/policies/helper_state.py. '
                        'Do not edit by hand.',
               'moves': moves, 'markable': {state.value: event.value for state, event in policy.MARKABLE.items()},
               'finished': sorted(state.value for state in policy.FINISHED),
               'default_archive_grace_ms': policy.DEFAULT_ARCHIVE_GRACE_MS,
               'roles': roles, 'parents': parents, 'rows': rows, 'due': due}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(moves)} moves, {len(roles)} roles, {len(parents)} parents, '
          f'{len(due)} archive rounds, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
