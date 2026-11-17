"""Schedule reads project policy; scheduler selection durably owns it."""
from concurrent.futures import ThreadPoolExecutor
import copy
import sqlite3
import threading
import time

import pytest

from lib import agents, db, heartbeat
from lib.protocol import AgentState
from tests.unit.test_heartbeat import _heartbeat_test_env
from tests.unit.test_heartbeat_accounting_review import _Status
from tests.unit.test_heartbeat_state_protocol import (
    _seed, _call, _row, _fresh_process, _persistent_cache, PERSISTED,
)

NOW = 70_000.0


def _world(monkeypatch, *, interrupted=False, signal=0, cold=False):
    _heartbeat_test_env(monkeypatch)
    world = _seed(monkeypatch)
    aid = world['aid']
    agents.update_agent(aid, heartbeat_enabled=True)
    if interrupted:
        agents.record_state(aid, AgentState.INTERRUPTED, {'reason': 'usage_limit'})
        db.conn().execute('UPDATE state_log SET ts=10000000 WHERE agent_id=?', (aid,))
    monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', lambda _aid: signal)
    monkeypatch.setattr(heartbeat.time, 'time', lambda: NOW)
    # Provider execution ownership is independent of the back-off policy.
    monkeypatch.setattr(heartbeat, '_gather_snapshot', lambda agent, now:
        heartbeat._AgentSnapshot(latest=agents.latest_state(aid) or {}))
    if cold:
        heartbeat.reset_for_tests()
    return world


def _cache_snapshot():
    return {key: copy.deepcopy(vars(state)) for key, state in heartbeat._STATE_BY_AGENT.items()}


def _deny_writes(connection, attempts):
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}
    def authorize(action, first, second, _database, _source):
        if action in writes:
            attempts.append((action, first, second))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)


@pytest.mark.parametrize('cold', [False, True], ids=['warm','cold'])
@pytest.mark.parametrize('interrupted,signal', [(False,5000), (True,0)], ids=['wake','expired'])
def test_status_policy_projection_neither_writes_nor_changes_the_cache(monkeypatch, cold, interrupted, signal):
    world = _world(monkeypatch, interrupted=interrupted, signal=signal, cold=cold)
    aid = world['aid']; con = db.conn()
    before_row = _row(aid)
    before_cache = _cache_snapshot()
    attempts = []
    _deny_writes(con, attempts)
    try:
        result = _Status()._handle_agent_heartbeat_status()
    finally:
        con.set_authorizer(None)
    assert attempts == [], attempts
    assert _cache_snapshot() == before_cache
    assert _row(aid) == before_row
    assert result['schedule']['dormant'] is interrupted
    assert result['schedule']['noop_streak'] == (2 if interrupted else 0)


def test_same_agent_status_is_read_only_while_its_wake_waits_for_the_writer(monkeypatch):
    world = _world(monkeypatch, signal=5000)
    aid = world['aid']; before = _cache_snapshot()
    at_write = threading.Event()
    def wake():
        try:
            con = db.conn()
            con.set_trace_callback(lambda sql: at_write.set() if sql.strip().upper() == 'BEGIN IMMEDIATE' else None)
            heartbeat._wake_from_external_signal(aid, heartbeat._state_for(aid))
        finally:
            db.close_local()
    blocker = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    attempts = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        waking = pool.submit(wake)
        try:
            assert at_write.wait(3)
            con = db.conn(); _deny_writes(con, attempts)
            started = time.monotonic()
            try:
                result = _Status()._handle_agent_heartbeat_status()
            finally:
                con.set_authorizer(None)
            elapsed = time.monotonic() - started
            assert attempts == [], attempts
            assert elapsed < 0.25, elapsed
            assert _cache_snapshot() == before
            assert result['schedule']['noop_streak'] == 0
        finally:
            blocker.rollback(); blocker.close()
        waking.result(timeout=5)
    assert _row(aid)['last_wake_signal_ms'] == 5000


@pytest.mark.parametrize('interrupted,signal', [(False,5000), (True,0)], ids=['wake','expired'])
def test_projection_matches_the_selection_that_then_makes_policy_durable(monkeypatch, interrupted, signal):
    world = _world(monkeypatch, interrupted=interrupted, signal=signal)
    aid = world['aid']; agent = agents.get_by_agent_id(aid)
    projection = heartbeat.agent_schedule(agent, now=NOW)
    ready = heartbeat.pending_heartbeat_agents(now=NOW)
    row = _row(aid)
    assert row['dormant'] == int(projection['dormant'])
    assert row['noop_streak'] == projection['noop_streak']
    assert heartbeat.agent_schedule(agent, now=NOW) == projection
    assert [a['agent_id'] for a in ready] == ([] if interrupted else [aid])
    assert _persistent_cache(aid) == _fresh_process(aid)


