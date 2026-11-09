"""Actual retained union/full-graph to False native report SOURCE, UNEXECUTED.

Real collector/index/source-loaded/kernel/exit-cohort provenance remains external.
This intermediate owner does NOT publish the final accepted evidence index.
"""
from pathlib import Path
import socket

from .coverage_bound_union import build_bound_union_graph
from .coverage_build_union import _verify_input
from .coverage_copy_read import _root, _same_capture
from .coverage_graph_publish import verify_graph
from .coverage_graph_wire import FORMAT
from .coverage_receipt_decode import decode_coverage_config
from .coverage_retained_bytes import read_retained_bytes, hash_retained_bytes
from .coverage_render import _render_report
from .suite_evidence import B_PIN, ROLES


def render_bound_report(*, coverage_module, source_root, bindings, receipts, captures,
                        receipt_root, output_roots, input_limits, json_limits,
                        coverage_limits, wire_limits, actual_exit, outer_seconds, cohort):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no report/control authority')
    if coverage_module.__version__ != '7.17.0':
        raise ValueError('exact supplied coverage tool required')
    union = build_bound_union_graph(
        bindings=bindings, receipts=receipts, captures=captures, receipt_root=receipt_root,
        output_roots=output_roots, input_limits=input_limits, json_limits=json_limits,
        coverage_limits=coverage_limits, wire_limits=wire_limits,
        coverage_data_class=coverage_module.CoverageData,
        actual_exit=actual_exit, outer_seconds=outer_seconds, cohort=cohort)
    checked = union['retained_binding_observations']
    config_raw = read_retained_bytes(checked['config'], byte_limit=input_limits['artifact_bytes']['config'],
                                     path_bytes_limit=input_limits['path_bytes'])
    config = decode_coverage_config(config_raw, limits=json_limits)
    source_root = _root(source_root)
    if config['source_root'] != str(source_root):
        raise ValueError('report source root differs from retained native run configuration')
    # Independent expectation uses original complete captures and ACTUAL returned
    # reference/closed aggregate, not the graph's previously decoded pointer.
    expected = {'format': FORMAT,
                'bindings': {'source_pin': B_PIN, 'source_catalog_sha256': checked['catalog']['sha256'],
                             'coverage_archive_sha256': checked['archive']['sha256'],
                             'config_sha256': checked['config']['sha256'],
                             'run_receipts_sha256': checked['index']['sha256']},
                'role_captures': captures, 'reference_union': union['reference_union'],
                'closed_aggregate_capture': union['closed_aggregate_capture']}
    graph_observation = union['published_graph']['observation']
    report_root = _root(output_roots['report'])
    if any(report_root.iterdir()):
        raise ValueError('fresh EMPTY declared report root required')
    # These child directories stay within the already preflighted output root.
    # Any creation/graph/report failure preserves partials/no retry or index.
    input_root, output_root = report_root / 'input', report_root / 'output'
    input_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    report = _render_report(
        coverage_module=coverage_module, source_root=source_root,
        aggregate_capture=union['closed_aggregate_capture'], input_root=input_root,
        output_root=output_root, limits=coverage_limits,
        graph_root=output_roots['graph'], graph_observation=graph_observation,
        expected_graph=expected, wire_limits=wire_limits,
        tool_archive_sha256=checked['archive']['sha256'])
    # Required native Data closes/warning restore completed inside renderer.
    # Recheck all original retained bindings/raw records/data AND full graph
    # before a normal intermediate return. Final accepted index still absent.
    for name, observed in checked.items():
        actual = hash_retained_bytes(observed, byte_limit=input_limits['artifact_bytes'][name],
                                     path_bytes_limit=input_limits['path_bytes'])
        if not _same_capture(actual, observed):
            raise ValueError('report changed retained binding epoch')
    for role in ROLES:
        for phase in ('start', 'finish'):
            observed = union['retained_receipt_observations'][role][phase]
            hash_retained_bytes(observed, byte_limit=input_limits['artifact_bytes'][role + '.' + phase],
                                path_bytes_limit=input_limits['path_bytes'])
        _verify_input(captures[role])
    verify_graph(root=output_roots['graph'], observation=graph_observation,
                 expected_graph=expected, limits=wire_limits)
    return {'union': union, 'report': report,
            'qualification': 'RETAINED_FULL_GRAPH_AND_NATIVE_REPORT_BYTES_NOT_MEASUREMENT_ACCEPTANCE',
            'actual_source_producer_coverage_or_no_worse_accepted': False}
