"""Actual filesystem/wire STORAGE control SOURCE, UNEXECUTED.

Supplied records are NOT semantic captures, traced coverage or producer proof.
"""
import hashlib
import json
from pathlib import Path

from .coverage_graph_publish import publish_graph, verify_graph, NAME
from .coverage_graph_wire import FORMAT, B_PIN, BINDINGS
from .coverage_copy_read import _same_capture
from .coverage_retained_bytes import read_retained_bytes, hash_retained_bytes
from . import coverage_retained_bytes as retained_adapter
from . import coverage_observe_file as observer
from .coverage_retained_bytes_controls import run_retained_reader_fault_table
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
    assert read_retained_bytes(result['observation'], byte_limit=200000,
                               path_bytes_limit=4096) == before
    first = observer.observe_file(str(actual), byte_limit=200000, path_bytes_limit=4096, retain_bytes=True)
    assert first['raw'] == before
    assert first['observation']['sha256'] == hashlib.sha256(before).hexdigest()
    assert _same_capture(first['observation'], result['observation'])
    assert first['process_source_or_closed_writer_authenticated'] is False
    sentinel = object()
    original_buffer = retained_adapter.__dict__.get('bytearray', sentinel)
    original_observer_buffer = observer.__dict__.get('bytearray', sentinel)
    buffer_calls = []

    def forbidden_buffer(*args, **kwargs):
        buffer_calls.append(True)
        raise AssertionError('hash-only mode retained an archive bytearray')

    try:
        # Controlled TOOL alias only, not global builtin/OS/product mutation.
        # This witnesses this constructor path, NOT a universal heap bound.
        retained_adapter.bytearray = forbidden_buffer
        observer.bytearray = forbidden_buffer
        observed_hash = observer.observe_file(str(actual), byte_limit=200000, path_bytes_limit=4096, retain_bytes=False)
        assert observed_hash['raw'] is None
        assert _same_capture(observed_hash['observation'], result['observation'])
        # Bound refusal must occur before payload-buffer construction.
        try:
            observer.observe_file(str(actual), byte_limit=len(before) - 1, path_bytes_limit=4096, retain_bytes=True)
        except ValueError as error:
            assert 'pre-allocation bound' in str(error)
        else:
            raise AssertionError('first observation ignored smaller byte limit')
        hashed = hash_retained_bytes(result['observation'], byte_limit=200000,
                                     path_bytes_limit=4096)
    finally:
        if original_observer_buffer is sentinel:
            del observer.bytearray
        else:
            observer.bytearray = original_observer_buffer
        if original_buffer is sentinel:
            del retained_adapter.bytearray
        else:
            retained_adapter.bytearray = original_buffer
    if original_buffer is sentinel:
        assert 'bytearray' not in retained_adapter.__dict__
    else:
        assert retained_adapter.bytearray is original_buffer
    assert not buffer_calls
    assert _same_capture(hashed, result['observation'])
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
    try:
        read_retained_bytes(bad, byte_limit=200000, path_bytes_limit=4096)
    except ValueError:
        pass
    else:
        raise AssertionError('retained reader accepted wrong observed bytes')
    assert actual.read_bytes() == before
    try:
        hash_retained_bytes(bad, byte_limit=200000, path_bytes_limit=4096)
    except ValueError:
        pass
    else:
        raise AssertionError('hash-only reader accepted wrong observed bytes')
    faults = run_retained_reader_fault_table(result['observation'])
    assert faults['boolean_observation_refusals'] == 5
    assert faults['fault_outcomes'] == ['control_read', 'control_close', 'ordinary_pair']
    hash_faults = run_retained_reader_fault_table(result['observation'], operation='hash')
    assert hash_faults['boolean_observation_refusals'] == 0
    assert hash_faults['fault_outcomes'] == ['control_read', 'control_close', 'ordinary_pair']
    for operation in ('observe_read', 'observe_hash'):
        faults = run_retained_reader_fault_table(result['observation'], operation=operation)
        assert faults['boolean_observation_refusals'] == 0
        assert faults['fault_outcomes'] == ['control_read', 'control_close', 'ordinary_pair']
    assert actual.read_bytes() == before
    # SAME filesystem owner: one early-index FS publication, NOT a real
    # collector/31receipt/indexproducer/kernel positive. Observations supplied.
    from .coverage_publish_index import _publish_index
    from .coverage_receipt_index import FORMAT as INDEX_FORMAT
    index_root = Path(root) / 'index-storage'
    index_root.mkdir(mode=0o700)
    index = {'format': INDEX_FORMAT, 'pin': B_PIN, 'run_id': 'SYNTHETIC_STORAGE_ONLY',
             'config_sha256': 'a' * 64,
             'roles': {r: {p: dict(result['observation']) for p in ('start', 'finish')}
                       for r in ROLES}}
    index_limits = {'nodes': 10000, 'depth': 64, 'string_characters': 10000, 'wire_bytes': 200000}
    json_limits = {'bytes': 200000, 'nodes': 10000, 'depth': 64, 'string_characters': 10000}
    observed_index = _publish_index(root=index_root, index=index, index_limits=index_limits,
                                    json_limits=json_limits, path_bytes_limit=4096)
    index_raw = (index_root / 'index.json').read_bytes()
    assert json.loads(index_raw) == index
    assert observed_index['sha256'] == hashlib.sha256(index_raw).hexdigest()
    assert observed_index['bytes'] == len(index_raw)
    try:
        _publish_index(root=index_root, index=index, index_limits=index_limits,
                       json_limits=json_limits, path_bytes_limit=4096)
    except ValueError as error:
        assert 'unexpected original' in str(error)
    else:
        raise AssertionError('existing early index overwritten/reused')
    assert (index_root / 'index.json').read_bytes() == index_raw
    assert actual.read_bytes() == before
    return {'scope': 'FILESYSTEM_WIRE_STORAGE_ONLY_NOT_GRAPH_OR_PRODUCER_PROOF',
            'outcomes': 3, 'actual_coverage_or_report_accepted': False}
