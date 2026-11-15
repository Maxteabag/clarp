#!/usr/bin/env python3
"""Writes the vectors the Rust Host's notification policy is tested against.

Whether a turn that ended tells the user (a badge, unread, a push) is decided in the Python
Host by one pure function, `server/lib/policies/notifications.py: notify_decision`. This calls
it for every combination of what it looks at and writes what it decided to
`crates/clarp-kernel/tests/fixtures/notify-vectors.json`.

    python3 server-rs/tools/notify-vectors.py

The file names the values each fact was tried with, the distinct decisions, and one decision
for every combination, in the order `itertools.product` goes through the facts as they are
listed: two hex digits each, an index into the decisions.

Nothing of a Host is touched or started: one pure function is called.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'notify-vectors.json'

FACTS = {
    'origin': ['user', 'oracle', 'agent', 'schedule', 'automation', 'watcher', 'heartbeat', 'dreaming',
               'leader_tick', 'janitor', 'system', '', '  user  ', 'something-new'],
    'content': ['speak', 'text-reply', '', 'something-else'],
    'agent_id': ['a1', ''],
    'has_cause': [True, False],
    'interrupted': [False, True],
    'client': ['', 'desktop', 'ios'],
    'present': [True, False],
    'can_chat': [True, False],
    'muted': [False, True],
    'is_team_leader': [False, True],
    'quiet': [False, True],
    'special_automation': [False, True],
}


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib.policies import notifications as policy

    decisions: list[str] = []
    table: list[str] = []
    names = list(FACTS)
    for values in itertools.product(*FACTS.values()):
        fact = dict(zip(names, values))
        decided = policy.notify_decision(
            fact['origin'],
            policy.CompletedTurn(agent_id=fact['agent_id'], has_cause=fact['has_cause'], content=fact['content'],
                                 interrupted=fact['interrupted'], client=fact['client']),
            policy.AgentCapabilities(present=fact['present'], can_chat=fact['can_chat'], muted=fact['muted'],
                                     is_team_leader=fact['is_team_leader'], quiet=fact['quiet']),
            policy.NotificationSettings(special_automation=fact['special_automation']))
        if isinstance(decided, policy.Notify):
            said = f'notify|{decided.reason}|{decided.kind}|{int(decided.push)}|{int(decided.muted)}'
        else:
            said = f'suppress|{decided.reason}'
        if said not in decisions:
            decisions.append(said)
        table.append(format(decisions.index(said), '02x'))
    if len(decisions) > 256:
        raise SystemExit(f'{len(decisions)} distinct decisions do not fit two hex digits')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/notify-vectors.py from server/lib/policies/notifications.py. '
                        'Do not edit by hand.',
               'facts': FACTS, 'decisions': decisions, 'table': ''.join(table)}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(table)} cases, {len(decisions)} distinct decisions, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
