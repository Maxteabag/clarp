#!/usr/bin/env python3
"""Writes the vectors the Rust Host's heartbeat is tested against.

A heartbeat is a turn the Host gives an idle agent that asked for it, so that the agent looks at
what it has standing and acts only when something needs it (`server/lib/heartbeat.py`). The Host
decides when: not while the agent works, waits for its user or was stopped by its user, not
sooner than the owner's interval, longer after each time the agent found nothing to do, and never
when it has gone dormant until something wakes it.

This runs that module itself and writes to `crates/clarp-kernel/tests/fixtures/heartbeat-vectors.json`
what it makes of replies to a heartbeat (the word that says nothing is to be done, and what
counts as such a reply), which settings it takes and why not, why an agent gets no heartbeat at a
moment or that it is due, how long its interval is, and what its screen shows of its cadence.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/heartbeat-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. The database it touches is a new one in that directory. No Host is touched or
started, and no agent is sent anything.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'heartbeat-vectors.json'

OK = 'HEARTBEAT_OK'
LONG = 'Something was found and done about it, and here is what. ' * 6
REPLIES = [
    '', '   ', OK, f'  {OK}  ', f'{OK}.', f'{OK}!!!', f'**{OK}**', f'"{OK}"', f'{OK}\n', f'\n\n{OK}', f'{OK} nothing to do',
    f'Nothing to do. {OK}', f'{OK} - all quiet, nothing needs me.', f'All quiet. {OK}.', f'{OK} {OK}', f'{OK}{OK}',
    f'{OK} and {OK}', f'I say {OK} but then I go on', f'Checked. {OK} is my answer for now, and more follows after it.',
    f'{OK} {LONG}', f'{LONG} {OK}', f'{OK} ' + 'x' * 300, f'{OK} ' + 'x' * 301, 'x' * 300 + f' {OK}', 'x' * 301 + f' {OK}',
    f'{OK}\n\n' + 'word ' * 60, f'{OK}\n\n' + 'word ' * 61, 'heartbeat_ok', 'HEARTBEAT_OKAY', f'{OK}AY', f'x{OK}', f'_{OK}_',
    f'({OK})', f'{OK}\t\tdone  \n  nothing', 'Checking heartbeat.', 'checking   heartbeat now', 'Heartbeat check: fine',
    'heartbeat pass', 'Heartbeat passed, nothing to do', 'I checked the heartbeat', 'Done with the migration.',
    'Checking heartbeat. ' + 'x' * 290, 'Heartbeat check ' + 'x' * 300, f'the word {OK.lower()} in small letters',
    'Tests are green and the branch is pushed.', f'{OK} ', f'​{OK}', f'{OK}…', f'✓ {OK}', f'{OK} ✓',
]

PROMPTS = ['', 'hello', '{PROMPT}', '  {PROMPT}  ', '{PROMPT}\n\nDurable plan: none active.', 'x {PROMPT}', '{RESTART}{PROMPT}',
           '{RESTART}', '{INTERRUPTED}{PROMPT}', '{INTERRUPTED}', 'The Clarp server has just restarted.', '{PROMPT}'[:-1],
           'This is a continuity check, not a new task.']

SETTINGS = [
    {}, {'heartbeat_interval_sec': 600}, {'heartbeat_interval_sec': 59}, {'heartbeat_interval_sec': 60},
    {'heartbeat_interval_sec': 86400, 'heartbeat_backoff_cap_sec': 86400}, {'heartbeat_interval_sec': 86401},
    {'heartbeat_interval_sec': '600'}, {'heartbeat_interval_sec': 600.0}, {'heartbeat_interval_sec': True},
    {'heartbeat_interval_sec': None}, {'heartbeat_backoff_cap_sec': 100}, {'heartbeat_backoff_cap_sec': 7200},
    {'heartbeat_backoff_cap_sec': 86401}, {'heartbeat_interval_sec': 7200}, {'heartbeat_interval_sec': 7200, 'heartbeat_backoff_cap_sec': 7200},
    {'heartbeat_backoff_strategy': 'fixed'}, {'heartbeat_backoff_strategy': ' linear '}, {'heartbeat_backoff_strategy': 'LINEAR'},
    {'heartbeat_backoff_strategy': 'quadratic'}, {'heartbeat_backoff_strategy': 5}, {'heartbeat_backoff_strategy': ''},
    {'heartbeat_dormant_after_noops': 0}, {'heartbeat_dormant_after_noops': 100}, {'heartbeat_dormant_after_noops': 101},
    {'heartbeat_dormant_after_noops': -1}, {'heartbeat_dormant_after_noops': '5'}, {'something_else': 1},
    {'heartbeat_interval_sec': 120, 'heartbeat_backoff_strategy': 'exponential', 'heartbeat_backoff_cap_sec': 480,
     'heartbeat_dormant_after_noops': 3},
    {'heartbeat_interval_sec': 60, 'heartbeat_backoff_cap_sec': 59}, {'heartbeat_interval_sec': 1800, 'heartbeat_backoff_cap_sec': 3600},
]

NOW = 1_800_000_000.0
HOUR = 3600.0


def latest(kind=None, ago=0.0, **detail):
    if kind is None:
        return {}
    return {'kind': kind, 'ts': int((NOW - ago) * 1000), 'detail': detail or None}


# What is known of an agent at a moment: what it last did, whether it works, and what the
# heartbeat remembers of it.
LATEST = [latest(), latest('done', 100), latest('waiting', 100), latest('interrupted', 100),
          latest('interrupted', 100, source='user_stop'), latest('interrupted', 100, reason='interrupted'),
          latest('interrupted', 100, source='server_restart'), latest('interrupted', 12 * HOUR),
          latest('interrupted', 11 * HOUR - 1), latest('interrupted', 11 * HOUR), latest('interrupted', 20),
          latest('interrupted', 12 * HOUR, source='user_stop'), latest('thinking', 5), latest('failed', 4000)]
WORKING = [(False, False, False, ''), (True, False, False, ''), (False, True, False, ''), (False, False, True, ''),
           (False, False, False, 'recent-user-activity'), (True, True, True, 'recent-user-activity')]
STATES = [
    {}, {'last_started': NOW - 10}, {'last_started': NOW - 29}, {'last_started': NOW - 30}, {'last_started': NOW - 1799},
    {'last_started': NOW - 1800}, {'last_started': NOW - 1801, 'noop_streak': 1}, {'last_started': NOW - 3599, 'noop_streak': 1},
    {'last_started': NOW - 3600, 'noop_streak': 1}, {'last_started': NOW - 3600, 'noop_streak': 2},
    {'last_started': NOW - 7200, 'noop_streak': 5}, {'last_started': NOW - 4000, 'dormant': True}, {'dormant': True},
    {'noop_streak': 3}, {'last_started': NOW - 5000, 'recent_starts': [NOW - 50, NOW - 40, NOW - 30, NOW - 20, NOW - 10]},
    {'last_started': NOW - 5000, 'recent_starts': [NOW - 61, NOW - 40, NOW - 30, NOW - 20, NOW - 10]},
    {'last_started': NOW - 5000, 'recent_starts': [NOW - 60, NOW - 40, NOW - 30, NOW - 20, NOW - 10]},
    {'last_started': NOW - 100, 'noop_streak': -1},
]
POLICIES = [
    {'interval_sec': 1800, 'backoff_strategy': 'exponential', 'backoff_cap_sec': 3600, 'dormant_after_noops': 5},
    {'interval_sec': 1800, 'backoff_strategy': 'linear', 'backoff_cap_sec': 86400, 'dormant_after_noops': 5},
    {'interval_sec': 600, 'backoff_strategy': 'fixed', 'backoff_cap_sec': 600, 'dormant_after_noops': 0},
    {'interval_sec': 60, 'backoff_strategy': 'exponential', 'backoff_cap_sec': 86400, 'dormant_after_noops': 2},
]


@tool_scratch.case('clarp-heartbeat-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's heartbeat does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import heartbeat, origins, settings_store

    words = {'PROMPT': heartbeat.HEARTBEAT_PROMPT, 'RESTART': heartbeat.RESTART_HEARTBEAT_PREFIX,
             'INTERRUPTED': heartbeat.INTERRUPTED_HEARTBEAT_PREFIX}

    def filled(text):
        for name, value in words.items():
            text = text.replace('{' + name + '}', value)
        return text

    def shown(text, origin):
        # What the conversation keeps of an answer, and what it counts as: the Python Host's
        # own steps for a row of the agent (`message_writes.py`), for this word alone.
        skip, kept = origins.ack_for_origin(heartbeat.strip_heartbeat_ack(text), text, token=heartbeat.HEARTBEAT_OK,
                                            origin=origin, own='heartbeat')
        counted = ''
        if skip:
            counted = 'noop' if origin == 'heartbeat' else ''
            kept = 'Heartbeat check: no action needed.'
        if origin == 'heartbeat' and not counted and not heartbeat.is_neutral_heartbeat_status(kept):
            counted = 'activity'
        return {'text': kept, 'counted': counted}

    def reply(text):
        skip, clean = heartbeat.strip_heartbeat_ack(text)
        return {'text': text, 'skip': skip, 'clean': clean, 'neutral': heartbeat.is_neutral_heartbeat_status(text),
                'only': origins.ack_only_token(text, heartbeat.HEARTBEAT_OK),
                'in_its_turn': shown(text, 'heartbeat'), 'in_another': shown(text, 'user')}

    def setting(change):
        for key in (heartbeat.KEY_INTERVAL_SEC, heartbeat.KEY_BACKOFF_STRATEGY, heartbeat.KEY_BACKOFF_CAP_SEC,
                    heartbeat.KEY_DORMANT_AFTER_NOOPS):
            settings_store.delete(key)
        before = heartbeat.get_settings().as_dict()
        try:
            return {'change': change, 'before': before, 'after': heartbeat.update_settings(dict(change)).as_dict()}
        except ValueError as why:
            return {'change': change, 'before': before, 'refused': str(why), 'after': heartbeat.get_settings().as_dict()}

    def decided():
        rows = []
        real = heartbeat.get_settings
        try:
            for policy in POLICIES:
                heartbeat.get_settings = lambda policy=policy: heartbeat.HeartbeatSettings(**policy)
                for last, (busy, active, compacting, recent), kept in itertools.product(LATEST, WORKING, STATES):
                    # Every combination would be thousands; the ones that decide are kept: an agent
                    # that works is asked of once for each state it was last in.
                    if (busy or active or compacting or recent) and kept is not STATES[0]:
                        continue
                    state = heartbeat._HeartbeatState(**{'recent_starts': [], **kept})
                    snapshot = heartbeat._AgentSnapshot(latest=dict(last), busy=busy, active=active,
                                                        compacting=compacting, recent_activity=recent)
                    kind = last.get('kind')
                    interrupted = kind == 'interrupted' and not heartbeat._is_user_stopped(last)
                    rows.append({'policy': policy, 'latest': last, 'busy': busy, 'active': active,
                                 'compacting': compacting, 'recent_activity': recent, 'state': kept,
                                 'reason': heartbeat._skip_reason(snapshot=snapshot, state=state, now=NOW),
                                 'interval': heartbeat._effective_interval_sec(state, is_interrupted=interrupted)})
        finally:
            heartbeat.get_settings = real
        return rows

    answered = {
        'ok': heartbeat.HEARTBEAT_OK, 'prompt': heartbeat.HEARTBEAT_PROMPT, 'restart_prefix': heartbeat.RESTART_HEARTBEAT_PREFIX,
        'interrupted_prefix': heartbeat.INTERRUPTED_HEARTBEAT_PREFIX, 'now': NOW,
        'numbers': {'default_interval_sec': heartbeat.DEFAULT_HEARTBEAT_INTERVAL_SEC,
                    'default_backoff_cap_sec': heartbeat.DEFAULT_BACKOFF_CAP_SEC,
                    'max_interrupted_retry_sec': heartbeat.MAX_INTERRUPTED_RETRY_SEC,
                    'min_wake_spacing_sec': heartbeat.MIN_WAKE_SPACING_SEC, 'flood_window_sec': heartbeat.FLOOD_WINDOW_SEC,
                    'flood_threshold': heartbeat.FLOOD_THRESHOLD, 'ack_max_chars': heartbeat.HEARTBEAT_ACK_MAX_CHARS},
        'replies': [reply(text) for text in REPLIES],
        'prompts': [{'text': filled(text), 'skip': heartbeat.should_skip_heartbeat_prompt(filled(text))} for text in PROMPTS],
        'settings': [setting(change) for change in SETTINGS],
        'decided': decided(),
    }
    shutil.rmtree(root, ignore_errors=True)
    return answered


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    for name in ('ok', 'prompt', 'restart_prefix', 'interrupted_prefix', 'now', 'numbers'):
        out.append(f' "{name}": ' + json.dumps(said[name], ensure_ascii=True) + ',')
    for at, part in enumerate(('replies', 'prompts', 'settings', 'decided')):
        out.append(f' "{part}": [')
        rows = said[part]
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append(' ]' + (',' if at < 3 else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    reasons = {}
    for row in said['decided']:
        reasons[row['reason'] or 'due'] = reasons.get(row['reason'] or 'due', 0) + 1
    print(f'{OUT.relative_to(REPO)}: {len(said["replies"])} replies, {len(said["prompts"])} prompts, '
          f'{len(said["settings"])} changes of the settings, {len(said["decided"])} decisions: '
          + ', '.join(f'{count} {reason}' for reason, count in sorted(reasons.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
