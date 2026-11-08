"""Path-receipt consumer controls only; UNEXECUTED, not native seed proof.

Primary owner: rejection of mismatched native source_file and operation roots.
Credible failure: comparing only measured/reference roots misses initial-prefix
rewrites with retained revisions. Existing closed-image tests protect DB byte
copies, not this logical identity. No product seam or runtime fixture is added.
"""
import copy
import socket

import pytest

from .seed_paths import (
    A_PIN, A_POLICY_SOURCES, DISABLED_A_INITIAL, REQUIRED_PUBLICATION,
    a_initial_checkpoint_policy, audit_initial_paths, require_same_workspace,
)

ROOT = '/var/tmp/criterion937/active'


@pytest.fixture(autouse=True)
def admitted_buildbox_only():
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no local control execution authorized')


def captured(*, published=False):
    # Explicit typed receipt fixture for consumer behavior only. It cannot
    # certify that any native producer, DB, seed or process existed.
    messages, checkpoints = [], []
    for i in range(3):
        agent, backend = f'synthetic-{i}', f'bs-synthetic-{i}'
        path = ROOT + f'/home/.claude/projects/project/bs-synthetic-{i}.jsonl'
        if published:
            checkpoints.append([['text', agent], ['text', backend], ['text', path]])
        messages.append([['text', agent], ['text', backend], ['text', path]])
        messages.append([['text', agent], ['text', backend], ['text', 'client:synthetic']])
    # Other native owners are outside this narrow contract, not omitted from
    # the source raw object or the independent whole-product oracle.
    messages.append([['text', 'other-owner'], ['null'], ['null']])
    columns = ['agent_id', 'backend_session_id', 'source_file']
    return {'format': 'criterion5-937-typed-raw-v1', 'capture': {'exclusions': []},
            'tables': {'messages': {'columns': columns, 'rows': messages},
                       'transcript_import_checkpoints': {'columns': columns, 'rows': checkpoints}}}


@pytest.mark.parametrize('route,published,count', [
    (DISABLED_A_INITIAL, False, 0), (REQUIRED_PUBLICATION, True, 1),
])
def test_native_literal_paths_and_client_rows_remain_distinct_without_mutation(route, published, count):
    raw = captured(published=published)
    before = copy.deepcopy(raw)
    result = audit_initial_paths(raw, root=ROOT, checkpoint_route=route)
    assert result['logical_root'] == '/var/tmp/criterion937/active'
    assert result['sources']['synthetic-1']['source_file'] == (
        '/var/tmp/criterion937/active/home/.claude/projects/project/bs-synthetic-1.jsonl')
    assert result['census'] == {
        'synthetic-0': {'transcript_rows': 1, 'client_rows': 1, 'checkpoint_rows': count},
        'synthetic-1': {'transcript_rows': 1, 'client_rows': 1, 'checkpoint_rows': count},
        'synthetic-2': {'transcript_rows': 1, 'client_rows': 1, 'checkpoint_rows': count}}
    assert raw == before
    assert require_same_workspace(initial_root=ROOT, operation_root=ROOT) == result['sources']


