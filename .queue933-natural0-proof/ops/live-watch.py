#!/usr/bin/env python3
"""Read-only release observations. Probe or notice errors are visible, non-green failures."""
from pathlib import Path
import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

PROGRAM = Path('/var/tmp/clarp-refactor-program')


class ProbeFailed(RuntimeError):
    pass


def run(args, timeout=35):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def cpu(history):
    result = run(['systemctl', '--user', 'show', 'clarp.service', '--property=MainPID'])
    if result.returncode:
        raise ProbeFailed('unit_query')
    pid = int(result.stdout.strip().split('=', 1)[1])
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    ticks, start, stamp = int(fields[11]) + int(fields[12]), int(fields[19]), time.monotonic()
    row = dict(pid=pid, process_start=start, cpu_ticks=ticks, mono=stamp)
    eligible = [r for r in history if r.get('pid') == pid and r.get('process_start') == start
                and stamp - r['mono'] >= 599]
    if eligible:
        old = min(eligible, key=lambda r: abs(stamp - r['mono'] - 600))
        seconds = stamp - old['mono']
        row.update(cpu_average_seconds=seconds,
                   cpu_average_percent=100 * (ticks - old['cpu_ticks']) / os.sysconf('SC_CLK_TCK') / seconds)
    return row


def db_locked(since, now):
    result = run(['journalctl', '--user', '-u', 'clarp.service', '-u', 'clarp-runtime.service',
                  '--since', '@' + str(since), '--until', '@' + str(now), '--grep', 'database is locked',
                  '--output=json', '--no-pager', '-n', '1000'])
    # journalctl --grep uses 1 for no matches. A diagnostic or any output keeps it unknown.
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return {'db_locked_lines_10min': 0}
    if result.returncode:
        raise ProbeFailed('journal_query')
    lines = result.stdout.splitlines()
    if len(lines) >= 1000:
        raise ProbeFailed('journal_truncated')
    for line in lines:
        if not isinstance(json.loads(line), dict):
            raise ProbeFailed('journal_shape')
    return {'db_locked_lines_10min': len(lines)}


def http(since, now):
    connection = sqlite3.connect('file:/home/peter/.local/share/clarp/telemetry.sqlite?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA busy_timeout=2000')
        count = connection.execute('SELECT COUNT(*) FROM diagnostic_events WHERE ts>=? AND ts<? AND status>=500',
                                   (int(since * 1000), int(now * 1000))).fetchone()[0]
        return {'http5xx_10min': count}
    finally:
        connection.close()


def runtime():
    def one():
        result = run([str(PROGRAM / 'bin/release-check'), 'status'])
        if result.returncode:
            raise ProbeFailed('status_query')
        probe = json.loads(result.stdout)['probe']
        if not isinstance(probe.get('runtime_available'), bool):
            raise ProbeFailed('runtime_unknown')
        return probe
    probe = one()
    row = {'host_code': probe.get('status_code'), 'host_ready': probe.get('health_ready'),
           'runtime_available': probe['runtime_available'], 'runtime_id': probe.get('runtime_release_id')}
    if probe['runtime_available'] is False:
        time.sleep(3)
        row['runtime_repeat_available'] = one()['runtime_available']
    return row


