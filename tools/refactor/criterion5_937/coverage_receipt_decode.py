"""937 strict retained-JSON/type barrier SOURCE ONLY, UNEXECUTED and unwired.

No filesystem, index producer, full receipt reduction, native API or acceptance.
Future byte owner must derive these inputs from actual retained versions and
qualify aggregate limits/parse memory before decoding. Original bytes unchanged.
"""
import hashlib
import json
import math
from pathlib import PurePosixPath

from .coverage_copy_read import MAX_INPUT_BYTES, _same_capture
from .roster import checked
from .suite_evidence import B_PIN, COVERAGE_SCOPE, ROLES, event_key, number

CONFIG_FIELDS = {'arm', 'pin', 'run_id', 'mode', 'source_root', 'output', 'cache_prefix',
                 'expected_workers', 'cohort', 'cohort_sha256', 'coverage_version',
                 'coverage_config', 'coverage_config_sha256', 'coverage_package_root'}
START_FIELDS = {'spec', 'config_sha256', 'worker', 'local_numprocesses', 'pid', 'start_ticks',
                'boot', 'started_mono', 'nodes_before', 'nodes_selected', 'deselected',
                'worker_collections', 'errors', 'python', 'pytest', 'executable', 'affinity',
                'pycache_prefix', 'coverage'}
FINISH_FIELDS = START_FIELDS | {'events', 'subtests', 'coverage_data', 'primary_pytest_exit',
                                'qualified_exit', 'ended_mono', 'session_hook_elapsed_seconds',
                                'timing_scope', 'observer_events', 'nested_subtests'}
COVERAGE_FIELDS = {'config_sha256', 'version', 'module_path', 'native_core',
                   'native_tracer_file', 'scope'}
TIMING_SCOPE = 'session hooks only; outer pytest invocation/summary separately retained'


def decode_json(raw, *, limits):
    if (type(limits) is not dict or set(limits) != {'bytes', 'nodes', 'depth', 'string_characters'}
            or any(type(v) is not int or v <= 0 for v in limits.values())
            or limits['bytes'] > MAX_INPUT_BYTES):
        raise ValueError('explicit finite JSON limits required')
    if type(raw) is not bytes or not raw or len(raw) > limits['bytes']:
        raise ValueError('missing/oversized retained JSON bytes')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate retained JSON key')
            result[key] = value
        return result

    def bad_constant(token):
        raise ValueError('nonfinite retained JSON constant')

    def finite_float(token):
        result = float(token)
        if not math.isfinite(result):
            raise ValueError('overflowed nonfinite retained JSON number')
        return result

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_constant=bad_constant, parse_float=finite_float)
    nodes, characters = 0, 0

    def walk(row, depth):
        nonlocal nodes, characters
        nodes += 1
        if nodes > limits['nodes'] or depth > limits['depth']:
            raise ValueError('retained JSON structure bound; no partial input')
        if type(row) is str:
            characters += len(row)
            if '\x00' in row or characters > limits['string_characters']:
                raise ValueError('invalid/oversized retained JSON text')
        elif type(row) is dict:
            for key, item in row.items():
                if type(key) is not str:
                    raise ValueError('retained JSON key type changed')
                walk(key, depth + 1); walk(item, depth + 1)
        elif type(row) is list:
            for item in row:
                walk(item, depth + 1)
        elif type(row) is float:
            if not math.isfinite(row):
                raise ValueError('nonfinite retained JSON value')
        elif type(row) not in (int, bool, type(None)):
            raise ValueError('unsupported retained JSON primitive')
    walk(value, 1)
    return value  # post-parse bounds are NOT decoder memory/recursion fit proof


def _text(value):
    if type(value) is not str or not value or '\x00' in value:
        raise ValueError('nonempty exact string required')


def _digest(value):
    _text(value)
    if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('exact lowercase digest required')


def _absolute(value):
    _text(value)
    path = PurePosixPath(value)
    if '\\' in value or value.startswith('//') or not path.is_absolute() or str(path) != value or '..' in path.parts:
        raise ValueError('exact canonical absolute path required')
    return path


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('exact integer identity/count required; no bool coercion')


def _config(spec):
    if type(spec) is not dict or set(spec) != CONFIG_FIELDS:
        raise ValueError('exact emitted configuration fields required')
    if (spec['arm'] != 'B' or spec['pin'] != B_PIN or spec['mode'] != 'coverage'
            or spec['coverage_version'] != '7.17.0'):
        raise ValueError('wrong frozen coverage arm/pin/mode/tool')
    _text(spec['run_id']); _integer(spec['expected_workers'])
    if spec['expected_workers'] != 8:
        raise ValueError('wrong actual worker count')
    for key in ('source_root', 'output', 'cache_prefix', 'cohort', 'coverage_config', 'coverage_package_root'):
        _absolute(spec[key])
    for key in ('cohort_sha256', 'coverage_config_sha256'):
        _digest(spec[key])


