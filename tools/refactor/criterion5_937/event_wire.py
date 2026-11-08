"""Compact native evidence FIELD CONTRACT, SOURCE ONLY/UNEXECUTED.

No recorder/allocator interception, event-order inference, producer attestation,
measured bank allocation or capacity fit is implemented here. Actual source
profiles/consumer and numeric ALL-producer peaks remain required. Local IDs
below are ALWAYS qualified by the complete producer/physical-DB domain bytes.
"""
import json
from pathlib import PurePosixPath

from .packed_columns import WIDTH, MAX_BANK_STORAGE_BYTES
from .terminal_channel import SIZE as TERMINAL_BYTES

A_PIN = '4f33cdc9287eafcddd0adfb17b51487c84ace28b'
B_PIN = '0073254687d22ac5a6c1d01b8af29fa678dcf4a2'
VERSION = 1

# Each table is a separate Bank. None of its append indices is a native order.
# IDs refer to explicit local identity columns, not assumed bank row positions.
SCHEMAS = {
    'connections': (
        ('connection','u64'), ('stage','text'), ('native_call','optional_u64'),
        ('mono_lower','u64'), ('mono_upper','u64')),
    'operations': (
        ('operation','u64'), ('stage','text'), ('owner','text'), ('label','text'),
        ('source_role','text'), ('mono_lower','u64'), ('mono_upper','u64'),
        ('wall_lower','i64'), ('wall_upper','i64')),
    'native_calls': (
        ('native_call','u64'), ('connection','u64'), ('connection_sequence','u64'),
        ('epoch','optional_u64'), ('savepoints','ids'), ('source_role','text'),
        ('method','text'), ('outcome','text'), ('before','native_state'),
        ('after','native_state'), ('mono_lower','u64'), ('mono_upper','u64'),
        ('wall_lower','i64'), ('wall_upper','i64')),
    'transactions': (
        ('epoch','u64'), ('connection','u64'), ('stage','text'),
        ('native_call','u64'), ('outcome','text')),
    'savepoints': (
        ('savepoint','u64'), ('epoch','u64'), ('connection','u64'),
        ('parent_savepoint','optional_u64'), ('stage','text'), ('name','text'),
        ('native_call','u64')),
    'ranges': (
        ('range','u64'), ('connection','u64'), ('epoch','u64'), ('operation','u64'),
        ('owner','text'), ('source_role','text'), ('savepoints','ids'),
        ('first_attempt','u64'), ('first_revision','i64'), ('count','u64')),
    # Retain the actual per-attempt counter-statement identity. Ranges do NOT
    # erase this linkage to save bytes. Numeric rollback reuse has another attempt.
    'attempts': (
        ('attempt','u64'), ('range','u64'), ('offset','u64'),
        ('counter_call','u64'), ('allocator_role','text')),
    'key_edges': (
        ('edge','u64'), ('connection','u64'), ('epoch','u64'), ('operation','u64'),
        ('native_call','u64'), ('writer_role','text'), ('table','text'),
        ('key','key'), ('action','text'), ('attempt','optional_u64'),
        ('revision','optional_i64'), ('replace_revision','optional_i64'),
        ('savepoints','ids')),
    # Append-only resolution edges, NOT in-place edits of range/attempt rows.
    # [first_offset,stop_offset) permits a rollback cutting the middle of a run.
    'resolutions': (
        ('resolution','u64'), ('range','u64'), ('first_offset','u64'),
        ('stop_offset','u64'), ('outcome','text'), ('native_call','u64'),
        ('savepoint','optional_u64')),
}

STAGES = {
    'connections': frozenset(('BIRTH','CLOSE','EXPIRED','UNKNOWN')),
    'operations': frozenset(('ENTER','RETURN','ERROR','UNKNOWN')),
    'transactions': frozenset(('BEGIN','END')),
    'savepoints': frozenset(('BEGIN','RELEASE','ROLLBACK_TO')),
}
OUTCOMES = frozenset(('RETURNED','ERROR','COMMITTED','DISCARDED','UNKNOWN'))
ACTIONS = frozenset(('ASSIGN','RETAIN','DELETE','HEAD_MAX','HEAD_REPLACE'))


