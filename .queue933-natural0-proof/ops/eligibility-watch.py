#!/usr/bin/env python3
"""888 read-only eligibility recorder. A qualifying window is never a ship GO."""
from pathlib import Path
import argparse
import collections
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

P = Path('/var/tmp/clarp-refactor-program')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parent
base = load('accepted_independent_watch', ROOT / 'live-watch.py')
metrics = load('release_metrics883', ROOT / 'release-metrics.py')


def giveups(since, until):
    result = base.run(['journalctl', '--user', '-u', 'clarp-runtime.service', '--since', '@' + str(since),
                       '--until', '@' + str(until), '--grep', 'sqliteWriteWait.*outcome=gave_up',
                       '--output=json', '--no-pager', '-n', '1000'])
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return 0
    if result.returncode or len(result.stdout.splitlines()) >= 1000:
        raise metrics.ObservationUnknown('runtime_giveups_query')
    for row in result.stdout.splitlines():
        json.loads(row)
    return len(result.stdout.splitlines())


def collect(history, reader, telemetry, since, now):
    # Baseline phase: old absolute alarms do not apply. Fault notices are retained.
    faults = {}
    def baseline_notice(kind, fields):
        if kind == 'WATCH_PROBE_FAILED':
            faults.update(fields['probes'])
    row = base.sample(history, now, since, float('inf'), baseline_notice)
    def turns():
        reader.restart_witnesses = metrics.supervised_restart_witnesses(
            P / 'registers/reship883-supervised-controls.json', P / 'logs/releases')
        return reader.poll(int(since * 1000), int(now * 1000))
    for name, probe in [('rss', metrics.rss), ('push', lambda: metrics.push(telemetry, int(since * 1000), int(now * 1000))),
                        ('turns', turns),
                        ('giveups', lambda: {'runtime_writer_giveups': giveups(since, now)})]:
        try:
            row.update(probe())
        except Exception as error:
            faults[name] = type(error).__name__
    row['clock_ticks_per_second'] = os.sysconf('SC_CLK_TCK')
    if 'mono' not in row:
        row['mono'] = time.monotonic()
    if history and abs((now - history[-1]['epoch']) - (row['mono'] - history[-1]['mono'])) > 5:
        faults['clock'] = 'WALL_MONOTONIC_DIVERGENCE'
    failed = row.get('failed_turn_counts', {})
    if failed.get('unknown', 0):
        faults['failed_turn_classifier'] = 'UNKNOWN_CAUSE'
    needed = ['pid', 'process_start', 'cpu_ticks', 'boot', 'rss_bytes', 'rss_pid', 'rss_start',
              'host_code', 'host_ready', 'runtime_available', 'db_locked_lines_10min', 'http5xx_10min',
              'runtime_writer_giveups', 'failed_turn_counts', 'push_device_delivery']
    for key in needed:
        if key not in row:
            faults[key] = 'MISSING'
    if faults:
        row['unknown'] = faults
    row['error_counts'] = {
        'db_locked_records': row.get('db_locked_lines_10min'),
        'http_5xx': row.get('http5xx_10min'),
        'runtime_writer_giveups': row.get('runtime_writer_giveups'),
        'genuine_failed_turns': (failed.get('genuine', 0) if 'failed_turn_counts' in row
                                and not failed.get('unknown') else None)}
    row['phase'] = 'PRE_SWITCH_ELIGIBILITY_ONLY_NO_SHIP_AUTHORITY'
    return row


