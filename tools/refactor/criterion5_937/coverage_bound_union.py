"""937 actual retained bytes -> native union -> graph SOURCE, UNEXECUTED.

No alternative parsed config/start/finish inputs. Actual loaded source/class,
process/index-producer/outer-exit/cohort provenance remain external prerequisites.
No report/index publication, retry, deletion or measurement acceptance.
"""
import hashlib
import socket

from .coverage_copy_read import _root, _same_capture
from .coverage_graph_wire import _check
from .coverage_sets import check_limits
from .coverage_retained_bytes import read_retained_bytes, hash_retained_bytes
from .coverage_receipt_decode import decode_coverage_config, decode_phase_record, check_json_limits
from .coverage_receipt_index import preflight_inputs, decode_receipt_index
from .coverage_union_graph import build_union_with_graph
from .coverage_build_union import _verify_input
from .suite_evidence import ROLES


def build_bound_union_graph(*, bindings, receipts, captures, receipt_root, output_roots,
                            input_limits, json_limits, coverage_limits, wire_limits,
                            coverage_data_class, actual_exit, outer_seconds, cohort):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local retained union/control authority')
    if type(captures) is not dict or set(captures) != set(ROLES):
        raise ValueError('complete nine captures required')
    protected = {role: {kind: captures[role]['read_close_receipt'][kind]
                        for kind in ('original_observation', 'copy_observation')} for role in ROLES}
    # Complete pure metadata preflight FIRST: no binding read/native API before
    # all41 observations and caller byte/root constraints have been checked.
    preflight_inputs(bindings=bindings, receipts=receipts, data_observations=protected,
                     receipt_root=receipt_root, output_roots=output_roots, limits=input_limits)
    check_limits(coverage_limits)
    _check(None, wire_limits)
    check_json_limits(json_limits)
    for path in output_roots.values():
        if any(_root(path).iterdir()):
            raise ValueError('fresh EMPTY union/graph/report output roots required')

    def read(observation, label):
        return read_retained_bytes(observation, byte_limit=input_limits['artifact_bytes'][label],
                                   path_bytes_limit=input_limits['path_bytes'])

    def hashed(observation, label):
        return hash_retained_bytes(observation, byte_limit=input_limits['artifact_bytes'][label],
                                   path_bytes_limit=input_limits['path_bytes'])

    # Hash-only inputs remain actual retained files; no unnecessary archive/
    # catalog/profile whole-file buffers, extraction or module imports.
    checked_bindings = {name: hashed(bindings[name], name) for name in ('catalog', 'archive', 'profile')}
    config_raw = read(bindings['config'], 'config')
    index_raw = read(bindings['index'], 'index')
    for name in ('config', 'index'):
        # Each complete original byte string just passed exact physical/version/
        # digest read checks. Preserve the same immutable checked observation.
        checked_bindings[name] = dict(bindings[name])
    config = decode_coverage_config(config_raw, limits=json_limits)
    decode_receipt_index(index_raw, config_raw=config_raw, bindings=bindings, receipts=receipts,
                         data_observations=protected, receipt_root=receipt_root,
                         output_roots=output_roots, limits=input_limits, json_limits=json_limits)
    starts, finishes = {}, {}
    for role in ROLES:
        for phase, destination in (('start', starts), ('finish', finishes)):
            observed = receipts[role][phase]
            raw = read(observed, role + '.' + phase)
            destination[role] = decode_phase_record(
                raw, config_raw=config_raw, role=role, phase=phase,
                artifact_path=observed['path'], limits=json_limits)
    # Full reducer/reference/native API/data-version validation is still owned
    # by the existing union. Only objects decoded from actual retained bytes
    # above reach it; no caller starts/finishes or successful-union flag exists.
    result = build_union_with_graph(
        config_raw, starts, finishes, actual_exit=actual_exit, outer_seconds=outer_seconds,
        cohort=cohort, captures=captures, limits=coverage_limits,
        aggregate_root=output_roots['aggregate'], verify_root=output_roots['verify'],
        coverage_data_class=coverage_data_class, graph_root=output_roots['graph'],
        wire_limits=wire_limits,
        bindings={'source_pin': config['pin'],
                  'source_catalog_sha256': checked_bindings['catalog']['sha256'],
                  'coverage_archive_sha256': checked_bindings['archive']['sha256'],
                  'config_sha256': hashlib.sha256(config_raw).hexdigest(),
                  'run_receipts_sha256': hashlib.sha256(index_raw).hexdigest()})
    # All native API and publication-owned handles have completed mandatory
    # cleanup. Any failure below means no usable owner result/index/report.
    for name in ('catalog', 'archive', 'profile', 'config', 'index'):
        if not _same_capture(hashed(bindings[name], name), checked_bindings[name]):
            raise ValueError('binding changed after required union/publication cleanup')
    checked_receipts = {}
    for role in ROLES:
        checked_receipts[role] = {}
        for phase in ('start', 'finish'):
            checked_receipts[role][phase] = hashed(receipts[role][phase], role + '.' + phase)
    for role in ROLES:
        _verify_input(captures[role])  # actual original/copy versions after graph publication
    return {**result, 'retained_binding_observations': checked_bindings,
            'retained_receipt_observations': checked_receipts,
            'qualification': 'ACTUAL_RETAINED_BYTES_TO_UNION_GRAPH_NOT_KERNEL_SOURCE_OR_REPORT_ACCEPTANCE',
            'source_config_tool_run_binding_bytes_authenticated': False,
            'actual_run_source_producer_schema_denominator_accepted': False}
