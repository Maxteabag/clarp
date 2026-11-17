"""An old scheduler decision must not overwrite newer lifecycle/back-off facts."""
import sqlite3
import threading

import pytest

from lib import agents, db, heartbeat, message_store, turn_lifecycle
from lib.protocol import AgentState
from lib.turn_lifecycle import TurnEvent
from tests.unit.test_heartbeat_schedule_projection import _world, NOW
from tests.unit.test_heartbeat_state_protocol import _row, _fresh_process, _persistent_cache


@pytest.mark.parametrize('pause', ['after-decision','writer-busy'])
@pytest.mark.parametrize('resolution', ['wake','activity','interrupted-noop','run-start',
                                      'new-interruption','create-reset','noop-same-stamp',
                                      'same-stamp-interruption'])
def test_obsolete_dormancy_never_overwrites_newer_facts(monkeypatch, pause, resolution):
    real_signal = heartbeat._latest_wake_signal_ms
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']; con = db.conn()
    if resolution == 'create-reset':
        con.execute('DELETE FROM heartbeat_state WHERE agent_id=?', (aid,))
        heartbeat.reset_for_tests()
    if resolution == 'noop-same-stamp':
        con.execute('UPDATE heartbeat_state SET updated_at=3000 WHERE agent_id=?', (aid,))
    stopped = threading.Event(); release = threading.Event(); ready = threading.Event(); go = threading.Event()
    original_skip = heartbeat._skip_reason
    original_busy = db._is_lock_contention
    armed = []; failures = []; lines = []; errors = []
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event,detail)))
    monkeypatch.setattr(heartbeat, 'log_exception', lambda event, error, **kw: errors.append((event,type(error).__name__)))
    def skip(**kwargs):
        reason = original_skip(**kwargs)
        if threading.current_thread().name == 'stale-policy':
            assert reason == 'dormant'
            if pause == 'after-decision':
                stopped.set(); assert release.wait(5)
        return reason
    def busy(error):
        result = original_busy(error)
        if result and pause == 'writer-busy' and threading.current_thread().name == 'stale-policy' and not armed:
            armed.append(True); stopped.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(heartbeat, '_skip_reason', skip)
    monkeypatch.setattr(db, '_is_lock_contention', busy)
    def old_selection():
        try:
            db.conn(); ready.set(); assert go.wait(5)
            assert heartbeat.pending_heartbeat_agents(now=NOW) == []
        except BaseException as error:
            failures.append(repr(error))
        finally:
            db.close_local()
    old = threading.Thread(target=old_selection, name='stale-policy')
    blocker = None; old.start()
    try:
        assert ready.wait(3)
        if pause == 'writer-busy':
            blocker = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
            blocker.execute('BEGIN IMMEDIATE')
        go.set(); assert stopped.wait(3)
        if blocker is not None:
            # The old thread has really received SQLITE_BUSY and is latched
            # before its retry. Let the real newer writers finish first.
            blocker.rollback(); blocker.close(); blocker = None
        if resolution == 'wake':
            trace='user-resolution'
            agents.open_turn(agent_id=aid, source='pwa', trace_id=trace)
            message_store.record_user_message(agent_id=aid, backend_session_id='b-1',
                client_msg_id='resolution', text='Synthetic request', origin='user', trace_id=trace)
            turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED, {'trace_id':trace})
            turn_lifecycle.transition(aid, TurnEvent.PROCESS_EXITED_OK, {'trace_id':trace})
            monkeypatch.setattr(heartbeat, '_latest_wake_signal_ms', real_signal)
            assert [a['agent_id'] for a in heartbeat.pending_heartbeat_agents(now=NOW)] == [aid]
        elif resolution == 'activity':
            agents.record_state(aid, AgentState.DONE, {'source':'synthetic-resolution'})
            heartbeat.record_heartbeat_activity(aid)
            assert [a['agent_id'] for a in heartbeat.pending_heartbeat_agents(now=NOW)] == [aid]
        elif resolution in ('interrupted-noop','noop-same-stamp'):
            heartbeat.record_heartbeat_noop(aid, is_interrupted=True)
        elif resolution == 'run-start':
            heartbeat._record_run_start(aid, NOW)
        elif resolution in ('new-interruption','same-stamp-interruption'):
            agents.record_state(aid, AgentState.INTERRUPTED, {'reason':'usage_limit'})
            if resolution == 'same-stamp-interruption':
                con.execute('UPDATE state_log SET ts=10000000 WHERE state_id=(SELECT MAX(state_id) FROM state_log WHERE agent_id=?)', (aid,))
        else:
            # A newly created row is then reset to all default back-off
            # fields. It must remain distinct from the original absence.
            heartbeat.record_heartbeat_noop(aid, is_interrupted=True)
            heartbeat.record_heartbeat_activity(aid)
        newer = _row(aid)
        assert newer['dormant'] == 0
        if resolution == 'noop-same-stamp':
            assert newer['updated_at'] == 3000 and newer['noop_streak'] == 3
    finally:
        if blocker is not None:
            blocker.rollback(); blocker.close()
        release.set(); go.set(); old.join(5)
    assert not old.is_alive() and not failures, failures
    assert errors == []
    assert _row(aid) == newer, 'the obsolete decision must change no stored field'
    assert _persistent_cache(aid) == _fresh_process(aid)
    assert not [line for line in lines if line[0] == 'heartbeatDormant']
    # A new selection, not a retry of the old decision, uses current facts.
    heartbeat.reset_for_tests()
    again=heartbeat.pending_heartbeat_agents(now=NOW)
    if resolution in ('wake','activity'):
        assert [a['agent_id'] for a in again] == [aid]
        assert _row(aid)['dormant'] == 0
    elif resolution == 'new-interruption':
        assert again == [] and _row(aid)['dormant'] == 0
    else:
        assert again == [] and _row(aid)['dormant'] == 1
        assert len([line for line in lines if line[0] == 'heartbeatDormant']) == 1
    assert _persistent_cache(aid) == _fresh_process(aid)


@pytest.mark.parametrize('resolution', ['activity','newer-expired'])
def test_moved_gather_facts_are_not_revised_into_a_write_in_the_same_selection(monkeypatch, resolution):
    world = _world(monkeypatch, interrupted=True)
    aid = world['aid']; gather = heartbeat._gather_snapshot; resolved = []; lines = []
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: lines.append((event,detail)))
    def gather_then_resolve(agent, now):
        snapshot = gather(agent,now)
        if not resolved:
            if resolution == 'activity':
                agents.record_state(aid, AgentState.DONE, {'source':'synthetic-resolution'})
                heartbeat.record_heartbeat_activity(aid)
            else:
                agents.record_state(aid, AgentState.INTERRUPTED, {'reason':'usage_limit'})
                db.conn().execute('UPDATE state_log SET ts=20000000 WHERE state_id=(SELECT MAX(state_id) FROM state_log WHERE agent_id=?)', (aid,))
            resolved.append(_row(aid))
        return snapshot
    monkeypatch.setattr(heartbeat, '_gather_snapshot', gather_then_resolve)
    assert heartbeat.pending_heartbeat_agents(now=NOW) == []
    assert _row(aid) == resolved[0]
    assert not [line for line in lines if line[0] == 'heartbeatDormant']
    again = heartbeat.pending_heartbeat_agents(now=NOW)
    if resolution == 'activity':
        assert [a['agent_id'] for a in again] == [aid]
    else:
        assert again == [] and _row(aid)['dormant'] == 1
    assert _persistent_cache(aid) == _fresh_process(aid)
