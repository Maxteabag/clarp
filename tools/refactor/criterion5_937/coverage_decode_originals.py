"""Actual31 observations -> strict receipt/data binding SOURCE, UNEXECUTED.

No index/SQLite/API or process authentication. Parent actual exit/closure,
source/interpreter/tracer custody and full package admission remain external.
"""
import hashlib

from .coverage_originals import observe_originals, LABELS
from .coverage_receipt_decode import decode_coverage_config, decode_phase_record, check_json_limits
from .coverage_retained_bytes import check_observation
from .coverage_receipt_index import PROFILE_SHA256
from .coverage_union_graph import SOURCE_CATALOG_SHA256, COVERAGE_ARCHIVE_SHA256
from .suite_evidence import coverage_inputs, ROLES
from .roster import checked


def _decode_originals(originals, *, receipt_root, paths, limits, json_limits,
                      actual_exit, outer_seconds, cohort):
    """Pure byte/record boundary; supplied records alone are LOWER evidence."""
    check_json_limits(json_limits)
    observations, raw = originals['observations'], originals['raw']
    payload_labels = {'config'} | {r + '.' + p for r in ROLES for p in ('start', 'finish')}
    if (type(observations) is not dict or set(observations) != set(LABELS)
            or type(raw) is not dict or set(raw) != payload_labels):
        raise ValueError('complete31 observations/original19 payloads required')
    for label in LABELS:
        observed = observations[label]
        check_observation(observed, byte_limit=limits['file_bytes'][label],
                          path_bytes_limit=limits['path_bytes'])
        if observed['path'] != paths[label]:
            raise ValueError('observed original path changed')
        if label in payload_labels:
            data = raw[label]
            if (type(data) is not bytes or len(data) != observed['bytes']
                    or hashlib.sha256(data).hexdigest() != observed['sha256']):
                raise ValueError('original payload differs from actual observation')
    for label, expected in (('catalog', SOURCE_CATALOG_SHA256),
                            ('archive', COVERAGE_ARCHIVE_SHA256), ('profile', PROFILE_SHA256)):
        if observations[label]['sha256'] != expected:
            raise ValueError('observed frozen source/tool/profile digest mismatch')
    config_raw = raw['config']
    config = decode_coverage_config(config_raw, limits=json_limits)
    if (config['output'] != str(receipt_root)
            or config['coverage_config'] != observations['profile']['path']
            or config['coverage_config_sha256'] != observations['profile']['sha256']):
        raise ValueError('actual config output/profile observation mismatch')
    starts, finishes = {}, {}
    for role in ROLES:
        for phase, rows in (('start', starts), ('finish', finishes)):
            label = role + '.' + phase
            rows[role] = decode_phase_record(
                raw[label], config_raw=config_raw, role=role, phase=phase,
                artifact_path=observations[label]['path'], limits=json_limits)
    # Same full run reducer as later union, no stripped fields/coerced metadata.
    reduced = coverage_inputs(config_raw, starts, finishes, actual_exit=actual_exit,
                              outer_seconds=outer_seconds, cohort=cohort)
    wanted = checked(cohort)
    for role in ROLES[1:]:
        if checked(finishes[role]['nodes_before']) != wanted or finishes[role]['deselected'] != []:
            raise ValueError('coverage not full declared B selection; retain omissions/PARTIAL')
    for role in ROLES:
        observed = observations[role + '.data']
        candidate = reduced['coverage_candidates'][role]
        if any(observed[k] != candidate[k] for k in ('path', 'bytes', 'sha256')):
            raise ValueError('actual native PID data differs from finish declaration')
    return {'config': config, 'starts': starts, 'finishes': finishes, 'receipt_logic': reduced,
            'source_process_or_closed_writer_authenticated': False}


def collect_decoded_originals(*, paths, receipt_root, limits, json_limits,
                             actual_exit, outer_seconds, cohort):
    if type(actual_exit) is not int:
        raise ValueError('exact raw outer pytest exit required')
    if actual_exit != 0:
        # No parsing/collection can replace or refresh the raw primary status.
        # Outer runner retains its existing logs/artifacts and failure status.
        return {'actual_exit': actual_exit, 'collector_started': False,
                'qualification': 'RAW_PYTEST_FAILURE_NO_USABLE_ORIGINALS_OR_INDEX',
                'source_process_or_closed_writer_authenticated': False}
    check_json_limits(json_limits)  # explicit decoder ceilings before file effects
    payload_labels = {'config'} | {r + '.' + p for r in ROLES for p in ('start', 'finish')}
    if (type(limits) is not dict or type(limits.get('file_bytes')) is not dict
            or any(type(limits['file_bytes'].get(k)) is not int
                   or not 0 < limits['file_bytes'][k] <= json_limits['bytes'] for k in payload_labels)):
        raise ValueError('ALL19 payload prefile bounds must fit decoder byte ceiling')
    originals = observe_originals(paths=paths, receipt_root=receipt_root, limits=limits)
    decoded = _decode_originals(originals, receipt_root=receipt_root, paths=paths,
                                limits=limits, json_limits=json_limits, actual_exit=actual_exit,
                                outer_seconds=outer_seconds, cohort=cohort)
    return {'originals': originals, 'decoded': decoded,
            'actual_exit': actual_exit, 'collector_started': True,
            'qualification': 'ACTUAL_ORIGINAL_BYTES_AND_RECEIPT_DATA_BINDING_NOT_INDEX_ACCEPTANCE',
            'source_process_or_closed_writer_authenticated': False}
