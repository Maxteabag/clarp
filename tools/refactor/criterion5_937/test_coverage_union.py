"""LOWER pure reference-union SOURCE controls, NOT RUN.

Reuse the existing synthetic full-receipt fixture, not a copied validator.
Captures are supplied records, NOT actual API/custody/pytest-positive evidence.
"""
from copy import deepcopy

import pytest

from .coverage_union import reference_union
from .suite_evidence import ROLES
from .test_suite_evidence import NODES, receipts

LIMITS = {'files': 10, 'contexts': 64, 'pairs': 128, 'items': 1024,
          'name_bytes': 256, 'total_name_bytes': 8192}


def inputs(*, unique_contexts=False):
    raw, starts, finishes = receipts('coverage')
    captures = {}
    for i, role in enumerate(ROLES):
        pid = finishes[role]['pid']
        path = '/private/coverage-out/coverage-pid-' + str(pid) + '.data'
        context = 'ctx[.*]' if i == 0 else 'ctxaXYZ'
        unused = 'unused-' + role if unique_contexts or i < 2 else 'unused-shared'
        digest = f'{i + 1:064x}' if unique_contexts else ('b' if i == 0 else 'c' if i == 1 else 'a') * 64
        finishes[role]['coverage_data']['sha256'] = digest
        values = ((-1, 1), (1, 2), (2, -1)) if i == 0 else ((-1, 1), (1, 3), (3, -1))
        lines = (1, 2) if i == 0 else (1, 3)
        files = {'server/shared.py': {'tracer': '', 'values': values, 'lines': lines,
                                     'contexts': {context: values}, 'exhaustive_context_query': False}}
        if i == 0:
            files['server/negative.py'] = {'tracer': '', 'values': ((-5, -5),), 'lines': (),
                                          'contexts': {context: ((-5, -5),), unused: ()},
                                          'exhaustive_context_query': True}
        if i == 1:
            files['server/only-gw0.py'] = {'tracer': '', 'values': ((-4, 4), (4, -4)), 'lines': (4,),
                                         'contexts': {context: ((-4, 4), (4, -4))},
                                         'exhaustive_context_query': False}
        observation = {'dev': 1, 'bytes': 4096, 'sha256': digest,
                       'mtime_ns': 100, 'ctime_ns': 100}
        captures[role] = {
            'qualification': 'SAME_VERSION_COPY_API_CAPTURE_ONLY_NOT_INPUT_ACCEPTANCE',
            'source_producer_schema_valid_empty_or_union_accepted': False,
            'read_close_receipt': {'public_read_returned': True, 'public_close_returned': True,
                                   'copy_bytes_unchanged': True, 'original_bytes_unchanged': True,
                                   'artifact_sha256': digest, 'artifact_bytes': 4096,
                                   'schema_valid_empty_union_or_producer_accepted': False,
                                   'original_observation': {**observation, 'path': path, 'ino': 1000 + i},
                                   'copy_observation': {**observation, 'path': '/private/copies/' + role + '/data',
                                                        'ino': 2000 + i}},
            'accessor_sets': {'mode': 'branch', 'contexts': [context, unused], 'files': files,
                              'qualification': 'ACCESSOR_SETS_ONLY_NOT_INPUT_OR_COVERAGE_ACCEPTANCE',
                              'valid_empty_or_source_producer_custody_accepted': False}}
    return raw, starts, finishes, captures


def consume(raw, starts, finishes, captures):
    return reference_union(raw, starts, finishes, captures=captures, actual_exit=0,
                           outer_seconds=10.0, cohort=NODES, limits=LIMITS)


