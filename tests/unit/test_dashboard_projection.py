"""Differential checks for the batched read model and its query budget."""
import json
import pytest

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
        message_store.record_user_message(
            agent_id=aid, backend_session_id=f'conversation-{i}',
            client_msg_id=f'{i}-oracle', text='Oracle work', origin='oracle')
        message_store.record_user_message(
            agent_id=aid, backend_session_id=f'conversation-{i}',
            client_msg_id=f'{i}-agent', text='Delegated work', origin='agent',
            sender_agent_id='peer-agent')
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
        assert messages[aid]['chat_activity'] == agents.chat_activity(aid)
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
    # 20 roster-independent reads, two for the backend quota projection (which
    # must come from the database: the runtime process records limits and this
    # process serves the snapshot), and one for the agent-goal projection.
    assert counts[1] <= 23


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
    # Real writers bump updated_at (and usually revision); the preview cache keys on both.
    db.conn().execute("UPDATE messages SET text='Growing unfinished reply', updated_at=3 WHERE message_id='preview-2'")
    assert message_store.dashboard_messages()[aid]['completed_head']['preview'] == 'Completed reply'
    db.conn().execute("UPDATE messages SET source_file='transcript:two', text='New finished reply', updated_at=4 WHERE message_id='preview-2'")
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


@pytest.mark.parametrize("reply_origin", ["agent", "automation", "schedule", "oracle", "user"])
def test_delegated_reply_preview_and_overview_clock_stay_together(reply_origin):
    """A fresh peer-driven reply must not retain the old direct-chat date."""
    aid = agents.create_agent(persona='Worker', voice_id='', cwd='/tmp', session='worker-recency')
    db.conn().execute('UPDATE agents SET created_at=1 WHERE agent_id=?', (aid,))
    for mid, role, text, origin, timestamp in (
        ('direct', 'user', 'Investigate', 'user', '2026-09-23T10:17:00Z'),
        ('delegation', 'user', 'Continue the investigation', 'agent', '2026-09-23T13:28:00Z'),
        ('reply', 'assistant', 'Latest investigation finding', reply_origin, '2026-09-23T13:29:00Z'),
        ('heartbeat', 'assistant', 'HEARTBEAT_OK', 'heartbeat', '2026-09-23T13:31:00Z'),
    ):
        db.conn().execute('''INSERT INTO messages
            (message_id, agent_id, role, text, origin, timestamp, updated_at,
             tools_json, sender_agent_id, source_file, seq)
            VALUES(?,?,?,?,?,?,9999999999999,'[]','coordinator','transcript:test',0)''',
            (mid, aid, role, text, origin, timestamp))
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    assert row['last_message'] == 'Latest investigation finding'
    assert abs(row['last_activity'] - 1790170140000) <= 1  # 13:29 UTC
    expected_engagement = 1790170140000 if reply_origin == 'user' else 1790158620000
    assert abs(agents.last_activity(aid) - expected_engagement) <= 1
    assert row['last_activity'] == agents.chat_activity(aid)


