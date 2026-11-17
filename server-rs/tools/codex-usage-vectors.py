#!/usr/bin/env python3
"""What the Python Host shows an app of how much of a Codex account is used, from what Codex
itself says of its limits: the windows of the provider as an app's usage screen reads them
(`server/lib/backend_usage.py`, `_structured_provider`), the row of the tool beside them
(`backend_usage_store._response_row`), and the limit events told along the way, as its own
keeping of such records makes them (`backend_usage_codex.capture_codex_rate_limits`).

Its own code is given made-up records over a made-up day, one story at a time, each story in a
store of its own, with a clock the script holds, and is asked after every step as an app's
request asks it. What it answers is written to
`crates/clarp-host/tests/fixtures/codex-usage-vectors.json`, so that what the Rust Host shows
of the same records is held to it.

A record is given as Codex writes one into a conversation's rollout with a count of tokens
(`rate_limits`: `primary` and `secondary`, each with `used_percent`, `window_minutes` and
`resets_at`), under the name its usage endpoint gives it, and as an observation that says
nothing of what it leaves out (not as a whole reading of the account): that is what a record in
a rollout is. The account is a made-up one: the login is never read.

A story's steps: `record` (a record of limits is seen), `advance` (the clock goes on, in
seconds), `asked` (an app asks: limits whose window began anew are closed first, as a request
closes them).

It runs in processes of its own, one for each story, whose every home, data, config and cache
directory is a new empty directory (the replay harness's own isolation,
`tests/replay/host.py`), with no way out to any network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/codex-usage-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-host' / 'tests' / 'fixtures' / 'codex-usage-vectors.json'

# The moment a story begins, in milliseconds, and the made-up account its records are of.
BEGAN = 1_900_000_000_000
IDENTITY = ['generation-one', 'account-one']
# What is said of where a record came from.
SEEN_IN = 'rollout/token_count'
HOUR = 3_600
FIVE_HOURS_ON = BEGAN // 1000 + 4 * HOUR          # the five-hour window begins anew in four hours
WEEK_ON = BEGAN // 1000 + 100 * HOUR


def window(used, minutes, resets_at):
    return {'used_percent': used, 'window_minutes': minutes, 'resets_at': resets_at}


def record(five=None, week=None, **more):
    """A record of limits as a rollout has it: the five-hour window first, the week second."""
    return {'record': {
        'limit_id': 'codex', 'limit_name': None,
        'primary': None if five is None else window(five, 300, more.pop('five_resets', FIVE_HOURS_ON)),
        'secondary': None if week is None else window(week, 10_080, more.pop('week_resets', WEEK_ON)),
        'credits': {'has_credits': False, 'unlimited': False, 'balance': None},
        'individual_limit': None, 'spend_control_reached': None, 'plan_type': 'pro',
        'rate_limit_reached_type': None, **more}}


def advance(seconds):
    return {'advance': seconds}


ASKED = {'asked': True}

STORIES = [
    ('nothing-seen', 'Nothing was seen of the account yet', [ASKED]),
    ('both-windows', 'A record with both windows, asked at once, a quarter of an hour on, and an hour on',
     [record(37.5, 82.0), ASKED, advance(14 * 60), ASKED, advance(2 * 60), ASKED, advance(50 * 60), ASKED]),
    ('the-newer-record', 'A newer record takes the place of the older',
     [record(10.0, 40.0), advance(600), record(12.5, 40.5), ASKED]),
    ('only-five-hours', 'A record with the five-hour window alone, then one with the week alone',
     [record(20.0), ASKED, advance(60), record(None, 55.0), ASKED]),
    ('no-window', 'A record with no window in it is no record', [record(), ASKED]),
    ('past-its-reset', 'A window whose reset is past is stale, however young the record',
     [record(60.0, 30.0, five_resets=BEGAN // 1000 + 300), ASKED, advance(301), ASKED]),
    ('the-warning', 'Four fifths of the five-hour window are used: a warning; it is told once',
     [record(79.9, 10.0), ASKED, advance(60), record(80.0, 10.0), ASKED, advance(60), record(91.0, 10.5), ASKED]),
    ('the-limit', 'The five-hour window is used up: a hard limit, and it is over when the window begins anew',
     [record(85.0, 10.0), advance(60), record(100.0, 11.0), ASKED, advance(60), record(100.0, 11.0), ASKED,
      advance(4 * HOUR), ASKED, advance(60), record(1.0, 11.5, five_resets=FIVE_HOURS_ON + 5 * HOUR), ASKED]),
    ('straight-to-the-limit', 'Used up at the first record that is seen',
     [record(100.0, 50.0), ASKED]),
    ('the-week-is-used-up', 'The week is used up while the five hours are not',
     [record(12.0, 100.0), ASKED]),
    ('back-under', 'Under the warning again before the window began anew (another account took its place)',
     [record(90.0, 10.0), ASKED, advance(60), record(5.0, 10.0), ASKED]),
    ('another-window', 'The five-hour window began anew between two records',
     [record(95.0, 10.0), ASKED, advance(4 * HOUR + 60),
      record(2.0, 10.5, five_resets=FIVE_HOURS_ON + 5 * HOUR), ASKED]),
    ('odd-lengths', 'Windows of lengths nobody has a name for',
     [{'record': {'primary': window(33.0, 60, FIVE_HOURS_ON), 'secondary': window(44.0, 43_200, WEEK_ON),
                  'plan_type': 'team'}}, ASKED]),
    ('no-lengths', 'Windows that do not say how long they are',
     [{'record': {'primary': {'used_percent': 33.0, 'resets_at': FIVE_HOURS_ON},
                  'secondary': {'used_percent': 44.0, 'resets_at': WEEK_ON}}}, ASKED]),
    ('no-reset', 'A window that does not say when it begins anew',
     [{'record': {'primary': {'used_percent': 97.0, 'window_minutes': 300},
                  'secondary': {'used_percent': 44.0, 'window_minutes': 10_080}}}, ASKED,
      advance(60), {'record': {'primary': {'used_percent': 100.0, 'window_minutes': 300}}}, ASKED]),
]


def told(story):
    """Runs in a process of its own: one story, from an empty store."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-codex-usage-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import backend_usage as usage
    from lib import backend_usage_codex as codex
    from lib import backend_usage_store as store
    from lib import db
    db.conn()
    clock = [BEGAN]
    # Every module of the Host that took the clock's reading by name is given this clock.
    for module in list(sys.modules.values()):
        if getattr(module, '__name__', '').startswith('lib.') and hasattr(module, '_now_ms'):
            module._now_ms = lambda: clock[0]
    name, what, steps = story
    done = []
    for step in steps:
        at = {'at': (clock[0] - BEGAN) // 1000}
        if 'advance' in step:
            clock[0] += step['advance'] * 1000
            continue
        if 'record' in step:
            seen = step['record']
            payload = {'rate_limit': seen, 'plan_type': seen.get('plan_type')}
            try:
                kept = codex.capture_codex_rate_limits(payload, source_detail=SEEN_IN, identity=tuple(IDENTITY))
                did = {'limit_events': kept.get('limit_events') or []}
            except RuntimeError as refused:
                did = {'refused': str(refused)}
            done.append({**at, 'record': seen, **did})
            continue
        now = clock[0]
        closed = usage._reconcile_expired_limits(now)
        done.append({**at, 'asked': True, 'limit_events': closed,
                     'provider': usage._structured_provider('codex', now),
                     'row': store._response_row('codex', now)})
    return {'name': name, 'what': what, 'steps': done}


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == '--story':
        story = next(story for story in STORIES if story[0] == sys.argv[2])
        sys.stdout.write('\nVECTORS ' + json.dumps(told(story)) + '\n')
        return 0
    stories = []
    for story in STORIES:
        run = subprocess.run([sys.executable, __file__, '--story', story[0]], capture_output=True, text=True)
        lines = [line for line in run.stdout.splitlines() if line.startswith('VECTORS ')]
        if run.returncode or not lines:
            sys.stderr.write(f'{story[0]}: the story did not run to its end\n{run.stderr[-1500:]}\n')
            return 1
        stories.append(json.loads(lines[-1][len('VECTORS '):]))
    vectors = {'began_ms': BEGAN, 'identity': IDENTITY, 'seen_in': SEEN_IN, 'stories': stories}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    steps = sum(len(story['steps']) for story in stories)
    asked = sum(1 for story in stories for step in story['steps'] if step.get('asked'))
    events = sum(len(step.get('limit_events') or []) for story in stories for step in story['steps'])
    print(f'{len(stories)} stories, {steps} steps, {asked} askings, {events} limit events -> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
