#!/usr/bin/env python3
"""Read the working-state projection rules from the Python Host's PRs 558–561."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/coordination-working-state-vectors.json'


@tool_scratch.case('clarp-coordination-working-state-vectors-')
def generate(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import coordination, live_activity

    rows = []
    for name, hide, recovery in (
        ('Boss', True, False),
        ('', True, False),
        (None, True, False),
        ('Boss', False, False),
        ('Boss', True, True),
    ):
        headline = coordination.veiled({'sender_name': name}) if hide else ''
        activity = {
            'phase': 'account_recovery' if recovery else 'tool',
            'tool': 'Read', 'file_path': '/fixture/plan.md', 'call_id': 'call-1',
            'summary': 'Waiting for an account with available usage' if recovery else 'Reading plan.md',
            'action': 'waiting for account' if recovery else 'reading',
        }
        live = {'state': 'tool', 'headline': 'Reading plan.md', 'tool': {'name': 'Read', 'label': 'plan.md'}}
        detail = {'trace_id': 'trace-1', 'phase': activity['phase'], 'input': {'path': 'plan.md'}, 'tool': 'Read'}
        if recovery:
            detail['account_recovery'] = 'waiting'
        veiled_live = coordination.veil_live(live, headline)
        pushed = {**veiled_live, 'veiled': True} if hide else live
        rows.append(dict(
            name=name, hide=hide, headline=headline,
            activity=activity, live=live, detail=detail,
            veiled_activity=coordination.veil_activity(activity, headline),
            veiled_live=veiled_live, veiled_detail=coordination.veil_detail(detail, headline),
            pushed_headline=live_activity.row_headline(pushed),
        ))
    return rows


def main():
    if sys.argv[1:] == ['--run']:
        print(json.dumps(generate()))
        return
    proc = subprocess.run(
        [sys.executable, '-I', __file__, '--run'],
        env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}),
        text=True, check=True, capture_output=True)
    rows = json.loads(proc.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(rows, ensure_ascii=True, indent=2) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
