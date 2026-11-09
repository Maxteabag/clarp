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


# LOWER raw-record decoding table, SOURCE ONLY/UNEXECUTED. These fixture objects
# are not retained native files, actual plugin/process or source proof.
DECODE_LIMITS = {'bytes': 1000000, 'nodes': 100000, 'depth': 64, 'string_characters': 1000000}


def _decoder_raw(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode()


def _decoder_fixture():
    raw, starts, finishes = receipts('coverage')
    spec = json.loads(raw)
    spec.update(source_root='/private/source', cohort='/private/cohort.json',
                coverage_config='/private/coverage.ini', coverage_config_sha256='d' * 64)
    raw = _decoder_raw(spec)
    for phase, container in (('start', starts), ('finish', finishes)):
        for role, row in container.items():
            row['spec'] = deepcopy(spec)
            row['config_sha256'] = hashlib.sha256(raw).hexdigest()
            row['coverage']['config_sha256'] = row['config_sha256']
            row['local_numprocesses'] = 8 if role == 'controller' else None
            if phase == 'start':
                row.update(started_mono=None, nodes_before=[], nodes_selected=[],
                           deselected=[], worker_collections={})
            else:
                row['timing_scope'] = 'session hooks only; outer pytest invocation/summary separately retained'
    return raw, starts, finishes


@pytest.mark.parametrize('phase', ['start', 'finish'])
def test_retained_phase_lower_finite_values_and_original_bytes(phase):
    from .coverage_receipt_decode import decode_phase_record
    config, starts, finishes = _decoder_fixture()
    row = starts['gw0'] if phase == 'start' else finishes['gw0']
    raw = _decoder_raw(row)
    digest = hashlib.sha256(raw).hexdigest()
    result = decode_phase_record(raw, config_raw=config, role='gw0', phase=phase,
                                 artifact_path='/private/coverage-out/gw0.' + phase + '.json',
                                 limits=DECODE_LIMITS)
    assert type(result['pid']) is int and result['pid'] == 101
    if phase == 'finish':
        assert type(result['events'][0]['duration']) is float
        assert result['events'][0]['duration'] == 0.125
        assert type(result['session_hook_elapsed_seconds']) is float
        assert result['session_hook_elapsed_seconds'] == 8.0
    else:
        assert result['started_mono'] is None
    assert hashlib.sha256(raw).hexdigest() == digest


@pytest.mark.parametrize('phase', ['start', 'finish'])
@pytest.mark.parametrize('fault', ['nested-duplicate', 'pid-bool', 'pid-float',
                                  'birth-bool', 'affinity-bool', 'embedded-float-worker',
                                  'wrong-role', 'wrong-phase-channel', 'foreign-output'])
def test_retained_phase_lower_exact_types_and_channel(phase, fault):
    from .coverage_receipt_decode import decode_phase_record
    config, starts, finishes = _decoder_fixture()
    row = deepcopy(starts['gw0'] if phase == 'start' else finishes['gw0'])
    path = '/private/coverage-out/gw0.' + phase + '.json'
    if fault == 'pid-bool':
        row['pid'] = True
    elif fault == 'pid-float':
        row['pid'] = 101.0
    elif fault == 'birth-bool':
        row['start_ticks'] = True
    elif fault == 'affinity-bool':
        row['affinity'][0] = False
    elif fault == 'embedded-float-worker':
        row['spec']['expected_workers'] = 8.0
    elif fault == 'wrong-role':
        row['worker'] = 'gw1'
    elif fault == 'wrong-phase-channel':
        path = '/private/coverage-out/gw0.' + ('finish' if phase == 'start' else 'start') + '.json'
    elif fault == 'foreign-output':
        path = '/different/gw0.' + phase + '.json'
    raw = _decoder_raw(row)
    if fault == 'nested-duplicate':
        assert raw.count(b'"expected_workers":8') == 1
        raw = raw.replace(b'"expected_workers":8', b'"expected_workers":8,"expected_workers":8', 1)
    original = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        decode_phase_record(raw, config_raw=config, role='gw0', phase=phase,
                            artifact_path=path, limits=DECODE_LIMITS)
    assert hashlib.sha256(raw).hexdigest() == original


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999'])
def test_retained_phase_lower_nested_nonfinite_refuses(token):
    from .coverage_receipt_decode import decode_phase_record
    config, _, finishes = _decoder_fixture()
    raw = _decoder_raw(finishes['gw0'])
    assert b'"duration":0.125' in raw
    raw = raw.replace(b'"duration":0.125', b'"duration":' + token.encode(), 1)
    original = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        decode_phase_record(raw, config_raw=config, role='gw0', phase='finish',
                            artifact_path='/private/coverage-out/gw0.finish.json', limits=DECODE_LIMITS)
    assert hashlib.sha256(raw).hexdigest() == original


def test_retained_phase_lower_boolean_counter_refuses():
    from .coverage_receipt_decode import decode_phase_record
    config, _, finishes = _decoder_fixture()
    row = deepcopy(finishes['gw0']); row['observer_events'] = True
    with pytest.raises(ValueError):
        decode_phase_record(_decoder_raw(row), config_raw=config, role='gw0', phase='finish',
                            artifact_path='/private/coverage-out/gw0.finish.json', limits=DECODE_LIMITS)


# LOWER index/declaration fixtures only. Physical versions/digests below are
# synthetic except config/index byte hashes; there are NO actual source files.
def _index_fixture():
    config, _, _ = _decoder_fixture()
    spec = json.loads(config)
    spec['coverage_config'] = '/private/bindings/coverage.ini'
    spec['coverage_config_sha256'] = '3cc8a9f20b04a5bd3e287364480ddfc039e996954da359540f1fe24472cf7de7'
    config = _decoder_raw(spec)
    numbers = iter(range(1, 100))

    def observed(path, *, size=100, digest='a' * 64):
        return {'path': path, 'dev': 1, 'ino': next(numbers), 'bytes': size,
                'mtime_ns': 0, 'ctime_ns': 0, 'sha256': digest}

    bindings = {
        'catalog': observed('/private/bindings/catalog.json', digest=
                            'c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19'),
        'archive': observed('/private/bindings/coverage.tar.gz', digest=
                            '49d58df8284f40cb16b781d5fc8b1d4fdefe185ce0c691e39e4b73d1f83c575b'),
        'profile': observed(spec['coverage_config'], digest=spec['coverage_config_sha256']),
        'config': observed('/private/bindings/config.json', size=len(config),
                           digest=hashlib.sha256(config).hexdigest()),
        'index': observed('/private/bindings/run-index.json')}
    receipts_index = {role: {phase: observed('/private/coverage-out/' + role + '.' + phase + '.json')
                              for phase in ('start', 'finish')} for role in ROLES}
    data = {role: {kind: observed(parent + '/coverage-pid-' + str(100 + i) + '.data')
                   for kind, parent in (('original_observation', '/private/coverage-out'),
                                        ('copy_observation', '/private/copies'))}
            for i, role in enumerate(ROLES)}
    index = {'format': 'criterion5-937-run-receipt-index-v1', 'pin': spec['pin'],
             'run_id': spec['run_id'], 'config_sha256': hashlib.sha256(config).hexdigest(),
             'roles': deepcopy(receipts_index)}
    raw = _decoder_raw(index)
    bindings['index'].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    names = set(bindings) | {role + '.' + phase for role in ROLES for phase in ('start', 'finish')}
    return {'raw': raw, 'config_raw': config, 'bindings': bindings, 'receipts': receipts_index,
            'data_observations': data, 'receipt_root': '/private/coverage-out',
            'output_roots': {key: '/private/' + key for key in ('aggregate', 'verify', 'graph', 'report')},
            'limits': {'artifact_bytes': {name: 1000000 for name in names},
                       'total_artifact_bytes': 10000000, 'path_bytes': 4096,
                       'data_file_bytes': 1000000, 'total_data_bytes': 18000000},
            'json_limits': DECODE_LIMITS}


def test_retained_index_lower_complete_declarations_not_authentication():
    from .coverage_receipt_index import decode_receipt_index, preflight_inputs
    args = _index_fixture()
    index = decode_receipt_index(**args)
    assert set(index['roles']) == set(ROLES)
    assert index['roles']['gw0']['start']['path'] == '/private/coverage-out/gw0.start.json'
    preflight = preflight_inputs(**{key: args[key] for key in (
        'bindings', 'receipts', 'data_observations', 'receipt_root', 'output_roots', 'limits')})
    assert preflight['declared_artifact_count'] == 23
    assert preflight['declared_protected_data_count'] == 18
    assert preflight['actual_source_run_or_byte_authentication_accepted'] is False


@pytest.mark.parametrize('fault', ['missing-profile', 'missing-role', 'bool-observation',
                                  'shared-inode', 'wrong-phase-path', 'data-alias',
                                  'output-overlap', 'artifact-total', 'data-total',
                                  'wrong-profile-hash', 'extra-observation', 'missing-copy'])
def test_retained_index_lower_prefile_declarations_refuse(fault):
    from .coverage_receipt_index import preflight_inputs
    args = _index_fixture()
    if fault == 'missing-profile':
        del args['bindings']['profile']
    elif fault == 'missing-role':
        del args['receipts']['gw7']
    elif fault == 'bool-observation':
        args['receipts']['gw0']['start']['ino'] = True
    elif fault == 'shared-inode':
        args['receipts']['gw0']['start']['ino'] = args['receipts']['gw1']['start']['ino']
    elif fault == 'wrong-phase-path':
        args['receipts']['gw0']['start']['path'] = '/private/coverage-out/gw0.other.json'
    elif fault == 'data-alias':
        args['data_observations']['gw0']['copy_observation']['ino'] = args['bindings']['catalog']['ino']
    elif fault == 'output-overlap':
        args['output_roots']['graph'] = '/private/coverage-out'
    elif fault == 'artifact-total':
        args['limits']['total_artifact_bytes'] = 1
    elif fault == 'data-total':
        args['limits']['total_data_bytes'] = 1
    elif fault == 'wrong-profile-hash':
        args['bindings']['profile']['sha256'] = 'd' * 64
    elif fault == 'extra-observation':
        args['bindings']['config']['accepted'] = True
    elif fault == 'missing-copy':
        del args['data_observations']['gw0']['copy_observation']
    with pytest.raises(ValueError):
        preflight_inputs(**{key: args[key] for key in (
            'bindings', 'receipts', 'data_observations', 'receipt_root', 'output_roots', 'limits')})


@pytest.mark.parametrize('fault', ['run', 'config-hash', 'format', 'index-bool-identity'])
def test_retained_index_lower_actual_raw_linkage_types(fault):
    from .coverage_receipt_index import decode_receipt_index
    args = _index_fixture()
    index = json.loads(args['raw'])
    if fault == 'run':
        index['run_id'] = 'another-run'
    elif fault == 'config-hash':
        index['config_sha256'] = 'f' * 64
    elif fault == 'format':
        index['format'] = 'unknown'
    else:
        assert index['roles']['gw0']['start']['dev'] == 1
        index['roles']['gw0']['start']['dev'] = True  # ordinary equality would hide this
    args['raw'] = _decoder_raw(index)
    args['bindings']['index'].update(bytes=len(args['raw']), sha256=hashlib.sha256(args['raw']).hexdigest())
    original = hashlib.sha256(args['raw']).hexdigest()
    with pytest.raises(ValueError):
        decode_receipt_index(**args)
    assert hashlib.sha256(args['raw']).hexdigest() == original
