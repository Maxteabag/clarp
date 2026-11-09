"""Read-only 883/888 metric adapters. Only aggregate codes/numbers leave this module."""
from pathlib import Path
import collections
import hashlib
import json
import math
import re
import sqlite3
import sqlite3
import statistics
import subprocess
import time


class ObservationUnknown(RuntimeError):
    pass


ACCOUNT = frozenset({'usage_limit', 'auth', 'account_plan'})
GENUINE = frozenset({'connection', 'transient', 'runner_exit', 'timeout', 'muse_session_locked'})

# 901: exact product templates and bundled AgentBackend ids, not provider text.
ACCOUNT_WAIT_MESSAGES = ('Waiting for an account with available usage',) + tuple(
    'Waiting for a '+backend+' account with available usage'
    for backend in ('claude', 'codex', 'agy', 'grok', 'opencode', 'deepseek', 'muse'))
ACCOUNT_WAIT_SQL = ' OR '.join(
    "instr(COALESCE(json_extract(detail,'$.error'),''),?)>0"
    for _message in ACCOUNT_WAIT_MESSAGES)


def failed_turn_bucket(outcome, reason, source, account_wait=False, signal_exit=False,
                       stop_witness=False, restart_witness=False):
    if restart_witness and (source == 'server_restart' or reason == 'server_restart'):
        return 'excluded_restart'
    if account_wait or reason in ACCOUNT:
        return 'excluded_account'
    if stop_witness and source in ('user_stop', 'janitor_pause'):
        return 'excluded_stop'
    if outcome == 'completed':
        return 'not_failed'
    if outcome != 'failed' or signal_exit or reason == 'interrupted' or source == 'server_restart':
        return 'unknown'
    return 'genuine' if reason in GENUINE else 'unknown'


def supervised_restart_witnesses(path, receipt_root):
    """Only completed803/830 request+summary+journal receipt joins, never a label.

    The owner may register these existing receipts after a deliberate restart.
    Missing manifest means no planned-restart exclusion authority. No writes.
    """
    path, receipt_root = Path(path), Path(receipt_root).resolve()
    if not path.exists():
        return ()
    with path.open('rb') as stream:
        raw_manifest = stream.read(65537)
    if len(raw_manifest) > 65536:
        raise ObservationUnknown('supervised_control_manifest_size')
    data = json.loads(raw_manifest)
    if data.get('version') != 1 or not isinstance(data.get('entries'), list) or len(data['entries']) > 16:
        raise ObservationUnknown('supervised_control_manifest')
    witnesses = []
    for entry in data['entries']:
        receipts = {}
        for key in ('request', 'summary', 'events'):
            record = entry[key]
            file = Path(record['path'])
            if not file.is_absolute() or file.is_symlink() or not file.resolve().is_relative_to(receipt_root):
                raise ObservationUnknown('supervised_control_path')
            with file.open('rb') as stream:
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise ObservationUnknown('supervised_control_size')
            if hashlib.sha256(raw).hexdigest() != record['sha256']:
                raise ObservationUnknown('supervised_control_hash')
            receipts[key] = json.loads(raw)
        request, summary, events = (receipts[key] for key in ('request', 'summary', 'events'))
        invocation = summary.get('runtime_invocation')
        counts = summary.get('resume_receipts', {})
        total = summary.get('interrupted_current_boot')
        if (type(request.get('authority')) is not int or request.get('authority') not in (803, 830)
                or type(request.get('attempt')) is not int or request.get('attempt') != 1
                or request.get('argv') != ['systemctl', '--user', 'restart', 'clarp-runtime.service']
                or summary.get('result') != 'runtime_activation_health_pass'
                or type(summary.get('forward_restart_attempts')) is not int
                or summary.get('forward_restart_attempts') != 1 or summary.get('rolled_back') is not False
                or not isinstance(invocation, str) or not re.fullmatch(r'[0-9a-f]{32}', invocation)
                or type(counts.get('unknown')) is not int or counts['unknown'] != 0
                or type(total) is not int or total < 0
                or type(counts.get('accepted')) is not int or type(counts.get('skipped')) is not int
                or min(counts['accepted'], counts['skipped']) < 0
                or counts['accepted'] + counts['skipped'] != total
                or not isinstance(events.get('events'), list) or len(events['events']) != total
                or type(events.get('restart_interrupt_failures')) is not int
                or events.get('restart_interrupt_failures') != 0):
            raise ObservationUnknown('supervised_control_unproven')
        epoch = request['epoch']
        if type(epoch) not in (int, float) or not math.isfinite(epoch):
            raise ObservationUnknown('supervised_control_clock')
        start = events.get('start_epoch')
        if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= epoch - start <= 10:
            raise ObservationUnknown('supervised_control_capture_clock')
        for event in events['events']:
            fields = event.get('fields', {})
            agent, trace = fields.get('agent'), fields.get('trace')
            stamp = int(event['journal_usec']) // 1000
            if event.get('invocation') != invocation or not (epoch * 1000 <= stamp <= (epoch + 600) * 1000):
                raise ObservationUnknown('supervised_control_event')
            if not isinstance(agent, str) or not agent:
                raise ObservationUnknown('supervised_control_agent')
            if trace in (None, '', '∅'):
                trace = None
            elif not isinstance(trace, str):
                raise ObservationUnknown('supervised_control_trace')
            witnesses.append((agent, trace, invocation, stamp))
    return tuple(witnesses)


