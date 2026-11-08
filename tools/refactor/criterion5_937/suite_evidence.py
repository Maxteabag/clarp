"""Pure receipt consumer; does not launch/collect pytest or bless source/lifecycle.

Transport, raw-byte manifests, source/dependency/CPU/cache binding and process
drain are separate prerequisites. These functions refuse incomplete timing
evidence rather than treating an absent outcome as a faster test.
"""
from collections import Counter
import hashlib
import json
import math

from .roster import checked

ROLES = ('controller', *(f'gw{i}' for i in range(8)))
EVENT_FIELDS = {'nodeid', 'when', 'outcome', 'duration', 'report_type', 'wasxfail'}


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid nonnegative finite measurement')
    return value


def event_key(row):
    if (set(row) != EVENT_FIELDS or row['when'] not in ('setup', 'call', 'teardown')
            or row['outcome'] not in ('passed', 'failed', 'skipped')
            or type(row['wasxfail']) is not bool
            or row['report_type'] not in ('TestReport', 'SubtestReport', 'SubTestReport')):
        raise ValueError('invalid report record')
    checked([row['nodeid']])
    number(row['duration'])
    return tuple(row[name] for name in sorted(EVENT_FIELDS))


def outcomes(events, cohort):
    """One exact node outcome; setup skip/error cannot disappear from divisor."""
    wanted = checked(cohort)
    grouped = {node: {} for node in wanted}
    for row in events:
        event_key(row)
        if row['report_type'] != 'TestReport' or row['nodeid'] not in grouped:
            raise ValueError('non-cohort or nested report in primary outcomes')
        phases = grouped[row['nodeid']]
        if row['when'] in phases:
            raise ValueError('duplicate primary phase report')
        phases[row['when']] = row
    result = {}
    for node, phases in grouped.items():
        if not {'setup', 'teardown'}.issubset(phases):
            raise ValueError('missing primary node/phase')
        setup, teardown = phases['setup'], phases['teardown']
        call = phases.get('call')
        if setup['outcome'] == 'passed':
            if call is None:
                raise ValueError('missing call after successful setup')
        elif call is not None:
            raise ValueError('call after unsuccessful setup')
        if teardown['outcome'] != 'passed' or setup['outcome'] == 'failed':
            label = 'error'
        elif setup['outcome'] == 'skipped':
            label = 'xfailed' if setup['wasxfail'] else 'skipped'
        elif call['wasxfail']:
            label = 'xfailed' if call['outcome'] == 'skipped' else 'xpassed'
        else:
            label = call['outcome']
        result[node] = label
    return result


