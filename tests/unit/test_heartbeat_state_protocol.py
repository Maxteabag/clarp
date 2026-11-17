"""Owned SQL mutations converge despite delayed commits and cache publication."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from lib import agents, db, heartbeat, message_store, settings_store
from tests.unit.test_heartbeat_turn_accounting import _activate, _turn, _end
from tests.unit.test_heartbeat_accounting_review import _Status

PERSISTED = ('last_started', 'noop_streak', 'dormant', 'last_wake_signal_ms')
MUTATORS = ('run-start', 'interrupted-noop', 'plain-noop', 'activity', 'external-wake')
ORDERS = ('first-commit/second-complete/first-publish',
          'first-commit/second-prewrite/first-publish/second-write',
          'second-prewrite/first-complete/second-write')


def _seed(monkeypatch, profile='warm'):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    threshold = 3 if profile == 'threshold' else 0 if profile == 'cold' else 5
    settings_store.set_int(heartbeat.KEY_DORMANT_AFTER_NOOPS, threshold)
    row = {'agent_id': aid, 'last_started': 12.5, 'noop_streak': 2,
           'dormant': int(profile == 'cold'), 'last_wake_signal_ms': 10, 'updated_at': 1000}
    db.conn().execute('INSERT INTO heartbeat_state VALUES (?,?,?,?,?,?)', tuple(row.values()))
    heartbeat.reset_for_tests()
    recent = [] if profile == 'cold' else [11.0, 12.0]
    if profile != 'cold':
        heartbeat._state_for(aid).recent_starts = list(recent)
    monkeypatch.setattr(heartbeat, 'now_ms', lambda: 3000)
    turn = dict(db.conn().execute('SELECT started_at,settled_at,outcome FROM turns WHERE agent_id=?', (aid,)).fetchone())
    return {'aid': aid, 'row': row, 'recent': recent, 'threshold': threshold, 'turn': turn}


def _call(world, path, started=99.0):
    aid = world['aid']
    if path == 'counter':
        heartbeat.record_ended_turn(aid, 'heartbeat-1', 'HEARTBEAT_OK', origin='heartbeat', turn=world['turn'])
    elif path == 'run-start':
        heartbeat._record_run_start(aid, started)
    elif path == 'interrupted-noop':
        heartbeat.record_heartbeat_noop(aid, is_interrupted=True)
    elif path == 'plain-noop':
        heartbeat.record_heartbeat_noop(aid, is_interrupted=False)
    elif path == 'activity':
        heartbeat.record_heartbeat_activity(aid)
    elif path == 'external-wake':
        heartbeat._wake_from_external_signal(aid, heartbeat._state_for(aid))
    else:
        raise AssertionError(path)


def _expected(initial, path, threshold, signal, started):
    out = dict(initial)
    if path in ('counter', 'plain-noop', 'interrupted-noop'):
        out['noop_streak'] += 1
        if path != 'interrupted-noop' and threshold > 0 and out['noop_streak'] >= threshold:
            out['dormant'] = 1
    elif path == 'run-start':
        out['last_started'] = started
        out['last_wake_signal_ms'] = max(out['last_wake_signal_ms'], signal)
    elif path == 'activity':
        out['noop_streak'] = out['dormant'] = 0
    elif path == 'external-wake':
        if signal <= out['last_wake_signal_ms']:
            return out
        out['last_wake_signal_ms'] = signal
        out['noop_streak'] = out['dormant'] = 0
    out['updated_at'] = 3000
    return out


def _row(aid):
    return dict(db.conn().execute('SELECT * FROM heartbeat_state WHERE agent_id=?', (aid,)).fetchone())


def _persistent_cache(aid):
    state = heartbeat._state_for(aid)
    return {key: getattr(state, key) for key in PERSISTED}


def _fresh_process(aid):
    script = '''import json,sys,pathlib
sys.path.insert(0,sys.argv[1])
from lib import db,heartbeat
db.reset_for_tests(pathlib.Path(sys.argv[2]))
s=heartbeat._state_for(sys.argv[3])
print(json.dumps({k:getattr(s,k) for k in ('last_started','noop_streak','dormant','last_wake_signal_ms')}))
'''
    result = subprocess.run([sys.executable, '-c', script, str(Path(__file__).resolve().parents[2] / 'server'),
        str(db.DB_PATH), aid], capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr, result.stderr
    return json.loads(result.stdout)


def _schedule(monkeypatch, world, first, second, order, signal):
    """A commits first; B may have prepared inputs before A wrote anything."""
    signals = signal if isinstance(signal, tuple) else (signal, signal)
    monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms',
        lambda aid: signals[0 if threading.current_thread().name == 'protocol-a' else 1])
    ready_a = threading.Event(); release_a = threading.Event()
    ready_b = threading.Event(); release_b = threading.Event()
    done_a = threading.Event(); done_b = threading.Event()
    failures = []; paused = set()
    execute = db._TrackedConnection.execute
    def gated(self, sql, parameters=()):
        name = threading.current_thread().name
        normalized = ' '.join(sql.upper().split())
        state_write = normalized.startswith(('INSERT ', 'UPDATE ')) and 'HEARTBEAT_STATE' in normalized
        write_admission = normalized == 'BEGIN IMMEDIATE' or state_write
        if name == 'protocol-b' and order != ORDERS[0] and write_admission and 'b' not in paused:
            paused.add('b'); ready_b.set(); assert release_b.wait(5)
        result = execute(self, sql, parameters)
        committed = normalized == 'COMMIT' or (state_write and not self.in_transaction)
        if name == 'protocol-a' and order != ORDERS[2] and committed and 'a' not in paused:
            paused.add('a'); ready_a.set(); assert release_a.wait(5)
        return result
    monkeypatch.setattr(db._TrackedConnection, 'execute', gated)
    def worker(which, path, started, done, ready):
        try:
            _call(world, path, started)
        except BaseException as exc:
            failures.append((which, repr(exc)))
        finally:
            db.close_local(); done.set(); ready.set()
    a = threading.Thread(target=worker, name='protocol-a', args=('a', first, 99.0, done_a, ready_a))
    b = threading.Thread(target=worker, name='protocol-b', args=('b', second, 101.0, done_b, ready_b))
    try:
        if order == ORDERS[2]:
            b.start(); assert ready_b.wait(5)
            a.start(); assert done_a.wait(5)
            release_b.set()
        else:
            a.start(); assert ready_a.wait(5)
            b.start()
            if order == ORDERS[0]:
                assert done_b.wait(5)
                release_a.set()
            else:
                assert ready_b.wait(5)
                release_a.set(); assert done_a.wait(5)
                release_b.set()
    finally:
        release_a.set(); release_b.set()
        if a.ident is not None: a.join(5)
        if b.ident is not None: b.join(5)
    assert not a.is_alive() and not b.is_alive() and not failures, failures
    # These three orderings all linearize A's durable operation before B's.
    expected = _expected(world['row'], first, world['threshold'], signals[0], 99.0)
    expected = _expected(expected, second, world['threshold'], signals[1], 101.0)
    actual = _row(world['aid']); cache = _persistent_cache(world['aid'])
    restarted = _fresh_process(world['aid'])
    assert actual == expected
    expected_fields = {key: expected[key] for key in PERSISTED}
    assert cache == expected_fields and restarted == expected_fields
    recent = world['recent'] + ([99.0] if first == 'run-start' else []) + ([101.0] if second == 'run-start' else [])
    assert sorted(heartbeat._state_for(world['aid']).recent_starts) == sorted(recent)
    claims = db.conn().execute("SELECT COUNT(*) FROM heartbeat_accounting WHERE outcome='noop'").fetchone()[0]
    assert claims == int(first == 'counter') + int(second == 'counter')


@pytest.mark.parametrize('profile', ['warm', 'threshold', 'cold'])
@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('path', ['run-start', 'interrupted-noop', 'external-wake', 'activity'])
def test_counter_and_mutator_schedules(monkeypatch, profile, order, path):
    world = _seed(monkeypatch, profile)
    _schedule(monkeypatch, world, 'counter', path, order, 5000 if path == 'external-wake' else 0)


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('first', MUTATORS)
@pytest.mark.parametrize('second', MUTATORS)
def test_mutator_against_mutator_schedules(monkeypatch, order, first, second):
    world = _seed(monkeypatch)
    _schedule(monkeypatch, world, first, second, order,
              (5000 if first == 'external-wake' else 0, 5000 if second == 'external-wake' else 0))


@pytest.mark.parametrize('run_first', [False, True])
def test_run_start_and_wake_absorb_one_signal_by_commit_order(monkeypatch, run_first):
    world = _seed(monkeypatch)
    lines = []
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event, detail)))
    _schedule(monkeypatch, world, 'run-start' if run_first else 'external-wake',
              'external-wake' if run_first else 'run-start', ORDERS[0], 5000)
    assert [event for event, _ in lines] == ([] if run_first else ['heartbeatWake'])


@pytest.mark.parametrize('path', ('counter',) + MUTATORS)
def test_status_does_not_wait_for_any_state_mutators_writer(monkeypatch, path):
    world = _seed(monkeypatch)
    viewer = agents.create_agent(persona='Viewer', voice_id='v', cwd='/tmp', session='viewer')
    heartbeat._state_for(viewer)
    monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', lambda aid: 5000 if aid == world['aid'] else 0)
    at_write = threading.Event()
    class Viewer(_Status):
        def _query(self): return {'session': ['viewer']}
    def mutate():
        try:
            def trace(sql):
                normalized = ' '.join(sql.upper().split())
                if normalized == 'BEGIN IMMEDIATE' or (normalized.startswith('INSERT ') and 'HEARTBEAT_STATE' in normalized):
                    at_write.set()
            db.conn().set_trace_callback(trace)
            _call(world, path)
        finally: db.close_local()
    def status():
        try: return Viewer()._handle_agent_heartbeat_status()
        finally: db.close_local()
    blocker = sqlite3.connect(str(db.DB_PATH), isolation_level=None); blocker.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(mutate); reading = None
        try:
            assert at_write.wait(5)
            reading = pool.submit(status)
            try: response = reading.result(timeout=0.5); while_held = True
            except TimeoutError: while_held = False
        finally: blocker.rollback(); blocker.close()
        mutation.result(timeout=5)
        if reading is not None: response = reading.result(timeout=5)
    assert while_held and response['ok']


@pytest.mark.parametrize('path', ('counter',) + MUTATORS)
def test_write_failure_keeps_each_paths_main_memory_effect(monkeypatch, path):
    world = _seed(monkeypatch, 'threshold')
    monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', lambda aid: 5000)
    errors = []; lines = []
    monkeypatch.setattr(heartbeat, 'log_exception', lambda event, exc, **kwargs: errors.append(event))
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event, detail)))
    db.conn().execute("CREATE TRIGGER refuse_state BEFORE INSERT ON heartbeat_state BEGIN SELECT RAISE(ABORT,'synthetic state write failure'); END")
    _call(world, path)
    assert _row(world['aid']) == world['row']
    expected = world['row'] if path == 'counter' else _expected(world['row'], path, world['threshold'], 5000, 99.0)
    assert _persistent_cache(world['aid']) == {key: expected[key] for key in PERSISTED}
    assert heartbeat._state_for(world['aid']).recent_starts == world['recent'] + ([99.0] if path == 'run-start' else [])
    assert errors == ['heartbeatAccountingFail' if path == 'counter' else 'heartbeatStatePersistFail']
    assert [event for event, _ in lines] == (['heartbeatNoop'] if path in ('plain-noop','interrupted-noop') else ['heartbeatWake'] if path == 'external-wake' else [])
    assert db.conn().execute('SELECT COUNT(*) FROM heartbeat_accounting').fetchone()[0] == 0


def test_run_start_keeps_a_later_wake_cursor_from_the_committed_row(monkeypatch):
    world = _seed(monkeypatch)
    _schedule(monkeypatch, world, 'external-wake', 'run-start', ORDERS[2], (7000, 5000))


def test_real_deferred_diagnostic_flush_cannot_lose_the_counted_noop(monkeypatch):
    from lib import diagnostics_settings, telemetry
    world = _seed(monkeypatch)
    monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', lambda aid: 0)
    diagnostics_settings.update({'enabled': True, 'categories': ['database']})
    telemetry.conn()
    ready = threading.Event(); go = threading.Event(); busy = threading.Event()
    retry = threading.Event(); flushing = threading.Event(); done = threading.Event()
    errors = []
    ordinary_sleep = db.time.sleep
    def pause_after_real_busy(seconds):
        if threading.current_thread().name == 'protocol-flush' and not busy.is_set():
            busy.set(); assert retry.wait(5)
        else:
            ordinary_sleep(seconds)
    monkeypatch.setattr(db.time, 'sleep', pause_after_real_busy)
    def counter():
        try:
            db.conn(); tc = telemetry.conn(); tc.execute('PRAGMA busy_timeout=2000')
            def trace(sql):
                if 'INSERT INTO DIAGNOSTIC_EVENTS' in ' '.join(sql.upper().split()):
                    flushing.set()
            tc.set_trace_callback(trace)
            ready.set(); assert go.wait(5)
            _call(world, 'counter')
        except BaseException as exc: errors.append(repr(exc))
        finally:
            db.close_local(); telemetry.close_local(); done.set()
    thread = threading.Thread(target=counter, name='protocol-flush'); thread.start()
    assert ready.wait(5)
    state_writer = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    sink_writer = sqlite3.connect(str(telemetry.TELEMETRY_PATH), isolation_level=None)
    state_writer.execute('BEGIN IMMEDIATE'); sink_writer.execute('BEGIN IMMEDIATE')
    threshold = db._LONG_WRITE_WAIT_SECONDS
    # Real BUSY and the real deferred sink are used. Only the warning threshold
    # is lowered in scratch, so no timed sleep is needed to queue the diagnostic.
    monkeypatch.setattr(db, '_LONG_WRITE_WAIT_SECONDS', 0)
    try:
        go.set(); assert busy.wait(5)
        state_writer.rollback(); retry.set()
        assert flushing.wait(5)
        assert not done.is_set() and not heartbeat._STATE_LOCK.locked()
        assert _row(world['aid'])['noop_streak'] == 3
        monkeypatch.setattr(db, '_LONG_WRITE_WAIT_SECONDS', threshold)
        heartbeat._record_run_start(world['aid'], 99.0)
        assert _row(world['aid'])['noop_streak'] == 3
        assert _persistent_cache(world['aid'])['noop_streak'] == 3
        assert not done.is_set(), 'the sink must still be held during the other state mutation'
    finally:
        retry.set(); go.set()
        state_writer.rollback(); sink_writer.rollback()
        state_writer.close(); sink_writer.close(); thread.join(5)
    assert not thread.is_alive() and not errors, errors
    assert _row(world['aid'])['noop_streak'] == 3
    assert _persistent_cache(world['aid'])['noop_streak'] == 3
    assert _fresh_process(world['aid'])['noop_streak'] == 3