def readonly(path):
    connection = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=.25)
    connection.execute('PRAGMA busy_timeout=250')
    return connection


def rss():
    def pid():
        result = subprocess.run(['systemctl', '--user', 'show', 'clarp.service', '--property=MainPID', '--value'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode or not result.stdout.strip().isdigit() or int(result.stdout) <= 0:
            raise ObservationUnknown('host_pid')
        return int(result.stdout)
    first = pid()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    def start():
        return int(Path(f'/proc/{first}/stat').read_text().rsplit(')', 1)[1].split()[19])
    before = start()
    text = Path(f'/proc/{first}/status').read_text()
    matches = re.findall(r'^VmRSS:\s+(\d+)\s+kB$', text, re.M)
    if len(matches) != 1 or pid() != first or start() != before:
        raise ObservationUnknown('rss_identity')
    if int(matches[0]) <= 0:
        raise ObservationUnknown('rss_zero')
    return {'rss_bytes': int(matches[0]) * 1024, 'rss_pid': first,
            'rss_start': before, 'boot': boot}


def push(connection, since_ms, until_ms):
    # Error strings/payloads/identifiers never become output fields.
    rows = connection.execute("""SELECT event,
        CASE WHEN event='apnsSendResult' AND json_valid(detail)
             THEN substr(json_extract(detail,'$.msg'),1,2049) END
        FROM diagnostic_events WHERE ts>=? AND ts<? AND event IN
        ('apnsSendResult','apnsSendFail','apnsBatchFail','ios.push.received') LIMIT 10001""",
        (since_ms, until_ms)).fetchall()
    if len(rows) > 10000:
        raise ObservationUnknown('push_truncated')
    codes, received, failures = collections.Counter(), 0, 0
    for event, message in rows:
        if event == 'ios.push.received':
            received += 1
        elif event in ('apnsSendFail', 'apnsBatchFail'):
            failures += 1
        else:
            found = re.search(r'\bstatus=(\d{3})\b', message or '')
            if not found or len(message or '') > 2048:
                raise ObservationUnknown('push_status_unknown')
            codes[found[1]] += 1
    return {'push_apns_status_counts': dict(codes), 'push_transport_failure_records': failures,
            'push_device_received_records': received,
            'push_device_delivery': 'UNKNOWN_UNCORRELATED_PASSIVE_COUNTS',
            'push_traffic': 'OBSERVED' if rows else 'NONE_OBSERVED_UNKNOWN_DELIVERY'}


class TurnReader:
    """Bounded new-state primary-key reads, private identity joins, aggregate output.

    A new observer starts coverage now. It never scans old conversation rows or
    claims a retrospective failed-turn baseline from incomplete state metadata.
    """
    def __init__(self, connection, started_ms, restart_witnesses=()):
        self.connection = connection
        self.cursor = connection.execute('SELECT COALESCE(MAX(state_id),0) FROM state_log').fetchone()[0]
        self.started_ms = started_ms
        self.seen = {}
        self.waits = {}
        self.restart_witnesses = restart_witnesses

    def account_wait_in_turn(self, agent, trace, start, end):
        if (not trace or type(start) is not int or type(end) is not int
                or start < 0 or end < start):
            return None
        # idx_state_log_agent_ts bounds the actual interval. Only integer flags
        # leave SQLite; no errors, provider payload or conversation fields.
        try:
            rows = self.connection.execute("""SELECT CASE WHEN json_valid(detail) THEN
              (json_extract(detail,'$.account_recovery')='waiting' AND
               json_extract(detail,'$.trace_id')=?) ELSE 0 END
              FROM state_log WHERE agent_id=? AND ts>=? AND ts<=?
              ORDER BY ts DESC LIMIT 10001""", (trace, agent, start, end)).fetchall()
        except sqlite3.Error:
            return None
        if len(rows) >= 10000:
            return None
        return any(flag == 1 for (flag,) in rows)

    def poll(self, since_ms, until_ms):
        rows = self.connection.execute("""SELECT state_id,agent_id,ts,kind,
          CASE WHEN json_valid(detail) THEN json_extract(detail,'$.trace_id') END,
          CASE WHEN json_valid(detail) THEN json_extract(detail,'$.reason') END,
          CASE WHEN json_valid(detail) THEN json_extract(detail,'$.source') END,
          CASE WHEN json_valid(detail) THEN json_extract(detail,'$.account_recovery') END,
          CASE WHEN json_valid(detail) THEN
            (instr(COALESCE(json_extract(detail,'$.error'),''),'rc=143')>0 OR
             instr(COALESCE(json_extract(detail,'$.error'),''),'rc=-15')>0 OR
             json_extract(detail,'$.rc') IN (143,-15)) ELSE 0 END,
          CASE WHEN json_valid(detail) THEN json_extract(detail,'$.stop_actor') END,
          CASE WHEN json_valid(detail) THEN json_type(detail,'$.stop_actor_verified') END,
          CASE WHEN json_valid(detail) THEN json_extract(detail,'$.origin') END,
          CASE WHEN json_valid(detail) THEN (""" + ACCOUNT_WAIT_SQL + """) ELSE 0 END
          FROM state_log WHERE state_id>? ORDER BY state_id LIMIT 10001""",
          (*ACCOUNT_WAIT_MESSAGES, self.cursor)).fetchall()
        if len(rows) > 10000:
            raise ObservationUnknown('turn_rows_truncated')
        # Do not consume rows committed after the observation's fixed cutoff.
        # They belong to the next minute, even if the queries take a few seconds.
        ready = []
        for row in rows:
            if row[2] >= until_ms:
                if row[2] > until_ms + 5000:
                    raise ObservationUnknown('state_clock_future')
                break
            ready.append(row)
        rows = ready
        for row in rows:
            _, agent, ts, _, trace, _, _, wait, _, _, _, _, _ = row
            if wait == 'waiting' and trace:
                self.waits[(agent, trace)] = ts
        counts = collections.Counter()
        for row in rows:
            sid, agent, ts, kind, trace, reason, source, _, signal, actor, verified, origin, account_message = row
            if not (self.started_ms <= ts < until_ms):
                continue
            if kind not in ('done', 'idle', 'interrupted', 'stopped'):
                continue
            # Existing agent/start index bounds the metadata read; no message rows.
            turns = self.connection.execute('SELECT turn_id,trace_id,settled_at,outcome,started_at FROM turns '
                'WHERE agent_id=? ORDER BY started_at DESC LIMIT 32', (agent,)).fetchall()
            matching = [t for t in turns if t[2] == ts and (not trace or trace == t[1])]
            if len(matching) != 1:
                counts['unknown'] += 1
                continue
            tid, actual_trace, _, outcome, started = matching[0]
            if tid in self.seen:
                continue
            self.seen[tid] = ts
            stop = (source == 'user_stop' and (actor == 'user' or verified == 'true')) or (
                source == 'janitor_pause' and origin == 'janitor' and trace == actual_trace and bool(trace))
            wait_ts = self.waits.get((agent, actual_trace))
            legacy_waiting = wait_ts is not None and abs(wait_ts - ts) <= 120000
            if legacy_waiting or account_message or reason in ACCOUNT:
                waiting = True
            elif outcome == 'completed' or stop or (reason == 'server_restart' and source == 'server_restart'):
                waiting = False
            else:
                waiting = self.account_wait_in_turn(agent, actual_trace, started, ts)
                if waiting is None:
                    counts['unknown'] += 1
                    continue
            traceless_restart = (not actual_trace and not trace and kind == 'interrupted'
                                 and outcome == 'interrupted' and reason == 'server_restart'
                                 and source == 'server_restart')
            if traceless_restart:
                # 892: only proven803/830 journal witnesses, exact agent and one unambiguous
                # event within60s. Invocation/receipt/health ownership was verified by loader.
                planned = [w for w in self.restart_witnesses if w[0] == agent
                           and abs(w[3] - ts) <= 60000]
            else:
                planned = [w for w in self.restart_witnesses if w[0] == agent
                           and bool(actual_trace) and w[1] == actual_trace
                           and abs(w[3] - ts) <= 5000]
            counts[failed_turn_bucket(outcome, reason, source, waiting, bool(signal), stop, len(planned) == 1)] += 1
        if rows:
            self.cursor = rows[-1][0]
        self.seen = {key: ts for key, ts in self.seen.items() if ts >= until_ms - 3600000}
        self.waits = {key: ts for key, ts in self.waits.items() if ts >= until_ms - 3600000}
        return {'failed_turn_counts': dict(counts), 'turn_state_rows_observed': len(rows),
                'turn_coverage_started_ms': self.started_ms}


CLASSIFIER_UNKNOWN_BUDGET922=6

def eligibility_classifier922(row):
    """Known genuine count and VISIBLE classifier-only unknown budget; no raw edits."""
    faults=row.get('unknown',{})
    counts=row.get('failed_turn_counts')
    errors=row.get('error_counts')
    if (not isinstance(faults,dict) or not isinstance(errors,dict) or row.get('result')!='OBSERVED' or
        any(type(errors.get(k)) is not int or errors[k]<0 for k in FLOORS if k!='genuine_failed_turns')):return None,0,False
    raw=errors.get('genuine_failed_turns')
    if row.get('probe_errors'):return None,0,False
    if (not isinstance(counts,dict) or set(counts)-{'not_failed','excluded_account','excluded_restart','excluded_stop','genuine','unknown'} or
        any(type(v) is not int or v<0 for v in counts.values())):return None,0,False
    unknown=counts.get('unknown',0);known=counts.get('genuine',0)
    if unknown:
        allowed=(faults=={'failed_turn_classifier':'UNKNOWN_CAUSE'}
                 and (raw is None or (type(raw) is int and raw==known)))
        return (known,unknown,True) if allowed else (None,unknown,False)
    if faults or type(raw) is not int or raw!=known:return None,0,False
    return known,0,True

def eligibility(history):
    if len(history) < 2:
        return {'qualifies': False, 'reason': 'NEED_FULL_30MIN'}
    end = history[-1]
    candidates = [r for r in history if end['mono'] - r['mono'] >= 1800]
    if not candidates:
        return {'qualifies': False, 'reason': 'NEED_FULL_30MIN'}
    first = max(candidates, key=lambda r: r['mono'])
    window = [r for r in history if r['mono'] >= first['mono']]
    duration = end['mono'] - first['mono']
    if duration > 1890 or any(b['mono'] - a['mono'] > 90 for a, b in zip(window, window[1:])):
        return {'qualifies': False, 'reason': 'COVERAGE_GAP'}
    reasons = []
    classifier=[eligibility_classifier922(r) for r in window]
    classifier_unknown=sum(x[1] for x in classifier)
    if not all(x[2] for x in classifier):reasons.append('OBSERVATION_UNKNOWN')
    if classifier_unknown>CLASSIFIER_UNKNOWN_BUDGET922:reasons.append('CLASSIFIER_UNKNOWN_BUDGET_EXCEEDED')
    def identity(row):
        return (row.get('pid'), row.get('process_start'), row.get('boot'))
    known = [r for r in window if all(x is not None for x in identity(r))]
    cpu = None
    if (all(x is not None for x in identity(first)) and identity(first) == identity(end)
            and all(identity(r) == identity(first) for r in known)
            and isinstance(first.get('cpu_ticks'), int) and isinstance(end.get('cpu_ticks'), int)):
        cpu = 100 * (end['cpu_ticks'] - first['cpu_ticks']) / end['clock_ticks_per_second'] / duration
        if not math.isfinite(cpu) or cpu < 0: cpu = None
    if cpu is None: reasons.append('CPU_UNKNOWN')
    elif cpu > 75: reasons.append('CPU_ABOVE75')
    if len(known) != len(window): reasons.append('PROCESS_IDENTITY_UNKNOWN')
    if known and any(identity(r) != identity(known[0]) for r in known): reasons.append('PROCESS_IDENTITY_CHANGED')
    rss_known = [r for r in window if type(r.get('rss_bytes')) is int and r['rss_bytes'] > 0
                 and r.get('rss_pid') == r.get('pid') and r.get('rss_start') == r.get('process_start')]
    if len(rss_known) != len(window): reasons.append('RSS_UNKNOWN')
    def total(rows):
        result = {}
        for key in FLOORS:
            values = ([eligibility_classifier922(r)[0] for r in rows] if key=='genuine_failed_turns'
                      else [(r['error_counts'].get(key) if isinstance(r.get('error_counts'),dict) else None) for r in rows])
            result[key] = sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None
        return result
    last10 = total([r for r in window[1:] if end['mono'] - r['mono'] < 600])
    prior10 = total([r for r in window[1:] if 600 <= end['mono'] - r['mono'] < 1200])
    if any(last10[k] is None or prior10[k] is None for k in FLOORS): reasons.append('ERROR_TREND_UNKNOWN')
    elif any(last10[k] - prior10[k] >= FLOORS[k] for k in FLOORS): reasons.append('ERROR_TREND_RISING')
    if any(r.get('host_code') != 200 or r.get('host_ready') is not True for r in window): reasons.append('HOST_NOT_READY')
    # 917 pure eligibility uses the existing60s/<=90s covered observations.
    # A recovered historical120s episode still disqualifies this selectedwindow.
    runtime_start=None;runtime_max=0.0;runtime_unknown=False
    for row in window:
        value=row.get('runtime_available')
        if type(value) is not bool:
            runtime_unknown=True;runtime_start=None
        elif value is True:
            runtime_start=None
        else:
            if runtime_start is None:runtime_start=row['mono']
            runtime_max=max(runtime_max,row['mono']-runtime_start)
    if runtime_unknown:reasons.append('RUNTIME_OBSERVATION_UNKNOWN')
    if runtime_max>=120:reasons.append('RUNTIME_NOT_AVAILABLE')
    known_baseline=total(window[1:]);baseline_counts=dict(known_baseline)
    if classifier_unknown:baseline_counts['genuine_failed_turns']=None
    return {'qualifies': not reasons, 'reasons': reasons, 'start_epoch': first['epoch'],
            'end_epoch': end['epoch'], 'seconds': duration, 'samples': len(window),
            'cpu_mean_percent': cpu, 'cpu_identity_samples_known': len(known),
            'rss_median_bytes': statistics.median(r['rss_bytes'] for r in rss_known) if len(rss_known) == len(window) else None,
            'rss_max_observed_bytes': max((r['rss_bytes'] for r in rss_known), default=None),
            'rss_final_bytes': end.get('rss_bytes'), 'rss_samples_known': len(rss_known),
            'last10_error_counts': last10, 'prior10_error_counts': prior10,
            'baseline_error_counts': baseline_counts,'eligibility_known_baseline_error_counts':known_baseline,
            'runtime_unavailable_max_seconds': runtime_max,
            'eligibility_policy':'917_922','classifier_unknown_rows':classifier_unknown,
            'classifier_unknown_budget':CLASSIFIER_UNKNOWN_BUDGET922,
            'failed_turn_trigger_evaluable':not bool(classifier_unknown),
            'residual':'Failed-turn trigger unevaluable when baseline classifierunknowns exist; other protections unchanged' if classifier_unknown else None,
            'genuine_count_scope':'KNOWN_GENUINE_ONLY_UNKNOWN_SEPARATELY_VISIBLE'}


FLOORS = {'db_locked_records': 10, 'http_5xx': 5, 'runtime_writer_giveups': 2, 'genuine_failed_turns': 3}


def recalibrated_triggers(sample, baseline, elapsed):
    """Pure 883 policy evaluation, never authorization or a recovery command."""
    triggers, unknown = [], []
    for key, floor in FLOORS.items():
        actual = sample.get('post_error_counts', {}).get(key)
        before = baseline.get('baseline_error_counts', {}).get(key)
        if type(actual) is not int or type(before) is not int or min(actual, before) < 0:
            unknown.append(key)
        elif actual > 3 * before and actual > floor:
            triggers.append(key)
    cpu_value = sample.get('cpu_average_percent')
    cpu_before = baseline.get('cpu_mean_percent')
    if (type(cpu_value) not in (int, float) or type(cpu_before) not in (int, float)
            or not all(math.isfinite(x) and x >= 0 for x in (cpu_value, cpu_before))
            or sample.get('cpu_average_seconds', 0) < 599):
        unknown.append('cpu_full_10min')
    else:
        limit = max(3 * cpu_before, 150) if elapsed < 3600 else max(2 * cpu_before, 75)
        if cpu_value > limit:
            triggers.append('cpu')
    value, before = sample.get('rss_bytes'), baseline.get('rss_median_bytes')
    if (type(value) is not int or type(before) not in (int, float) or value <= 0
            or not math.isfinite(before) or before <= 0):
        unknown.append('rss')
    elif value > 1.5 * before:
        triggers.append('rss')
    return {'triggers': triggers, 'unknown': unknown}