def timing(config_bytes, starts, finishes, *, actual_exit, outer_seconds, cohort):
    """Verify nine process receipts and worker/controller report agreement.

    Prior nonzero remains a failure. The caller retains it; this reducer refuses
    to construct a usable timing result. No flake disposition is inferred here.
    """
    spec = json.loads(config_bytes)
    wanted = checked(cohort)
    cohort_bytes = (json.dumps(wanted, separators=(',', ':')) + '\n').encode()
    if (spec['mode'] != 'timing' or spec['expected_workers'] != 8
            or spec['cohort_sha256'] != hashlib.sha256(cohort_bytes).hexdigest()):
        raise ValueError('wrong mode/worker count/cohort binding')
    if type(actual_exit) is not int or actual_exit != 0:
        raise ValueError('raw pytest failure; do not compare timing')
    if set(starts) != set(ROLES) or set(finishes) != set(ROLES):
        raise ValueError('incomplete or replacement process census')
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    identities = set()
    controller = finishes['controller']
    collected = None
    omissions = None
    if set(controller['worker_collections']) != set(ROLES[1:]):
        raise ValueError('missing worker collection')
    if any(nodes != wanted for nodes in controller['worker_collections'].values()):
        raise ValueError('worker collection drift')
    scope = ('boot', 'affinity', 'python', 'pytest', 'executable')
    for role in ROLES:
        start, finish = starts[role], finishes[role]
        for row in (start, finish):
            if (row['spec'] != spec or row['config_sha256'] != config_sha
                    or row['worker'] != role or row['errors']
                    or row['pytest'] != '9.1.1'
                    or row['pycache_prefix'] != spec['cache_prefix']
                    or any(row[k] != controller[k] for k in scope)):
                raise ValueError('process provenance/error mismatch')
        identity = (finish['boot'], finish['pid'], finish['start_ticks'])
        if (type(finish['pid']) is not int or finish['pid'] <= 0
                or type(finish['start_ticks']) is not int or finish['start_ticks'] <= 0
                or identity != (start['boot'], start['pid'], start['start_ticks'])
                or identity in identities):
            raise ValueError('reused or changed process identity')
        identities.add(identity)
        if (finish['primary_pytest_exit'] != 0 or finish['qualified_exit'] != 0
                or ('coverage' in finish) or ('coverage_data' in finish)):
            raise ValueError('failure or instrumented run cannot become timing')
        if role != 'controller' and finish['nodes_selected'] != wanted:
            raise ValueError('worker selected roster drift')
        if role != 'controller':
            before = checked(finish['nodes_before'])
            excluded = finish['deselected']
            if (len(set(excluded)) != len(excluded)
                    or sorted(excluded) != sorted(set(before) - set(wanted))
                    or not set(wanted).issubset(before)):
                raise ValueError('omission accounting drift')
            if collected is None:
                collected, omissions = before, sorted(excluded)
            elif before != collected or sorted(excluded) != omissions:
                raise ValueError('worker full collection/omissions disagree')
        elapsed = number(finish['session_hook_elapsed_seconds'])
        if (elapsed <= 0 or number(finish['ended_mono']) - number(finish['started_mono']) != elapsed):
            raise ValueError('invalid session interval')
        for field, counter in (('events', 'observer_events'), ('subtests', 'nested_subtests')):
            if finish[counter] != len(finish[field]):
                raise ValueError('report count mismatch')
            for row in finish[field]:
                event_key(row)
                if row['nodeid'] not in wanted or row['outcome'] == 'failed':
                    raise ValueError('non-cohort or failed report')
                if (field == 'subtests') == (row['report_type'] == 'TestReport'):
                    raise ValueError('nested/primary report partition mismatch')
    for field in ('events', 'subtests'):
        primary = Counter(event_key(row) for row in controller[field])
        workers = Counter(event_key(row) for role in ROLES[1:] for row in finishes[role][field])
        if primary != workers:
            raise ValueError('worker/controller report multiset mismatch')
    labels = outcomes(controller['events'], wanted)
    if any(label in ('failed', 'error', 'xpassed') for label in labels.values()):
        raise ValueError('non-comparable test outcome')
    outer = number(outer_seconds)
    elapsed = controller['session_hook_elapsed_seconds']
    if outer < elapsed:
        raise ValueError('outer interval does not contain pytest session')
    return {'run_id': spec['run_id'], 'arm': spec['arm'], 'pin': spec['pin'],
            'cohort_sha256': spec['cohort_sha256'], 'node_count': len(wanted),
            'full_collected_count': len(collected), 'deselected_nodes': omissions,
            'node_outcomes': labels, 'outcome_counts': dict(Counter(labels.values())),
            'nested_subtests': controller['nested_subtests'],
            'session_hook_seconds': elapsed, 'outer_invocation_seconds': outer,
            'hook_ms_per_node_outcome': 1000 * elapsed / len(wanted),
            'outer_ms_per_node_outcome': 1000 * outer / len(wanted),
            'process_scope': {k: controller[k] for k in scope},
            'claim': 'complete common-cohort point observation only; not full G1 or no-worse verdict'}


def compare_abba(runs):
    """Expose preregistered paired gaps, not a variance-free MET conclusion."""
    if len(runs) != 4 or [r['arm'] for r in runs] != ['A', 'B', 'B', 'A']:
        raise ValueError('incomplete or changed ABBA order')
    if len({r['run_id'] for r in runs}) != 4:
        raise ValueError('not four independent invocations')
    reference = runs[0]
    for row in runs:
        if any(row[k] != reference[k] for k in ('cohort_sha256', 'node_count', 'node_outcomes', 'process_scope')):
            raise ValueError('non-comparable cohort/outcomes/process environment')
    if runs[0]['pin'] != runs[3]['pin'] or runs[1]['pin'] != runs[2]['pin']:
        raise ValueError('pin changed within arm')
    pairs = []
    for a, b in ((runs[0], runs[1]), (runs[3], runs[2])):
        gaps = {}
        for field in ('session_hook_seconds', 'outer_invocation_seconds'):
            av, bv = number(a[field]), number(b[field])
            if av == 0:
                raise ValueError('zero baseline interval')
            gaps[field] = {'A': av, 'B': bv, 'B_minus_A': bv - av,
                           'percent': 100 * (bv / av - 1)}
        pairs.append(gaps)
    return {'pairs': pairs, 'target_margin_percent': 0,
            'verdict': 'NOT_ESTABLISHED_WITHOUT_TIMER_LOAD_BOUNDS',
            'limits': 'matching receipts do not prove machine/cache/load equivalence or measurement resolution'}
