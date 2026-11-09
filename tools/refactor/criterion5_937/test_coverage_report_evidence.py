"""LOWER report/analysis record contracts SOURCE, NOT RUN/API coverage proof.

Inputs are deliberately supplied records, not an actual valid report/analysis.
No actual code statement/branch execution or source authenticity is asserted.
"""
from copy import deepcopy

import pytest

from .coverage_report_evidence import B_PIN, COUNTS, reconcile_report


def records():
    inventory = {'pin': B_PIN, 'files': {
        'server/a.py': {'sha256': 'a' * 64, 'bytes': 100, 'physical_lines': 5},
        'server/empty.py': {'sha256': 'b' * 64, 'bytes': 0, 'physical_lines': 0}}}
    counts = dict(zip(COUNTS, (2, 3, 1, 1, 2, 2, 0, 0)))
    zero = dict.fromkeys(COUNTS, 0)
    report = {'meta': {'format': 3, 'version': '7.17.0', 'branch_coverage': True, 'show_contexts': False},
              'files': {
                  'server/a.py': {'executed_lines': [1, 2], 'missing_lines': [3], 'excluded_lines': [4],
                                  'executed_branches': [[2, -1], [2, 4]], 'missing_branches': [],
                                  'summary': counts},
                  'server/empty.py': {'executed_lines': [], 'missing_lines': [], 'excluded_lines': [],
                                      'executed_branches': [], 'missing_branches': [],
                                      'summary': zero}},
              'totals': deepcopy(counts)}
    analyses = {
        'server/a.py': {'source_sha256': 'a' * 64, 'statements': [1, 2, 3], 'excluded': [4], 'missing': [3],
                        'branch_stats': [{'line': 2, 'total': 2, 'taken': 2}]},
        'server/empty.py': {'source_sha256': 'b' * 64, 'statements': [], 'excluded': [], 'missing': [],
                            'branch_stats': []}}
    return report, analyses, inventory


def test_statement_denominator_exclusion_credits_and_empty_units_are_separate():
    report, analyses, inventory = records()
    before = deepcopy((report, analyses, inventory))
    value = reconcile_report(report, analyses, inventory=inventory)
    assert (report, analyses, inventory) == before
    assert value['source_file_count'] == 2
    assert value['files']['server/a.py']['counts']['covered_lines'] == 2
    assert value['files']['server/a.py']['statement_units'] == [1, 2, 3]
    assert value['files']['server/a.py']['literal_executed_branch_arcs'] == 2
    assert value['files']['server/a.py']['observed_arcs_to_excluded_destinations'] == 1
    assert value['files']['server/a.py']['tool_credited_unobserved_branches'] == 1
    assert value['files']['server/empty.py']['statement_covered_percent'] is None
    assert value['files']['server/empty.py']['branch_covered_percent'] is None
    assert value['source_input_process_coverage_or_no_worse_accepted'] is False


@pytest.mark.parametrize('fault', ['missing-file', 'extra-file', 'missing-analysis', 'duplicate-statement',
                                  'wrong-source', 'bool-count', 'wrong-statements', 'excluded-statement',
                                  'wrong-branch-credit', 'wrong-missing-branch', 'bool-branch',
                                  'wrong-totals', 'excluded-executed', 'excluded-branch-source', 'unexpected-file-context'])
def test_missing_inventory_or_inconsistent_units_cannot_be_report_consistent(fault):
    report, analyses, inventory = records()
    row = report['files']['server/a.py']
    if fault == 'missing-file':
        del report['files']['server/empty.py']
    elif fault == 'extra-file':
        report['files']['server/unexpected.py'] = deepcopy(row)
    elif fault == 'missing-analysis':
        del analyses['server/empty.py']
    elif fault == 'duplicate-statement':
        analyses['server/a.py']['statements'].append(2)
    elif fault == 'wrong-source':
        analyses['server/a.py']['source_sha256'] = 'c' * 64
    elif fault == 'bool-count':
        row['summary']['num_statements'] = True
    elif fault == 'wrong-statements':
        row['summary']['num_statements'] = 4
    elif fault == 'excluded-statement':
        analyses['server/a.py']['statements'].append(4)
    elif fault == 'wrong-branch-credit':
        row['summary'].update(num_branches=3, covered_branches=3)
    elif fault == 'wrong-missing-branch':
        row['missing_branches'] = [[2, 3]]
    elif fault == 'bool-branch':
        row['executed_branches'] = [[True, -1]]
    elif fault == 'excluded-branch-source':
        analyses['server/a.py']['branch_stats'] = [{'line': 4, 'total': 2, 'taken': 2}]
        row['executed_branches'] = [[4, -1], [4, 1]]
    elif fault == 'excluded-executed':
        row['executed_lines'].append(4)
    elif fault == 'wrong-totals':
        report['totals']['missing_lines'] = 0
    else:
        row['contexts'] = {'01': ['ctx[.*]']}  # every JSON context map now refuses
    reason = 'excluded source cannot be a public branch-statistics source' if fault == 'excluded-branch-source' else None
    with pytest.raises(ValueError, match=reason):
        reconcile_report(report, analyses, inventory=inventory)


