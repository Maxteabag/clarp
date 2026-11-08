"""Actual nine-input union plus complete graph publication SOURCE, UNEXECUTED.

Retained source/config/tool/run-byte and producer authentication are EXTERNAL
prerequisites. Binding digests here remain declarations, NOT their verification.
No no-context reporting, index commit, retry or measurement acceptance.
"""
import hashlib
from pathlib import Path
import socket

from .coverage_build_union import build_union
from .coverage_graph_publish import publish_graph
from .coverage_graph_wire import FORMAT, _check, _envelope
from .coverage_copy_read import _root
from .suite_evidence import ROLES

SOURCE_CATALOG_SHA256 = 'c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19'
COVERAGE_ARCHIVE_SHA256 = '49d58df8284f40cb16b781d5fc8b1d4fdefe185ce0c691e39e4b73d1f83c575b'


def build_union_with_graph(config_bytes, starts, finishes, *, actual_exit, outer_seconds,
                           cohort, captures, limits, aggregate_root, verify_root,
                           coverage_data_class, graph_root, wire_limits, bindings):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local union/publication authority')
    # Validate fixed binding/role shapes and caller limits before the actual
    # native API union. These are declarations, not actual provenance proof.
    graph = {'format': FORMAT, 'bindings': bindings, 'role_captures': captures,
             'reference_union': {}, 'closed_aggregate_capture': {}}
    _check(graph, wire_limits)
    _envelope(graph)
    if (type(config_bytes) is not bytes
            or bindings['config_sha256'] != hashlib.sha256(config_bytes).hexdigest()
            or bindings['source_catalog_sha256'] != SOURCE_CATALOG_SHA256
            or bindings['coverage_archive_sha256'] != COVERAGE_ARCHIVE_SHA256):
        raise ValueError('union/config/frozen catalog/tool declaration mismatch')
    graph_root = _root(graph_root)
    if any(graph_root.iterdir()):
        raise ValueError('fresh EMPTY graph root required before union')
    other_roots = [_root(aggregate_root), _root(verify_root)]
    for role in ROLES:
        for kind in ('original_observation', 'copy_observation'):
            # Actual raw file versions/graph validity are then rechecked by the
            # existing union owner before any native aggregate construction.
            other_roots.append(_root(Path(captures[role]['read_close_receipt'][kind]['path']).parent))
    if any(graph_root == other or graph_root.is_relative_to(other)
           or other.is_relative_to(graph_root) for other in other_roots):
        raise ValueError('graph root overlaps union input/output custody')
    union = build_union(config_bytes, starts, finishes, actual_exit=actual_exit,
                        outer_seconds=outer_seconds, cohort=cohort, captures=captures,
                        limits=limits, aggregate_root=aggregate_root, verify_root=verify_root,
                        coverage_data_class=coverage_data_class)
    # These full objects originate in the actual owner call above, not supplied
    # successful union flags or a subset copied from an API graph declaration.
    graph['reference_union'] = union['reference_union']
    graph['closed_aggregate_capture'] = union['closed_aggregate_capture']
    publication = publish_graph(root=graph_root, graph=graph, limits=wire_limits)
    # Publication failure/control propagates: no normal return or next stage.
    return {**union, 'published_graph': publication,
            'qualification': 'NINE_API_UNION_GRAPH_BYTES_ONLY_NOT_SOURCE_PRODUCER_OR_REPORT_ACCEPTANCE',
            'source_config_tool_run_binding_bytes_authenticated': False,
            'actual_run_source_producer_schema_denominator_accepted': False}
