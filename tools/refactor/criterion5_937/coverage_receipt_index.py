"""937 pure receipt-index/declaration preflight SOURCE ONLY, UNEXECUTED.

No file read/index producer/native API or authentication. Future byte owner
must apply the complete preflight before ANY binding read, then verify actual
bytes/versions and pass only strict read-derived records to the union.
"""
import hashlib

from .coverage_copy_read import MAX_INPUT_BYTES, _same_capture
from .coverage_retained_bytes import check_observation
from .coverage_receipt_decode import decode_json, decode_coverage_config, _absolute, _text
from .coverage_union_graph import SOURCE_CATALOG_SHA256, COVERAGE_ARCHIVE_SHA256
from .suite_evidence import B_PIN, ROLES

FORMAT = 'criterion5-937-run-receipt-index-v1'
BINDINGS = {'catalog', 'archive', 'config', 'index', 'profile'}
PHASES = ('start', 'finish')
OUTPUTS = {'aggregate', 'verify', 'graph', 'report'}
PROFILE_SHA256 = '3cc8a9f20b04a5bd3e287364480ddfc039e996954da359540f1fe24472cf7de7'


def preflight_inputs(*, bindings, receipts, data_observations, receipt_root, output_roots, limits):
    keys = {'artifact_bytes', 'total_artifact_bytes', 'path_bytes', 'data_file_bytes', 'total_data_bytes'}
    if type(limits) is not dict or set(limits) != keys:
        raise ValueError('all retained-input limits must be frozen')
    for key in keys - {'artifact_bytes'}:
        if type(limits[key]) is not int or limits[key] <= 0:
            raise ValueError('exact positive aggregate/path/data limits required')
    names = BINDINGS | {r + '.' + phase for r in ROLES for phase in PHASES}
    if (type(limits['artifact_bytes']) is not dict or set(limits['artifact_bytes']) != names
            or type(bindings) is not dict or set(bindings) != BINDINGS
            or type(receipts) is not dict or set(receipts) != set(ROLES)
            or type(data_observations) is not dict or set(data_observations) != set(ROLES)
            or type(output_roots) is not dict or set(output_roots) != OUTPUTS):
        raise ValueError('exact23 artifacts/18 protected data/four outputs required')
    if not 0 < limits['data_file_bytes'] <= MAX_INPUT_BYTES:
        raise ValueError('data refusal ceiling cannot be expanded')
    root = _absolute(receipt_root)
    outputs = [_absolute(output_roots[name]) for name in sorted(OUTPUTS)]
    for i, path in enumerate(outputs):
        if any(path == other or path.is_relative_to(other) or other.is_relative_to(path)
               for other in outputs[:i]):
            raise ValueError('output custody roots overlap')
    paths, identities, artifact_total, data_total = set(), set(), 0, 0

    def observation(row, byte_limit):
        path = check_observation(row, byte_limit=byte_limit, path_bytes_limit=limits['path_bytes'])
        name, identity = str(path), (row['dev'], row['ino'])
        if name in paths or identity in identities:
            raise ValueError('retained binding/record/protected data path or inode alias')
        paths.add(name); identities.add(identity)
        parent = _absolute(str(path.parent))
        if any(parent == out or parent.is_relative_to(out) or out.is_relative_to(parent)
               for out in outputs):
            raise ValueError('retained input custody overlaps an output')
        return _absolute(name)

    for name in sorted(BINDINGS):
        row = bindings[name]
        observation(row, limits['artifact_bytes'][name])
        artifact_total += row['bytes']
    for role in ROLES:
        if type(receipts[role]) is not dict or set(receipts[role]) != set(PHASES):
            raise ValueError('exact start/finish observations required for every role')
        for phase in PHASES:
            row = receipts[role][phase]
            path = observation(row, limits['artifact_bytes'][role + '.' + phase])
            if path != root / (role + '.' + phase + '.json'):
                raise ValueError('raw receipt role/phase/output-root channel mismatch')
            artifact_total += row['bytes']
    for role in ROLES:
        rows = data_observations[role]
        if type(rows) is not dict or set(rows) != {'original_observation', 'copy_observation'}:
            raise ValueError('exact original/copy protected observations required')
        for kind in ('original_observation', 'copy_observation'):
            row = rows[kind]
            observation(row, limits['data_file_bytes'])
            data_total += row['bytes']
    if artifact_total > limits['total_artifact_bytes'] or data_total > limits['total_data_bytes']:
        raise ValueError('declared aggregate byte bound; no omitted inputs')
    if (bindings['catalog']['sha256'] != SOURCE_CATALOG_SHA256
            or bindings['archive']['sha256'] != COVERAGE_ARCHIVE_SHA256
            or bindings['profile']['sha256'] != PROFILE_SHA256):
        raise ValueError('frozen catalog/archive/profile declaration mismatch')
    return {'declared_artifact_count': 23, 'declared_protected_data_count': 18,
            'declared_artifact_bytes': artifact_total, 'declared_data_bytes': data_total,
            'qualification': 'PURE_DECLARATION_PREFLIGHT_ONLY_NOT_ACTUAL_BYTES_OR_PROVENANCE',
            'actual_source_run_or_byte_authentication_accepted': False}


def decode_receipt_index(raw, *, config_raw, bindings, receipts, data_observations,
                         receipt_root, output_roots, limits, json_limits):
    preflight_inputs(bindings=bindings, receipts=receipts, data_observations=data_observations,
                     receipt_root=receipt_root, output_roots=output_roots, limits=limits)
    for data, key in ((raw, 'index'), (config_raw, 'config')):
        if (type(data) is not bytes or len(data) != bindings[key]['bytes']
                or hashlib.sha256(data).hexdigest() != bindings[key]['sha256']):
            raise ValueError('supplied raw index/config bytes differ from observations')
    config = decode_coverage_config(config_raw, limits=json_limits)
    index = decode_json(raw, limits=json_limits)
    if type(index) is not dict or set(index) != {'format', 'pin', 'run_id', 'config_sha256', 'roles'}:
        raise ValueError('exact retained receipt-index format required')
    _text(index['run_id'])
    if (index['format'] != FORMAT or index['pin'] != B_PIN or index['run_id'] != config['run_id']
            or index['config_sha256'] != bindings['config']['sha256']
            or config['output'] != str(_absolute(receipt_root))
            or config['coverage_config'] != bindings['profile']['path']
            or config['coverage_config_sha256'] != bindings['profile']['sha256']):
        raise ValueError('index/config/run/output/profile linkage mismatch')
    if not _same_capture(index['roles'], receipts):
        raise ValueError('index does not bind every exact typed raw role/phase observation')
    return index  # raw typed declarations, NOT original-file or producer proof
