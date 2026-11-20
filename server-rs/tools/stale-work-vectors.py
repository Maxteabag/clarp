#!/usr/bin/env python3
"""Writes down when the Python Host retires a work label that has outlived the work behind it.

Its three rules (`lib/policies/stale_work.py`): a running job nobody can be asked after goes stale and then is lost
(`job_heartbeat`); a helper that is working and sits idle counts as having reported (`helper_idle_due`); an idle agent's
own status line older than its time is taken back (`custom_status_due`). Pure functions, run here over their edges.

    <the Python Host's interpreter> server-rs/tools/stale-work-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'stale-work-vectors.json'
MIN = 60 * 1000
HOUR = 60 * MIN


@tool_scratch.case('clarp-stale-work-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib.policies import stale_work as policy

    default = policy.Thresholds()
    limits = [('default', default),
              ('job rule off', policy.Thresholds(job_stale_after_ms=0)),
              ('no grace', policy.Thresholds(job_grace_ms=0)),
              ('negative grace', policy.Thresholds(job_grace_ms=-5 * MIN)),
              ('helper rule off', policy.Thresholds(helper_idle_after_ms=0)),
              ('status rule off', policy.Thresholds(custom_status_ttl_ms=0)),
              ('short', policy.Thresholds(job_stale_after_ms=MIN, job_grace_ms=MIN,
                                          helper_idle_after_ms=MIN, custom_status_ttl_ms=MIN))]
    jobs, helpers, statuses = [], [], []
    activities = [(False, 0, 0), (True, 0, 0), (False, 1, 0), (False, 0, 1)]
    for name, t in limits:
        for verified in (False, True):
            for age in (0, MIN, MIN + 1, 15 * MIN, 15 * MIN + 1, 2 * MIN + 1, 30 * MIN, 30 * MIN + 1, 2 * HOUR):
                verdict = policy.job_heartbeat(heartbeat_age_ms=age, worker_verified=verified, thresholds=t)
                jobs.append({'limits': name, 'stale_ms': t.job_stale_after_ms, 'grace_ms': t.job_grace_ms,
                             'verified': verified, 'age_ms': age, 'verdict': str(verdict)})
        for state in ('running', 'reported', None, 'done'):
            for busy, active_jobs, children in activities:
                for idle_for in (0, MIN, MIN + 1, 30 * MIN, 30 * MIN + 1):
                    activity = policy.Activity(busy=busy, active_jobs=active_jobs, running_children=children,
                                               idle_since_ms=1000)
                    due = policy.helper_idle_due(helper_state=state, activity=activity, now_ms=1000 + idle_for,
                                                 thresholds=t)
                    helpers.append({'limits': name, 'limit_ms': t.helper_idle_after_ms, 'state': state,
                                    'busy': busy, 'active_jobs': active_jobs, 'running_children': children,
                                    'idle_since_ms': 1000, 'now_ms': 1000 + idle_for, 'due': due})
        for status in ('', '   ', 'Building', ' æ '):
            for status_at in (None, 1000):
                for owned in (False, True):
                    for busy, active_jobs, children in activities:
                        for age in (0, MIN + 1, 2 * HOUR, 2 * HOUR + 1):
                            activity = policy.Activity(busy=busy, active_jobs=active_jobs, running_children=children,
                                                       idle_since_ms=0)
                            due = policy.custom_status_due(status=status, status_at=status_at, janitor_owned=owned,
                                                           activity=activity, now_ms=1000 + age, thresholds=t)
                            statuses.append({'limits': name, 'ttl_ms': t.custom_status_ttl_ms, 'status': status,
                                             'status_at': status_at, 'janitor_owned': owned, 'busy': busy,
                                             'active_jobs': active_jobs, 'running_children': children,
                                             'now_ms': 1000 + age, 'due': due})
    return {'what': 'server/lib/policies/stale_work.py job_heartbeat, helper_idle_due, custom_status_due',
            'defaults': {'job_stale_after_ms': default.job_stale_after_ms, 'job_grace_ms': default.job_grace_ms,
                         'helper_idle_after_ms': default.helper_idle_after_ms,
                         'custom_status_ttl_ms': default.custom_status_ttl_ms},
            'jobs': jobs, 'helpers': helpers, 'statuses': statuses}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        sys.stdout.write(json.dumps(run()) + '\n')
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    lines = ['{', f' "what": {json.dumps(said["what"])},', f' "defaults": {json.dumps(said["defaults"])},']
    for part in ('jobs', 'helpers', 'statuses'):
        rows = said[part]
        lines.append(f' "{part}": [')
        lines.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(rows) - 1 else '')
                     for n, one in enumerate(rows))
        lines.append(' ],' if part != 'statuses' else ' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(said["jobs"])} jobs, {len(said["helpers"])} helpers, '
          f'{len(said["statuses"])} statuses')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