def decode_coverage_config(raw, *, limits):
    value = decode_json(raw, limits=limits)
    _config(value)
    return value


def decode_phase_record(raw, *, config_raw, role, phase, artifact_path, limits):
    config = decode_coverage_config(config_raw, limits=limits)
    if (type(role) is not str or role not in ROLES
            or type(phase) is not str or phase not in ('start', 'finish')):
        raise ValueError('fixed role and phase required')
    if _absolute(artifact_path) != _absolute(config['output']) / (role + '.' + phase + '.json'):
        raise ValueError('receipt filename/output-root channel mismatch')
    row = decode_json(raw, limits=limits)
    expected = START_FIELDS if phase == 'start' else FINISH_FIELDS
    if type(row) is not dict or set(row) != expected:
        raise ValueError('exact native phase fields required')
    _config(row['spec'])
    if not _same_capture(row['spec'], config) or row['config_sha256'] != hashlib.sha256(config_raw).hexdigest():
        raise ValueError('typed embedded configuration/raw hash mismatch')
    _digest(row['config_sha256'])
    if row['worker'] != role:
        raise ValueError('raw receipt role mismatch')
    for key in ('pid', 'start_ticks'):
        _integer(row[key], 1)  # BOTH start and finish, before any pairing equality
    for key in ('boot', 'python', 'pytest'):
        _text(row[key])
    if row['pytest'] != '9.1.1':
        raise ValueError('wrong frozen pytest version')
    _absolute(row['executable']); _absolute(row['pycache_prefix'])
    if row['pycache_prefix'] != config['cache_prefix']:
        raise ValueError('receipt cache path mismatch')
    if role == 'controller':
        _integer(row['local_numprocesses'])
        if row['local_numprocesses'] != 8:
            raise ValueError('wrong controller worker configuration')
    elif row['local_numprocesses'] is not None:
        raise ValueError('worker local process setting must be native None')
    if type(row['affinity']) is not list or not row['affinity']:
        raise ValueError('nonempty actual affinity list required')
    for value in row['affinity']:
        _integer(value)
    if len(set(row['affinity'])) != len(row['affinity']):
        raise ValueError('duplicate affinity identity')
    if type(row['errors']) is not list or any(type(v) is not str for v in row['errors']):
        raise ValueError('raw error list type changed')
    for key in ('nodes_before', 'nodes_selected', 'deselected'):
        if type(row[key]) is not list:
            raise ValueError('raw node list type changed')
        if row[key]:
            checked(row[key])
    collections = row['worker_collections']
    if type(collections) is not dict or not set(collections).issubset(ROLES[1:]):
        raise ValueError('unexpected worker collection role')
    for nodes in collections.values():
        if type(nodes) is not list:
            raise ValueError('worker node collection type changed')
        if nodes:
            checked(nodes)
    meta = row['coverage']
    if (type(meta) is not dict or set(meta) != COVERAGE_FIELDS
            or meta['config_sha256'] != row['config_sha256'] or meta['version'] != '7.17.0'
            or meta['native_core'] != 'CTracer' or meta['scope'] != COVERAGE_SCOPE
            or _absolute(meta['module_path']) != _absolute(config['coverage_package_root']) / '__init__.py'
            or _absolute(meta['native_tracer_file']).parent != _absolute(config['coverage_package_root'])):
        raise ValueError('raw coverage metadata type/source/config mismatch')
    if phase == 'start':
        if row['started_mono'] is not None:
            raise ValueError('native start receipt precedes session clock')
    else:
        for key in ('primary_pytest_exit', 'qualified_exit', 'observer_events', 'nested_subtests'):
            _integer(row[key])
        for key in ('started_mono', 'session_hook_elapsed_seconds'):
            if row[key] is not None:
                number(row[key])
        number(row['ended_mono'])
        if row['timing_scope'] != TIMING_SCOPE:
            raise ValueError('unexpected native timing scope')
        for field in ('events', 'subtests'):
            if type(row[field]) is not list:
                raise ValueError('raw event collection type changed')
            for event in row[field]:
                if type(event) is not dict:
                    raise ValueError('raw event must be an exact dictionary')
                event_key(event)  # actual finite duration and exact bool contract
        data = row['coverage_data']
        if (type(data) is not dict or set(data) != {'file', 'sha256', 'bytes', 'scope'}
                or data['file'] != 'coverage-pid-' + str(row['pid']) + '.data'
                or data['scope'] != COVERAGE_SCOPE):
            raise ValueError('coverage data channel/type mismatch')
        _integer(data['bytes'], 1); _digest(data['sha256'])
    return row  # typed raw phase ONLY; full outcome/pair/producer validity external
