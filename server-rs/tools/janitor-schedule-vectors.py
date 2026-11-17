#!/usr/bin/env python3
"""Writes the vectors the Rust Host's schedule of a janitor is tested against.

A janitor's schedule is a cron expression read in a time zone: `30 8 * * 1-5` in `Europe/Oslo`.
The Python Host finds its next run in `server/lib/janitor_schedule.py`: it goes through the local
days, skips a local time that does not come (the hour a clock is put forward over) and runs one that
comes twice (the hour it is put back over) the first time. This calls that module for expressions
and zones chosen to cross such hours, and writes what it answered to
`crates/clarp-kernel/tests/fixtures/janitor-schedule-vectors.json`: the next run after each of many
moments, the time between two runs, the next few runs as an app previews them, and which names
are a time zone at all.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/janitor-schedule-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. Nothing of a Host is touched or started. The zones are the machine's own
(`/usr/share/zoneinfo`), which is what both Hosts read.
"""
from __future__ import annotations

import calendar
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-schedule-vectors.json'

HOUR = 3_600_000


def at(year, month, day, hour=0, minute=0):
    """A moment in UTC, in milliseconds."""
    return calendar.timegm((year, month, day, hour, minute, 0)) * 1000


# Each zone with the moments its clocks change near (UTC), in the years the Host will live in, and
# one far ahead, where a zone's rule is read from the end of its file and not from its table.
ZONES = {
    'UTC': [at(2026, 12, 31, 12), at(2028, 2, 28, 12)],
    'Europe/Oslo': [at(2027, 3, 28, 1), at(2027, 10, 31, 1), at(2026, 12, 31, 12), at(2045, 3, 26, 1), at(2045, 10, 29, 1)],
    'America/New_York': [at(2027, 3, 14, 7), at(2027, 11, 7, 6), at(2045, 3, 12, 7)],
    # Half an hour forward and back.
    'Australia/Lord_Howe': [at(2027, 4, 3, 15), at(2027, 10, 2, 15, 30)],
    # No change, and three quarters of an hour off.
    'Asia/Kathmandu': [at(2027, 6, 1, 12)],
    # A change at midnight: the first hour of a day does not come.
    'America/Santiago': [at(2027, 4, 4, 3), at(2027, 9, 5, 4)],
    'Pacific/Chatham': [at(2027, 4, 3, 14), at(2027, 9, 25, 14)],
    'Etc/GMT+5': [at(2027, 1, 1)],
}
# Around each such moment: from two days before to two days after, every seven hours and a bit.
STEPS = [step * (7 * HOUR + 13 * 60_000) - 48 * HOUR for step in range(14)]
EXPRESSIONS = ['30 2 * * *', '0 2 * * *', '*/20 0-3 * * *', '15 1 * * *', '0 9 * * 1-5', '17 * * * *', '0 0 1 * *',
               '0 0 29 2 *', '45 23 31 12 *', '0 0 * * 0', '30 0 * * *', '0 0 30 2 *']
NAMES = ['UTC', 'Europe/Oslo', 'europe/oslo', 'EUROPE/OSLO', 'Europe/Nowhere', '', ' ', 'Europe', 'Europe/', '/Europe/Oslo',
         '../Europe/Oslo', 'Europe/../Europe/Oslo', '/usr/share/zoneinfo/UTC', 'Etc/GMT+5', 'Etc/GMT-14', 'GMT', 'Z', 'utc',
         'America/Argentina/Buenos_Aires', 'America/Indiana/Indianapolis', 'US/Eastern', 'CET', 'EST5EDT', 'Zulu',
         'Asia/Calcutta', 'Asia/Kolkata', 'Antarctica/Troll', 'Pacific/Kiritimati', 'Europe/Oslo ', ' Europe/Oslo',
         'Europe\\Oslo', 'Europe/Oslo\x00', 'localtime', 'posixrules', 'posix/Europe/Oslo', 'right/Europe/Oslo',
         'zone.tab', 'tzdata.zi', 'leapseconds', '+02:00', 'UTC+2', 'Factory']


def run() -> dict:
    """Runs in a process of its own: what the Python Host's schedule makes of each."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-janitor-schedule-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from zoneinfo import ZoneInfo
    from lib import janitor_schedule

    def known(name):
        try:
            ZoneInfo(name)
            return True
        except Exception:  # noqa: BLE001 - every way a name is no zone
            return False

    def attempt(call):
        try:
            return {'is': call()}
        except ValueError as refused:
            return {'refused': str(refused)}

    runs = []
    for zone, moments in ZONES.items():
        for expression in EXPRESSIONS:
            for moment in moments:
                # The moments in a row, each with the run that comes next: told once for each run.
                row = [[moment + step, janitor_schedule.compute_next_run(expression, zone, moment + step)] for step in STEPS]
                runs.append({'expression': expression, 'zone': zone, 'next': row})
    apart = [{'expression': expression, 'zone': zone, 'from': moments[0] - 30 * HOUR,
              'seconds': janitor_schedule.base_interval_seconds(expression, zone, moments[0] - 30 * HOUR)}
             for zone, moments in ZONES.items() for expression in EXPRESSIONS]
    previews = [{'expression': expression, 'zone': zone, 'from': moment - 26 * HOUR, 'count': count,
                 **attempt(lambda: janitor_schedule.preview_next_runs(expression, zone, moment - 26 * HOUR, count))}
                for zone, moments in ZONES.items() for moment in moments[:2]
                for expression, count in (('*/20 0-3 * * *', 10), ('30 2 * * *', 3), ('0 0 30 2 *', 3), ('0 9 * * 1-5', 0),
                                          ('0 9 * * 1-5', 25), ('17 * * * *', -1))]
    previews += [{'expression': expression, 'zone': zone, 'from': at(2027, 1, 1), 'count': 3,
                  **attempt(lambda: janitor_schedule.preview_next_runs(expression, zone, at(2027, 1, 1), 3))}
                 for expression, zone in (('0 9 * * *', 'Europe/Nowhere'), ('0 9 * * *', ''), ('0 9 * *', 'UTC'),
                                          ('61 9 * * *', 'Europe/Nowhere'), ('', 'UTC'), ('@daily', 'Europe/Oslo'),
                                          ('0 9 * * *', 'europe/oslo'))]
    return {'runs': runs, 'apart': apart, 'previews': previews, 'names': [[name, known(name)] for name in NAMES]}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    parts = list(said)
    for at_part, part in enumerate(parts):
        rows = said[part]
        out.append(f' "{part}": [')
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append(' ]' + (',' if at_part < len(parts) - 1 else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    moments = sum(len(row['next']) for row in said['runs'])
    none = sum(1 for row in said['runs'] for _, run_at in row['next'] if run_at is None)
    zones = sum(1 for _, is_zone in said['names'] if is_zone)
    print(f'{OUT.relative_to(REPO)}: {moments} moments over {len(said["runs"])} schedules ({none} with no next run), '
          f'{len(said["apart"])} times between two runs, {len(said["previews"])} previews, '
          f'{len(said["names"])} names ({zones} of them a zone)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
