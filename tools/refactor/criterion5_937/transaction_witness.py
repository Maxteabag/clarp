"""Bounded observational native-connection witness, SOURCE ONLY/UNEXECUTED.

No SQL is added, retried, fetched, closed or rewritten. No clocks are patched.
This is NOT the complete row-contract/alias/source/causal oracle. The owning
source-qualified adapter must provide native SQL/key/role decoders, all actual
allocator aliases and source/operation identities. Unknown paths refuse proof.
Connection references are WEAK: observing must not retain TLS page caches.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import functools
import re
import sys
import threading
import time
import weakref

MAX_CONNECTIONS = 512
MAX_RECORDS = 8192
MAX_SAVEPOINTS = 16
MAX_OPERATIONS = 64
METHODS = ('execute', 'executemany', 'executescript', 'commit', 'rollback', '__exit__', 'close', 'cursor')
COUNTER_SQL = 'UPDATE message_clock SET revision = revision + 1 WHERE singleton = 0 RETURNING revision'


def control(sql):
    """Finite native transaction grammar; NOT a general SQL interpreter."""
    if type(sql) is not str or len(sql) > 65536:
        return ('unsupported', '')
    text = ' '.join(sql.strip().removesuffix(';').split()).upper()
    if re.fullmatch(r'BEGIN(?: (?:DEFERRED|IMMEDIATE|EXCLUSIVE))?(?: TRANSACTION)?', text):
        return ('begin', '')
    if re.fullmatch(r'(?:COMMIT|END)(?: TRANSACTION)?', text):
        return ('commit', '')
    if re.fullmatch(r'ROLLBACK(?: TRANSACTION)?', text):
        return ('rollback', '')
    match = re.fullmatch(r'(SAVEPOINT|RELEASE(?: SAVEPOINT)?|ROLLBACK(?: TRANSACTION)? TO(?: SAVEPOINT)?) ([A-Z_][A-Z_0-9]{0,63})', text)
    if match:
        verb = match[1]
        return ('savepoint' if verb == 'SAVEPOINT' else 'release' if verb.startswith('RELEASE') else 'rollback_to', match[2])
    if text.split(' ', 1)[0] in ('BEGIN', 'COMMIT', 'END', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'):
        return ('unsupported', '')
    return ('ordinary', '')


@dataclass
class Connection:
    number: int
    reference: object
    epoch: int | None = None
    begin_id: int | None = None
    savepoints: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    pending_allocations: dict = field(default_factory=dict)
    unsupported: bool = False
    closed: bool = False
    call_active: bool = False


class Witness:
    def __init__(self, *, decode_write=None, caller_role=None):
        self.decode_write = decode_write
        self.caller_role = caller_role
        self.lock = threading.RLock()
        self.local = threading.local()
        self.connections = {}
        self.next_connection = 0
        self.next_epoch = 0
        self.next_savepoint = 0
        self.next_operation = 0
        self.records = []
        self.refusals = set()
        self.operations = []
        self.plan_allocations = 0

    def refuse(self, code):
        # Diagnostic failure must not replace/prevent an actual native call.
        with self.lock:
            if len(self.refusals) < 32:
                self.refusals.add(code)
            else:
                self.refusals.add('REFUSAL_CAP')

    def safe(self, function, *args):
        try:
            with self.lock:
                return function(*args)
        except BaseException as error:
            self.refuse('OBSERVER_' + type(error).__name__)
            return None

    def record(self, value):
        if len(self.records) >= MAX_RECORDS:
            self.refuse('RECORD_CAP')
            return None
        value = dict(value, id=len(self.records))
        self.records.append(value)
        return value

    def connection(self, native):
        key = id(native)
        state = self.connections.get(key)
        if state is not None and state.reference() is native:
            return state
        if state is not None and state.pending:
            self.finish(state, 'UNKNOWN', 'connection_incarnation_replaced')
            self.refuse('REPLACED_CONNECTION_WITH_PENDING_EVIDENCE')
        if self.next_connection >= MAX_CONNECTIONS:
            self.refuse('CONNECTION_CAP')
            return None
        self.next_connection += 1
        number = self.next_connection
        owner = weakref.ref(self)
        def expired(reference):
            witness = owner()
            if witness is not None:
                witness.safe(witness.expire, key, number)
        state = Connection(number, weakref.ref(native, expired))
        self.connections[key] = state
        return state

    def expire(self, key, number):
        state = self.connections.get(key)
        if state is None or state.number != number:
            return
        if state.pending:
            self.finish(state, 'UNKNOWN', 'implicit_destruction')
            self.refuse('IMPLICIT_PENDING_DESTRUCTION')
        del self.connections[key]

    def begin(self, state, origin, before, after):
        self.next_epoch += 1
        state.epoch = self.next_epoch
        event = self.record({'kind': 'transaction_begin', 'connection': state.number,
                             'epoch': state.epoch, 'origin': origin,
                             'native_before': before, 'native_after': after})
        state.begin_id = event['id'] if event is not None else None

    def finish(self, state, disposition, boundary, native=None):
        terminal = self.record({'kind': 'boundary', 'connection': state.number, 'epoch': state.epoch,
                                'outcome': disposition, 'boundary': boundary,
                                'native_before': native[0] if native is not None else None,
                                'native_after': native[1] if native is not None else None})
        for record in state.pending:
            record['outcome'] = disposition
            record['boundary_id'] = terminal['id'] if terminal is not None else None
        state.pending.clear()
        state.pending_allocations.clear()
        state.savepoints.clear()
        state.epoch = None
        state.begin_id = None

    @staticmethod
    def native_state(native):
        try:
            return bool(native.in_transaction)
        except Exception:
            return None

    def before(self, native, method):
        state = self.connection(native)
        if state is None:
            return None
        if state.call_active or state.closed:
            state.unsupported = True
            self.refuse('CONCURRENT_OR_CLOSED_CONNECTION')
        state.call_active = True
        before = self.native_state(native)
        if state.epoch is not None and before is not True:
            self.finish(state, 'UNKNOWN', 'unobserved_transaction_end_or_connection_loss')
            self.refuse('UNOBSERVED_TRANSACTION_END')
            state.unsupported = True
        elif state.epoch is None and before is True:
            self.refuse('UNOBSERVED_TRANSACTION_BEGIN')
            state.unsupported = True
        return (state, before, state.epoch, threading.get_ident())

    def after(self, native, method, args, kwargs, observation, result, error):
        if observation is None:
            self.refuse('MISSING_NATIVE_BEFORE')
            return
        state, before, epoch, thread = observation
        state.call_active = False
        after = self.native_state(native)
        if thread != threading.get_ident() or epoch != state.epoch:
            state.unsupported = True
            self.refuse('CONNECTION_EPOCH_INTERLEAVE')
        if error is not None:
            # Even false after a native acknowledgement exception is not commit.
            if state.pending:
                self.finish(state, 'UNKNOWN', 'native_exception_' + method)
                self.refuse('NATIVE_ACKNOWLEDGEMENT_UNKNOWN')
            elif before != after:
                self.finish(state, 'UNKNOWN', 'unwitnessed_exception_transition')
            state.unsupported = True
            return
        if method == 'close':
            if after is not None:
                self.refuse('CLOSE_NOT_OBSERVED')
            if state.pending:
                self.finish(state, 'DISCARDED', 'successful_native_close', (before, after))
            else:
                state.epoch = None
                state.begin_id = None
                state.savepoints.clear()
            state.closed = True
            return
        if before is None or after is None:
            self.refuse('TRANSACTION_STATE_UNKNOWN')
            state.unsupported = True
            return
        kind, name = ('ordinary', '')
        if method == 'execute':
            sql = args[0] if args else kwargs.get('sql')
            kind, name = control(sql)
        elif method in ('commit', 'rollback'):
            kind = method
        elif method == '__exit__':
            if len(args) != 3:
                kind = 'unsupported'
            else:
                kind = 'commit' if all(value is None for value in args) else 'rollback'
        elif method in ('cursor', 'executescript'):
            kind = 'unsupported'
        if kind == 'unsupported':
            state.unsupported = True
            self.refuse('UNSUPPORTED_NATIVE_PATH')
            if state.pending:
                self.finish(state, 'UNKNOWN', 'unsupported_' + method)
                self.refuse('UNSUPPORTED_PENDING_CLOSURE')
            return
        if kind == 'begin':
            if before or not after or state.epoch is not None:
                self.refuse('BEGIN_STATE_MISMATCH')
                state.unsupported = True
            else:
                self.begin(state, 'BEGIN', before, after)
        elif kind == 'savepoint':
            if not after or len(state.savepoints) >= MAX_SAVEPOINTS:
                self.refuse('SAVEPOINT_STATE_OR_CAP')
                state.unsupported = True
            else:
                if not before:
                    self.begin(state, 'SAVEPOINT', before, after)
                elif state.epoch is None:
                    self.refuse('UNOBSERVED_OUTER_TRANSACTION')
                    state.unsupported = True
                self.next_savepoint += 1
                state.savepoints.append((name, len(state.pending), self.next_savepoint))
                self.record({'kind': 'savepoint_begin', 'connection': state.number, 'epoch': state.epoch,
                             'savepoint_epoch': self.next_savepoint, 'name': name,
                             'native_before': before, 'native_after': after})
        elif kind in ('rollback_to', 'release'):
            matches = [i for i, (value, _, _) in enumerate(state.savepoints) if value == name]
            if not matches:
                self.refuse('UNOBSERVED_SAVEPOINT')
                state.unsupported = True
            else:
                index = matches[-1]
                if kind == 'rollback_to':
                    offset = state.savepoints[index][1]
                    marker = self.record({'kind': 'savepoint_boundary', 'connection': state.number,
                                          'epoch': state.epoch, 'savepoint_epoch': state.savepoints[index][2],
                                          'action': 'ROLLBACK_TO', 'discarded_count': len(state.pending) - offset,
                                          'native_before': before, 'native_after': after})
                    for record in state.pending[offset:]:
                        record['outcome'] = 'DISCARDED'
                        record['boundary_id'] = marker['id'] if marker is not None else None
                    del state.pending[offset:]
                    state.pending_allocations = {row['revision']: row for row in state.pending
                                                 if row['kind'] == 'allocation'}
                    del state.savepoints[index + 1:]
                    if not after:
                        self.refuse('ROLLBACK_TO_ENDED_OUTER')
                else:
                    self.record({'kind': 'savepoint_release', 'connection': state.number,
                                 'epoch': state.epoch, 'savepoint_epoch': state.savepoints[index][2],
                                 'native_before': before, 'native_after': after})
                    del state.savepoints[index:]
                    if not after:
                        self.finish(state, 'COMMITTED', 'native_top_level_release', (before, after))
        elif kind in ('commit', 'rollback'):
            if after:
                self.refuse('TRANSACTION_END_STILL_ACTIVE')
                state.unsupported = True
            elif state.epoch is not None:
                self.finish(state, 'COMMITTED' if kind == 'commit' else 'DISCARDED',
                            'native_' + method + '_' + kind, (before, after))
            elif state.pending:
                self.finish(state, 'UNKNOWN', 'missing_transaction_epoch')
                self.refuse('MISSING_TRANSACTION_EPOCH')
        elif before != after:
            state.unsupported = True
            if state.pending:
                self.finish(state, 'UNKNOWN', 'implicit_transaction_transition')
            self.refuse('IMPLICIT_TRANSACTION_TRANSITION')
        if method == 'execute':
            sql = args[0] if args else kwargs.get('sql')
            if type(sql) is str and ' '.join(sql.split()) == COUNTER_SQL:
                token = getattr(self.local, 'allocation', None)
                if token is None or token.get('native') is not None:
                    self.refuse('UNOBSERVED_OR_DUPLICATE_ALLOCATOR')
                else:
                    token['native'] = (state.number, state.epoch)
                    token['state'] = state
            if self.decode_write is not None:
                bindings = self.decode_write(sql, args[1:] if len(args) > 1 else (), kwargs, result)
                # Exact source-bound decoder must never consume a generator or
                # cursor. Bounded keyed bindings only; no text payload retained.
                if bindings is not None:
                    if type(bindings) is not list or len(bindings) > 64:
                        self.refuse('WRITE_BINDING_CAP_OR_TYPE')
                    else:
                        for binding in bindings:
                            self.keyed(state, binding)
        elif method == 'executemany' and self.decode_write is not None:
            # No pre-consuming or replaying native parameter iterators.
            sql = args[0] if args else kwargs.get('sql')
            if (type(sql) is not str or len(sql) > 65536
                    or re.search(r'\b(?:MESSAGES|MESSAGE_CLOCK|CONVERSATION_HEADS)\b', sql.upper())):
                self.refuse('EXECUTEMANY_KEY_COVERAGE_UNQUALIFIED')

    def keyed(self, state, binding):
        if state.epoch is None or state.unsupported:
            self.refuse('KEY_WITHOUT_KNOWN_TRANSACTION')
            return
        if (type(binding) is not dict or set(binding) != {'table', 'key', 'revision', 'role'}
                or binding['table'] not in ('messages', 'conversation_heads')
                or type(binding['key']) is not tuple or not 1 <= len(binding['key']) <= 3
                or any(type(value) is not str or len(value) > 512 for value in binding['key'])
                or type(binding['revision']) is not int or binding['revision'] < 0
                or type(binding['role']) is not str or len(binding['role']) > 128):
            self.refuse('UNQUALIFIED_KEYED_WRITE')
            return
        allocation = state.pending_allocations.get(binding['revision'])
        record = self.record({'kind': 'keyed_write', 'connection': state.number,
                              'epoch': state.epoch, 'outcome': 'PENDING',
                              'begin_id': state.begin_id,
                              'allocation_id': allocation['id'] if allocation is not None else None,
                              'savepoint_epochs': [value[2] for value in state.savepoints], **binding})
        if record is not None:
            state.pending.append(record)

    @contextmanager
    def operation(self, *, owner, label):
        previous = getattr(self.local, 'operation', None)
        with self.lock:
            if (previous is not None or self.next_operation >= MAX_OPERATIONS
                    or type(owner) is not str or not owner or len(owner) > 128
                    or type(label) is not str or not label or len(label) > 256):
                self.refuse('OPERATION_NESTING_OR_CAP')
                operation = None
            else:
                self.next_operation += 1
                operation = {'id': self.next_operation, 'owner': owner, 'label': label,
                             'outcome': 'INCOMPLETE', 'started_mono': time.monotonic(),
                             'started_wall_ns': time.time_ns()}
                self.operations.append(operation)
        self.local.operation = operation
        try:
            yield operation
        except BaseException:
            self.refuse('NATIVE_OPERATION_ERROR')
            if operation is not None:
                operation['outcome'] = 'ERROR'
            raise
        else:
            if operation is not None:
                operation['outcome'] = 'RETURNED'
        finally:
            if operation is not None:
                operation.update(ended_mono=time.monotonic(), ended_wall_ns=time.time_ns())
                if operation['ended_wall_ns'] < operation['started_wall_ns']:
                    self.refuse('NATIVE_WALL_CLOCK_JUMP')
            self.local.operation = previous

    def allocator(self, original, *, plan_class_getter=None):
        @functools.wraps(original)
        def call(*args, **kwargs):
            previous = getattr(self.local, 'allocation', None)
            token = {'native': None}
            # Bounded source-role lookup on the actual caller code/line, no
            # inspect.stack(), frame storage or per-row formatted stack.
            frame = sys._getframe(1)
            try:
                token['role'] = self.safe(self.caller_role, frame) if self.caller_role is not None else None
            finally:
                del frame
            if previous is not None:
                self.refuse('NESTED_ALLOCATION')
            self.local.allocation = token
            try:
                value = original(*args, **kwargs)  # exact native args/return/primary
            except BaseException:
                self.refuse('ALLOCATION_PRIMARY_EXCEPTION')
                raise
            else:
                self.safe(self.allocated, token, value, args, kwargs, plan_class_getter)
                return value
            finally:
                self.local.allocation = previous
        return call

    def allocated(self, token, value, args, kwargs, plan_class_getter):
        database = args[0] if len(args) == 1 else kwargs.get('database')
        if token['native'] is None:
            plan_class = plan_class_getter() if plan_class_getter is not None else None
            if plan_class is not None and type(database) is plan_class:
                if self.plan_allocations >= MAX_RECORDS:
                    self.refuse('PLAN_COUNT_CAP')
                else:
                    self.plan_allocations += 1
            else:
                self.refuse('ALLOCATION_WITHOUT_NATIVE_COUNTER_STATEMENT')
            return
        state = token['state']
        operation = getattr(self.local, 'operation', None)
        if (type(value) is not int or value <= 0 or state.unsupported or state.epoch is None
                or token['native'] != (state.number, state.epoch) or operation is None):
            self.refuse('ALLOCATION_CONNECTION_EPOCH_OWNER_UNKNOWN')
            return
        role = token['role']
        if type(role) is not str or not role or len(role) > 128:
            self.refuse('ALLOCATION_CALLER_ROLE_UNKNOWN')
            return
        if value in state.pending_allocations:
            self.refuse('DUPLICATE_PENDING_REVISION')
            return
        record = self.record({'kind': 'allocation', 'connection': state.number, 'epoch': state.epoch,
                              'begin_id': state.begin_id,
                              'operation': operation['id'], 'owner': operation['owner'],
                              'role': role, 'revision': value, 'outcome': 'PENDING',
                              'savepoint_epochs': [entry[2] for entry in state.savepoints]})
        if record is not None:
            state.pending.append(record)
            state.pending_allocations[value] = record

    @contextmanager
    def observe_class(self, native_class):
        """Python native subclass only; actual native cursor/return preserved."""
        absent = object()
        attributes = {name: native_class.__dict__.get(name, absent) for name in METHODS}
        originals = {name: getattr(native_class, name) for name in METHODS}
        installed = []
        try:
            for name, original in originals.items():
                def make(method, function):
                    @functools.wraps(function)
                    def wrapped(native, *args, **kwargs):
                        before = self.safe(self.before, native, method)
                        try:
                            result = function(native, *args, **kwargs)
                        except BaseException as primary:
                            self.safe(self.after, native, method, args, kwargs, before, None, primary)
                            raise
                        else:
                            self.safe(self.after, native, method, args, kwargs, before, result, None)
                            return result
                    return wrapped
                setattr(native_class, name, make(name, original))
                installed.append(name)
            yield self
        finally:
            for name in reversed(installed):
                value = attributes[name]
                try:
                    if value is absent:
                        delattr(native_class, name)
                    else:
                        setattr(native_class, name, value)
                except BaseException:
                    self.refuse('CLASS_BINDING_RESTORE_UNKNOWN')

    def receipt(self):
        with self.lock:
            records = [dict(row) for row in self.records]
            for row in records:
                if 'savepoint_epochs' in row:
                    row['savepoint_epochs'] = list(row['savepoint_epochs'])
            return {'records': records,
                    'operations': [dict(row) for row in self.operations],
                    'connections_seen': self.next_connection, 'plan_allocations': self.plan_allocations,
                    'refusals': sorted(self.refusals),
                    'pending_records': sum(len(state.pending) for state in self.connections.values()),
                    'pending_native_calls': sum(state.call_active for state in self.connections.values()),
                    'scope': 'observed native boundaries ONLY; not keyed final-row/reference/clock validation'}


def causal_key_signatures(receipt, rows, seed_rows, *, counter_before, counter_after):
    """Controller-side keyed REVISION sub-oracle, not a whole-product oracle.

    `rows`/`seed_rows` are exact raw-derived {(table, tuple(PK)): {owner,revision}}
    maps. The complete adapter must also validate every payload/source/trace/
    timestamp/replace-head field, writer eligibility, all producers, and hashes.
    There is no automatic field removal or acceptance of changed row semantics.
    """
    if (receipt['refusals'] or receipt['pending_records'] or receipt['pending_native_calls']
            or any(row['outcome'] != 'RETURNED' for row in receipt['operations'])):
        raise ValueError('incomplete/unknown native transaction witness')
    if (type(counter_before) is not int or type(counter_after) is not int
            or counter_before < 0 or counter_after < counter_before
            or counter_after - counter_before > MAX_RECORDS):
        raise ValueError('unqualified global counter interval')
    records = receipt['records']
    if (len(records) > MAX_RECORDS or any(type(row['id']) is not int for row in records)
            or [row['id'] for row in records] != list(range(len(records)))):
        raise ValueError('missing/duplicate/reordered witness record')
    operations = {row['id']: row for row in receipt['operations']}
    if len(operations) != len(receipt['operations']):
        raise ValueError('duplicate native operation')
    allocations, bindings = {}, {}
    positive = {'native_execute_commit', 'native_commit_commit', 'native___exit___commit',
                'native_top_level_release'}
    for record in records:
        if record['kind'] not in ('allocation', 'keyed_write'):
            continue
        if (type(record['revision']) is not int or type(record['connection']) is not int
                or type(record['epoch']) is not int or record['connection'] <= 0 or record['epoch'] <= 0):
            raise ValueError('invalid typed allocation/connection/epoch')
        begin_id = record.get('begin_id')
        if type(begin_id) is not int or not 0 <= begin_id < record['id']:
            raise ValueError('missing native BEGIN witness')
        begin = records[begin_id]
        if (begin['kind'] != 'transaction_begin' or begin['native_before'] is not False
                or begin['native_after'] is not True
                or any(begin[name] != record[name] for name in ('connection', 'epoch'))):
            raise ValueError('wrong native transaction incarnation')
        outcome = record['outcome']
        if outcome not in ('COMMITTED', 'DISCARDED'):
            raise ValueError('unknown allocation/key durability')
        boundary_id = record.get('boundary_id')
        if type(boundary_id) is not int or not record['id'] < boundary_id < len(records):
            raise ValueError('missing or reordered native ending receipt')
        boundary = records[boundary_id]
        if any(boundary[name] != record[name] for name in ('connection', 'epoch')):
            raise ValueError('wrong connection/transaction ending receipt')
        if outcome == 'COMMITTED':
            if (boundary['kind'] != 'boundary' or boundary['outcome'] != 'COMMITTED'
                    or boundary['boundary'] not in positive
                    or boundary['native_before'] is not True or boundary['native_after'] is not False):
                raise ValueError('no positive native commit acknowledgement')
        elif boundary['kind'] == 'savepoint_boundary':
            if (boundary['action'] != 'ROLLBACK_TO'
                    or boundary['savepoint_epoch'] not in record['savepoint_epochs']
                    or boundary['native_before'] is not True or boundary['native_after'] is not True):
                raise ValueError('discard not bound to enclosing savepoint')
        elif boundary['kind'] != 'boundary' or boundary['outcome'] != 'DISCARDED':
            raise ValueError('discard not positively witnessed')
        elif boundary['native_before'] is not True or boundary['native_after'] not in (False, None):
            raise ValueError('discard state unqualified')
        if outcome == 'DISCARDED':
            continue
        if record['kind'] == 'allocation':
            value = record['revision']
            if type(value) is not int or not counter_before < value <= counter_after or value in allocations:
                raise ValueError('duplicate/reused/foreign COMMITTED revision')
            operation = operations.get(record['operation'])
            if operation is None or operation['owner'] != record['owner']:
                raise ValueError('allocation operation/owner not bound')
            allocations[value] = record
        else:
            key = (record['table'], tuple(record['key']))
            bindings.setdefault(key, []).append(record)
    if sorted(allocations) != list(range(counter_before + 1, counter_after + 1)):
        raise ValueError('global clock domain incomplete; external/plan-only increment')
    # Native COMMITTED values order allocations across connections within this
    # complete captured DB domain. Never use callback-arrival/commit-log order.
    ordinals, signatures = {}, {}
    for value, allocation in sorted(allocations.items()):
        operation = operations[allocation['operation']]
        group = (allocation['owner'], operation['label'], allocation['role'])
        ordinals[group] = ordinals.get(group, 0) + 1
        signatures[allocation['id']] = (*group, ordinals[group])
    result = {}
    for key, row in rows.items():
        value = row['revision']
        prior = seed_rows.get(key)
        candidates = bindings.get(key, [])
        if type(value) is not int or type(row['owner']) is not str:
            raise ValueError('raw keyed revision/owner invalid')
        highest = max([prior['revision']] if prior is not None else [-1])
        if candidates:
            highest = max(highest, *(entry['revision'] for entry in candidates))
        if value != highest:
            raise ValueError('final keyed row/head differs from witnessed native writes')
        if prior is not None and prior == row:
            result[key] = {'raw_revision': value, 'correspondence': ('exact_seed_key', value, row['owner'])}
            continue
        allocation = allocations.get(value)
        if allocation is None or allocation['owner'] != row['owner']:
            raise ValueError('new keyed product lacks own committed allocation')
        valid = []
        for binding in candidates:
            if binding['revision'] != value or binding['allocation_id'] != allocation['id']:
                continue
            if any(binding[name] != allocation[name] for name in ('connection', 'epoch', 'boundary_id')):
                raise ValueError('row assignment not in allocation transaction')
            valid.append(binding)
        if not valid:
            raise ValueError('same-role row swap/unbound assignment')
        result[key] = {'raw_revision': value, 'correspondence': signatures[allocation['id']]}
    return {'keys': result, 'committed_allocations': len(allocations),
            'scope': 'keyed revision correspondence only; full raw-field/source/owner/clock/head rules separately required'}
