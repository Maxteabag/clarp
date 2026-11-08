"""Path-receipt consumer controls only; UNEXECUTED, not native seed proof.

Primary owner: rejection of mismatched native source_file and operation roots.
Credible failure: comparing only measured/reference roots misses initial-prefix
rewrites with retained revisions. Existing closed-image tests protect DB byte
copies, not this logical identity. No product seam or runtime fixture is added.
"""
import copy
import socket

import pytest

from .seed_paths import audit_initial_paths, require_same_workspace

ROOT = '/var/tmp/criterion937/active'


@pytest.fixture(autouse=True)
def admitted_buildbox_only():
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no local control execution authorized')


def captured():
    # Explicit typed receipt fixture for consumer behavior only. It cannot
    # certify that any native producer, DB, seed or process existed.
    messages, checkpoints = [], []
    for i in range(3):
        agent, backend = f'synthetic-{i}', f'bs-synthetic-{i}'
        path = ROOT + f'/home/.claude/projects/project/bs-synthetic-{i}.jsonl'
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


def test_native_literal_paths_and_client_rows_remain_distinct_without_mutation():
    raw = captured()
    before = copy.deepcopy(raw)
    result = audit_initial_paths(raw, root=ROOT)
    assert result['logical_root'] == '/var/tmp/criterion937/active'
    assert result['sources']['synthetic-1']['source_file'] == (
        '/var/tmp/criterion937/active/home/.claude/projects/project/bs-synthetic-1.jsonl')
    assert result['census'] == {
        'synthetic-0': {'transcript_rows': 1, 'client_rows': 1, 'checkpoint_rows': 1},
        'synthetic-1': {'transcript_rows': 1, 'client_rows': 1, 'checkpoint_rows': 1},
        'synthetic-2': {'transcript_rows': 1, 'client_rows': 1, 'checkpoint_rows': 1}}
    assert raw == before
    assert require_same_workspace(initial_root=ROOT, operation_root=ROOT) == result['sources']


@pytest.mark.parametrize('fault,reason', [
    ('message-path', 'source_file differs'), ('checkpoint-path', 'source_file differs'),
    ('backend', 'unexpected backend'), ('missing-checkpoint', 'missing/duplicate'),
    ('duplicate-checkpoint', 'missing/duplicate'), ('missing-transcript', 'missing/duplicate'),
    ('typed-path', 'not native text'), ('short-row', 'incomplete native raw row'),
    ('excluded', 'format/exclusions'), ('column', 'source-path columns'),
])
def test_path_evidence_refuses_wrong_initial_identity_or_incomplete_rows(fault, reason):
    raw = captured()
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
        audit_initial_paths(raw, root=ROOT)


@pytest.mark.parametrize('operation_root', [
    '/var/tmp/criterion937/measured', '/var/tmp/criterion937/active/',
    '/var/tmp/criterion937/./active', '/var/tmp/criterion937/a/../active',
    '//var/tmp/criterion937/active', '/', 'relative', ROOT + '\x00',
])
def test_operation_cannot_substitute_an_alias_or_different_initial_root(operation_root):
    with pytest.raises(ValueError):
        require_same_workspace(initial_root=ROOT, operation_root=operation_root)