def test_reference_keeps_roles_files_context_identity_and_negative_arcs_without_acceptance():
    args = inputs()
    before = deepcopy(args)
    result = consume(*args)
    assert args == before
    assert [v['role'] for v in result['inputs']] == list(ROLES)
    assert len({v['sha256'] for v in result['inputs']}) == 3
    shared = result['files']['server/shared.py']
    assert shared['values'] == ((-1, 1), (1, 2), (1, 3), (2, -1), (3, -1))
    assert shared['contexts']['ctx[.*]'] == ((-1, 1), (1, 2), (2, -1))
    assert shared['contexts']['ctxaXYZ'] == ((-1, 1), (1, 3), (3, -1))
    assert result['files']['server/only-gw0.py']['values'] == ((-4, 4), (4, -4))
    negative = result['files']['server/negative.py']
    assert negative['values'] == ((-5, -5),) and negative['lines'] == ()
    assert set(negative['contexts']) == set(result['contexts'])
    assert negative['contexts']['ctxaXYZ'] == ()
    assert result['actual_data_source_producer_or_denominator_accepted'] is False


@pytest.mark.parametrize('fault', ['missing-role', 'source-path', 'inode-alias', 'bool-arc',
                                  'wrong-attribution', 'wrong-lines', 'unresolved-mode', 'hidden-omission'])
def test_reference_refuses_missing_alias_mistyped_or_incompatible_contributions(fault):
    raw, starts, finishes, captures = inputs()
    first, second = captures['controller'], captures['gw0']
    if fault == 'missing-role':
        del captures['gw7']
    elif fault == 'source-path':
        second['read_close_receipt']['original_observation']['path'] = '/private/not-the-role-file'
    elif fault == 'inode-alias':
        second['read_close_receipt']['copy_observation']['ino'] = 2000
    elif fault == 'bool-arc':
        first['accessor_sets']['files']['server/shared.py']['values'] = ((-1, True), (1, 2), (2, -1))
    elif fault == 'wrong-attribution':
        first['accessor_sets']['files']['server/shared.py']['contexts']['ctx[.*]'] = ((-1, 1), (1, 3), (3, -1))
    elif fault == 'wrong-lines':
        first['accessor_sets']['files']['server/shared.py']['lines'] = (1, 3)
    elif fault == 'unresolved-mode':
        second['accessor_sets'].update(mode='UNRESOLVED_EMPTY_MODE', contexts=[], files={})
    else:
        extra = 'tests/test_extra.py::test_B_only'
        for role in ROLES[1:]:
            finishes[role]['nodes_before'] = sorted([*NODES, extra])
            finishes[role]['deselected'] = [extra]
    with pytest.raises(ValueError):
        consume(raw, starts, finishes, captures)



def test_expanded_negative_only_context_output_must_fit_without_dropping_empty_relations():
    raw, starts, finishes, captures = inputs(unique_contexts=True)
    # Input has12 file/context pairs; the negative-only aggregate must expose
    # all global contexts, yielding14 output pairs. This is not a corpus bound.
    with pytest.raises(ValueError, match='expanded reference output limit'):
        reference_union(raw, starts, finishes, captures=captures, actual_exit=0,
                        outer_seconds=10.0, cohort=NODES, limits={**LIMITS, 'pairs': 12})



def test_identical_bytes_identical_sets_still_retain_all_nine_roles():
    raw, starts, finishes, captures = inputs()
    first = deepcopy(captures['controller'])
    digest = first['read_close_receipt']['artifact_sha256']
    for role in ROLES:
        # Same content, different owned source/copy identity declarations.
        identity = captures[role]['read_close_receipt']
        row = deepcopy(first)
        for kind in ('original_observation', 'copy_observation'):
            row['read_close_receipt'][kind]['path'] = identity[kind]['path']
            row['read_close_receipt'][kind]['ino'] = identity[kind]['ino']
        captures[role] = row
        finishes[role]['coverage_data']['sha256'] = digest
    result = consume(raw, starts, finishes, captures)
    assert len(result['inputs']) == 9
    assert len({v['sha256'] for v in result['inputs']}) == 1
    captures['gw7']['accessor_sets']['contexts'].append('incompatible-unused-context')
    with pytest.raises(ValueError, match='identical source bytes'):
        consume(raw, starts, finishes, captures)
