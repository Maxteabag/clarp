"""Actual filesystem/wire STORAGE control SOURCE, UNEXECUTED.

Supplied records are NOT semantic captures, traced coverage or producer proof.
"""
from pathlib import Path

from .coverage_graph_publish import publish_graph, verify_graph, NAME
from .coverage_graph_wire import FORMAT, B_PIN, BINDINGS
from .coverage_copy_read import _same_capture
from .suite_evidence import ROLES


def run_graph_publication_storage_control(*, root):
    # Real publisher requires a private fresh buildbox root. These are deliberately
    # supplied wire records; no actual semantic/API/source producer claim.
    graph = {'format': FORMAT,
             'bindings': {k: B_PIN if k == 'source_pin' else 'a' * 64 for k in BINDINGS},
             'role_captures': {r: {'contexts': {'named': [[-7, -1]], 'empty': []},
                                   'typed': [True, 1]} for r in ROLES},
             'reference_union': {'contexts': ['', 'named']},
             'closed_aggregate_capture': {'supplied_wire_only': True}}
    limits = {'nodes': 10000, 'depth': 64, 'string_characters': 10000, 'wire_bytes': 200000}
    result = publish_graph(root=root, graph=graph, limits=limits)
    assert _same_capture(result['decoded_graph'], graph)
    assert result['actual_graph_source_producer_or_report_accepted'] is False
    actual = Path(root) / NAME
    before = actual.read_bytes()
    assert before and result['observation']['bytes'] == len(before)
    # Existing output refuses without overwrite, even with the same graph.
    try:
        publish_graph(root=root, graph=graph, limits=limits)
    except ValueError:
        pass
    else:
        raise AssertionError('existing graph was overwritten or silently reused')
    assert actual.read_bytes() == before
    # An incorrect receipt cannot claim the real bytes, and the original stays.
    bad = dict(result['observation']); bad['sha256'] = '0' * 64
    try:
        verify_graph(root=root, observation=bad, expected_graph=graph, limits=limits)
    except ValueError:
        pass
    else:
        raise AssertionError('wrong graph linkage hash accepted')
    assert actual.read_bytes() == before
    return {'scope': 'FILESYSTEM_WIRE_STORAGE_ONLY_NOT_GRAPH_OR_PRODUCER_PROOF',
            'outcomes': 3, 'actual_coverage_or_report_accepted': False}
