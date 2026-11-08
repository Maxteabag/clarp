"""ONE bounded remote native-component control, authored NOT EXECUTED.

This is a finite measurement-tool control, not a product gate or performance
run. No servers/providers. Parent must admit it inside the inclusive setup cap
and bind this script, helpers, actual native source/dependencies and cache roots.
"""
import argparse
from copy import deepcopy
import gc
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import weakref


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def component_controls(owned):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY')
    source, work = Path(args.source).resolve(strict=True), Path(args.workspace).resolve(strict=True)
    if Path.home() != work / 'home' or Path(os.environ['CLAUDE_PWA_DB']) != work / 'state.sqlite':
        raise RuntimeError('private native-component environment not supplied')
    os.chdir(work)
    helpers = Path(__file__).resolve().parent
    guard_module = load(helpers / 'native_isolation.py', '_c5_native_control_guard')
    usage = 'server/lib/backend_usage_codex.py'
    if not (source / usage).exists():
        usage = 'server/lib/backend_usage.py'
    guard = guard_module.Guard(source=source, usage_file=usage,
        usage_sha256=hashlib.sha256((source / usage).read_bytes()).hexdigest(),
        failure_file=work / 'guard-failure.jsonl', unix_paths=(), tcp_endpoints=(),
        search_dirs=('/usr/bin', '/bin'), expected_cwd=work)
    guard.install()
    witness_module = load(helpers / 'transaction_witness.py', '_c5_transaction_control')
    sys.path.insert(0, str(source / 'server'))
    db = importlib.import_module('lib.db')
    writes = importlib.import_module('lib.message_writes')
    allocator_module = importlib.import_module('lib.message_import_common') if (
        source / 'server/lib/message_import_common.py').exists() else writes
    native_allocator = allocator_module._next_revision
    Native = db._TrackedConnection
    clock = importlib.import_module('lib.clock')
    clock_before, db_clock_before = clock.now_ms, db.now_ms
    cases = []
    insert = 'INSERT INTO messages(message_id,agent_id,revision,seq) VALUES(?,?,?,?)'
    retained = insert + ' ON CONFLICT(message_id) DO UPDATE SET seq=excluded.seq,revision=excluded.revision'

    def connect(name, cls=Native):
        path = work / (name + '.sqlite')
        fresh = not path.exists()
        con = sqlite3.connect(path, isolation_level=None, timeout=1, factory=cls)
        owned.add(con)
        con.row_factory = sqlite3.Row
        if fresh:
            con.execute('CREATE TABLE message_clock(singleton INTEGER PRIMARY KEY,revision INTEGER)')
            con.execute('INSERT INTO message_clock VALUES(0,0)')
            con.execute('CREATE TABLE messages(message_id TEXT PRIMARY KEY,agent_id TEXT,revision INTEGER,seq INTEGER)')
            con.execute('CREATE TABLE auxiliary(value INTEGER)')
        return con

    def decode(sql, positional, keywords, cursor):
        if sql not in (insert, retained):
            return None
        assert len(positional) == 1 and not keywords
        params = positional[0]
        assert type(params) in (tuple, list) and len(params) == 4 and cursor.rowcount == 1
        return [{'table': 'messages', 'key': (params[0],), 'revision': params[2],
                 'role': 'controlled_keyed_native_insert'}]

    def probe():
        value = witness_module.Witness(decode_write=decode, caller_role=lambda frame: 'controlled_counter')
        native_call = value.allocator(native_allocator, plan_class_getter=lambda: writes._PlanWriter)
        def allocate(*args, **kwargs):
            # Unit owner context, NOT a manufactured real importer-span proof.
            # Explicit larger-operation controls keep their own native scope.
            if getattr(value.local, 'operation', None) is not None:
                return native_call(*args, **kwargs)
            with value.operation(owner='A', label='v1'):
                return native_call(*args, **kwargs)
        return value, allocate

    def actual(con):
        rows = {('messages', (row['message_id'],)): {'owner': row['agent_id'], 'revision': row['revision']}
                for row in con.execute('SELECT * FROM messages')}
        counter = con.execute('SELECT revision FROM message_clock WHERE singleton=0').fetchone()[0]
        return rows, counter

    def validate(value, con, seed=None, before=0):
        rows, counter = actual(con)
        return witness_module.causal_key_signatures(value.receipt(), rows, seed or {},
                                                   counter_before=before, counter_after=counter)

    def refused(function):
        try:
            function()
        except ValueError:
            return
        raise AssertionError('intended incomplete/causal negative was accepted')

    def emit(name, value, con, expected):
        rows, counter = actual(con)
        cases.append({'case': name, 'expected': expected, 'receipt': value.receipt(),
                      'actual_counter': counter,
                      'actual_rows': [{'table': table, 'key': list(key), **row} for (table, key), row in rows.items()]})

    # Nested RELEASE cannot certify allocations later rolled back by the outer.
    con = connect('nested')
    value, allocate = probe()
    with value.observe_class(Native):
        con.execute('BEGIN'); con.execute('SAVEPOINT nested')
        revision = allocate(con); con.execute(insert, ('one', 'A', revision, 0))
        con.execute('RELEASE nested')
        assert value.receipt()['pending_records'] == 2
        assert not any(row.get('outcome') == 'COMMITTED' for row in value.receipt()['records'])
        con.execute('ROLLBACK')
        validate(value, con)
        assert [row['outcome'] for row in value.receipt()['records'] if row['kind'] == 'allocation'] == ['DISCARDED']
        emit('nested_release_outer_rollback', value, con, 'DISCARDED_NOT_COMMITTED')
        con.close()

    # A discarded numerical revision may be reused, with a different attempt id.
    con = connect('reuse'); value, allocate = probe()
    with value.observe_class(Native):
        con.execute('BEGIN')
        first = allocate(con); con.execute(insert, ('first', 'A', first, 0))
        con.execute('SAVEPOINT suffix')
        lost = allocate(con); con.execute(insert, ('lost', 'A', lost, 1))
        con.execute('ROLLBACK TO suffix'); con.execute('RELEASE suffix')
        reused = allocate(con); con.execute(insert, ('final', 'A', reused, 1))
        assert lost == reused
        con.execute('END')
        validate(value, con)
        allocations = [row for row in value.receipt()['records'] if row['kind'] == 'allocation']
        assert [row['outcome'] for row in allocations] == ['COMMITTED', 'DISCARDED', 'COMMITTED']
        assert allocations[1]['id'] != allocations[2]['id']
        emit('rollback_to_numeric_reuse', value, con, 'DISTINCT_DISCARDED_AND_COMMITTED_ATTEMPTS')
        con.close()

    # Native C context closure bypasses Python commit/rollback methods.
    for rollback in (False, True):
        con = connect('context-' + str(rollback)); value, allocate = probe()
        with value.observe_class(Native):
            con.execute('BEGIN')
            try:
                with con:
                    revision = allocate(con); con.execute(insert, ('one', 'A', revision, 0))
                    if rollback:
                        raise LookupError('owned context rollback control')
            except LookupError:
                assert rollback
            validate(value, con)
            allocation = next(row for row in value.receipt()['records'] if row['kind'] == 'allocation')
            assert allocation['outcome'] == ('DISCARDED' if rollback else 'COMMITTED')
            emit('native_c_context_' + str(rollback), value, con, allocation['outcome'])
            con.close()

    con = connect('release'); value, allocate = probe()
    with value.observe_class(Native):
        con.execute('SAVEPOINT root')
        revision = allocate(con); con.execute(insert, ('one', 'A', revision, 0))
        con.execute('RELEASE root')
        validate(value, con)
        emit('top_level_release', value, con, 'POSITIVE_NATIVE_COMMIT')
        con.close()

    # Preserve a real earlier commit; later importer failure is NOT all-durable.
    con = connect('later-error'); value, allocate = probe()
    with value.observe_class(Native):
        try:
            with value.operation(owner='A', label='v1'):
                con.execute('BEGIN')
                r = allocate(con); con.execute(insert, ('first', 'A', r, 0)); con.commit()
                con.execute('BEGIN')
                r = allocate(con); con.execute(insert, ('later', 'A', r, 1))
                raise ArithmeticError('owned later batch failure')
        except ArithmeticError:
            con.rollback()
        outcomes = [row['outcome'] for row in value.receipt()['records'] if row['kind'] == 'allocation']
        assert outcomes == ['COMMITTED', 'DISCARDED']
        assert actual(con)[1] == 1
        refused(lambda: validate(value, con))
        emit('committed_then_failed_batch', value, con, 'PARTIAL_PRIOR_COMMIT_PRESERVED')
        con.close()

    class AckLost(Native):
        def commit(self):
            super().commit()
            raise RuntimeError('ACK_LOST_CONTROL')
    con = connect('ack-lost', AckLost); value, allocate = probe()
    with value.observe_class(AckLost):
        con.execute('BEGIN'); r = allocate(con); con.execute(insert, ('one', 'A', r, 0))
        try:
            con.commit()
        except RuntimeError as error:
            assert str(error) == 'ACK_LOST_CONTROL'
        else:
            raise AssertionError('native primary acknowledgement exception lost')
        assert actual(con)[1] == 1  # native DB committed; observer MUST refuse proof
        assert next(row for row in value.receipt()['records'] if row['kind'] == 'allocation')['outcome'] == 'UNKNOWN'
        refused(lambda: validate(value, con))
        emit('commit_acknowledgement_exception', value, con, 'UNKNOWN_NOT_INFERRED_COMMIT')
        con.close()

    # An actual cursor can end a transaction below the watched Python methods.
    con = connect('missing-ack'); value, allocate = probe()
    with value.observe_class(Native):
        cursor = con.execute('SELECT 1')
        con.execute('BEGIN'); r = allocate(con); con.execute(insert, ('one', 'A', r, 0))
        cursor.execute('COMMIT')
        assert value.receipt()['pending_records'] > 0
        refused(lambda: validate(value, con))  # next native observation sees lost epoch
        emit('cursor_commit_bypass', value, con, 'REFUSED_UNOBSERVED_NATIVE_END')
        cursor.close(); con.close()

    # Plan returns are observed separately, never counted as durable allocations.
    con = connect('plan'); value, allocate = probe()
    with value.observe_class(Native):
        plan = writes._PlanWriter(con)
        assert allocate(plan) == 1
        con.rollback()
        assert actual(con)[1] == 0 and value.receipt()['plan_allocations'] == 1
        assert not any(row['kind'] == 'allocation' for row in value.receipt()['records'])
        external = sqlite3.connect(work / 'plan.sqlite', isolation_level=None)
        external.execute('UPDATE message_clock SET revision=1 WHERE singleton=0')
        external.execute(insert, ('unobserved', 'A', 1, 0)); external.close()
        refused(lambda: validate(value, con))
        emit('plan_only_and_external_increment', value, con, 'REFUSED_NOT_DURABLE_PLAN')
        con.close()

    # Exact keyed reference: same-role swaps fail; real cross-owner interleave may
    # differ in raw global values ONLY with complete keyed native correspondence.
    def ordered(name, items):
        con = connect(name); value, allocate = probe()
        with value.observe_class(Native):
            for owner, key in items:
                with value.operation(owner=owner, label='immutable-v1'):
                    con.execute('BEGIN'); r = allocate(con)
                    con.execute(insert, (key, owner, r, 0)); con.commit()
            output = validate(value, con)
            emit(name, value, con, 'COMPLETE_KEYED_TRACE')
            receipt = value.receipt()
            rows, count = actual(con)
            con.close()
        return output, receipt, rows, count
    ref, receipt, rows, count = ordered('row-reference', [('A', 'first'), ('A', 'second')])
    swapped, _, _, _ = ordered('row-swap', [('A', 'second'), ('A', 'first')])
    sig = lambda result: {key: row['correspondence'] for key, row in result['keys'].items()}
    assert sig(ref) != sig(swapped), 'same-role row revision swap was hidden'
    for fault in ('missing', 'duplicate', 'wrong-begin', 'wrong-head'):
        broken, raw = deepcopy(receipt), deepcopy(rows)
        if fault == 'missing':
            broken['records'].pop()
        elif fault == 'duplicate':
            broken['records'].append(deepcopy(broken['records'][-1]))
        elif fault == 'wrong-begin':
            next(row for row in broken['records'] if row['kind'] == 'allocation')['begin_id'] = 999
        else:
            raw[('conversation_heads', ('A', 'bs-A'))] = {'owner': 'A', 'revision': count}
        refused(lambda: witness_module.causal_key_signatures(broken, raw, {}, counter_before=0, counter_after=count))
    a, _, _, _ = ordered('interleave-ref', [('A', 'first'), ('B', 'second')])
    b, _, _, _ = ordered('interleave-actual', [('B', 'second'), ('A', 'first')])
    assert sig(a) == sig(b)

    con = connect('retain'); con.execute('UPDATE message_clock SET revision=5')
    con.execute(insert, ('seed', 'A', 5, 0)); value, _ = probe()
    seed = actual(con)[0]
    with value.observe_class(Native):
        con.execute('BEGIN'); con.execute(retained, ('seed', 'A', 5, 9)); con.commit()
        assert con.execute('SELECT seq FROM messages').fetchone()[0] == 9
        validate(value, con, seed, 5)
        assert not any(row['kind'] == 'allocation' for row in value.receipt()['records'])
        emit('source_sequence_only_revision_retained', value, con, 'VALID_RETENTION_NOT_NEW_ALLOCATION')
        con.close()

    # Scripts/cursor factories are explicitly unsupported, never silently green.
    for mode in ('script', 'cursor', 'implicit'):
        con = connect('unsupported-' + mode); value, allocate = probe()
        with value.observe_class(Native):
            if mode == 'cursor':
                cursor = con.cursor(); cursor.close()
            elif mode == 'implicit':
                con.isolation_level = 'DEFERRED'
            if mode != 'implicit':
                con.execute('BEGIN')
            r = allocate(con); con.execute(insert, ('one', 'A', r, 0))
            if mode == 'script':
                con.executescript('SELECT 1;')
            else:
                con.commit()
            refused(lambda: validate(value, con))
            emit('unsupported-' + mode, value, con, 'REFUSED_PATH')
            con.close()

    # Successful actual close rolls back, whereas implicit destruction with a
    # pending transaction remains UNKNOWN. Neither can certify a commit.
    for implicit in (False, True):
        con = connect('close-' + str(implicit)); value, allocate = probe()
        with value.observe_class(Native):
            con.execute('BEGIN'); r = allocate(con)
            con.execute(insert, ('one', 'A', r, 0))
            if implicit:
                reference = weakref.ref(con)
                del con
                gc.collect()
                assert reference() is None
                assert 'IMPLICIT_PENDING_DESTRUCTION' in value.receipt()['refusals']
                allocation = next(row for row in value.receipt()['records'] if row['kind'] == 'allocation')
                assert allocation['outcome'] == 'UNKNOWN'
            else:
                con.close()
                allocation = next(row for row in value.receipt()['records'] if row['kind'] == 'allocation')
                assert allocation['outcome'] == 'DISCARDED'
            receipt = value.receipt()
        # Independent reopened native DB, not observer output, proves rollback.
        con = connect('close-' + str(implicit))
        assert actual(con) == ({}, 0)
        if implicit:
            refused(lambda: witness_module.causal_key_signatures(receipt, {}, {}, counter_before=0, counter_after=0))
        else:
            witness_module.causal_key_signatures(receipt, {}, {}, counter_before=0, counter_after=0)
        cases.append({'case': 'implicit_pending_destruction' if implicit else 'native_close_rollback',
                      'receipt': receipt, 'actual_counter': 0, 'actual_rows': [],
                      'expected': 'UNKNOWN' if implicit else 'DISCARDED'})
        con.close()

    # The wrapper delegates native args and cursor/primary objects exactly once;
    # the observer never fetches from or substitutes a returned cursor.
    primary = RuntimeError('owned-native-primary')
    class Counted(Native):
        calls = 0
        cursor_value = None
        def execute(self, *args, **kwargs):
            self.calls += 1
            if args == ('OWNED_PRIMARY',):
                raise primary
            result = super().execute(*args, **kwargs)
            self.cursor_value = result
            return result
    con = connect('delegation', Counted); value, _ = probe()
    before_calls = con.calls
    with value.observe_class(Counted):
        cursor = con.execute('SELECT ?', (19,))
        assert cursor is con.cursor_value and con.calls == before_calls + 1
        assert cursor.fetchone()[0] == 19 and cursor.fetchone() is None
        try:
            con.execute('OWNED_PRIMARY')
        except RuntimeError as error:
            assert error is primary and con.calls == before_calls + 2
        else:
            raise AssertionError('native primary exception replaced or swallowed')
        con.close()
    cases.append({'case': 'native_delegation_identity', 'expected': 'EXACT_CURSOR_PRIMARY_AND_CALL_COUNT'})

    # Replacing observer caps is confined to THIS tooling module/control child;
    # native DB statements must still execute once, while proof visibly refuses.
    con = connect('record-cap'); value, allocate = probe()
    limit = witness_module.MAX_RECORDS
    try:
        witness_module.MAX_RECORDS = 2
        with value.observe_class(Native):
            con.execute('BEGIN'); r = allocate(con)
            con.execute(insert, ('one', 'A', r, 0)); con.commit()
            assert actual(con)[1] == 1
            assert len(value.receipt()['records']) <= 2
            assert 'RECORD_CAP' in value.receipt()['refusals']
            refused(lambda: validate(value, con))
            emit('bounded_observer_exhaustion', value, con, 'NATIVE_RESULT_PRESERVED_PROOF_REFUSED')
            con.close()
    finally:
        witness_module.MAX_RECORDS = limit

    # Weak connection references do not preserve a dead TLS connection/page cache.
    con = connect('weak'); value, _ = probe()
    originals = {name: getattr(Native, name) for name in witness_module.METHODS}
    with value.observe_class(Native):
        con.execute('SELECT 1').fetchone()
        reference = weakref.ref(con)
        del con
        gc.collect()
        assert reference() is None, 'observer retained a native connection'
    assert all(getattr(Native, name) is original for name, original in originals.items())
    assert clock.now_ms is clock_before and db.now_ms is db_clock_before
    assert not guard.failed and not (work / 'guard-failure.jsonl').exists()
    result = {'scope': 'finite native SQLite component controls ONLY; no Root/field-time/measurement certificate',
              'native_class': Native.__module__ + '.' + Native.__qualname__,
              'native_allocator': native_allocator.__module__ + '.' + native_allocator.__qualname__,
              'clocks_unchanged': True, 'cases': cases,
              'remaining': 'actual source SQL decoders/alias wiring/full raw semantics/head-time oracle and Root execution still owed'}
    with (work / 'transaction-control-report.json').open('x') as stream:
        json.dump(result, stream, sort_keys=True)
    print(json.dumps({'native_component_cases': len(cases), 'tooling_controls_completed': True,
                      'product_or_measurement_green': False}))


def main():
    # Weak membership preserves the native implicit-destruction control. Every
    # still-live control-owned connection is closed even after a failed assert.
    owned = weakref.WeakSet()
    primary = None
    try:
        component_controls(owned)
    except BaseException as error:
        primary = error
        raise
    finally:
        failed = False
        for connection in list(owned):
            try:
                connection.close()
            except BaseException as error:
                failed = True
                print('OWNED_CONNECTION_CLEANUP_' + type(error).__name__, file=sys.stderr)
        if failed and primary is None:
            raise RuntimeError('owned native-control cleanup incomplete')


if __name__ == '__main__':
    main()
