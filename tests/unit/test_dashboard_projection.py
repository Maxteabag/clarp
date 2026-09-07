"""Differential checks for the batched read model and its query budget."""
import json

from lib import agents, db, message_store
from lib.snapshot import build_agent_snapshot


def test_batched_clocks_match_individual_queries_with_ties_and_automation():
    for i in range(4):
        aid = agents.create_agent(persona=f'Person{i}', voice_id='', cwd='/tmp', session=f'p{i}')
        for j, kind in enumerate(('thinking', 'tool', 'idle', 'thinking', 'done', 'compacting')):
            db.conn().execute('INSERT INTO state_log(agent_id, kind, ts, detail) VALUES(?,?,?,?)',
                              (aid, kind, (j // 2 + 1) * 100, json.dumps({'index': j})))
        for j, origin in enumerate(('user', 'heartbeat', 'user', 'dreaming')):
            message_store.record_user_message(
                agent_id=aid, backend_session_id=f'conversation-{i}',
                client_msg_id=f'{i}-{j}', text=f'message {j}', origin=origin)
    states = agents.dashboard_states()
    messages = message_store.dashboard_messages()
    for agent in agents.list_agents():
        aid = agent['agent_id']
        state = states[aid]
        assert {key: state[key] for key in ('kind', 'ts', 'detail')} == agents.latest_state(aid)
        assert (state['turn_started_at'] or 0) == agents.turn_started_at(aid)
        assert (state['last_turn_end'] or 0) == agents.last_turn_end(aid)
        assert messages[aid]['head'] == message_store.last_message_head(agent_id=aid)
        assert messages[aid]['activity'] == agents.last_activity(aid)
        for session, revision in messages[aid]['revisions'].items():
            assert revision == message_store.latest_revision(agent_id=aid, backend_session_id=session)


def test_idle_snapshot_query_count_does_not_grow_with_roster():
    counts = []
    for count in (1, 100):
        for i in range(len(agents.list_agents()), count):
            agents.create_agent(persona=f'Person{i}', voice_id='', cwd='/tmp', session=f'p{i}')
        build_agent_snapshot(None)  # materialize initial persona/config state
        statements = []
        db.conn().set_trace_callback(statements.append)
        try:
            assert len(build_agent_snapshot(None)['agents']) == count
        finally:
            db.conn().set_trace_callback(None)
        counts.append(len(statements))
    assert counts[1] == counts[0]
    assert counts[1] <= 20


def test_cached_busy_state_cannot_overwrite_a_new_background_state():
    from lib import reconcile
    aid = agents.create_agent(persona='Worker', voice_id='', cwd='/tmp', session='worker')
    agents.record_state(aid, 'background', {'label': 'waiting for a build'})
    repaired = reconcile.reconcile_agent(aid, 'codex', observed_state={'kind': 'thinking'})
    assert 'state' not in repaired
    assert agents.latest_state(aid)['kind'] == 'background'


def test_completed_preview_survives_live_updates_for_unopened_chats():
    aid = agents.create_agent(persona='Ready', voice_id='', cwd='/tmp', session='ready')
    for number, source, text in ((1, 'transcript:one', 'Completed reply'),
                                 (2, 'live:ready', 'Unfinished reply')):
        db.conn().execute('''INSERT INTO messages
            (message_id, agent_id, seq, role, text, tools_json, updated_at, origin, source_file)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'preview-{number}', aid, number, 'assistant', text, '[]', number, 'user', source))
    row = next(row for row in build_agent_snapshot(None)['agents'] if row['agent_id'] == aid)
    assert row['last_message'] == 'Unfinished reply'
    assert row['last_completed_message'] == 'Completed reply'
    db.conn().execute("UPDATE messages SET text='Growing unfinished reply' WHERE message_id='preview-2'")
    assert message_store.dashboard_messages()[aid]['completed_head']['preview'] == 'Completed reply'
    db.conn().execute("UPDATE messages SET source_file='transcript:two', text='New finished reply' WHERE message_id='preview-2'")
    assert message_store.dashboard_messages()[aid]['completed_head']['preview'] == 'New finished reply'


def test_completed_preview_is_ranked_independently_of_provisional_rows():
    aid = agents.create_agent(persona='History', voice_id='', cwd='/tmp', session='history')
    for number in range(62):
        db.conn().execute('''INSERT INTO messages
            (message_id, agent_id, seq, role, text, tools_json, updated_at, origin, source_file)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (f'history-{number}', aid, number, 'assistant', 'Completed' if number == 0 else 'Partial',
             '[]', number + 1, 'user', 'transcript:old' if number == 0 else f'live:{number}'))
    assert message_store.dashboard_messages()[aid]['completed_head']['preview'] == 'Completed'