def typed_int(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value < (1 << 64):
        raise ValueError('typed unsigned identity/quantity')
    return value


def digest(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('exact lower-case SHA256 required')
    return value


def domain_bytes(value):
    """Lossless canonical declaration, NOT proof the named producer ran.

    Parent must bind these exact values to admitted source + actual PID/start/
    boot and physical DB stat/seed/schema receipts. Do not join by PID/path or
    digest alone. Retain BOTH raw declarations when comparing producer domains.
    """
    if type(value) is not dict or set(value) != {'attempt','producer','database','source'}:
        raise ValueError('producer domain fields')
    attempt, producer, database, source = (value[k] for k in ('attempt','producer','database','source'))
    if type(attempt) is not str or not attempt or len(attempt.encode('utf-8','surrogatepass')) > 128:
        raise ValueError('attempt identity bytes')
    if type(producer) is not dict or set(producer) != {'boot_id','pid','start_ticks','role','incarnation'}:
        raise ValueError('producer identity fields')
    if (type(producer['boot_id']) is not str or not producer['boot_id'] or len(producer['boot_id']) > 64
            or type(producer['role']) is not str or not producer['role'] or len(producer['role']) > 64):
        raise ValueError('producer boot/role')
    typed_int(producer['pid'],positive=True);typed_int(producer['start_ticks'],positive=True)
    typed_int(producer['incarnation'],positive=True)
    if type(database) is not dict or set(database) != {'path','device','inode','incarnation','seed_sha256','initial_schema_sha256','initial_counter'}:
        raise ValueError('physical DB identity fields')
    path = database['path']
    if (type(path) is not str or len(path.encode('utf-8','surrogatepass')) > 1024
            or '\x00' in path or not path.startswith('/') or path.startswith('//') or str(PurePosixPath(path)) != path
            or '..' in PurePosixPath(path).parts):
        raise ValueError('exact physical absolute DB path declaration')
    typed_int(database['device']);typed_int(database['inode'],positive=True)
    typed_int(database['incarnation'],positive=True)
    # SQLite counter storage is signed64, not a u64 evidence identity.
    if type(database['initial_counter']) is not int or not 0 <= database['initial_counter'] < (1 << 63):
        raise ValueError('initial SQLite counter range')
    digest(database['seed_sha256']);digest(database['initial_schema_sha256'])
    if type(source) is not dict or set(source) != {'pin','manifest_sha256','profile_sha256'} or source['pin'] not in (A_PIN,B_PIN):
        raise ValueError('exact per-pin source/profile declaration')
    digest(source['manifest_sha256']);digest(source['profile_sha256'])
    result = json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode('ascii')
    if len(result) > 8192:
        raise ValueError('domain byte cap')
    return result


def semantic_fields(table, record):
    """Local FIELD checks only; NOT transaction/cross-reference acceptance."""
    if table not in SCHEMAS or type(record) is not dict or set(record) != {name for name,_ in SCHEMAS[table]}:
        raise ValueError('exact event table/field contract')
    for column in ('stage','outcome','action'):
        if column not in record:
            continue
        allowed = STAGES.get(table) if column == 'stage' else OUTCOMES if column == 'outcome' else ACTIONS
        if allowed is None or type(record[column]) is not str or record[column] not in allowed:
            raise ValueError('unqualified event enum')
    if 'mono_lower' in record:
        typed_int(record['mono_lower']);typed_int(record['mono_upper'])
        if record['mono_lower'] > record['mono_upper']:
            raise ValueError('native-call monotonic bracket reversed')
    if 'wall_lower' in record and record['wall_lower'] > record['wall_upper']:
        raise ValueError('native wall-clock jump/refusal')
    if table == 'ranges':
        if (type(record['first_revision']) is not int or not 0 < record['first_revision'] < (1 << 63)
                or type(record['count']) is not int or record['count'] <= 0
                or record['first_revision'] + record['count'] - 1 >= (1 << 63)):
            raise ValueError('native signed64 allocation range')
        typed_int(record['first_attempt']);typed_int(record['count'],positive=True)
        if record['first_attempt'] + record['count'] > 1 << 64:
            raise ValueError('allocation attempt identity overflow')
    if table == 'resolutions':
        typed_int(record['first_offset']);typed_int(record['stop_offset'])
        if record['first_offset'] >= record['stop_offset'] or record['outcome'] not in ('COMMITTED','DISCARDED','UNKNOWN'):
            raise ValueError('nonempty resolution interval/outcome required')
    # The Bank independently checks every scalar type/width. The future replay
    # consumer must validate per-producer refs/membership/coverage and native
    # SQL predicates, including ASSIGN vs RETAIN/DELETE/head semantics.
    return record


def fixed_buffer_arithmetic(capacities):
    """Exact fixed buffers ONLY; never returns total-peak or corpus-fit True.

    Required input per table: rows, dictionary_entries, dictionary_bytes. Every
    actual row count needs a source-derived all-producer/schedule bound first.
    No default cap and no missing table silently treated as zero. Returning a
    number neither creates banks nor allocates/reserves a compute allowance.
    """
    if type(capacities) is not dict or set(capacities) != set(SCHEMAS):
        raise ValueError('complete explicit per-table capacities required')
    result = {}
    for table,schema in SCHEMAS.items():
        item = capacities[table]
        if type(item) is not dict or set(item) != {'rows','dictionary_entries','dictionary_bytes'}:
            raise ValueError('capacity fields')
        for number in item.values():
            if type(number) is not int or not 0 <= number < (1 << 32):
                raise ValueError('finite typed bank capacity')
        if item['rows'] == 0:
            raise ValueError('Bank requires positive reserved row capacity even for an empty used table')
        width = sum(WIDTH[kind] for _,kind in schema)
        result[table] = {'row_width':width,'column_bytes':width*item['rows'],
                         'dictionary_bytes':item['dictionary_bytes'],
                         'dictionary_offset_bytes':4*(item['dictionary_entries']+1),
                         'independent_refusal_bytes':TERMINAL_BYTES,
                         'exceeds_primitive_safety_ceiling':(width*item['rows']+item['dictionary_bytes']+4*(item['dictionary_entries']+1)+TERMINAL_BYTES>MAX_BANK_STORAGE_BYTES)}
    return {'tables':result,
            'fixed_buffer_bytes_per_producer':sum(sum(v[k] for k in ('column_bytes','dictionary_bytes','dictionary_offset_bytes','independent_refusal_bytes')) for v in result.values()),
            'additional_peak':('ALL producer multiplicities; Python key/lookup objects; live connection/epoch/range indices; '
                               'transient decode; export/hash buffers; consumer/merge; actual page touch and CPU/lock cost'),
            'source_cardinality_approved':False,'total_peak_established':False,'corpus_fit_established':False}
