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
    report = {'meta': {'format': 3, 'version': '7.17.0', 'branch_coverage': True, 'show_contexts': True},
              'files': {
                  'server/a.py': {'executed_lines': [1, 2], 'missing_lines': [3], 'excluded_lines': [4],
                                  'executed_branches': [[2, -1], [2, 4]], 'missing_branches': [],
                                  'contexts': {'1': ['ctx[.*]'], '2': ['1'], '4': ['ctx[.*]']},
                                  'summary': counts},
                  'server/empty.py': {'executed_lines': [], 'missing_lines': [], 'excluded_lines': [],
                                      'executed_branches': [], 'missing_branches': [],
                                      'contexts': {}, 'summary': zero}},
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
                                  'wrong-totals', 'excluded-executed', 'noncanonical-line-context'])
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
    elif fault == 'excluded-executed':
        row['executed_lines'].append(4)
    elif fault == 'wrong-totals':
        report['totals']['missing_lines'] = 0
    else:
        row['contexts']['01'] = row['contexts'].pop('1')
    with pytest.raises(ValueError):
        reconcile_report(report, analyses, inventory=inventory)
