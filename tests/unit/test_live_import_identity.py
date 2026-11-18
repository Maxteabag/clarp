"""First-turn imports must retain the row a provider reply streamed into."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from lib import agents, backends, clock, db, message_store, transcript_incremental, turn_lifecycle
from lib import user_notifications
from lib.backend import claude
from tests.readmodel.rm_n14_harness import HOSTS_SWITCHES


CONFIGURATIONS = {
    'defaults': {},
    'carry-only': {'carry_live_rows': True},
    'carry-words': {'carry_live_rows': True, 'carry_live_rows_by_words': True},
    'hosts-nine': {**HOSTS_SWITCHES, 'carry_live_rows_by_words': True,
                   'tail_hashes_while_parsing': True},
}
BASE = 1_794_900_000_000


def _record(path, role, text, offset):
    record = {'type': role, 'uuid': f'{role}-{offset}',
              'timestamp': clock.iso_from_ms(BASE + offset),
              'message': {'role': role, 'model': 'synthetic-fixture',
                          'content': text if role == 'user' else [{'type': 'text', 'text': text}]}}
    with path.open('a') as stream:
        stream.write(json.dumps(record) + '\n')


def _opened_case(tmp_path, monkeypatch, configuration):
    now = [BASE]
    monkeypatch.setattr(time, 'time', lambda: now[0] / 1000)
    monkeypatch.setattr(transcript_incremental, 'settings',
                        lambda: {**transcript_incremental._DEFAULTS,
                                 **CONFIGURATIONS[configuration]})
    aid = agents.create_agent(persona='Synthetic', session='r19-case', voice_id='v',
                              cwd=str(tmp_path), backend='claude')
    now[0] = BASE + 10
    agents.bind_backend_session(aid, 'native-session')
    agents.open_turn(agent_id=aid, source='pwa', trace_id='turn-1')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROMPT_ADMITTED,
                              {'trace_id': 'turn-1', 'origin': 'user'})
    question = 'Synthetic question'
    now[0] = BASE + 100
    message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
                                      client_msg_id='r19-client', text=question,
                                      trace_id='turn-1')
    path = tmp_path / 'native.jsonl'
    _record(path, 'user', question, 100)
    return aid, path, now


@pytest.mark.parametrize('configuration', CONFIGURATIONS)
@pytest.mark.parametrize('streamed', [False, True], ids=['plain', 'streamed'])
@pytest.mark.parametrize('import_first', [False, True], ids=['live-first', 'import-first'])
def test_first_turn_keeps_live_identity_after_transcript_materialization(
        tmp_path, monkeypatch, configuration, streamed, import_first):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    answer = 'Synthetic complete answer.'
    _record(path, 'assistant', answer, 200)
    imported, emitted = threading.Event(), threading.Event()

    def import_once():
        return transcript_incremental.import_transcript(
            backend='claude', path=path, agent_id=aid, backend_session_id='native-session')

    def first_import():
        try:
            if not import_first:
                assert emitted.wait(5)
            now[0] = BASE + (300 if import_first else 450)
            return import_once()
        finally:
            db.close_local()
            imported.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_import)
        try:
            if import_first:
                assert imported.wait(5)
            now[0] = BASE + 400
            if streamed:
                message_store.upsert_live_assistant_message(
                    agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
                    item_key='provider-message-1', text=answer[:10])
            live = message_store.upsert_live_assistant_message(
                agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
                item_key='provider-message-1', text=answer)
            assert live is not None
            claude._settle_live_message(
                agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
                item_key='provider-message-1', session='r19-case', stream=None)
        finally:
            emitted.set()
        assert first.result(timeout=5)['apply_mode'] == 'full'
    now[0] = BASE + 500
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK,
                              {'trace_id': 'turn-1'})
    notification = user_notifications.classify_completed_turn(
        agent_id=aid, session='r19-case', persona='Synthetic', done_ts=now[0],
        backend_session_id='native-session', trace_id='turn-1', settle_timeout_s=0,
        wait_for_message=lambda select, timeout: select())
    assert notification['source_message_id'] == live['id']
    now[0] = BASE + 600
    import_once()
    rows = message_store.list_messages(agent_id=aid, backend_session_id='native-session')
    replies = [row for row in rows if row['role'] == 'assistant']
    assert [row['id'] for row in replies] == [live['id']]
    assert replies[0]['turn']['turn_id'] == 'turn-1'
    assert notification['source_message_id'] in {row['id'] for row in rows}
    message_store.list_messages(agent_id=aid, backend_session_id='native-session',
                                before_message_id=live['id'])
    assert message_store.has_client_message('r19-client')


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('shape', ['final-after-prefix-remark', 'prefix-after-prefix-remark',
                                   'late-live-prefix'])
def test_live_id_is_never_given_to_an_earlier_message_of_its_turn(
        tmp_path, monkeypatch, configuration, shape):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    final = 'Synthetic complete answer.'
    if shape != 'late-live-prefix':
        _record(path, 'assistant', 'Synthetic', 150)
    _record(path, 'assistant', final, 200)

    def imported():
        return transcript_incremental.import_transcript(
            backend='claude', path=path, agent_id=aid, backend_session_id='native-session')

    if shape == 'late-live-prefix':
        now[0] = BASE + 250
        imported()
    now[0] = BASE + 300
    partial = shape != 'final-after-prefix-remark'
    value = 'Synthetic' if partial else final
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='provider-final', text=value)
    if not partial:
        message_store.mark_live_message_final(
            agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
            item_key='provider-final')
    before = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?',
                                    (live['id'],)).fetchone())
    now[0] = BASE + 400
    imported()
    after = db.conn().execute('SELECT * FROM messages WHERE message_id=?',
                               (live['id'],)).fetchone()
    assert after is not None
    if shape == 'late-live-prefix':
        assert dict(after) == before
        message_store.upsert_live_assistant_message(
            agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
            item_key='provider-final', text=final)
        message_store.mark_live_message_final(
            agent_id=aid, backend_session_id='native-session', trace_id='turn-1', item_key='provider-final')
        imported()
        after = db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone()
        assert after['text'] == final
        assert after['source_file'] == str(path)
        assert after['seq'] == 1
    else:
        # Two compatible provider messages: decline without altering the
        # running turn's live row, rather than lending its id to a remark.
        assert dict(after) == before


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
def test_streaming_ambiguity_is_temporary_then_unique_final_adopts_its_live_row(
        tmp_path, monkeypatch, configuration):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    remark, final = 'Checking the first file.', 'Checking the second file.'
    _record(path, 'assistant', remark, 150)
    _record(path, 'assistant', final, 200)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='provider-final', text='Checking')
    before = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?',
                                    (live['id'],)).fetchone())

    def imported():
        transcript_incremental.import_transcript(
            backend='claude', path=path, agent_id=aid, backend_session_id='native-session')

    imported()
    kept = db.conn().execute('SELECT * FROM messages WHERE message_id=?',
                              (live['id'],)).fetchone()
    assert kept is not None and dict(kept) == before
    now[0] = BASE + 400
    updated = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='provider-final', text=final)
    assert updated['id'] == live['id']
    message_store.mark_live_message_final(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='provider-final')
    now[0] = BASE + 500
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK,
                              {'trace_id': 'turn-1'})
    imported()
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role']=='assistant']
    assert len(replies) == 2
    assert replies[-1]['id'] == live['id'] and replies[-1]['text'] == final
    assert replies[-1]['turn']['turn_id'] == 'turn-1'


def _import(aid, path):
    return transcript_incremental.import_transcript(
        backend='claude', path=path, agent_id=aid, backend_session_id='native-session')


def _full_replay(aid, path):
    return message_store.store_transcript_turns(
        agent_id=aid, backend_session_id='native-session', source_file=str(path),
        turns=backends.parse_turns('claude', path))


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('ending', ['PROCESS_EXITED_OK', 'PROCESS_EXITED_FAILED', 'STOP_REQUESTED'])
@pytest.mark.parametrize('unique', [False, True], ids=['identical-replies', 'unique-final'])
def test_decline_rebuilt_after_reopen_and_never_keeps_a_settled_twin(
        tmp_path, monkeypatch, configuration, ending, unique):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    texts = ['Checking file one.', 'Checking file two.'] if unique else ['Checking.', 'Checking.']
    for offset, text in enumerate(texts, 150):
        _record(path, 'assistant', text, offset)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text='Checking')
    _import(aid, path)
    assert db.conn().execute('SELECT 1 FROM messages WHERE message_id=?', (live['id'],)).fetchone()
    db.close_local()  # No in-memory decline state survives the next import.
    now[0] = BASE + 400
    message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text=texts[-1])
    message_store.mark_live_message_final(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1', item_key='opaque-final')
    turn_lifecycle.transition(aid, getattr(turn_lifecycle.TurnEvent, ending), {'trace_id': 'turn-1'})
    now[0] = BASE + 500
    _import(aid, path)
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant']
    assert [r['text'] for r in replies] == texts
    assert (replies[-1]['id'] == live['id']) is unique
    assert not any(r['id'] == live['id'] for r in replies[:-1])
    assert not db.conn().execute("SELECT 1 FROM messages WHERE source_file LIKE 'live:%'").fetchone()
    if unique:
        assert replies[-1]['turn']['turn_id'] == 'turn-1'
    snapshot = [dict(r) for r in db.conn().execute('SELECT * FROM messages ORDER BY message_id')]
    _full_replay(aid, path)
    replayed = [dict(r) for r in db.conn().execute('SELECT * FROM messages ORDER BY message_id')]
    if unique:
        assert replayed == snapshot
    else:
        # Existing cleanup removes the unresolved live twin and its summary;
        # a later full replay can stamp the transcript row from the turn index.
        assert [(r['message_id'], r['text']) for r in replayed] == [
            (r['message_id'], r['text']) for r in snapshot]


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('shape', ['different-text', 'other-turn', 'unknown-trace', 'exact-before-prefix'])
def test_conservative_live_matching_at_rest(tmp_path, monkeypatch, configuration, shape):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    answer = 'Synthetic complete answer.'
    _record(path, 'assistant', answer, 200)
    now[0] = BASE + 300
    trace = 'another-turn' if shape == 'other-turn' else 'turn-1'
    if shape == 'exact-before-prefix':
        message_store.upsert_live_assistant_message(
            agent_id=aid, backend_session_id='native-session', trace_id=trace,
            item_key='older-prefix', text='Synthetic')
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text='Unrelated terminal payload' if shape == 'different-text' else answer)
    if shape in ('other-turn', 'unknown-trace'):
        db.conn().execute('UPDATE messages SET trace_id=? WHERE message_id=?',
                          (trace if shape == 'other-turn' else None, live['id']))
    if shape == 'different-text':
        message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
            trace_id='turn-1', item_key='opaque-final')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    _import(aid, path)
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant']
    materialized = [r for r in replies if r['text'] == answer and r['kind'] != 'live']
    assert len(materialized) == 1
    assert (materialized[0]['id'] == live['id']) is (shape == 'exact-before-prefix')
    # Unrelated and foreign/unknown-trace rows follow the existing cleanup policy.
    if shape != 'exact-before-prefix':
        kept = db.conn().execute('SELECT text, source_file FROM messages WHERE message_id=?', (live['id'],)).fetchone()
        assert kept is not None and kept['source_file'].startswith('live:')


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
def test_adoption_replaces_placeholder_in_client_delta_and_survives_replays(
        tmp_path, monkeypatch, configuration):
    from lib import clip_store, conversation, message_audio, revisioned_cache, tts_queue
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    answer = '<speak>Synthetic complete answer.</speak>'
    _record(path, 'assistant', answer, 200)
    _import(aid, path)
    before = conversation.load_conversation(session='r19-case', background_import=False)
    placeholder = next(r['id'] for r in before['turns'] if r['role'] == 'assistant')
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text=answer)
    message_store.mark_live_message_final(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1', item_key='opaque-final')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    audio = tmp_path / 'speech.mp3'
    audio.write_bytes(b'synthetic retained bytes')
    clip = clip_store.record_clip(agent_id=aid, path=str(audio), trace_id='turn-1', runtime_id=lambda _: None)
    queue = tts_queue.enqueue(agent_id=aid, session='r19-case', text='Synthetic complete answer.',
                              voice_id='v', source='pwa', trace_id='turn-1')
    tts_queue.mark_done(queue, clip_id=clip)
    clip_store.mark_clip_status(clip_id=clip, status='play-ok')
    now[0] = BASE + 400
    _import(aid, path)
    delta = conversation.load_conversation(session='r19-case', after_revision=before['latest_revision'],
                                           background_import=False)
    assert delta['replace_required']
    assert placeholder not in {r['id'] for r in delta['turns']}
    assert live['id'] in {r['id'] for r in delta['turns']}
    assert len(message_audio.retained_events(session='r19-case', message_id=live['id'], audio_dir=tmp_path)) == 1
    baseline = [dict(r) for r in db.conn().execute('SELECT * FROM messages ORDER BY message_id')]
    _import(aid, path)  # second tail
    _full_replay(aid, path)  # no frontier
    db.close_local()
    revisioned_cache.reset_all()
    _full_replay(aid, path)  # reopened DB and empty process caches
    db.conn().execute('UPDATE transcript_import_checkpoints SET imports_since_audit=1000000, audited_at=0')
    _import(aid, path)  # due audit
    assert [dict(r) for r in db.conn().execute('SELECT * FROM messages ORDER BY message_id')] == baseline
    assert conversation.load_conversation(session='r19-case', before_message_id=live['id'], background_import=False)


def test_unknown_reply_trace_does_not_query_adopt_or_diagnose(monkeypatch):
    from lib import message_import_rows as rows
    diagnoses = []
    monkeypatch.setattr(rows, '_diagnose', lambda *args: diagnoses.append(args))
    # These sentinels have no database/cache methods: unknown identity must
    # return before accessing either owner, as it does on main.
    assert rows._streamed_twin(object(), 'agent', 'session', 'answer', exclude=set(),
                               trace='', final_rows=object(), seq=1) == ''
    assert diagnoses == []


def test_decline_guard_exception_deletes_by_default_and_logs_once_without_content(monkeypatch):
    from types import SimpleNamespace
    from lib import message_import_rows as rows
    diagnoses = []
    monkeypatch.setattr(rows, '_diagnose', lambda *args: diagnoses.append(args))
    final_rows = rows._FinalRows(object(), 'agent', 'session',
        turns=[{'role': 'user'}, {'role': 'assistant', 'text': 'Sensitive answer one'},
               {'role': 'assistant', 'text': 'Sensitive answer two'}],
        turn_index=SimpleNamespace(_rows=[{'trace_id': 'private-trace'}]))
    candidate = {'message_id': 'private-live', 'visible': 'Sensitive'}
    assert not final_rows.unique_reply(candidate, 2, 'private-trace')
    live = {'message_id': 'private-live', 'text': 'Sensitive', 'trace_id': 'private-trace'}
    # A malformed turn index must never retain a live twin or fail the import.
    assert final_rows.keep_declined_live(live) is False
    assert final_rows.keep_declined_live(live) is False
    assert diagnoses == [('transcriptLiveDeclineGuardFail', 'reason=KeyError count=1')]


@pytest.mark.parametrize('foreign', ['agent', 'session'])
def test_same_trace_and_words_cannot_adopt_a_foreign_identity(tmp_path, monkeypatch, foreign):
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'defaults')
    answer = 'Synthetic complete answer.'
    _record(path, 'assistant', answer, 200)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='foreign-provider-id', text=answer)
    column = 'agent_id' if foreign == 'agent' else 'backend_session_id'
    db.conn().execute(f'UPDATE messages SET {column}=? WHERE message_id=?',
                      ('foreign-owner', live['id']))
    before = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone())
    _import(aid, path)
    after = db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone()
    assert dict(after) == before
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant']
    assert len(replies) == 1 and replies[0]['id'] != live['id'] and replies[0]['text'] == answer


@pytest.mark.parametrize('settled_at,outcome', [(10, None), (None, 'completed')])
def test_partial_terminal_evidence_never_retains_an_ambiguous_twin(settled_at, outcome):
    from types import SimpleNamespace
    from lib import message_import_rows as rows
    final_rows = rows._FinalRows(object(), 'agent', 'session',
        turns=[{'role': 'user'}, {'role': 'assistant', 'text': 'reply one'},
               {'role': 'assistant', 'text': 'reply two'}],
        turn_index=SimpleNamespace(_rows=[{'trace_id': 'trace', 'settled_at': settled_at,
                                          'outcome': outcome}]))
    assert not final_rows.unique_reply({'message_id': 'live', 'visible': 'reply'}, 2, 'trace')
    assert not final_rows.keep_declined_live({'message_id': 'live', 'trace_id': 'trace', 'text': 'reply'})


def test_live_matching_work_is_bounded_by_its_trace(tmp_path, monkeypatch):
    from lib import message_import_rows as rows
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'defaults')
    answer = 'Synthetic complete answer.'
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text=answer)
    database = db.conn()
    database.executemany(
        'INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,text,trace_id,updated_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?)',
        [(f'history-{n}', aid, 'native-session', '/synthetic/history', n+100,
          'assistant', 'Unrelated old history', f'old-{n}', 1, 1) for n in range(1000)])
    final_rows = rows._FinalRows(database, aid, 'native-session',
        turns=[{'role': 'user'}, {'role': 'assistant', 'text': answer}])
    steps = [0]
    def tick():
        steps[0] += 1
        return 0
    database.set_progress_handler(tick, 1)
    try:
        matched = rows._streamed_twin(database, aid, 'native-session', answer,
            exclude=set(), trace='turn-1', final_rows=final_rows, seq=1)
    finally:
        database.set_progress_handler(None, 0)
    assert matched == live['id']
    assert steps[0] < 200, f'matching one trace walked unrelated history: {steps[0]} VM steps'


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
def test_three_provider_messages_adopt_only_the_unique_last_reply(tmp_path, monkeypatch, configuration):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    texts = ['Reading the first file.', 'Comparing the second file.', 'Synthetic complete answer.']
    for offset, text in enumerate(texts, 200):
        _record(path, 'assistant', text, offset)
    _import(aid, path)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final-not-an-ordinal', text=texts[-1])
    message_store.mark_live_message_final(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final-not-an-ordinal')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    _import(aid, path)
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant']
    assert [r['text'] for r in replies] == texts
    assert replies[-1]['id'] == live['id']
    assert all(r['id'] != live['id'] for r in replies[:-1])


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('shape', ['streaming-exact', 'streaming-prefix', 'final-prefix',
                                   'two-final-exact', 'final-exact'])
def test_occupied_slot_requires_one_final_exact_candidate(tmp_path, monkeypatch, configuration, shape):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    answer = 'Synthetic complete answer.'
    _record(path, 'assistant', answer, 200)
    _import(aid, path)
    placeholder = next(r['id'] for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant')
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text='Synthetic' if 'prefix' in shape else answer)
    if shape.startswith('final') or shape == 'two-final-exact':
        message_store.mark_live_message_final(
            agent_id=aid, backend_session_id='native-session', trace_id='turn-1', item_key='opaque-final')
    if shape == 'two-final-exact':
        message_store.upsert_live_assistant_message(
            agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
            item_key='second-final', text=answer)
        message_store.mark_live_message_final(
            agent_id=aid, backend_session_id='native-session', trace_id='turn-1', item_key='second-final')
    before = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone())
    _import(aid, path)
    current = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone())
    if shape == 'final-exact':
        assert current['source_file'] == str(path) and current['text'] == answer
        assert not db.conn().execute('SELECT 1 FROM messages WHERE message_id=?', (placeholder,)).fetchone()
    else:
        assert current == before
        assert db.conn().execute('SELECT 1 FROM messages WHERE message_id=?', (placeholder,)).fetchone()


def test_unoccupied_ambiguous_earlier_replies_do_not_keep_a_live_row(tmp_path, monkeypatch):
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'defaults')
    replies = ['Checking the first file.', 'Checking the second file.', 'The final result.']
    for index, text in enumerate(replies):
        _record(path, 'assistant', text, 150 + 20 * index)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='earlier-message', text='Checking the')

    _import(aid, path)
    rows = [r for r in message_store.list_messages(agent_id=aid, backend_session_id='native-session')
            if r['role'] == 'assistant']
    assert [r['text'] for r in rows] == replies
    assert live['id'] not in {r['id'] for r in rows}


@pytest.mark.parametrize('finalized', [False, True])
def test_earlier_reply_of_same_trace_keeps_main_cleanup(tmp_path, monkeypatch, finalized):
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'hosts-nine')
    old, later = 'Synthetic older reply.', 'A later reply of this trace.'
    _record(path, 'assistant', old, 200)
    _record(path, 'assistant', later, 220)
    _import(aid, path)
    before = [r['id'] for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant']
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='new-or-stale-provider-message', text=old if finalized else old[:12])
    if finalized:
        message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
            trace_id='turn-1', item_key='new-or-stale-provider-message')
    _import(aid, path)
    assert not db.conn().execute('SELECT 1 FROM messages WHERE message_id=?', (live['id'],)).fetchone()
    assert [r['id'] for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant'] == before


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
def test_identical_replies_across_steered_inputs_of_one_trace_decline(tmp_path, monkeypatch, configuration):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    answer = 'The same visible answer.'
    _record(path, 'assistant', answer, 200)
    now[0] = BASE + 240
    message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
        client_msg_id='steered-client', text='A steered input', trace_id='turn-1')
    _record(path, 'user', 'A steered input', 250)
    _record(path, 'assistant', answer, 280)
    _import(aid, path)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text=answer)
    message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
        trace_id='turn-1', item_key='opaque-final')
    before = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone())
    _import(aid, path)
    assert dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone()) == before
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    _import(aid, path)
    assert not db.conn().execute('SELECT 1 FROM messages WHERE message_id=?', (live['id'],)).fetchone()
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role'] == 'assistant']
    assert [r['text'] for r in replies] == [answer, answer]


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('timestamp_gap', ['within-index', 'beyond-index'])
def test_unoccupied_ambiguity_spans_steered_inputs_of_same_trace(tmp_path, monkeypatch, configuration, timestamp_gap):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    first, final = 'Checking the first file.', 'Checking the second file.'
    _record(path, 'assistant', first, 200)
    from lib.message_turns import _UNSETTLED_REACH_MS
    late = 250 if timestamp_gap == 'within-index' else _UNSETTLED_REACH_MS + 100000
    now[0] = BASE + late
    message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
        client_msg_id='steered-client', text='A steered input', trace_id='turn-1')
    _record(path, 'user', 'A steered input', late)
    _record(path, 'assistant', final, late+10)
    now[0] = BASE + late+20
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text='Checking')
    before = dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone())
    _import(aid, path)
    assert dict(db.conn().execute('SELECT * FROM messages WHERE message_id=?', (live['id'],)).fetchone()) == before
    message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text=final)
    message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
        trace_id='turn-1', item_key='opaque-final')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    _import(aid, path)
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role']=='assistant']
    assert [r['text'] for r in replies] == [first, final]
    assert replies[-1]['id'] == live['id'] and replies[0]['id'] != live['id']


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('earlier_trace', ['turn-1', 'earlier-turn'])
def test_unoccupied_unique_reply_keeps_authored_trace_beyond_time_index_reach(tmp_path, monkeypatch, configuration, earlier_trace):
    from lib.message_turns import _UNSETTLED_REACH_MS
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 10**9)
    monkeypatch.setattr(message_store, 'IMPORT_WRITE_BUDGET_SECONDS', 10**6)
    if earlier_trace != 'turn-1':
        first_user = next(r for r in message_store.list_messages(
            agent_id=aid, backend_session_id='native-session') if r['role']=='user')
        db.conn().execute('UPDATE messages SET trace_id=? WHERE message_id=?',
                          ('earlier-turn', first_user['id']))
        db.conn().execute('INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) VALUES(?,?,?,?,?,?,?)',
            (aid, 'pwa', 'earlier-turn', BASE+50, BASE+500, BASE+500, 'completed'))
    first, final = 'An earlier unrelated answer.', 'A distinct later answer.'
    _record(path, 'assistant', first, 200)
    late = _UNSETTLED_REACH_MS + 100000
    now[0] = BASE + late
    message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
        client_msg_id='late-steered-client', text='A later steered input', trace_id='turn-1')
    _record(path, 'user', 'A later steered input', late)
    _record(path, 'assistant', final, late + 10)
    now[0] = BASE + late + 20
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='opaque-final', text='A distinct')
    _import(aid, path)
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role']=='assistant']
    assert [r['text'] for r in replies] == [first, final]
    assert replies[-1]['id'] == live['id']


def test_rich_history_replay_select_budget(tmp_path, monkeypatch):
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'defaults')
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 10**9)
    monkeypatch.setattr(message_store, 'IMPORT_WRITE_BUDGET_SECONDS', 10**6)
    path.write_text('')
    for n in range(500):
        offset = -20_000_000 + n*20000
        trace = f'history-trace-{n}'
        db.conn().execute('INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) VALUES(?,?,?,?,?,?,?)',
            (aid, 'pwa', trace, BASE+offset, BASE+offset+700, BASE+offset+700, 'completed'))
        now[0] = BASE+offset
        question = f'Historical question {n}'
        message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
            client_msg_id=f'history-client-{n}', text=question, trace_id=trace)
        _record(path, 'user', question, offset)
        _record(path, 'assistant', f'Historical answer {n}', offset+500)
    _record(path, 'user', 'Synthetic question', 100)
    _record(path, 'assistant', 'The current completed answer.', 200)
    now[0] = BASE+300
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    _import(aid, path)
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        _full_replay(aid, path)
    finally:
        db.conn().set_trace_callback(None)
    selects = sum(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    # Main's six reads per traced reply plus fixed prelude/summary overhead;
    # one matching read family must not become one lookup per placeholder.
    assert selects <= 6*501+24, selects


def test_batch_candidates_keep_equal_text_in_different_traces_separate(tmp_path, monkeypatch):
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'hosts-nine')
    now[0] = BASE-10000
    message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
        client_msg_id='cancelled-client', text='Synthetic question', trace_id='cancelled-trace')
    now[0] = BASE+100
    answer = 'The same words in different turns.'
    _record(path, 'assistant', answer, 200)
    _import(aid, path)
    now[0] = BASE+300
    first = message_store.upsert_live_assistant_message(agent_id=aid,
        backend_session_id='native-session', trace_id='turn-1', item_key='one', text=answer)
    message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
        trace_id='turn-1', item_key='one')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    now[0] = BASE+10000
    agents.open_turn(agent_id=aid, source='pwa', trace_id='turn-2')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROMPT_ADMITTED, {'trace_id': 'turn-2'})
    now[0] = BASE+10010
    message_store.record_user_message(agent_id=aid, backend_session_id='native-session',
        client_msg_id='second-client', text='Synthetic question', trace_id='turn-2')
    _record(path, 'user', 'Synthetic question', 10010)
    _record(path, 'assistant', answer, 10020)
    now[0] = BASE+10300
    second = message_store.upsert_live_assistant_message(agent_id=aid,
        backend_session_id='native-session', trace_id='turn-2', item_key='two', text=answer)
    message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
        trace_id='turn-2', item_key='two')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-2'})
    _full_replay(aid, path)
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role']=='assistant']
    assert [r['id'] for r in replies] == [first['id'], second['id']]
    assert [r['turn']['turn_id'] for r in replies] == ['turn-1', 'turn-2']


def test_tail_matching_read_refreshes_after_writer_release(tmp_path, monkeypatch):
    from lib import message_writes
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'hosts-nine')
    _import(aid, path)
    _record(path, 'assistant', 'A distinct remark.', 200)
    _record(path, 'assistant', 'The final answer.', 220)
    now[0] = BASE+300
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 1)
    monkeypatch.setattr(message_store, 'IMPORT_WRITE_BUDGET_SECONDS', 10**6)
    original = message_writes._ImportWriter.yield_writer
    inserted = []

    def foreign_final():
        try:
            row = message_store.upsert_live_assistant_message(agent_id=aid,
                backend_session_id='native-session', trace_id='turn-1',
                item_key='last-provider-message', text='The final answer.')
            message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
                trace_id='turn-1', item_key='last-provider-message')
            return row['id']
        finally:
            db.close_local()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def released(writer):
            if writer.seq == 2 and not inserted:
                assert isinstance(writer, message_writes._TailWriter)
                assert writer.began_at is None
                assert db.conn().execute('SELECT 1 FROM messages WHERE source_file=? AND seq=1', (str(path),)).fetchone()
                inserted.append(pool.submit(foreign_final).result(timeout=5))
            return original(writer)
        monkeypatch.setattr(message_writes._ImportWriter, 'yield_writer', released)
        _import(aid, path)
    assert len(inserted) == 1
    replies = [r for r in message_store.list_messages(
        agent_id=aid, backend_session_id='native-session') if r['role']=='assistant']
    assert [r['text'] for r in replies] == ['A distinct remark.', 'The final answer.']
    assert replies[-1]['id'] == inserted[0]


def test_matching_family_chunks_at_equality_and_preserves_candidate_order():
    from lib import message_import_rows, message_writes
    connection = db.conn()
    for ident, trace, stamp, seq in [('cache-later','trace-0','2026-11-17T00:00:00Z',-2),
                                    ('cache-oldest','trace-0',None,-1),
                                    ('cache-32','trace-32',None,-32),
                                    ('cache-64','trace-64',None,-64)]:
        connection.execute('INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,timestamp,text,updated_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ident, 'cache-agent', 'cache-session', f'live:{trace}', seq, 'assistant', stamp, 'reply', 1, 1))
        connection.execute('UPDATE messages SET trace_id=? WHERE message_id=?', (trace, ident))
    turns = [turn for n in range(65) for turn in (
        {'role':'user','trace_id':f'trace-{n}','text':'question'},
        {'role':'assistant','text':'reply'})]
    turns += [{'role':'user','text':'padding'} for _ in range(196)]
    writer = message_writes._ImportWriter(connection, locked=True)
    cache = message_import_rows._FinalRows(writer, 'cache-agent', 'cache-session', turns=turns)
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        assert message_import_rows._streamed_twin(writer, 'cache-agent', 'cache-session', 'reply',
            trace='trace-0', seq=1, final_rows=cache, exclude=set()) == 'cache-oldest'
        assert [r['message_id'] for r in cache.live_rows('trace-32')] == ['cache-32']
        assert [r['message_id'] for r in cache.live_rows('trace-64')] == ['cache-64']
        cache.discard(('cache-oldest',))
        assert message_import_rows._streamed_twin(writer, 'cache-agent', 'cache-session', 'reply',
            trace='trace-0', seq=1, final_rows=cache, exclude=set()) == 'cache-later'
    finally:
        connection.set_trace_callback(None)
        writer.finish()
    queries = [sql for sql in statements if "source_file LIKE 'live:%'" in sql]
    assert len(queries) == 3  # 65 distinct traces at total_turns//5 ==65; 32 per chunk.
    assert all(any('idx_messages_trace' in row[3] for row in connection.execute(
        'EXPLAIN QUERY PLAN '+sql)) for sql in queries)


def test_effective_visible_reply_counts_toward_trace_ambiguity():
    from lib import message_import_rows
    connection = db.conn()
    connection.execute('INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,text,kind,trace_id,updated_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('provider-row', 'visible-agent', 'visible-session', 'live:trace', -1,
         'assistant', 'shared reply', 'live', 'trace', 1, 1))
    cache = message_import_rows._FinalRows(connection, 'visible-agent', 'visible-session',
        turns=[{'role':'user','trace_id':'trace'},
               {'role':'assistant','text':'shared reply'},
               {'role':'assistant','text':'raw text before origin processing'}])
    # The write loop passes effective text after origin/hidden-block handling.
    assert message_import_rows._streamed_twin(connection, 'visible-agent', 'visible-session',
        'shared reply', visible='shared reply', trace='trace', seq=2,
        final_rows=cache, exclude=set()) == ''


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('import_first', [False, True], ids=['live-first', 'import-first'])
@pytest.mark.parametrize('message_count', [2, 3])
@pytest.mark.parametrize('live_shape', ['all', 'remark-only', 'final-prefix'])
@pytest.mark.parametrize('ending', ['PROCESS_EXITED_OK', 'PROCESS_EXITED_FAILED'])
def test_cleanup_preserves_summary_on_surviving_final_reply(
        tmp_path, monkeypatch, configuration, import_first, message_count, live_shape, ending):
    from lib import revisioned_cache
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    words = ['A distinct first remark.', 'An independent second remark.', 'The final complete reply.']
    words = words if message_count == 3 else [words[0], words[-1]]
    for index, text in enumerate(words):
        _record(path, 'assistant', text, 150 + index * 20)
    if import_first:
        now[0] = BASE + 250
        _import(aid, path)
    live_ids = []
    supplied = words[:-1] if live_shape == 'remark-only' else words
    for index, text in enumerate(supplied):
        now[0] = BASE + 300 + index * 20
        if live_shape == 'final-prefix' and index == len(words) - 1:
            text = 'The final complete'
        key = f'provider-{index}'
        row = message_store.upsert_live_assistant_message(agent_id=aid,
            backend_session_id='native-session', trace_id='turn-1', item_key=key, text=text)
        live_ids.append(row['id'])
        message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
                                              trace_id='turn-1', item_key=key)
    now[0] = BASE + 400
    turn_lifecycle.transition(aid, getattr(turn_lifecycle.TurnEvent, ending), {'trace_id': 'turn-1'})
    now[0] = BASE + 500
    _import(aid, path)
    replies = [r for r in message_store.list_messages(agent_id=aid,
        backend_session_id='native-session') if r['role'] == 'assistant']
    assert [r['text'] for r in replies] == words
    assert [r['id'] for r in replies if r.get('turn')] == [replies[-1]['id']]
    assert replies[-1]['turn']['turn_id'] == 'turn-1'
    if live_shape != 'remark-only':
        adopted = not import_first or live_shape == 'all'
        assert (replies[-1]['id'] == live_ids[-1]) is adopted
    def snapshot():
        return {table: [tuple(r) for r in db.conn().execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                for table in ('messages', 'conversation_heads', 'message_clock')}
    baseline = snapshot()
    _import(aid, path)
    assert snapshot() == baseline
    _full_replay(aid, path)
    assert snapshot() == baseline
    db.close_local()
    revisioned_cache.reset_all()
    _full_replay(aid, path)
    assert snapshot() == baseline


@pytest.mark.parametrize('fault', ['yield', 'raise', 'yield-raise'])
@pytest.mark.parametrize('recovery', ['full', 'tail'])
def test_summary_recovers_after_cleanup_writer_window(tmp_path, monkeypatch, fault, recovery):
    from lib import message_writes
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 0)
    monkeypatch.setattr(message_store, 'IMPORT_WRITE_BUDGET_SECONDS', 10**6)
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'hosts-nine')
    for text, offset in [('A distinct remark.', 150), ('The final reply.', 200)]:
        _record(path, 'assistant', text, offset)
    now[0] = BASE + 250
    _import(aid, path)
    live_ids = []
    for index, text in enumerate(('A distinct remark.', 'The final reply.')):
        now[0] = BASE + 300 + index * 50
        key = f'provider-{index}'
        live_ids.append(message_store.upsert_live_assistant_message(agent_id=aid,
            backend_session_id='native-session', trace_id='turn-1', item_key=key, text=text)['id'])
        message_store.mark_live_message_final(agent_id=aid, backend_session_id='native-session',
            trace_id='turn-1', item_key=key)
    now[0] = BASE + 400
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    now[0] = BASE + 500
    execute = message_writes._ImportWriter.execute
    injected = []
    captured = {}
    turns = backends.parse_turns('claude', path)

    def interrupted(writer, sql, parameters=()):
        result = execute(writer, sql, parameters)
        if (writer.locked and sql.strip().startswith('DELETE FROM messages')
                and parameters == (live_ids[0],) and not injected):
            assert isinstance(writer, message_writes._TailWriter)
            injected.append(True)
            if 'yield' in fault:
                writer.commit()
                assert not db.conn().in_transaction
                writer.yield_writer()
            if 'raise' in fault:
                raise RuntimeError('synthetic cleanup interruption')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(message_writes._ImportWriter, 'execute', interrupted)
        def imported_tail():
            message_writes.store_transcript_tail(agent_id=aid, backend_session_id='native-session',
                source_file=str(path), turns=turns, start_seq=0, carry=None,
                capture_at=len(turns), captured=captured, yield_after_first_write=True)
        if 'raise' in fault:
            with pytest.raises(RuntimeError, match='synthetic cleanup interruption'):
                imported_tail()
        else:
            imported_tail()
    assert injected == [True]
    rows = db.conn().execute("SELECT turn_json FROM messages WHERE role='assistant'").fetchall()
    expected = (2, 0) if fault == 'yield-raise' else (4, 1) if fault == 'raise' else (2, 1)
    assert (len(rows), sum(r['turn_json'] is not None for r in rows)) == expected
    if recovery == 'full':
        _full_replay(aid, path)
    else:
        # An admitted tail carries this trace as newest even with no new rows.
        # A fast no-op which never calls the writer cannot repair a missing stamp.
        assert captured['newest_trace'] == 'turn-1'
        message_writes.store_transcript_tail(agent_id=aid, backend_session_id='native-session',
            source_file=str(path), turns=[], start_seq=len(turns), carry=captured,
            yield_after_first_write=True)
    replies = [r for r in message_store.list_messages(agent_id=aid,
        backend_session_id='native-session') if r['role'] == 'assistant']
    if fault == 'raise' and recovery == 'tail':
        # Rollback kept the leftover rows; an empty tail does not clean old replies.
        # The full import is needed to finish both identity adoption and cleanup.
        _full_replay(aid, path)
        replies = [r for r in message_store.list_messages(agent_id=aid,
            backend_session_id='native-session') if r['role'] == 'assistant']
    assert [r['text'] for r in replies] == ['A distinct remark.', 'The final reply.']
    assert replies[-1]['id'] == live_ids[-1]
    assert [r['id'] for r in replies if r.get('turn')] == [live_ids[-1]]


@pytest.mark.parametrize('prefix_records', [7, 12], ids=['live-final-survives', 'placeholder-final'])
def test_settled_tool_turn_keeps_summary_after_carry_input_full_import(
        tmp_path, monkeypatch, prefix_records):
    from lib import log as log_module
    aid, path, now = _opened_case(tmp_path, monkeypatch, 'defaults')
    first, remark, final = 'A distinct first remark.', 'Another independent remark.', 'The final tool reply.'
    records = [{'type': 'user', 'message': {'role': 'user', 'content': 'Synthetic question'}}]
    groups = [(first, ['Read']), ('', ['Read', 'Grep']), (remark, ['Edit']),
              ('', ['Bash']), ('', ['mcp__synthetic__lookup'])]
    for index, (text, names) in enumerate(groups):
        blocks = ([{'type': 'text', 'text': text}] if text else []) + [
            {'type': 'tool_use', 'id': f'tool-{index}-{n}', 'name': name, 'input': {}}
            for n, name in enumerate(names)]
        records.append({'type': 'assistant', 'message': {'role': 'assistant',
            'id': f'provider-{index}', 'content': blocks}})
        records.append({'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': f'tool-{index}-{n}',
             'content': 'Synthetic result', 'is_error': False} for n in range(len(names))]}})
    records.append({'type': 'assistant', 'message': {'role': 'assistant',
        'id': 'provider-final', 'content': [{'type': 'text', 'text': final}]}})
    for index, record in enumerate(records):
        record.update(uuid=f'record-{index}', timestamp=clock.iso_from_ms(
            BASE + (100 if index == 0 else 200 if index == 11 else 150 + index)))
    path.write_text(''.join(json.dumps(r) + '\n' for r in records[:prefix_records]))

    def provider_message(key, text, offset):
        now[0] = BASE + offset
        claude._store_live_partial(agent_id=aid, backend_session_id='native-session',
            trace_id='turn-1', item_key=key, text=text, session='r19-case', stream=None,
            provider_genuine=True)
        claude._settle_live_message(agent_id=aid, backend_session_id='native-session',
            trace_id='turn-1', item_key=key, session='r19-case', stream=None)
        return db.conn().execute("SELECT message_id FROM messages WHERE text=? "
                                 "AND source_file LIKE 'live:%'", (text,)).fetchone()[0]

    provider_message('provider-0', first, 230)
    now[0] = BASE + 250
    _import(aid, path)  # running-turn frontier, complete tools before its boundary
    with path.open('a') as handle:
        handle.write(''.join(json.dumps(r) + '\n' for r in records[prefix_records:]))
    stale_remark = provider_message('provider-2', remark, 300)
    live_final = provider_message('provider-final', final, 350)
    now[0] = BASE + 400
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    notification = user_notifications.classify_completed_turn(agent_id=aid, session='r19-case',
        persona='Synthetic', done_ts=BASE+400, backend_session_id='native-session',
        trace_id='turn-1', settle_timeout_s=0, wait_for_message=lambda select, timeout: select())
    assert notification['source_message_id'] == live_final
    events = []
    original_log = log_module.log
    def capture(event, detail='', **kwargs):
        events.append((event, detail))
        return original_log(event, detail, **kwargs)
    monkeypatch.setattr(log_module, 'log', capture)
    now[0] = BASE + 500
    _import(aid, path)
    assert ('transcriptCarryInputsChanged', 'what=row_live,turn_of_old_row') in events
    assert any(event == 'transcriptTailDecision' and 'code=full_carry_inputs_changed ' in detail
               for event, detail in events)
    replies = [r for r in message_store.list_messages(agent_id=aid,
        backend_session_id='native-session') if r['role'] == 'assistant']
    assert len(replies) == 6
    assert replies[-1]['id'] == live_final
    assert replies[-1]['text'] == final
    assert [r['id'] for r in replies if r.get('turn')] == [live_final]
    assert replies[-1]['turn']['turn_id'] == 'turn-1'
    assert not db.conn().execute('SELECT 1 FROM messages WHERE message_id=?', (stale_remark,)).fetchone()


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('settle_in_gap', [False, True])
def test_cleanup_checks_current_turn_after_publishing_reply(
        tmp_path, monkeypatch, configuration, settle_in_gap):
    from lib import message_writes

    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    _record(path, 'assistant', 'The parcel arrived.', 200)
    _import(aid, path)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(agent_id=aid,
        backend_session_id='native-session', trace_id='turn-1', item_key='parcel', text='The parcel')
    revised = 'The parcel arrived this morning.'
    path.write_bytes(path.read_bytes().replace(b'The parcel arrived.', revised.encode()))
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 1)
    monkeypatch.setattr(message_store, 'IMPORT_WRITE_BUDGET_SECONDS', 0)
    release = message_writes._ImportWriter.yield_writer
    published = []

    def observe_committed_reply():
        try:
            if not db.conn().execute('SELECT 1 FROM messages WHERE source_file=? AND text=?',
                                     (str(path), revised)).fetchone():
                return False
            assert db.conn().execute('SELECT 1 FROM messages WHERE message_id=?',
                                     (live['id'],)).fetchone()
            if settle_in_gap:
                turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK,
                                          {'trace_id': 'turn-1'})
            return True
        finally:
            db.close_local()

    with ThreadPoolExecutor(max_workers=1) as peer:
        def released(writer):
            assert not db.conn().in_transaction
            if not published and peer.submit(observe_committed_reply).result(timeout=5):
                published.append(True)
            return release(writer)
        monkeypatch.setattr(message_writes._ImportWriter, 'yield_writer', released)
        _full_replay(aid, path)
    assert published == [True]
    assert bool(db.conn().execute('SELECT 1 FROM messages WHERE message_id=?',
                                  (live['id'],)).fetchone()) is (not settle_in_gap)


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
@pytest.mark.parametrize('occupied', [False, True])
def test_one_terminal_prompt_equal_replies_keep_each_traces_live_id(
        tmp_path, monkeypatch, configuration, occupied):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    path.write_text('')
    _record(path, 'user', 'Prompt from the terminal', 100)
    answer = 'The check passed.'
    _record(path, 'assistant', answer, 200)
    def finalized(trace):
        key = dict(agent_id=aid, backend_session_id='native-session', trace_id=trace, item_key='final')
        row = message_store.upsert_live_assistant_message(**key, text=answer)
        message_store.mark_live_message_final(**key)
        return (trace, row['id'])

    if occupied:
        _full_replay(aid, path)
    now[0] = BASE + 350
    expected = [finalized('turn-1')]
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-1'})
    now[0] = BASE + 30000
    agents.open_turn(agent_id=aid, source='pwa', trace_id='turn-2')
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROMPT_ADMITTED, {'trace_id': 'turn-2'})
    _record(path, 'assistant', answer, 30200)
    if occupied:
        _full_replay(aid, path)
    now[0] = BASE + 30350
    expected.append(finalized('turn-2'))
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': 'turn-2'})
    _import(aid, path)
    rows = db.conn().execute("SELECT trace_id,message_id FROM messages "
                             "WHERE role='assistant' ORDER BY seq").fetchall()
    assert [tuple(row) for row in rows] == expected


@pytest.mark.parametrize('configuration', ['defaults', 'hosts-nine'])
def test_untraced_reply_can_veto_a_match_but_cannot_take_the_live_id(
        tmp_path, monkeypatch, configuration):
    aid, path, now = _opened_case(tmp_path, monkeypatch, configuration)
    path.write_text('')
    _record(path, 'user', 'An older terminal prompt', -60000)
    answer = 'Same output.'
    _record(path, 'assistant', answer, -59000)  # before the Host turn index begins
    _record(path, 'assistant', answer, 200)
    now[0] = BASE + 300
    live = message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='native-session', trace_id='turn-1',
        item_key='current', text=answer)
    _import(aid, path)

    rows = db.conn().execute("SELECT message_id, trace_id FROM messages "
                             "WHERE source_file=? AND role='assistant' ORDER BY seq",
                             (str(path),)).fetchall()
    assert [row['trace_id'] or '' for row in rows] == ['', 'turn-1']
    assert live['id'] not in {row['message_id'] for row in rows}
    assert db.conn().execute("SELECT trace_id FROM messages WHERE message_id=?",
                             (live['id'],)).fetchone()[0] == 'turn-1'
