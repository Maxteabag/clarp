"""R28 accounting cannot block status reads or tax ordinary settlements."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from lib import agents, db, heartbeat, message_store, message_writes, turn_lifecycle
from lib.routes.heartbeat import HeartbeatRoutes
from lib.turn_lifecycle import TurnEvent
from tests.unit.test_heartbeat_turn_accounting import _activate, _turn, _import, _end, _streak
from tests.unit.test_log_turn_summary import _iso


def _read(aid, answer, trace='heartbeat-1'):
    turn = db.conn().execute('SELECT started_at,settled_at,outcome FROM turns '
        'WHERE agent_id=? AND trace_id=?', (aid, trace)).fetchone()
    message_store.apply_final_assistant_side_effects(agent_id=aid, backend_session_id='b-1',
        trace_id=trace, row={'id': 'terminal-' + trace, 'text': answer, '_heartbeat_turn': dict(turn)})


class _Status(HeartbeatRoutes):
    def _query(self):
        return {'session': ['nia']}

    def _json_ok(self, payload):
        return payload

    def _json_error(self, *args):
        raise AssertionError(args)


def test_status_returns_while_another_connection_blocks_the_counter_writer(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    heartbeat._state_for(aid)
    at_write = threading.Event()
    def counter():
        try:
            def trace(sql):
                if sql.strip().upper() == 'BEGIN IMMEDIATE':
                    at_write.set()
            db.conn().set_trace_callback(trace)
            _read(aid, 'HEARTBEAT_OK')
        finally:
            db.close_local()
    def status():
        try:
            started = time.monotonic()
            result = _Status()._handle_agent_heartbeat_status()
            return result, time.monotonic() - started
        finally:
            db.close_local()
    blocker = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=2) as pool:
        counting = pool.submit(counter)
        reading = None
        try:
            assert at_write.wait(3)
            reading = pool.submit(status)
            try:
                result, duration = reading.result(timeout=0.5)
                while_locked = True
            except TimeoutError:
                while_locked = False
        finally:
            blocker.rollback()
            blocker.close()
        counting.result(timeout=3)
        if reading is not None:
            result, duration = reading.result(timeout=3)
    assert while_locked, 'heartbeat status waited for the counter SQLite writer'
    assert result['ok'] is True and 'schedule' in result
    assert _streak(aid) == 1


def test_late_cache_publication_reads_the_latest_of_two_committed_counters(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    agents.open_turn(agent_id=aid, source='pwa', trace_id='heartbeat-2')
    message_store.record_user_message(agent_id=aid, backend_session_id='b-1',
        client_msg_id='prompt-2', text=heartbeat.HEARTBEAT_PROMPT, origin='heartbeat', trace_id='heartbeat-2')
    turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED, {'trace_id': 'heartbeat-2'})
    turn_lifecycle.transition(aid, TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'heartbeat-2'})
    heartbeat._state_for(aid)
    first_ident = []
    committed = threading.Event()
    release = threading.Event()
    execute = db._TrackedConnection.execute
    def hold_after_commit(self, sql, parameters=()):
        result = execute(self, sql, parameters)
        if (first_ident and threading.get_ident() == first_ident[0]
                and sql.strip().upper() == 'COMMIT' and not committed.is_set()):
            committed.set()
            assert release.wait(3)
        return result
    monkeypatch.setattr(db._TrackedConnection, 'execute', hold_after_commit)
    def counter(trace, first=False):
        try:
            if first:
                first_ident.append(threading.get_ident())
            _read(aid, 'HEARTBEAT_OK', trace)
        finally:
            db.close_local()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(counter, 'heartbeat-1', True)
        second = None
        try:
            assert committed.wait(3)
            second = pool.submit(counter, 'heartbeat-2')
            try:
                second.result(timeout=0.5)
                second_published_first = True
            except TimeoutError:
                second_published_first = False
        finally:
            release.set()
        first.result(timeout=3)
        if second is not None:
            second.result(timeout=3)
    assert second_published_first, 'first counter held the cache lock after its commit'
    persisted = db.conn().execute('SELECT noop_streak FROM heartbeat_state WHERE agent_id=?', (aid,)).fetchone()[0]
    assert persisted == 2 and _streak(aid) == persisted


@pytest.mark.parametrize('authority', [False, True])
def test_ordinary_settlement_has_no_accounting_statement_cost(monkeypatch, authority):
    _activate(monkeypatch)
    aid = _turn(monkeypatch, origin='user')
    if authority:
        snapshot = message_store.begin_agy_assistant_turn(agent_id=aid, backend_session_id='b-1',
            trace_id='heartbeat-1', observed_assistant_count=0)
        message_store.commit_agy_assistant_turn(agent_id=aid, backend_session_id='b-1',
            trace_id='heartbeat-1', snapshot=snapshot, terminal_status='success', text='Ordinary answer.')
    _end(aid)
    def statements():
        seen = []
        db.conn().set_trace_callback(seen.append)
        try:
            message_writes.restamp_turn(aid, 'heartbeat-1')
        finally:
            db.conn().set_trace_callback(None)
        return seen
    actual = statements()
    # Disabling the newly added effect gives the pre-R28 restamp operation;
    # its message/summary queries and transaction still execute unchanged.
    monkeypatch.setattr(message_writes, '_account_heartbeat_turn', lambda *args, **kwargs: None)
    without_effect = statements()
    assert actual == without_effect


def test_counter_uses_writer_provenance_without_reading_message_history(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    actual = db.conn()
    seen = []
    class Audited:
        def __getattr__(self, name):
            return getattr(actual, name)
        def execute(self, sql, parameters=()):
            seen.append(' '.join(sql.lower().split()))
            return actual.execute(sql, parameters)
    monkeypatch.setattr(heartbeat, 'conn', lambda: Audited())
    _read(aid, 'HEARTBEAT_OK')
    assert not any('from messages' in sql for sql in seen)
    assert _streak(aid) == 1


def test_conflict_receipt_is_a_ledger_receipt_and_leaves_settings_unchanged(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    _read(aid, 'HEARTBEAT_OK')
    before = [tuple(row) for row in db.conn().execute('SELECT * FROM settings ORDER BY key')]
    _read(aid, 'The branch is fixed.')
    heartbeat.reset_for_tests()
    _read(aid, 'The branch is fixed.')
    assert [tuple(row) for row in db.conn().execute('SELECT * FROM settings ORDER BY key')] == before
    receipts = db.conn().execute("SELECT accounting_key,outcome FROM heartbeat_accounting "
        "WHERE accounting_key LIKE 'heartbeat-conflict:%'").fetchall()
    assert len(receipts) == 1 and receipts[0]['outcome'] == 'conflict'
    turns = db.conn().execute("SELECT outcome FROM heartbeat_accounting "
        "WHERE agent_id=? AND outcome IN ('noop','activity','neither')", (aid,)).fetchall()
    assert [row[0] for row in turns] == ['noop'] and _streak(aid) == 1


def test_completed_noop_logs_once_after_the_claim_and_counter_commit(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    seen = []
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: seen.append((event, detail, db.conn().in_transaction)))
    _read(aid, 'HEARTBEAT_OK')
    _read(aid, 'HEARTBEAT_OK')
    assert [row for row in seen if row[0] == 'heartbeatNoop'] == [
        ('heartbeatNoop', f'agent={aid} streak=1 dormant=False', False)]


@pytest.mark.parametrize('earlier', ['HEARTBEAT_OK', 'The branch is repaired.'])
def test_empty_tail_without_its_user_boundary_claims_nothing(monkeypatch, earlier):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    carry = {}
    _import(aid, [earlier], capture_at=2, captured=carry)
    _end(aid)
    message_writes.store_transcript_tail(agent_id=aid, backend_session_id='b-1',
        source_file='/scratch/heartbeat.jsonl', start_seq=2, carry=carry,
        turns=[{'role': 'assistant', 'text': '', 'trace_id': 'heartbeat-1', 'timestamp': _iso(2025)}])
    assert db.conn().execute('SELECT COUNT(*) FROM heartbeat_accounting').fetchone()[0] == 0
    _import(aid, [earlier, ''])
    assert db.conn().execute('SELECT outcome FROM heartbeat_accounting').fetchone()[0] == (
        'noop' if earlier == 'HEARTBEAT_OK' else 'activity')


def test_nonheartbeat_provenance_needs_no_accounting_queries(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch, origin='user')
    _end(aid)
    turn = dict(db.conn().execute('SELECT started_at,settled_at,outcome FROM turns WHERE agent_id=?', (aid,)).fetchone())
    queries = []
    db.conn().set_trace_callback(queries.append)
    try:
        heartbeat.record_ended_turn(aid, 'heartbeat-1', 'HEARTBEAT_OK', origin='user', turn=turn)
    finally:
        db.conn().set_trace_callback(None)
    assert queries == []