def sample(history, now, since, cpu_after, notify):
    """Every probe, threshold and fault notice runs despite failures in its siblings."""
    row = {'epoch': now, 't': datetime.datetime.fromtimestamp(now).astimezone().isoformat()}
    errors = {}
    for name, probe in [('cpu', lambda: cpu(history)), ('db', lambda: db_locked(since, now)),
                        ('http', lambda: http(since, now)), ('runtime', runtime)]:
        try:
            row.update(probe())
        except Exception as error:
            errors[name] = type(error).__name__
    notices = []
    if now >= cpu_after and row.get('cpu_average_percent', 0) > 35:
        notices.append(('HostCPU_over35_10min', {'percent': row['cpu_average_percent'],
                                              'seconds': row['cpu_average_seconds']}))
    if row.get('db_locked_lines_10min', 0) > 5:
        notices.append(('DBlocked_over5_10min', {'lines': row['db_locked_lines_10min']}))
    if row.get('http5xx_10min', 0) >= 3:
        notices.append(('HTTP5xx_burst', {'count': row['http5xx_10min']}))
    if row.get('runtime_available') is False and row.get('runtime_repeat_available') is False:
        notices.append(('runtime_unavailable_two_probes', {'host_ready': row.get('host_ready')}))
    if errors:
        notices.append(('WATCH_PROBE_FAILED', {'probes': errors}))
        row['probe_errors'] = errors
    delivery_errors = {}
    for kind, fields in notices:
        try:
            notify(kind, dict(t=row['t'], **fields))
        except Exception as error:
            delivery_errors[kind] = type(error).__name__
    if delivery_errors:
        row['notice_errors'] = delivery_errors
    row['result'] = 'WATCH_FAILED' if errors or delivery_errors else 'OBSERVED'
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--until', required=True)
    parser.add_argument('--cpu-after', required=True)
    parser.add_argument('--since', required=True)
    parser.add_argument('--prefix', required=True)
    args = parser.parse_args()
    if not args.prefix or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._' for c in args.prefix):
        parser.error('invalid receipt prefix')
    end, cpu_after, floor = (datetime.datetime.fromisoformat(v).timestamp()
                             for v in (args.until, args.cpu_after, args.since))
    if end <= time.time():
        parser.error('cutoff already passed; no old watch restart')
    root = PROGRAM / 'logs/releases'
    history, accepted, attempted = [], set(), set()

    def notify(kind, data):
        if kind in accepted:
            return
        if kind in attempted:
            raise ProbeFailed('notice_unknown_no_resend')
        path = root / (args.prefix + '.' + kind + '.notice.json')
        attempted.add(kind)
        with path.open('x') as receipt:
            json.dump({'state': 'attempted', 'kind': kind, 'fields': data}, receipt)
            receipt.flush()
            os.fsync(receipt.fileno())
        result = run(['clarp-admin', 'prompt', '--to', 'hobbles-18b0', '--from', 'rf-releasec-29c5',
                      '--text', 'Read-only watch ' + kind + ': ' + json.dumps(data)], 30)
        response = json.loads(result.stdout) if result.returncode == 0 else {}
        trace = response.get('trace_id')
        if (result.returncode or response.get('ok') is not True
                or response.get('session') != 'hobbles-18b0'
                or not isinstance(trace, str) or not re.fullmatch(r'[0-9a-f]{16,64}', trace)):
            raise ProbeFailed('notice_delivery_unknown')
        with path.open('a') as receipt:
            receipt.write('\n' + json.dumps({'state': 'accepted', 'trace_id': response.get('trace_id')}))
            receipt.flush()
            os.fsync(receipt.fileno())
        accepted.add(kind)

    while time.time() < end:
        begin, now = time.monotonic(), time.time()
        row = sample(history, now, max(floor, now - 600), cpu_after, notify)
        history.append(row)
        # Even a disk sink failure leaves a structured stderr record and a failed durable worker.
        print(json.dumps(row), flush=True)
        if row['result'] != 'OBSERVED':
            print(json.dumps({'event': 'WATCH_FAILED', 'probe_errors': row.get('probe_errors'),
                              'notice_errors': row.get('notice_errors')}), file=sys.stderr, flush=True)
        try:
            with (root / (args.prefix + '.jsonl')).open('a') as output:
                output.write(json.dumps(row) + '\n')
        except OSError as error:
            failure = {'event': 'WATCH_SINK_FAILED', 'class': type(error).__name__}
            print(json.dumps(failure), file=sys.stderr, flush=True)
            try:
                notify('WATCH_SINK_FAILED', failure)
            except Exception as notice_error:
                print(json.dumps({'event': 'WATCH_NOTICE_FAILED', 'class': type(notice_error).__name__}),
                      file=sys.stderr, flush=True)
            return 11
        if row['result'] != 'OBSERVED':
            return 11
        remaining = min(300 - (time.monotonic() - begin), end - time.time())
        if remaining > 0:
            time.sleep(remaining)
    print(json.dumps({'event': 'WATCH_CUTOFF', 'samples': len(history), 'notices': sorted(accepted)}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
