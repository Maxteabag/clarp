#!/usr/bin/env python3
"""Team hierarchy and update rules from the Python Host in a sealed scratch database."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates/clarp-kernel/tests/fixtures/teams-vectors.json'
sys.path.insert(0, str(HERE))
import scratch


@scratch.case('clarp-teams-vectors-')
def reference(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import team_store

    steps = [
        ('make', 'team-root', {'name': '  Root \u00a0 Team ', 'color': '#123'}),
        ('make', 'team-child', {'name': 'Child', 'parent_team_id': 'team-root'}),
        ('make', 'team-empty', {'name': '\t '}),
        ('make', 'team-missing-parent', {'name': 'Other', 'parent_team_id': 'missing'}),
        ('make', 'team-bad-parent', {'name': 'Other', 'parent_team_id': False}),
        ('change', 'team-root', {'parent_team_id': 'team-child'}),
        ('change', 'team-root', {'parent_team_id': 'team-root'}),
        ('change', 'team-child', {'leader': 'not-a-member'}),
        ('change', 'team-root', {'leader_enabled': 'yes'}),
        ('change', 'team-root', {'communication_enabled': 1}),
        ('change', 'team-child', {'name': '  New  Child ', 'color': 65, 'communication_enabled': True}),
        ('change', 'team-child', {'parent_team_id': None, 'leader_enabled': False}),
        ('change', 'team-root', {'archived': True}),
        ('change', 'team-child', {'parent_team_id': 'team-root'}),
        ('change', 'team-root', {'archived': False, 'color': None}),
        ('change', 'team-child', {'parent_team_id': 'team-root'}),
    ]
    keys = ('team_id', 'name', 'color', 'created_at', 'updated_at', 'archived_at',
            'leader_agent_id', 'nudge_enabled', 'parent_team_id', 'leader_enabled',
            'communication_enabled')
    out = []
    for now, (operation, id, body) in enumerate(steps, 100):
        row = {'operation': operation, 'id': id, 'body': body, 'now': now}
        with patch.object(team_store, '_new_team_id', return_value=id), \
                patch.object(team_store, 'now_ms', return_value=now):
            try:
                result = (team_store.create_team(**body) if operation == 'make'
                          else team_store.update_team(id, **body))
            except ValueError as error:
                row['error'] = str(error)
            else:
                row['team'] = {key: result[key] for key in keys}
        out.append(row)
    return out


def main():
    if sys.argv[1:] == ['--child']:
        print(json.dumps(reference(), ensure_ascii=True))
        return
    ran = subprocess.run([sys.executable, '-I', __file__, '--child'],
                         capture_output=True, text=True, check=False,
                         env=scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}))
    if ran.returncode:
        print(ran.stderr[-3000:], file=sys.stderr)
        raise SystemExit(ran.returncode)
    rows = json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(rows, ensure_ascii=True, sort_keys=True, indent=2) + '\n')
    print(f'{len(rows)} team management vectors')


if __name__ == '__main__':
    main()