def proof_ready(path):
    record = json.loads(path.read_text())
    expected = {'live-watch.py', 'release-metrics.py', 'eligibility-watch.py'}
    if set(record['files']) != expected or record.get('B_final') is not True:
        raise RuntimeError('coverage_proof_not_accepted')
    for name, digest in record['files'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('coverage_source_changed')
    report = Path(record['B_report'])
    if hashlib.sha256(report.read_bytes()).hexdigest() != record['B_report_sha256']:
        raise RuntimeError('coverage_review_receipt_changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', default=str(P / 'registers/reship883-proof-acceptance.json'))
    parser.add_argument('--prefix', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.prefix):
        parser.error('invalid receipt prefix')
    if Path(args.proof).resolve() != (P / 'registers/reship883-proof-acceptance.json').resolve():
        parser.error('only the registered coverage proof receipt is accepted')
    proof_ready(Path(args.proof))
    deadline = datetime.datetime.fromisoformat('2026-11-09T03:00:00+01:00').timestamp()
    if time.time() >= deadline:
        parser.error('eligibility deadline passed; no watch restart')
    logs = P / 'logs/releases'
    history, lowest, qualifying_count, last_qualified, fault_active = [], None, 0, False, False

    def notify(kind, data):
        marker = logs / (args.prefix + '.' + kind + '.' + str(int(time.time() * 1000)) + '.notice.json')
        with marker.open('x') as output:
            json.dump({'state': 'attempted', 'kind': kind, 'counts': data}, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            result = base.run(['clarp-admin', 'prompt', '--to', 'hobbles-18b0', '--from', 'rf-releasec-29c5',
                               '--text', '888 read-only ' + kind + ': ' + json.dumps(data)], 30)
        except subprocess.TimeoutExpired:
            print(json.dumps({'event':'NOTICE_UNKNOWN','kind':kind,'receipt':str(marker),
                              'resend':False,'sampling_continues':True}),flush=True)
            return
        response = json.loads(result.stdout) if result.returncode == 0 else {}
        if (response.get('ok') is not True or response.get('session') != 'hobbles-18b0'
                or not isinstance(response.get('trace_id'), str)
                or not re.fullmatch(r'[0-9a-f]{16,64}', response['trace_id'])):
            raise RuntimeError('notice_unknown_no_resend')
        with marker.open('a') as output:
            output.write('\n' + json.dumps({'state': 'accepted', 'trace_id': response['trace_id']}))
            output.flush()
            os.fsync(output.fileno())
        if kind == 'QUALIFYING_WINDOW_NOT_GO':
            # 892/895 pre-given conditionalGO: wake the owner at the same first window.
            # Separate durable presend receipt prevents any uncertain owner-wake resend.
            owner_marker=marker.with_name(marker.name+'.owner')
            with owner_marker.open('x') as output:
                json.dump({'state':'attempted','kind':kind,'counts':data},output)
                output.flush();os.fsync(output.fileno())
            try:
                owner_result=base.run(['clarp-admin','prompt','--to','rf-releasec-29c5','--from','rf-releasec-29c5',
                    '--text','931 Hotseat read-only qualifying window: '+json.dumps(data)+'. Wait for exact captain Hotseat READY and reviewed Host-only plan/current f666 baseline before standing931 conditional ship; no repeated attempt.'],30)
            except subprocess.TimeoutExpired:
                print(json.dumps({'event':'NOTICE_UNKNOWN','kind':kind,'recipient':'owner',
                                  'receipt':str(owner_marker),'resend':False,'sampling_continues':True}),flush=True)
                return
            accepted=json.loads(owner_result.stdout) if owner_result.returncode==0 else{}
            if (accepted.get('ok') is not True or accepted.get('session')!='rf-releasec-29c5'
                    or not isinstance(accepted.get('trace_id'),str)
                    or not re.fullmatch(r'[0-9a-f]{16,64}',accepted['trace_id'])):
                raise RuntimeError('notice_unknown_no_resend')
            with owner_marker.open('a') as output:
                output.write('\n'+json.dumps({'state':'accepted','trace_id':accepted['trace_id']}))
                output.flush();os.fsync(output.fileno())

    telemetry = metrics.readonly('/home/peter/.local/share/clarp/telemetry.sqlite')
    state = metrics.readonly('/home/peter/.local/share/clarp/state.sqlite')
    since = time.time()
    reader = metrics.TurnReader(state, int(since * 1000))
    try:
        while time.time() < deadline:
            begin, now = time.monotonic(), time.time()
            row = collect(history, reader, telemetry, since, now)
            history.append(row)
            # Freeze a full preceding window; no backfilled or extrapolated zero.
            result = metrics.eligibility(history)
            row['eligibility'] = result
            print(json.dumps(row), flush=True)
            with (logs / (args.prefix + '.jsonl')).open('a') as output:
                output.write(json.dumps(row) + '\n')
                output.flush()
            if row.get('unknown') and not fault_active:
                notify('OBSERVATION_UNKNOWN', {'probes': row['unknown'], 'qualification': False})
            fault_active = bool(row.get('unknown'))
            if result.get('cpu_mean_percent') is not None and (lowest is None or result['cpu_mean_percent'] < lowest['cpu_mean_percent']):
                lowest = result
            if result['qualifies'] and not last_qualified:
                qualifying_count += 1
                notify('QUALIFYING_WINDOW_NOT_GO', result)
            elif last_qualified and not result['qualifies']:
                notify('WINDOW_NO_LONGER_QUALIFIES', result)
            last_qualified = result['qualifies']
            since = now
            time.sleep(max(0, min(60 - (time.monotonic() - begin), deadline - time.time())))
        summary = {'result': 'ELIGIBILITY_DEADLINE_STOP_NO_AUTOSHIP', 'deadline': deadline,
                   'qualifying_episodes': qualifying_count, 'lowest_observed_complete_window': lowest,
                   'samples': len(history), 'unknown_samples': sum(bool(row.get('unknown')) for row in history)}
        (logs / (args.prefix + '.summary.json')).write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary), flush=True)
        if qualifying_count == 0:
            notify('NO_QUALIFYING_WINDOW_DO_NOT_SHIP', summary)
        return 0
    except Exception as error:
        failure = {'event': 'ELIGIBILITY_WATCH_FAILED', 'class': type(error).__name__, 'ship_GO': False}
        print(json.dumps(failure), file=sys.stderr, flush=True)
        # A failed/unknown send is not retried. The durable worker wakes its owner.
        if not isinstance(error, RuntimeError) or str(error) != 'notice_unknown_no_resend':
            try:
                notify('WATCH_FAILED', failure)
            except Exception as notice_error:
                print(json.dumps({'event': 'NOTICE_UNKNOWN', 'class': type(notice_error).__name__}), file=sys.stderr, flush=True)
        return 11
    finally:
        telemetry.close()
        state.close()


if __name__ == '__main__':
    raise SystemExit(main())