# Separate LOWER projection/identity records. No native analysis/statistics or
# actual catalog loading is fabricated by these two-file supplied declarations.
def region_records():
    report = {'meta': {'format': 3, 'version': '7.17.0', 'branch_coverage': True, 'show_contexts': False},
              'files': {
                  'server/a.py': {'functions': {'fun': {'start_line': 2}, '': {'start_line': 4}},
                                  'classes': {'Thing': {'start_line': 1}, '': {'start_line': 5}}},
                  'server/empty.py': {'functions': {'': {'start_line': 1}},
                                      'classes': {'': {'start_line': 1}}}}}
    inventory = {'pin': B_PIN, 'files': {
        'server/a.py': {'sha256': 'a' * 64, 'physical_lines': 5},
        'server/empty.py': {'sha256': 'b' * 64, 'physical_lines': 0}}}
    regions = {'format': 'criterion5-937-static-region-identities-v1', 'pin': B_PIN,
               'catalog_sha256': 'c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19',
               'files': {
                   'server/a.py': {'source_sha256': 'a' * 64, 'physical_lines': 5,
                                   'functions': {'fun': 2}, 'classes': {'Thing': 1}},
                   'server/empty.py': {'source_sha256': 'b' * 64, 'physical_lines': 0,
                                       'functions': {}, 'classes': {}}}}
    return report, inventory, regions


def test_lower_region_identity_preserves_native_outside_not_membership():
    from .coverage_region_projection import _check_projection
    report, inventory, regions = region_records()
    before = deepcopy((report, inventory, regions))
    value = _check_projection(report, inventory=inventory, regions=regions)
    assert (report, inventory, regions) == before
    assert value['source_file_count'] == 2
    assert value['named_function_count'] == 1 and value['named_class_count'] == 1
    assert value['actual_source_graph_producer_or_coverage_accepted'] is False
    # Outside4/5 are legitimate native values, not forced to1; empty defaults1.
    assert report['files']['server/a.py']['functions']['']['start_line'] == 4


@pytest.mark.parametrize('fault', ['true-meta', 'file-context', 'function-context', 'class-context',
                                  'function-outside-context', 'class-outside-context',
                                  'missing-functions', 'missing-classes', 'missing-function-outside',
                                  'missing-class-outside', 'missing-name', 'foreign-name',
                                  'wrong-start', 'bool-start', 'float-start', 'empty-outside-start',
                                  'source-link', 'physical-bool'])
def test_lower_region_identity_projection_refuses(fault):
    from .coverage_region_projection import _check_projection
    report, inventory, regions = region_records()
    row = report['files']['server/a.py']
    if fault == 'true-meta':
        report['meta']['show_contexts'] = True
    elif fault == 'file-context':
        row['contexts'] = {}
    elif fault == 'function-context':
        row['functions']['fun']['contexts'] = {}
    elif fault == 'class-context':
        row['classes']['Thing']['contexts'] = {}
    elif fault == 'function-outside-context':
        row['functions']['']['contexts'] = {}
    elif fault == 'class-outside-context':
        row['classes']['']['contexts'] = {}
    elif fault == 'missing-functions':
        del row['functions']
    elif fault == 'missing-classes':
        del row['classes']
    elif fault == 'missing-function-outside':
        del row['functions']['']
    elif fault == 'missing-class-outside':
        del row['classes']['']
    elif fault == 'missing-name':
        del row['functions']['fun']
    elif fault == 'foreign-name':
        row['functions']['foreign'] = {'start_line': 3}
    elif fault == 'wrong-start':
        row['functions']['fun']['start_line'] = 3
    elif fault == 'bool-start':
        row['classes']['Thing']['start_line'] = True  # True==1 is NOT typed identity
    elif fault == 'float-start':
        row['functions']['fun']['start_line'] = 2.0
    elif fault == 'empty-outside-start':
        report['files']['server/empty.py']['functions']['']['start_line'] = 2
    elif fault == 'source-link':
        regions['files']['server/a.py']['source_sha256'] = 'c' * 64
    elif fault == 'physical-bool':
        regions['files']['server/a.py']['physical_lines'] = True
    with pytest.raises(ValueError):
        _check_projection(report, inventory=inventory, regions=regions)


def test_region_public_entry_refuses_unpinned_catalog_before_decode():
    from .coverage_region_projection import validate_region_projection
    import json
    report, inventory, regions = region_records()
    # Valid downstream LOWER identity records, padded to the exact catalog byte
    # length: this must fail the actual hash guard, not merely JSON/shape/size.
    raw = json.dumps(regions).encode()
    assert len(raw) < 371842
    raw += b' ' * (371842 - len(raw))
    with pytest.raises(ValueError):
        validate_region_projection(report, inventory=inventory, region_catalog_raw=raw,
                                   tool_archive_sha256='49d58df8284f40cb16b781d5fc8b1d4fdefe185ce0c691e39e4b73d1f83c575b')
