"""SAME434 native report API STORAGE source, UNEXECUTED.

One shared retained-nine API fixture/union, actual B source analysis and full
physical graph; SYNTHETIC process metadata/arcs are NOT pytest/source/kernel proof.
"""
import json
from pathlib import Path

from .coverage_build_union_controls import _run_build_union_storage_fixture
from .coverage_copy_read import _same_capture
from .coverage_retained_bytes import read_retained_bytes
from .coverage_snapshot_control_priority import run_snapshot_priority_controls


def run_render_storage_control(*, coverage_module, source_root, root, coverage_archive_path):
    # Lower actual-source-hash fault adapter, not real failing FD/signal proof.
    # Isolated interpreter/no concurrent tooling users is required.
    run_snapshot_priority_controls(owner='report_source')
    fixture = _run_build_union_storage_fixture(
        root=root, coverage_data_class=coverage_module.CoverageData,
        coverage_archive_path=coverage_archive_path, report_module=coverage_module,
        source_root=source_root)
    combined = fixture['combined_report']
    report = combined['report']
    observed = report['raw_report_observation']
    raw = read_retained_bytes(observed, byte_limit=observed['bytes'], path_bytes_limit=4096)
    parsed = json.loads(raw)
    assert parsed['meta']['show_contexts'] is False
    assert parsed['meta']['branch_coverage'] is True
    assert len(parsed['files']) == 434
    for row in parsed['files'].values():
        assert 'contexts' not in row
        for kind in ('functions', 'classes'):
            assert '' in row[kind]
            assert all('contexts' not in region for region in row[kind].values())
    assert report['consistency']['source_file_count'] == 434
    assert report['region_identity']['source_file_count'] == 434
    assert report['region_identity']['named_function_count'] == 6151
    assert report['region_identity']['named_class_count'] == 476
    assert report['captured_files_without_frozen_source'] == ['server/negative.py', 'server/shared.py']
    assert set(report['analysis_records']) == set(parsed['files'])
    # Independent literal API-storage oracle and full graph/capture/index/original
    # byte checks already ran inside the SAME shared fixture, without re-union.
    graph = fixture['union']['published_graph']['decoded_graph']
    assert set(graph['role_captures']) == set(fixture['captures'])
    for role, capture in fixture['captures'].items():
        assert _same_capture(graph['role_captures'][role], capture)
    assert graph['closed_aggregate_capture']['accessor_sets']['files'][
        'server/negative.py']['contexts']['context[a].*'] == []
    assert report['actual_run_input_producer_coverage_or_no_worse_accepted'] is False
    assert report['consistency']['source_input_process_coverage_or_no_worse_accepted'] is False
    assert combined['actual_source_producer_coverage_or_no_worse_accepted'] is False
    # Actual graph raw bytes, not a missing-field success flag or source-range
    # invented attribution. Native report/producer/ABI acceptance remains false.
    assert Path(report['graph_observation']['path']).is_file()
    return {'result': 'EXPECTED_GRAPH_BOUND_FALSE_REPORT_API_STORAGE_SCOPE',
            'scope': 'actual API/source analysis+graph bytes; supplied arcs/SYNTHETIC metadata NOT traced pytest'}
