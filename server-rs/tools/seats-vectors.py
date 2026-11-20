#!/usr/bin/env python3
"""What the Python Host tells an app of the seats of a machine's accounts
(`server/lib/hotseat_status.py`, the answer of `GET /hotseat/status`), for the shared fixtures of
that contract (`tests/fixtures/hotseat_status`: what the owner's tool answered and what the
switcher did, written by hand, each with the exact body that is to be served).

The shared fixtures were written for the shape of the body, and many a rule of the keeping is
in none of them (planted wrongly into the Rust Host's port, twelve of thirty-nine rules went
unnoticed). So there are more cases here, written for those rules (`MORE`): they have no body
written by hand, and what the Python Host answers for them is the vector.

Every case is given to the Python Host's own functions as that Host's own test gives it
(`tests/unit/test_hotseat_status.py`, `test_shared_wire_fixture`): a read is recorded, a failed
read, what the switcher did; then the body is made. It must be the body the fixture says, or this
script fails: the fixtures are then no longer what the reference answers. What it answered is
written, with the case, to `crates/clarp-kernel/tests/fixtures/seats-vectors.json`, so that the
Rust Host's keeping and serving of the same is held to it.

It runs in processes of its own, one for each case, whose every home, data, config and cache
directory is a new empty directory (the replay harness's own isolation, `tests/replay/host.py`),
with no way out to any network. The owner's tool is never run: what it would answer is in the
fixtures.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/seats-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
CASES = REPO / 'tests' / 'fixtures' / 'hotseat_status'
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'seats-vectors.json'


T0 = 1_800_000_000_000


def claude(alias, *, active=False, error=None, error_code=None, **usage):
    """A row of `hotseat list --json`: an account of Claude, a quarter of its five hours used."""
    row = {'alias': alias, 'email': f'{alias.split("@")[0]}@example.invalid', 'org': 'An Org', 'plan': 'max',
           'plan_label': 'Max 5×', 'is_active': active, 'error': error,
           'usage': {'status': 'allowed', 'limited': False, 'used_5h': 0.25, 'reset_5h': T0 / 1000 + 3600,
                     'used_7d': 0.5, 'reset_7d': T0 / 1000 + 86400, 'scoped': [], **usage}}
    return row if error_code is None else {**row, 'error_code': error_code}


def codex(alias, windows, *, active=False, **usage):
    """A row of `hotseat codex --json`: an account of Codex with these windows."""
    return {'provider': 'codex', 'alias': alias, 'is_active': active, 'email': f'{alias}@example.invalid',
            'plan': 'plus', 'account_id': f'acct-{alias}',
            'usage': {'usable': True, 'blocked': False, 'error': None, 'windows': windows, **usage}}


def week(used, reset=T0 / 1000 + 86400):
    return {'label': 'codex primary weekly', 'used': used, 'reset': reset}


def read(provider, accounts, at=T0, **more):
    return {'op': 'read', 'provider': provider, 'received_at': at, 'payload': {'accounts': accounts, **more}}


def did(provider, decision, at, *, mode='automatic', switched=True, floor=25, cadence=300):
    return {'op': 'action', 'provider': provider, 'decision': decision, 'mode': mode, 'switched': switched,
            'now': at, 'floor_pct': floor, 'cadence_s': cadence, 'next_due_at': at + cadence * 1000}


def case(name, steps, *, served_at=T0 + 1000, enabled=True):
    return {'case': name, 'janitor_enabled': enabled, 'served_at': served_at, 'steps': steps}


NOOP = {'action': 'noop', 'active': 'work', 'remaining': 75}
MORE = [
    case('M01-above-the-whole', [
        read('claude', [claude('work', active=True, used_5h=1.2, used_7d=1.7)]),
        read('codex', [codex('main', [week(1.49), {'label': 'codex primary 5-hour', 'used': -0.1, 'reset': T0 / 1000 + 3600}])])]),
    case('M02a-fresh-within-twice-the-cadence', [read('claude', [claude('work', active=True)]),
                                               did('claude', NOOP, T0 + 1000, cadence=600)], served_at=T0 + 900_000),
    case('M02b-stale-after-twice-the-cadence', [read('claude', [claude('work', active=True)]),
                                              did('claude', NOOP, T0 + 1000, cadence=600)], served_at=T0 + 1_201_000),
    case('M03-an-alias-like-a-token', [read('claude', [claude('abcdefghijklmnopqrstuvwx', active=True), claude('Work-2')])]),
    case('M04-a-failed-row-of-codex-keeps-what-was-good', [
        read('codex', [codex('main', [week(0.3)], active=True)]),
        read('codex', [codex('main', [week(0.9)], active=True, error='the provider said no')], at=T0 + 60_000)],
         served_at=T0 + 61_000),
    case('M05-a-failed-row-of-claude-keeps-what-was-good', [
        read('claude', [claude('work', active=True)]),
        read('claude', [claude('work', active=True, error='refresh failed', used_5h=0.9, used_7d=0.95)], at=T0 + 60_000)],
         served_at=T0 + 61_000),
    case('M06-a-login-that-no-longer-holds-gives-no-numbers', [
        read('claude', [claude('work', active=True, error_code='auth_error', used_5h=0.9)]),
        read('codex', [codex('main', [week(0.9)], active=True, error_code='auth_error')])]),
    case('M07-every-reset-passed', [
        read('claude', [claude('work', active=True, reset_5h=T0 / 1000 + 60, reset_7d=T0 / 1000 + 120)])],
         served_at=T0 + 300_000),
    case('M08-a-kept-reading-says-its-age', [
        read('codex', [codex('main', [week(0.4)], active=True)], quota_age_s=120, quota_read=False)]),
    case('M09a-a-skip-nobody-wrote', [read('claude', [claude('work', active=True)]),
                                     did('claude', {'action': 'skip', 'reason': 'something nobody wrote'}, T0 + 1000)]),
    case('M09b-the-two-skips-there-are', [
        read('claude', [claude('work', active=True)]), read('codex', [codex('main', [week(0.4)], active=True)]),
        did('claude', {'action': 'skip', 'reason': 'Hotseat reports no account in use'}, T0 + 1000),
        did('codex', {'action': 'skip', 'reason': 'main: quota unknown'}, T0 + 1000, cadence=900)]),
    case('M10-a-receipt-names-the-alias-as-it-is-kept', [
        read('claude', [claude('sam@example.invalid'), claude('sue@elsewhere.invalid', active=True)]),
        did('claude', {'action': 'switch', 'active': 'sue@elsewhere.invalid', 'remaining': 5,
                       'target': 'sam@example.invalid', 'target_remaining': 90, 'rung': 25}, T0 + 1000)]),
    case('M11-limited-is-used-up', [read('claude', [claude('work', active=True, limited=True, used_5h=0.4)])]),
    case('M12-rejected-is-used-up', [read('claude', [claude('work', active=True, status='rejected', used_5h=0.4)])]),
    case('M13-not-usable-and-not-blocked', [read('codex', [codex('main', [week(0.4)], active=True, usable=False)])]),
    case('M14-a-switch-that-was-not-made', [
        read('claude', [claude('work', active=True), claude('spare')]),
        did('claude', {'action': 'switch', 'active': 'work', 'target': 'spare'}, T0 + 1000, switched=False)]),
]


@tool_scratch.case('clarp-seats-vectors-')
def served(root, case: str) -> dict:
    """Runs in a process of its own: one case, from an empty store."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import db
    from lib import hotseat_status as status
    db.conn()
    more = {one['case']: one for one in MORE}
    scenario = more[case] if case in more else json.loads((CASES / f'{case}.input.json').read_text())
    status.janitor_enabled = lambda: scenario['janitor_enabled']
    # A read that failed is handed over as the error it ended with; the fixtures name it by
    # what that Host makes of the error.
    errors = {'timeout': lambda: subprocess.TimeoutExpired(['hotseat'], 60), 'cli_failed': lambda: RuntimeError('exit 1'),
              'parse_failed': lambda: ValueError('bad json'), 'provider_error': lambda: KeyError('x')}
    for step in scenario['steps']:
        if step['op'] == 'read':
            status.record_read(step['provider'], step['payload'], step['received_at'])
        elif step['op'] == 'action':
            status.record_action(step['provider'], step['decision'], mode=step['mode'], switched=step['switched'],
                                 now=step['now'], floor_pct=step['floor_pct'], cadence_s=step['cadence_s'],
                                 next_due_at=step['next_due_at'])
        else:
            status.record_failure(step['provider'], errors[step['error']](), now=step['now'])
    return {**scenario, 'wire': status.payload(served_at=scenario['served_at'])}


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == '--case':
        sys.stdout.write('\nVECTORS ' + json.dumps(served(sys.argv[2])) + '\n')
        return 0
    shared = sorted(path.name[:-len('.input.json')] for path in CASES.glob('*.input.json'))
    told, unlike = [], []
    for case in [*shared, *(one['case'] for one in MORE)]:
        run = subprocess.run([sys.executable, __file__, '--case', case], capture_output=True, text=True)
        lines = [line for line in run.stdout.splitlines() if line.startswith('VECTORS ')]
        if run.returncode or not lines:
            sys.stderr.write(f'{case}: the case did not run to its end\n{run.stderr[-1500:]}\n')
            return 1
        one = json.loads(lines[-1][len('VECTORS '):])
        if case in shared and one['wire'] != json.loads((CASES / f'{case}.wire.json').read_text()):
            unlike.append(case)
        told.append(one)
    if unlike:
        sys.stderr.write('the Python Host no longer answers what these fixtures say: ' + ', '.join(unlike) + '\n')
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'cases': told}, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    steps = sum(len(one['steps']) for one in told)
    accounts = sum(len(provider['accounts']) for one in told for provider in one['wire']['providers'])
    print(f'{len(told)} cases ({len(shared)} shared, {len(MORE)} more), {steps} steps, {accounts} accounts served '
          f'-> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
