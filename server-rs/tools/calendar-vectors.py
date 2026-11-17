#!/usr/bin/env python3
"""What an agent may ask its user's phone to put into a calendar, as the Python Host reads the
request (`server/lib/calendar_request.py`, `build_calendar_request`): what its own function makes
of made-up requests, the event the apps are told or the word a request is refused with, written to
`crates/clarp-kernel/tests/fixtures/calendar-vectors.json`, so that the Rust Host's reading is
held to it request by request.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/calendar-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'calendar-vectors.json'

FULL = {'session': ' theo ', 'title': 'Standup', 'start': '2027-01-15T09:00:00+01:00', 'end': '2027-01-15T09:15:00+01:00',
        'time_zone': 'Europe/Oslo', 'location': 'Room 4', 'notes': 'bring coffee', 'url': 'https://x.example',
        'all_day': 1, 'calendar': 'Work'}
LEAST = {'session': 'theo', 'title': 'T', 'start': 's', 'end': 'e'}

# What is asked: a name, and the request as an app or an agent's tool sends it.
ASKED = [
    ('everything said', FULL),
    ('the least that can be said', LEAST),
    ('blanks around every word', {key: f'  {value}\t\n' if isinstance(value, str) else value for key, value in FULL.items()}),
    ('the zone under its other name', {**LEAST, 'timezone': ' Europe/Oslo '}),
    ('the zone under both names', {**LEAST, 'time_zone': 'Europe/Oslo', 'timezone': 'America/New_York'}),
    ('the zone under both names, the first of them blank', {**LEAST, 'time_zone': '  ', 'timezone': 'America/New_York'}),
    ('the zone under both names, the first of them nothing', {**LEAST, 'time_zone': None, 'timezone': 'UTC'}),
    *[(f'no {key}', {k: v for k, v in FULL.items() if k != key}) for key in ('session', 'title', 'start', 'end')],
    *[(f'{key} is nothing', {**FULL, key: None}) for key in ('session', 'title', 'start', 'end')],
    *[(f'{key} is blank', {**FULL, key: ' \t '}) for key in ('session', 'title', 'start', 'end')],
    ('nothing said at all', {}),
    ('two things missing: the first is named', {'title': 'T'}),
    ('a number for a title', {**LEAST, 'title': 5}),
    ('a fraction for a title', {**LEAST, 'title': 2.50}),
    ('a whole fraction for a title', {**LEAST, 'title': 3.0}),
    ('a truth for a title', {**LEAST, 'title': True}),
    ('a falsehood for a title', {**LEAST, 'title': False}),
    ('zero for a title', {**LEAST, 'title': 0}),
    ('a list for a title', {**LEAST, 'title': ['a', 1, None, True, 1.5]}),
    ('an empty list for a title', {**LEAST, 'title': []}),
    ('an object for a title', {**LEAST, 'title': {'k': 'v', 'n': 1}}),
    ('an object for notes', {**LEAST, 'notes': {'it\'s': "said \"so\""}}),
    ('numbers for the times', {**LEAST, 'start': 1800000000, 'end': 1800000900.5}),
    ('a large number', {**LEAST, 'title': 12345678901234567890}),
    ('a small fraction', {**LEAST, 'title': 0.00001}),
    ('all day: a truth', {**LEAST, 'all_day': True}),
    ('all day: a falsehood', {**LEAST, 'all_day': False}),
    ('all day: nothing', {**LEAST, 'all_day': None}),
    ('all day: zero', {**LEAST, 'all_day': 0}),
    ('all day: a fraction of zero', {**LEAST, 'all_day': 0.0}),
    ('all day: a number', {**LEAST, 'all_day': 2}),
    ('all day: the word false', {**LEAST, 'all_day': 'false'}),
    ('all day: an empty word', {**LEAST, 'all_day': ''}),
    ('all day: a blank word', {**LEAST, 'all_day': ' '}),
    ('all day: an empty list', {**LEAST, 'all_day': []}),
    ('all day: a list', {**LEAST, 'all_day': [0]}),
    ('all day: an empty object', {**LEAST, 'all_day': {}}),
    ('all day: an object', {**LEAST, 'all_day': {'a': 0}}),
    ('more than is known', {**LEAST, 'colour': 'red', 'request_id': 'cal-chosen-by-the-asker', 'type': 'another'}),
    ('other letters and signs', {**LEAST, 'title': 'Møte på kontoret ☕', 'location': '北京', 'notes': 'line one\nline two'}),
    ('a blank no keyboard writes', {**LEAST, 'title': '  Standup　', 'location': '\x1c\x1fRoom\x1e'}),
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host makes of the requests above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-calendar-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import calendar_request, events

    out = []
    for name, asked in ASKED:
        # As a request arrives: read from its JSON.
        said = json.loads(json.dumps(asked))
        try:
            request = calendar_request.build_calendar_request(said, request_id='cal-0123456789abcdef')
        except calendar_request.CalendarRequestError as refused:
            out.append({'name': name, 'asked': asked, 'refused': str(refused)})
            continue
        from dataclasses import asdict
        told = events.calendar_request(**asdict(request))
        # What every event carries beside what it says (when it was made, its number) is the stream's.
        said_of_it = {key: told[key] for key in request.as_event(told['type'])}
        assert said_of_it == request.as_event(told['type']), name
        out.append({'name': name, 'asked': asked, 'told': said_of_it})
    return {'asked': out}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    rows = said['asked']
    out = ['{', ' "asked": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, row in enumerate(rows))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    told = sum('told' in row for row in rows)
    print(f'{OUT.relative_to(REPO)}: {len(rows)} requests, {told} told, {len(rows) - told} refused; '
          f'all day: {sum(bool(row.get("told", {}).get("all_day")) for row in rows)} of them')
    for row in rows:
        shown = row.get('refused') or {k: row['told'][k] for k in ('title', 'start', 'end', 'time_zone', 'all_day')}
        print(f'  {row["name"]}: {shown}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