def test_batched_clocks_follow_repeated_idles_and_stops():
    """Indexed per-agent clocks must agree with the reference queries on odd histories."""
    histories = {
        'no-boundary': ('thinking', 'tool', 'tool'),
        'idle-after-idle': ('thinking', 'idle', 'idle', 'idle'),
        'stopped-then-busy': ('thinking', 'done', 'stopped', 'thinking', 'compacting'),
        'tie-ordering': ('idle', 'thinking', 'idle', 'done', 'thinking', 'tool', 'idle'),
    }
    for name, kinds in histories.items():
        aid = agents.create_agent(persona=name, voice_id='', cwd='/tmp', session=name)
        for j, kind in enumerate(kinds):
            db.conn().execute('INSERT INTO state_log(agent_id, kind, ts, detail) VALUES(?,?,?,?)',
                              (aid, kind, (j // 3 + 1) * 1000, json.dumps({'index': j})))
    states = agents.dashboard_states()
    for agent in agents.list_agents():
        aid = agent['agent_id']
        assert {key: states[aid][key] for key in ('kind', 'ts', 'detail')} == agents.latest_state(aid)
        assert (states[aid]['turn_started_at'] or 0) == agents.turn_started_at(aid)
        assert (states[aid]['last_turn_end'] or 0) == agents.last_turn_end(aid)


def test_dashboard_message_ranking_walks_the_activity_index():
    """The preview query must keep using the expression index; a drift in
    _message_activity_sql or the ORDER BY would silently fall back to a scan."""
    aid = agents.create_agent(persona='Indexed', voice_id='', cwd='/tmp', session='indexed')
    message_store.record_user_message(agent_id=aid, backend_session_id='c', client_msg_id='m', text='hi')
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        message_store.dashboard_messages()
    finally:
        db.conn().set_trace_callback(None)
    ranking = next(s for s in statements if 'FROM messages m' in s and 'LIMIT 50' in s)
    plan = ' '.join(row[3] for row in db.conn().execute('EXPLAIN QUERY PLAN ' + ranking))
    assert 'idx_messages_dashboard_activity' in plan, plan


def test_no_drift_snapshot_makes_no_repair_writes_and_no_per_agent_queries(tmp_path, monkeypatch):
    """The read-time reconciler must stay a pure in-memory check for a roster
    whose derived state already agrees with reality."""
    from lib import reconcile
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "-tmp-proj"
    projects.mkdir(parents=True)
    ids = []
    for i in range(30):
        aid = agents.create_agent(persona=f'Person{i}', voice_id='', cwd='/tmp', session=f'p{i}',
                                  backend='claude' if i % 3 else 'codex')
        agents.start_runtime(aid, f'p{i}')
        agents.record_state(aid, 'background' if i % 7 == 0 else 'done')
        if i % 2:
            agents.bind_backend_session(aid, f'sid-{i}')
            (projects / f'sid-{i}.jsonl').write_text('')
        ids.append(aid)
    build_agent_snapshot(None)  # warm caches, persona/config materialisation
    writes = []
    monkeypatch.setattr(reconcile.agents_db, "record_state",
                        lambda *a, **k: writes.append(("record_state", a)))
    monkeypatch.setattr(reconcile.agents_db, "end_current_runtime",
                        lambda *a, **k: writes.append(("end_current_runtime", a)))
    repairs = []
    original = reconcile.reconcile_agent

    def spy(*a, **k):
        repaired = original(*a, **k)
        repairs.append(repaired)
        return repaired

    monkeypatch.setattr(reconcile, "reconcile_agent", spy)
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        snap = build_agent_snapshot(None)
    finally:
        db.conn().set_trace_callback(None)
    assert len(snap['agents']) == 30
    assert writes == []
    assert repairs and all(r == {} for r in repairs)
    assert not [s for s in statements if any(aid in s for aid in ids)], statements
    from lib import transcript_log
    transcript_log.reset_transcript_index()


def test_snapshot_reads_the_external_runtime_status_once_per_roster(monkeypatch):
    """With clarp-runtime owning the turns, spawning and compaction used to be
    one status RPC per agent each; the snapshot now shares one cached status."""
    from lib import backends, reconcile

    class Runtime:
        calls = 0

        def status(self):
            Runtime.calls += 1
            return {"active": {}, "spawning": [spawning], "terminals": [],
                    "compactions": ["p2"], "queued": {}}

    ids = {}
    for i in range(40):
        ids[f'p{i}'] = agents.create_agent(persona=f'Person{i}', voice_id='', cwd='/tmp', session=f'p{i}')
        agents.record_state(ids[f'p{i}'], 'thinking' if i == 1 else 'done')
    spawning = ids['p1']
    backends.configure_runtime_client(Runtime())
    try:
        rows = {r['session']: r for r in build_agent_snapshot(None)['agents']}
        assert Runtime.calls <= 2, Runtime.calls
        assert reconcile._slot_is_spawning(ids['p1']) is True
        assert reconcile._slot_is_spawning(ids['p0']) is False
        assert Runtime.calls <= 2, Runtime.calls
    finally:
        backends.configure_runtime_client(None)
    assert rows['p2']['compacting'] is True
    assert rows['p0']['compacting'] is False
    # A spawning slot is live work: the thinking row is kept, not repaired.
    assert rows['p1']['busy'] is True and rows['p1']['latest_state'] == 'thinking'
    assert agents.latest_state(ids['p1'])['kind'] == 'thinking'


def test_snapshot_compacting_falls_back_to_persisted_state_when_runtime_is_down(monkeypatch):
    from lib import backends
    from lib.runtime_bridge import RuntimeUnavailable

    class Offline:
        def status(self):
            raise RuntimeUnavailable("down")

    quiet = agents.create_agent(persona='Quiet', voice_id='', cwd='/tmp', session='quiet')
    busy = agents.create_agent(persona='Busy', voice_id='', cwd='/tmp', session='busy')
    agents.record_state(quiet, 'done')
    agents.record_state(busy, 'compacting')
    backends.configure_runtime_client(Offline())
    try:
        rows = {r['session']: r for r in build_agent_snapshot(None)['agents']}
    finally:
        backends.configure_runtime_client(None)
    assert rows['busy']['compacting'] is True and rows['busy']['busy'] is True
    assert rows['quiet']['compacting'] is False



def test_active_background_jobs_show_the_agent_as_background():
    """A durable job or a Clarp sub-agent keeps the running indicator on after
    the turn ends, instead of the agent reading as idle."""
    from lib import background_jobs
    aid = agents.create_agent(persona='Waiter', voice_id='', cwd='/tmp', session='waiter')
    agents.record_state(aid, 'done', {})
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    assert row['latest_state'] == 'done'
    assert row['background_jobs'] == {'count': 0, 'sub_agents': 0}

    job = background_jobs.upsert(session='waiter', job_id='build', kind='implementation', title='Build')
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    assert row['latest_state'] == 'background'
    assert row['status_text'] == 'Build'

    background_jobs.upsert(session='waiter', job_id='sub-agent-a', kind='sub-agent', title='Stream A')
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    assert row['background_jobs'] == {'count': 2, 'sub_agents': 1}
    assert row['status_text'] == '2 background jobs running'

    background_jobs.finish('build', generation=job['generation'])
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    assert row['status_text'] == 'Stream A'
