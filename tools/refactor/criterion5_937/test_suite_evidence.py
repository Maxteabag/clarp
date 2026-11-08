"""Finite reducer contracts, authored NOT RUN; remote admitted controls only.

Primary owner: pure receipt interpretation, distinct from real pytest plugin
participation controls. Credible regressions lose outcomes, double-count nested
reports, compare incomplete workers or label setup savings pytest speed. No
product seam or imports; fixture bytes are inputs, not runtime proof.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from .suite_evidence import B_PIN, COVERAGE_SCOPE, ROLES, compare_abba, coverage_inputs, outcomes, timing

NODES = ['tests/test_sample.py::test_one', 'tests/test_sample.py::test_skipped']


def report(node, when, outcome='passed', *, nested=False, xfail=False):
    return {'nodeid': node, 'when': when, 'outcome': outcome, 'duration': 0.125,
            'report_type': 'SubtestReport' if nested else 'TestReport', 'wasxfail': xfail}


def receipts(mode='timing'):
    raw_cohort = (json.dumps(NODES, separators=(',', ':')) + '\n').encode()
    spec = {'mode': mode, 'expected_workers': 8, 'cohort_sha256': hashlib.sha256(raw_cohort).hexdigest(),
            'run_id': 'synthetic-evidence-only', 'arm': 'A', 'pin': 'a' * 40,
            'cache_prefix': '/private-synthetic-cache'}
    if mode == 'coverage':
        spec.update(arm='B', pin=B_PIN, coverage_version='7.17.0',
                    coverage_package_root='/private/tool/coverage', output='/private/coverage-out')
    raw = json.dumps(spec).encode()
    first = [report(NODES[0], phase) for phase in ('setup', 'call', 'teardown')]
    second = [report(NODES[1], 'setup', 'skipped'), report(NODES[1], 'teardown')]
    nested = [report(NODES[0], 'call', nested=True)]
    starts, finishes = {}, {}
    for i, role in enumerate(ROLES):
        state = {'spec': spec, 'config_sha256': hashlib.sha256(raw).hexdigest(),
                 'worker': role, 'errors': [], 'boot': 'synthetic-boot',
                 'affinity': list(range(8)), 'python': '3.12.14 synthetic', 'pytest': '9.1.1',
                 'executable': '/private/python', 'pycache_prefix': spec['cache_prefix'],
                 'pid': 100 + i, 'start_ticks': 1000 + i}
        if mode == 'coverage':
            state['coverage'] = {'config_sha256': hashlib.sha256(raw).hexdigest(),
                                 'version': '7.17.0', 'module_path': '/private/tool/coverage/__init__.py',
                                 'native_core': 'CTracer',
                                 'native_tracer_file': '/private/tool/coverage/tracer.cpython-312-x86_64-linux-gnu.so',
                                 'scope': COVERAGE_SCOPE}
        starts[role] = deepcopy(state)
        primary = first + second if role == 'controller' else first if role == 'gw0' else second if role == 'gw1' else []
        subtests = nested if role in ('controller', 'gw0') else []
        finishes[role] = {**deepcopy(state), 'primary_pytest_exit': 0, 'qualified_exit': 0,
                          'started_mono': 10.0, 'ended_mono': 18.0,
                          'session_hook_elapsed_seconds': 8.0,
                          'nodes_before': NODES, 'deselected': [],
                          'nodes_selected': NODES, 'events': deepcopy(primary),
                          'subtests': deepcopy(subtests), 'observer_events': len(primary),
                          'nested_subtests': len(subtests),
                          'worker_collections': {w: NODES for w in ROLES[1:]} if role == 'controller' else {}}
        if mode == 'coverage':
            finishes[role]['coverage_data'] = {'file': f'coverage-pid-{state["pid"]}.data',
                                              'sha256': 'a' * 64, 'bytes': 4096,
                                              'scope': COVERAGE_SCOPE}
    return raw, starts, finishes


def reduce(raw, starts, finishes, *, exit_code=0, mode='timing'):
    consumer = timing if mode == 'timing' else coverage_inputs
    return consumer(raw, starts, finishes, actual_exit=exit_code, outer_seconds=10.0, cohort=NODES)


def test_complete_outcomes_include_setup_skip_but_not_nested_divisor():
    value = reduce(*receipts())
    assert value['outcome_counts'] == {'passed': 1, 'skipped': 1}
    assert value['node_count'] == 2 and value['nested_subtests'] == 1
    assert value['hook_ms_per_node_outcome'] == 4000
    assert value['outer_ms_per_node_outcome'] == 5000
    assert value['session_hook_seconds'] != value['outer_invocation_seconds']


@pytest.mark.parametrize('fault', ['missing-worker', 'reused-pid', 'changed-config',
                                  'missing-call', 'duplicate-phase', 'report-mismatch',
                                  'primary-failure', 'worker-failure', 'coverage', 'nested-failure'])
@pytest.mark.parametrize('mode', ['timing', 'coverage'])
def test_incomplete_or_noncomparable_receipts_refuse(fault, mode):
    raw, starts, finishes = receipts(mode)
    controller = finishes['controller']
    if fault == 'missing-worker':
        del finishes['gw7']
    elif fault == 'reused-pid':
        for container in (starts, finishes):
            container['gw7']['pid'] = container['gw6']['pid']
            container['gw7']['start_ticks'] = container['gw6']['start_ticks']
    elif fault == 'changed-config':
        finishes['gw1']['config_sha256'] = 'f' * 64
    elif fault in ('missing-call', 'duplicate-phase'):
        for role in ('controller', 'gw0'):
            events = finishes[role]['events']
            if fault == 'missing-call':
                events[:] = [row for row in events if row['when'] != 'call']
            else:
                events.append(deepcopy(events[0]))
            finishes[role]['observer_events'] = len(events)
    elif fault == 'report-mismatch':
        finishes['gw0']['events'][0]['duration'] = 0.25
    elif fault == 'primary-failure':
        with pytest.raises(ValueError, match='raw pytest failure'):
            reduce(raw, starts, finishes, exit_code=1, mode=mode)
        return
    elif fault == 'worker-failure':
        finishes['gw2']['qualified_exit'] = 4
    elif fault == 'coverage':
        finishes['gw3']['coverage_data'] = {'file': 'coverage.data'}
    elif fault == 'nested-failure':
        for role in ('controller', 'gw0'):
            finishes[role]['subtests'][0]['outcome'] = 'failed'
    with pytest.raises(ValueError):
        reduce(raw, starts, finishes, mode=mode)


def test_xfail_remains_an_exact_outcome_instead_of_passed_work():
    events = [report(NODES[0], 'setup'), report(NODES[0], 'call', 'skipped', xfail=True),
              report(NODES[0], 'teardown')]
    assert outcomes(events, NODES[:1]) == {NODES[0]: 'xfailed'}


def abba():
    value = reduce(*receipts())
    runs = []
    for i, arm in enumerate(('A', 'B', 'B', 'A')):
        row = deepcopy(value)
        row.update(run_id=f'run-{i}', arm=arm, pin=('a' if arm == 'A' else 'b') * 40)
        if arm == 'B':
            row['session_hook_seconds'] = 9
            row['outer_invocation_seconds'] = 11
        runs.append(row)
    return runs


def test_opposite_order_gaps_do_not_invent_load_resolution_or_met_verdict():
    value = compare_abba(abba())
    assert [row['session_hook_seconds']['B_minus_A'] for row in value['pairs']] == [1, 1]
    assert value['pairs'][0]['session_hook_seconds']['percent'] == 12.5
    assert value['verdict'] == 'NOT_ESTABLISHED_WITHOUT_TIMER_LOAD_BOUNDS'


@pytest.mark.parametrize('fault', ['order', 'same-run', 'different-outcomes', 'missing-run'])
def test_aggregate_does_not_hide_order_identity_or_outcome_drift(fault):
    runs = abba()
    if fault == 'order':
        runs[2], runs[3] = runs[3], runs[2]
    elif fault == 'same-run':
        runs[2]['run_id'] = runs[1]['run_id']
    elif fault == 'different-outcomes':
        runs[2]['node_outcomes'][NODES[1]] = 'passed'
    else:
        runs.pop()
    with pytest.raises(ValueError):
        compare_abba(runs)



def test_coverage_candidates_keep_all_roles_even_with_identical_hashes_without_data_claim():
    value = reduce(*receipts('coverage'), mode='coverage')
    assert set(value['coverage_candidates']) == set(ROLES)
    assert len({c['sha256'] for c in value['coverage_candidates'].values()}) == 1
    assert len({c['path'] for c in value['coverage_candidates'].values()}) == 9
    assert value['qualification'] == 'RECEIPT_LOGIC_ONLY_NOT_COVERAGE_DATA_ACCEPTANCE'
    assert value['process_identity_custody_close_and_data_validated'] is False
    assert 'session_hook_seconds' not in value and 'outer_ms_per_node_outcome' not in value


@pytest.mark.parametrize('fault', ['missing-data', 'wrong-file', 'bad-hash', 'bool-size',
                                  'zero-size', 'changed-tool', 'missing-early',
                                  'bool-exit', 'metadata-drift'])
def test_coverage_declared_data_and_process_metadata_fail_closed(fault):
    raw, starts, finishes = receipts('coverage')
    row = finishes['gw1']
    if fault == 'missing-data':
        del row['coverage_data']
    elif fault == 'wrong-file':
        row['coverage_data']['file'] = finishes['gw0']['coverage_data']['file']
    elif fault == 'bad-hash':
        row['coverage_data']['sha256'] = 'not-a-hash'
    elif fault == 'bool-size':
        row['coverage_data']['bytes'] = True
    elif fault == 'zero-size':
        row['coverage_data']['bytes'] = 0
    elif fault == 'changed-tool':
        row['coverage']['native_core'] = 'PyTracer'
    elif fault == 'missing-early':
        del starts['gw1']['coverage']
    elif fault == 'bool-exit':
        row['qualified_exit'] = False
    elif fault == 'metadata-drift':
        row['coverage']['native_tracer_file'] = '/private/tool/coverage/another-tracer.so'
    with pytest.raises(ValueError):
        reduce(raw, starts, finishes, mode='coverage')


def test_mode_separation_never_sanitizes_or_treats_coverage_as_timing():
    raw, starts, finishes = receipts('coverage')
    before = deepcopy((starts, finishes))
    with pytest.raises(ValueError, match='mode'):
        reduce(raw, starts, finishes)
    assert (starts, finishes) == before
    with pytest.raises(ValueError, match='mode'):
        reduce(*receipts(), mode='coverage')
    runs = abba()
    runs[1]['mode'] = 'coverage'
    with pytest.raises(ValueError):
        compare_abba(runs)
