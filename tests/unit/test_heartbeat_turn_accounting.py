"""Heartbeat back-off follows one ended Host turn, never its earlier remarks."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib import agents, db, heartbeat, message_store, settings_store, turn_lifecycle
from lib.turn_lifecycle import TurnEvent
from tests.unit.test_log_turn_summary import _iso


ACTIVATION = 'heartbeat.accounting_activation_ms'


def _activate(monkeypatch, at=1000):
    monkeypatch.setattr(heartbeat, 'now_ms', lambda: at)
    monkeypatch.setattr(heartbeat.HeartbeatScheduler, '_loop', lambda self: None)
    scheduler = heartbeat.HeartbeatScheduler(send_heartbeat=lambda *args: None)
    scheduler.start()
    scheduler.stop()


def _turn(monkeypatch, *, start=2000, trace='heartbeat-1'):
    aid = agents.create_agent(persona='Nia', voice_id='v', cwd='/tmp', session='nia')
    agents.start_runtime(aid, 'nia')
    agents.open_turn(agent_id=aid, source='pwa', trace_id=trace)
    db.conn().execute('UPDATE turns SET started_at=? WHERE agent_id=?', (start, aid))
    message_store.record_user_message(agent_id=aid, backend_session_id='b-1',
        client_msg_id='prompt', text=heartbeat.HEARTBEAT_PROMPT,
        origin='heartbeat', trace_id=trace)
    turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED, {'trace_id': trace})
    return aid


def _import(aid, answers, *, trace='heartbeat-1', capture_at=None, captured=None):
    turns = [{'role': 'user', 'text': heartbeat.HEARTBEAT_PROMPT, 'timestamp': _iso(2010)}]
    for n, answer in enumerate(answers):
        turns.append({'role': 'assistant', 'text': answer, 'timestamp': _iso(2020+n),
                      'trace_id': trace})
    return message_store.store_transcript_turns(agent_id=aid, backend_session_id='b-1',
        source_file='/scratch/heartbeat.jsonl', turns=turns, capture_at=capture_at, captured=captured)


def _end(aid):
    turn_lifecycle.transition(aid, TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'heartbeat-1'})


def _live(aid, answer):
    message_store.apply_final_assistant_side_effects(agent_id=aid, backend_session_id='b-1',
        trace_id='heartbeat-1', row={'id': 'terminal', 'text': answer})


def _ledger(aid):
    return [tuple(r) for r in db.conn().execute(
        'SELECT accounting_key,outcome FROM heartbeat_accounting WHERE agent_id=?', (aid,))]


def _streak(aid):
    return heartbeat._state_for(aid).noop_streak


def test_partial_import_claims_nothing_then_the_last_answer_counts(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    heartbeat.record_heartbeat_noop(aid)
    _import(aid, ['I will check the branch.'])
    assert _streak(aid) == 1
    assert _ledger(aid) == []
    _import(aid, ['I will check the branch.', 'HEARTBEAT_OK'])
    assert _ledger(aid) == []  # text alone cannot end a Host turn
    _end(aid)
    _import(aid, ['I will check the branch.', 'HEARTBEAT_OK'])
    assert _streak(aid) == 2
    assert [outcome for _, outcome in _ledger(aid)] == ['noop']


@pytest.mark.parametrize(('last','wanted','streak'), [
    ('HEARTBEAT_OK', 'noop', 3),
    ('HEARTBEAT_OK — nothing changed', 'noop', 3),
    ('nothing changed HEARTBEAT_OK', 'noop', 3),
    ('The branch is repaired.', 'activity', 0),
    ('Checking heartbeat status.', 'neither', 2),
    ('Heartbeat check: no action needed.', 'neither', 2),
    ('Heartbeat pass complete.', 'neither', 2),
    ('', 'neither', 2),
])
def test_last_nonempty_answer_seals_one_outcome(monkeypatch, last, wanted, streak):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    heartbeat.record_heartbeat_noop(aid)
    heartbeat.record_heartbeat_noop(aid)
    _end(aid)
    answers = (['I did some work.', last, ''] if last else ['', ''])
    _import(aid, answers)
    assert _streak(aid) == streak
    assert [outcome for _, outcome in _ledger(aid)] == [wanted]
    _import(aid, answers)
    assert _streak(aid) == streak
    assert len(_ledger(aid)) == 1


@pytest.mark.parametrize('first_writer', ['live', 'transcript'])
@pytest.mark.parametrize(('first','later','wanted'), [
    ('HEARTBEAT_OK', 'The branch is repaired.', 'noop'),
    ('The branch is repaired.', 'HEARTBEAT_OK', 'activity'),
    ('Checking heartbeat.', 'HEARTBEAT_OK', 'neither'),
])
def test_first_authoritative_writer_wins_and_disagreement_is_logged_once(
        monkeypatch, first_writer, first, later, wanted):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    heartbeat.record_heartbeat_noop(aid)
    _end(aid)
    reports = []
    monkeypatch.setattr(heartbeat, 'log', lambda event, detail: reports.append((event, detail)))
    writers = {'live': lambda text: _live(aid, text), 'transcript': lambda text: _import(aid, [text])}
    writers[first_writer](first)
    other = writers['transcript' if first_writer == 'live' else 'live']
    other(later)
    heartbeat.reset_for_tests()  # the conflict receipt must survive a Host restart, too
    other(later)
    assert [outcome for _, outcome in _ledger(aid)] == [wanted]
    assert _streak(aid) == {'noop': 2, 'activity': 0, 'neither': 1}[wanted]
    conflicts = [detail for event, detail in reports if event == 'heartbeatAccountingConflict']
    assert conflicts == [f'agent={aid} turn=heartbeat-1 first={wanted} later='
                         + ('activity' if later != 'HEARTBEAT_OK' else 'noop')]
    assert first not in conflicts[0] and later not in conflicts[0]


@pytest.mark.parametrize('old_ledger', [False, True])
def test_old_and_straddling_turns_do_not_count_on_full_reimport(monkeypatch, old_ledger):
    aid = _turn(monkeypatch, start=500)
    if old_ledger:
        db.conn().execute('INSERT INTO heartbeat_accounting VALUES (?,?,?,?)',
                          ('legacy-key', aid, 'noop', 600))
    before = _ledger(aid)
    _activate(monkeypatch)
    _end(aid)
    _import(aid, ['HEARTBEAT_OK'])
    heartbeat.reset_for_tests()
    _activate(monkeypatch, at=9000)
    _import(aid, ['HEARTBEAT_OK'])
    assert _streak(aid) == 0
    assert _ledger(aid) == before
    assert settings_store.get(ACTIVATION) == '1000'


def test_restart_mid_new_turn_preserves_activation_and_counts_once(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _import(aid, ['Checking the branch.'])
    heartbeat.reset_for_tests()
    _activate(monkeypatch, at=9000)
    _end(aid)
    _live(aid, 'HEARTBEAT_OK')
    _import(aid, ['Checking the branch.', 'HEARTBEAT_OK'])
    assert settings_store.get(ACTIVATION) == '1000'
    assert _streak(aid) == 1
    assert len(_ledger(aid)) == 1


def test_two_starts_and_concurrent_first_use_persist_activation_exactly_once(monkeypatch):
    monkeypatch.setattr(heartbeat, 'now_ms', lambda: 1234)
    monkeypatch.setattr(heartbeat.HeartbeatScheduler, '_loop', lambda self: None)
    db.conn().execute('CREATE TABLE activation_writes(value TEXT)')
    for action in ('INSERT', 'UPDATE'):
        db.conn().execute(f"CREATE TRIGGER audit_activation_{action} AFTER {action} ON settings "
            "WHEN NEW.key='heartbeat.accounting_activation_ms' "
            "BEGIN INSERT INTO activation_writes VALUES(NEW.value); END")
    barrier = threading.Barrier(2)
    inserts = threading.Barrier(2)
    def start():
        paused = False
        def trace(sql):
            nonlocal paused
            if not paused and sql.lstrip().upper().startswith('INSERT') and ACTIVATION in sql:
                paused = True
                # Hold both first INSERTs before either executes, so both
                # starts have really observed an absent watermark. The real
                # SQLite uniqueness decision and triggers remain in control.
                inserts.wait(timeout=5)
        try:
            db.conn().set_trace_callback(trace)
            barrier.wait(timeout=5)
            scheduler = heartbeat.HeartbeatScheduler(send_heartbeat=lambda *args: None)
            scheduler.start()
            scheduler.stop()
        finally:
            db.conn().set_trace_callback(None)
            db.close_local()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: start(), range(2)))
    assert not inserts.broken
    assert [tuple(row) for row in db.conn().execute('SELECT value FROM activation_writes')] == [('1234',)]
    row = db.conn().execute('SELECT value,updated_at FROM settings WHERE key=?', (ACTIVATION,)).fetchone()
    assert tuple(row) == ('1234', 1234)
    # A committed trigger makes ANY later rewrite observable, even one of identical bytes.
    db.conn().execute("CREATE TRIGGER activation_immutable BEFORE UPDATE ON settings "
                      "WHEN OLD.key='heartbeat.accounting_activation_ms' "
                      "BEGIN SELECT RAISE(ABORT,'activation was rewritten'); END")
    _activate(monkeypatch, at=9999)
    assert tuple(db.conn().execute('SELECT value,updated_at FROM settings WHERE key=?',
                                  (ACTIVATION,)).fetchone()) == ('1234', 1234)
    assert db.conn().execute('SELECT COUNT(*) FROM activation_writes').fetchone()[0] == 1


@pytest.mark.parametrize('answer,wanted', [('HEARTBEAT_OK', 'noop'), ('', 'neither')])
def test_live_terminal_authority_waits_for_host_settlement(monkeypatch, answer, wanted):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    snapshot = message_store.begin_agy_assistant_turn(agent_id=aid, backend_session_id='b-1',
        trace_id='heartbeat-1', observed_assistant_count=0)
    row = message_store.commit_agy_assistant_turn(agent_id=aid, backend_session_id='b-1',
        trace_id='heartbeat-1', snapshot=snapshot, terminal_status='success' if answer else 'empty',
        text=answer)
    if answer:
        message_store.apply_final_assistant_side_effects(agent_id=aid, backend_session_id='b-1',
            trace_id='heartbeat-1', row=row)
    assert _ledger(aid) == []
    _end(aid)
    assert [outcome for _, outcome in _ledger(aid)] == [wanted]
    assert _streak(aid) == (1 if answer else 0)
    _import(aid, ['A transcript remark that is not the terminal answer.'])
    assert [outcome for _, outcome in _ledger(aid)] == [wanted]
    assert _streak(aid) == (1 if answer else 0)


def test_empty_ended_turn_is_sealed_without_an_assistant_row(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    _import(aid, [])
    assert [outcome for _, outcome in _ledger(aid)] == ['neither']


def test_live_finalizer_uses_raw_ack_normalization_after_its_message_commit(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    row = message_store.finalize_live_assistant_message(agent_id=aid, backend_session_id='b-1',
        trace_id='heartbeat-1', text='HEARTBEAT_OK — nothing changed')
    assert row['text'] == 'Heartbeat check: no action needed.'
    assert [outcome for _, outcome in _ledger(aid)] == ['noop']
    assert _streak(aid) == 1


def test_failed_turn_keeps_only_the_existing_interrupted_failure_accounting(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    turn_lifecycle.transition(aid, TurnEvent.PROCESS_EXITED_FAILED, {'trace_id': 'heartbeat-1'})
    heartbeat.record_heartbeat_noop(aid, is_interrupted=True)
    _import(aid, ['Earlier work.', 'HEARTBEAT_OK'])
    _live(aid, 'HEARTBEAT_OK')
    assert _streak(aid) == 1
    assert _ledger(aid) == []


def test_a_failed_counter_write_rolls_back_the_claim_and_the_cached_streak(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    heartbeat.record_heartbeat_noop(aid)
    _end(aid)
    reports = []
    monkeypatch.setattr(heartbeat, 'log_exception', lambda *args, **kwargs: reports.append(args[0]))
    db.conn().execute("CREATE TRIGGER refuse_counter BEFORE UPDATE ON heartbeat_state "
                      "BEGIN SELECT RAISE(ABORT,'counter unavailable'); END")
    _import(aid, ['HEARTBEAT_OK'])
    assert reports == ['heartbeatAccountingFail']
    assert _ledger(aid) == []
    assert _streak(aid) == 1
    db.conn().execute('DROP TRIGGER refuse_counter')
    _import(aid, ['HEARTBEAT_OK'])
    assert len(_ledger(aid)) == 1
    assert _streak(aid) == 2


@pytest.mark.parametrize('threshold', [1, 3])
def test_ended_noop_reaches_dormancy_and_activity_resets_it(monkeypatch, threshold):
    _activate(monkeypatch)
    settings_store.set_int(heartbeat.KEY_DORMANT_AFTER_NOOPS, threshold)
    aid = _turn(monkeypatch)
    for _ in range(threshold - 1):
        heartbeat.record_heartbeat_noop(aid)
    _end(aid)
    _import(aid, ['HEARTBEAT_OK'])
    assert _streak(aid) == threshold
    assert heartbeat._state_for(aid).dormant is True
    # A different completed turn with an actual result wakes the agent.
    agents.open_turn(agent_id=aid, source='pwa', trace_id='heartbeat-2')
    message_store.record_user_message(agent_id=aid, backend_session_id='b-1',
        client_msg_id='prompt-2', text=heartbeat.HEARTBEAT_PROMPT,
        origin='heartbeat', trace_id='heartbeat-2')
    turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED, {'trace_id': 'heartbeat-2'})
    turn_lifecycle.transition(aid, TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'heartbeat-2'})
    message_store.apply_final_assistant_side_effects(agent_id=aid, backend_session_id='b-1',
        trace_id='heartbeat-2', row={'id': 'terminal-2', 'text': 'Fixed the branch.'})
    assert _streak(aid) == 0
    assert heartbeat._state_for(aid).dormant is False


def test_concurrent_live_and_transcript_readings_count_the_turn_once(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    _end(aid)
    barrier = threading.Barrier(2)
    def read(writer):
        try:
            barrier.wait(timeout=5)
            if writer == 'live':
                _live(aid, 'HEARTBEAT_OK')
            else:
                _import(aid, ['HEARTBEAT_OK'])
        finally:
            db.close_local()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read, ['live', 'transcript']))
    assert _streak(aid) == 1
    assert [outcome for _, outcome in _ledger(aid)] == ['noop']


def test_an_outcome_without_a_host_settlement_is_not_authoritative(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    # The end contract requires the terminal stamp as well as its outcome;
    # a migrated/incomplete row cannot turn an imported remark into a verdict.
    db.conn().execute("UPDATE turns SET outcome='completed' WHERE agent_id=?", (aid,))
    _import(aid, ['HEARTBEAT_OK'])
    _live(aid, 'HEARTBEAT_OK')
    assert _ledger(aid) == []
    assert _streak(aid) == 0


@pytest.mark.parametrize('boundary', ['after_commit', 'before_revalidation'])
@pytest.mark.parametrize('writer_kind', ['full', 'tail'])
def test_a_turn_ending_after_a_partial_read_does_not_make_that_read_authoritative(monkeypatch, boundary, writer_kind):
    from lib import message_writes
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    carry = {}
    if writer_kind == 'tail':
        _import(aid, ['I will check the branch.'], capture_at=1, captured=carry)
    def store(answers):
        if writer_kind == 'full':
            return _import(aid, answers)
        return message_writes.store_transcript_tail(agent_id=aid, backend_session_id='b-1',
            source_file='/scratch/heartbeat.jsonl', start_seq=1, carry=carry,
            turns=[{'role': 'assistant', 'text': text, 'timestamp': _iso(2020 + n),
                    'trace_id': 'heartbeat-1'} for n, text in enumerate(answers)])
    account = message_writes._account_imported_heartbeats
    ended = []
    def settle_between_commit_and_accounting(writer, agent_id):
        if not ended:
            ended.append(True)
            _end(aid)
        account(writer, agent_id)
    if boundary == 'after_commit':
        monkeypatch.setattr(message_writes, '_account_imported_heartbeats',
                            settle_between_commit_and_accounting)
    else:
        revalidate = message_writes._revalidate_import_prelude
        def settle_before_revalidation(*args, **kwargs):
            if not ended:
                ended.append(True)
                _end(aid)
            return revalidate(*args, **kwargs)
        monkeypatch.setattr(message_writes, '_revalidate_import_prelude', settle_before_revalidation)
    # This read contains only a remark. A terminal callback arriving just
    # after its commit cannot turn that earlier snapshot into a final answer.
    store(['I will check the branch.', 'Looking closer.'])
    assert _ledger(aid) == []
    assert _streak(aid) == 0
    store(['I will check the branch.', 'Looking closer.', 'HEARTBEAT_OK'])
    assert [outcome for _, outcome in _ledger(aid)] == ['noop']
    assert _streak(aid) == 1


def test_successful_tool_only_turn_is_neither_activity_nor_noop(monkeypatch):
    _activate(monkeypatch)
    aid = _turn(monkeypatch)
    heartbeat.record_heartbeat_noop(aid)
    _end(aid)
    message_store.store_transcript_turns(agent_id=aid, backend_session_id='b-1',
        source_file='/scratch/heartbeat.jsonl', turns=[
            {'role': 'user', 'text': heartbeat.HEARTBEAT_PROMPT, 'timestamp': _iso(2010)},
            {'role': 'assistant', 'text': '', 'timestamp': _iso(2020),
             'tools': [{'id': 'synthetic-tool', 'name': 'Bash', 'status': 'ok', 'summary': 'synthetic'}]}])
    assert [outcome for _, outcome in _ledger(aid)] == ['neither']
    assert _streak(aid) == 1