@pytest.mark.parametrize('fault,route,published,reason', [
    ('message-path', DISABLED_A_INITIAL, False, 'source_file differs'),
    ('checkpoint-path', REQUIRED_PUBLICATION, True, 'source_file differs'),
    ('backend', DISABLED_A_INITIAL, False, 'unexpected backend'),
    ('missing-checkpoint', REQUIRED_PUBLICATION, True, 'missing/duplicate'),
    ('duplicate-checkpoint', REQUIRED_PUBLICATION, True, 'missing/duplicate'),
    ('unexpected-checkpoint', DISABLED_A_INITIAL, False, 'missing/duplicate'),
    ('missing-transcript', DISABLED_A_INITIAL, False, 'missing/duplicate'),
    ('typed-path', DISABLED_A_INITIAL, False, 'not native text'),
    ('short-row', DISABLED_A_INITIAL, False, 'incomplete native raw row'),
    ('excluded', DISABLED_A_INITIAL, False, 'format/exclusions'),
    ('column', DISABLED_A_INITIAL, False, 'source-path columns'),
    ('unknown-policy', 'CHOOSE_FROM_ROWS', False, 'checkpoint route required'),
])
def test_path_evidence_refuses_wrong_initial_identity_or_incomplete_rows(fault, route, published, reason):
    raw = captured(published=published)
    messages = raw['tables']['messages']
    checkpoints = raw['tables']['transcript_import_checkpoints']
    if fault == 'message-path':
        messages['rows'][0][2] = ['text', '/var/tmp/other-initial/bs-synthetic-0.jsonl']
    elif fault == 'checkpoint-path':
        checkpoints['rows'][0][2] = ['text', '/var/tmp/other-initial/bs-synthetic-0.jsonl']
    elif fault == 'backend':
        messages['rows'][0][1] = ['text', 'other-session']
    elif fault == 'missing-checkpoint':
        checkpoints['rows'].pop(0)
    elif fault == 'duplicate-checkpoint':
        checkpoints['rows'].append(copy.deepcopy(checkpoints['rows'][0]))
    elif fault == 'unexpected-checkpoint':
        checkpoints['rows'].append(copy.deepcopy(messages['rows'][0]))
    elif fault == 'missing-transcript':
        messages['rows'].pop(0)
    elif fault == 'typed-path':
        messages['rows'][0][2] = ['blob', '00']
    elif fault == 'short-row':
        messages['rows'][0].pop()
    elif fault == 'excluded':
        raw['capture']['exclusions'] = ['source_file']
    elif fault == 'column':
        messages['columns'] = ['agent_id', 'backend_session_id', 'different_field']
    with pytest.raises(ValueError, match=reason):
        audit_initial_paths(raw, root=ROOT, checkpoint_route=route)


def native_disabled_returns():
    # Shape-only policy-consumer fixture; no actual config/native return proof.
    operations = []
    for i in range(3):
        config = {'config_file': ROOT + '/config/config.toml', 'config_file_state': 'ABSENT',
                  'raw_transcript_shadow': {}, 'native_settings': {'enabled': False}}
        operations.append({'agent': f'synthetic-{i}', 'backend_session_id': f'bs-synthetic-{i}',
                           'source_file': ROOT + f'/home/.claude/projects/project/bs-synthetic-{i}.jsonl',
                           'native_shadow_before': copy.deepcopy(config),
                           'native_shadow_after': copy.deepcopy(config),
                           'native_return': {'shadow': 'disabled'}})
    return operations


@pytest.mark.parametrize('fault', [None, 'pin', 'mode', 'source', 'missing-call',
                                  'return', 'config-present', 'config-enabled', 'settings-enabled'])
def test_zero_policy_requires_declared_source_config_and_native_import_route(fault):
    pin, mode, sources, operations = A_PIN, 'initial_seed', dict(A_POLICY_SOURCES), native_disabled_returns()
    if fault == 'pin':
        pin = '0073254687d22ac5a6c1d01b8af29fa678dcf4a2'
    elif fault == 'mode':
        mode = 'measured'
    elif fault == 'source':
        sources['server/lib/transcript_incremental.py'] = '0' * 64
    elif fault == 'missing-call':
        operations.pop()
    elif fault == 'return':
        operations[0]['native_return']['shadow'] = 'not_sampled'
    elif fault == 'config-present':
        operations[0]['native_shadow_before']['config_file_state'] = 'PRESENT'
    elif fault == 'config-enabled':
        operations[0]['native_shadow_before']['raw_transcript_shadow'] = {'enabled': True}
    elif fault == 'settings-enabled':
        operations[0]['native_shadow_after']['native_settings']['enabled'] = True
    if fault is None:
        policy = a_initial_checkpoint_policy(pin=pin, mode=mode, root=ROOT,
                                             source_files=sources, native_imports=operations)
        assert policy['route'] == 'A_INITIAL_DEFAULT_DISABLED'
        assert policy['native_imports'] == operations
    else:
        with pytest.raises(ValueError):
            a_initial_checkpoint_policy(pin=pin, mode=mode, root=ROOT,
                                        source_files=sources, native_imports=operations)


@pytest.mark.parametrize('operation_root', [
    '/var/tmp/criterion937/measured', '/var/tmp/criterion937/active/',
    '/var/tmp/criterion937/./active', '/var/tmp/criterion937/a/../active',
    '//var/tmp/criterion937/active', '/', 'relative', ROOT + '\x00',
])
def test_operation_cannot_substitute_an_alias_or_different_initial_root(operation_root):
    with pytest.raises(ValueError):
        require_same_workspace(initial_root=ROOT, operation_root=operation_root)