@pytest.mark.parametrize('path', ['none','run-start','plain-noop','interrupted-noop','counter','activity','external-wake'])
def test_policy_dormancy_is_sticky_through_mutations_and_done_after_restart(monkeypatch, path):
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']
    assert heartbeat.pending_heartbeat_agents(now=NOW) == []
    assert _row(aid)['dormant'] == 1
    if path == 'external-wake':
        monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', lambda _aid: 5000)
    if path != 'none':
        _call(world, path)
    expected = path not in ('activity','external-wake')
    row = _row(aid)
    assert bool(row['dormant']) is expected
    assert heartbeat._state_for(aid).dormant is expected
    assert _fresh_process(aid)['dormant'] is expected
    # A later DONE without a wake no longer re-derives the 11-hour predicate.
    agents.record_state(aid, AgentState.DONE, {'source': 'synthetic-recovery'})
    heartbeat.reset_for_tests()
    monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', lambda _aid: 0)
    agent = agents.get_by_agent_id(aid)
    assert heartbeat.agent_schedule(agent, now=NOW)['dormant'] is expected
    assert aid not in heartbeat._STATE_BY_AGENT  # even a cold projection is pure
    ready = heartbeat.pending_heartbeat_agents(now=NOW)
    assert [a['agent_id'] for a in ready] == ([] if expected else [aid])
    assert _persistent_cache(aid) == _fresh_process(aid)


def test_two_selections_persist_and_log_the_dormancy_transition_only_once(monkeypatch):
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']; con = db.conn()
    statements = []; lines = []
    con.set_trace_callback(statements.append)
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event, detail)))
    try:
        assert heartbeat.pending_heartbeat_agents(now=NOW) == []
        first = _row(aid)
        assert heartbeat.pending_heartbeat_agents(now=NOW+1) == []
        assert _row(aid) == first
    finally:
        con.set_trace_callback(None)
    writes = [sql for sql in statements if 'HEARTBEAT_STATE' in sql.upper() and sql.lstrip().upper().startswith(('INSERT','UPDATE'))]
    assert len(writes) == 1
    assert [event for event, detail in lines if event == 'heartbeatDormant'] == ['heartbeatDormant']
    assert 'agent='+aid in next(detail for event, detail in lines if event == 'heartbeatDormant')


def test_policy_dormancy_defaults_for_an_agent_without_persisted_state(monkeypatch):
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']
    db.conn().execute('DELETE FROM heartbeat_state WHERE agent_id=?', (aid,))
    heartbeat.reset_for_tests()
    assert heartbeat.pending_heartbeat_agents(now=NOW) == []
    stored = db.conn().execute('SELECT * FROM heartbeat_state WHERE agent_id=?', (aid,)).fetchone()
    assert stored is not None
    row = dict(stored)
    assert {k: row[k] for k in PERSISTED} == {
        'last_started': 0.0, 'noop_streak': 0, 'dormant': 1, 'last_wake_signal_ms': 0}
    assert _persistent_cache(aid) == _fresh_process(aid)


def test_failed_policy_write_is_logged_and_retried_by_selection(monkeypatch):
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']; con = db.conn()
    con.execute("CREATE TRIGGER fail_policy BEFORE UPDATE ON heartbeat_state "
                "WHEN NEW.dormant=1 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    errors = []; lines = []
    monkeypatch.setattr(heartbeat, 'log_exception', lambda event, error, **kw: errors.append((event,type(error).__name__)))
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event,detail)))
    try:
        assert heartbeat.pending_heartbeat_agents(now=NOW) == []
        assert _row(aid)['dormant'] == 0
        assert heartbeat._state_for(aid).dormant is False
        assert errors == [('heartbeatStatePersistFail','IntegrityError')]
        assert not [line for line in lines if line[0] == 'heartbeatDormant']
    finally:
        con.execute('DROP TRIGGER fail_policy')
    assert heartbeat.pending_heartbeat_agents(now=NOW+1) == []
    assert _row(aid)['dormant'] == 1
    assert heartbeat._state_for(aid).dormant is True
    assert len([line for line in lines if line[0] == 'heartbeatDormant']) == 1


def test_selections_sharing_an_unpublished_policy_decision_log_one_transition(monkeypatch):
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']; committed = threading.Event(); release = threading.Event()
    execute = db._TrackedConnection.execute
    paused = []
    def gate(self, sql, parameters=()):
        result = execute(self, sql, parameters)
        if (threading.current_thread().name == 'policy-first' and sql.strip().upper() == 'COMMIT' and not paused):
            paused.append(True); committed.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(db._TrackedConnection, 'execute', gate)
    lines = []; failures = []
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event,detail)))
    def select_first():
        try:
            assert heartbeat.pending_heartbeat_agents(now=NOW) == []
        except BaseException as error:
            failures.append(repr(error))
        finally:
            db.close_local()
    first = threading.Thread(target=select_first, name='policy-first')
    first.start()
    try:
        assert committed.wait(3)
        assert heartbeat.pending_heartbeat_agents(now=NOW) == []
    finally:
        release.set(); first.join(5)
    assert not first.is_alive() and not failures, failures
    assert _row(aid)['dormant'] == 1
    assert _persistent_cache(aid) == _fresh_process(aid)
    assert len([line for line in lines if line[0] == 'heartbeatDormant']) == 1
