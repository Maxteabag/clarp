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
