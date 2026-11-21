"""Differential checks for the batched read model and its query budget."""
import json
import pytest

from lib import agents, db, message_previews, message_store
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
        # Budget the uncached revision walk; whether warm-up wrote must not decide it.
        message_previews._REVISION_CACHE.reset()
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
            (message_id, agent_id, backend_session_id, seq, role, text, tools_json, updated_at,
             revision, origin, source_file)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (f'preview-{number}', aid, 's', number, 'assistant', text, '[]', number, number, 'user', source))
    row = next(row for row in build_agent_snapshot(None)['agents'] if row['agent_id'] == aid)
    assert row['last_message'] == 'Unfinished reply'
    assert row['last_completed_message'] == 'Completed reply'

    def written(change, revision):
        """As the writers do it: a live row's delta, and the reply that takes
        the row over, each give the row the next revision and raise the
        conversation's head to it in the same transaction. The preview cache
        is keyed by the heads."""
        db.conn().execute(f"UPDATE messages SET {change}, revision=?, updated_at=? WHERE message_id='preview-2'",
                          (revision, revision))
        db.conn().execute('''INSERT INTO conversation_heads (agent_id, backend_session_id, revision)
                             VALUES (?, 's', ?) ON CONFLICT(agent_id, backend_session_id)
                             DO UPDATE SET revision = MAX(conversation_heads.revision, excluded.revision)''',
                          (aid, revision))

    written("text='Growing unfinished reply'", 3)
    assert message_store.dashboard_messages()[aid]['completed_head']['preview'] == 'Completed reply'
    assert message_store.dashboard_messages()[aid]['head']['preview'] == 'Growing unfinished reply'
    written("source_file='transcript:two', text='New finished reply'", 4)
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


def _map_statements(run):
    """The statements one projection issues for its revision map: everything
    but the three a preview costs."""
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        run()
    finally:
        db.conn().set_trace_callback(None)
    return [sql for sql in statements if 'conversation_heads' in sql or 'MAX(revision)' in sql]


def test_dashboard_revision_map_reads_the_heads_not_the_messages():
    """The revision map runs after every committed write. As a grouped walk of
    the messages index it read what there are messages: 173 MB a request on a
    Host with 2.3 million of them (issue 371). It reads one row a
    conversation; the messages are walked once for a database, for
    conversations from before the heads were kept, and that walk stays
    inside its index."""
    aid = agents.create_agent(persona='Covered', voice_id='', cwd='/tmp', session='covered')
    message_store.record_user_message(agent_id=aid, backend_session_id='c', client_msg_id='m', text='hi')
    first = _map_statements(message_store.dashboard_messages)
    walks = [sql for sql in first if 'FROM messages' in sql]
    assert len(walks) == 1 and len(first) == 2
    plan = [row[3] for row in db.conn().execute('EXPLAIN QUERY PLAN ' + walks[0])]
    scans = [step for step in plan if 'messages' in step.split(' USING')[0]]
    assert scans and all('COVERING INDEX' in step for step in scans), plan

    for number in range(3):                             # every later map, whatever was written
        message_store.record_user_message(agent_id=aid, backend_session_id='c',
                                          client_msg_id=f'later-{number}', text='more')
        later = _map_statements(message_store.dashboard_messages)
        assert len(later) == 1 and 'FROM messages' not in later[0], later
        plan = ' '.join(row[3] for row in db.conn().execute('EXPLAIN QUERY PLAN ' + later[0]))
        assert 'messages' not in plan, plan


def test_a_conversation_whose_messages_outrank_its_head_keeps_its_revision():
    """Rows from before the heads were kept, or written by something that
    left the head: the map still answers with the messages' revision, and a
    write that raises the head takes over from there."""
    aid = agents.create_agent(persona='Before', voice_id='', cwd='/tmp', session='before')
    db.conn().execute("""INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role, text, revision, updated_at)
                         VALUES ('old-1', ?, 'no-head', 1, 'user', 'from before', 7, 1000)""", (aid,))
    db.conn().execute("""INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role, text, revision, updated_at)
                         VALUES ('old-2', ?, 'low-head', 1, 'user', 'from before', 9, 1000)""", (aid,))
    db.conn().execute("INSERT INTO conversation_heads (agent_id, backend_session_id, revision) VALUES (?, 'low-head', 4)", (aid,))
    db.conn().commit()
    assert message_store.dashboard_messages()[aid]['revisions'] == {'no-head': 7, 'low-head': 9}
    message_store.record_user_message(agent_id=aid, backend_session_id='no-head', client_msg_id='now', text='now')
    # These rows carry numbers the message clock never gave out, so the new
    # row's revision is below them: the conversation's revision stays, and it
    # is the head appearing that tells the cached preview is out of date.
    assert message_store.latest_revision(agent_id=aid, backend_session_id='no-head') == 7
    assert message_store.dashboard_messages()[aid]['revisions'] == {'no-head': 7, 'low-head': 9}
    assert message_store.dashboard_messages()[aid]['head']['preview'].endswith('now')
    # On a database the clock has numbered, a write goes above everything.
    db.conn().execute("UPDATE message_clock SET revision = 20")
    message_store.record_user_message(agent_id=aid, backend_session_id='low-head', client_msg_id='later', text='later')
    assert message_store.dashboard_messages()[aid]['revisions'] == {'no-head': 7, 'low-head': 21}


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
    from lib import claude_transcript
    claude_transcript.reset_transcript_index()


def test_snapshot_reads_the_external_runtime_status_once_per_roster(monkeypatch):
    """With clarp-runtime owning the turns, spawning and compaction used to be
    one status RPC per agent each; the snapshot now shares one cached status."""
    from lib import backends, reconcile

    class Runtime:
        calls = 0

        def status_within(self, total_s):
            return self.status()

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
        def status_within(self, total_s):
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
    """A durable background process keeps the running indicator on after the
    turn ends, instead of the agent reading as idle."""
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

    worker = background_jobs.upsert(session='waiter', job_id='worker-a', kind='worker', title='Stream A')
    background_jobs.set_progress('worker-a', session='waiter', generation=worker['generation'],
                                 text='tests 3/10')
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    # The badge carries the count; the line says what is happening.
    assert row['background_jobs'] == {'count': 2, 'sub_agents': 0}
    assert row['status_text'] == 'Stream A: tests 3/10'

    background_jobs.finish('worker-a', generation=worker['generation'])
    row = next(r for r in build_agent_snapshot(None)['agents'] if r['agent_id'] == aid)
    assert row['status_text'] == 'Build'

def test_dashboard_revision_map_is_reused_until_a_write():
    aid = agents.create_agent(persona='Poller', voice_id='', cwd='/tmp', session='poller')
    message_store.record_user_message(agent_id=aid, backend_session_id='b',
                                      client_msg_id='first', text='first')
    captured: list[str] = []
    db.conn().set_trace_callback(captured.append)
    try:
        before = message_store.dashboard_messages()[aid]['revisions']['b']
        message_store.dashboard_messages()
        maps = sum('FROM conversation_heads' in sql or 'JOIN conversation_heads' in sql
                   for sql in captured if 'INSERT' not in sql)
        message_store.record_user_message(agent_id=aid, backend_session_id='b',
                                          client_msg_id='second', text='second')
        after = message_store.dashboard_messages()[aid]['revisions']['b']
    finally:
        db.conn().set_trace_callback(None)
    assert maps == 2            # the heads once and, once for a database, the floor beside them
    assert after > before
    assert after == message_store.latest_revision(agent_id=aid, backend_session_id='b')


# --- every real writer moves what the cached preview is keyed by (issue 371) ---

def _what_the_walk_saw(agent_id):
    """The key the agent list used before: every conversation's highest
    revision and highest updated_at, read from the messages themselves."""
    return sorted(tuple(row) for row in db.conn().execute(
        """SELECT COALESCE(backend_session_id, ''), MAX(revision), MAX(updated_at)
             FROM messages WHERE agent_id = ? GROUP BY backend_session_id""", (agent_id,)))


def _key(agent_id):
    return message_previews._current_keys([agent_id])[agent_id]


def _heads_cover_messages(agent_id):
    """Every conversation's head is at or above its messages' revisions."""
    return db.conn().execute(
        """SELECT COUNT(*) FROM (SELECT backend_session_id AS s, MAX(revision) AS r FROM messages
                                  WHERE agent_id = ? GROUP BY backend_session_id) m
            LEFT JOIN conversation_heads h ON h.agent_id = ? AND h.backend_session_id IS m.s
           WHERE h.revision IS NULL OR h.revision < m.r""", (agent_id, agent_id)).fetchone()[0] == 0


def _transcript(*texts):
    return [{"role": "assistant" if number % 2 else "user", "text": text,
             "timestamp": f"2026-11-16T10:00:{number:02d}Z"} for number, text in enumerate(texts)]


def _imported(aid, *texts):
    message_store.store_transcript_turns(agent_id=aid, backend_session_id='bs', source_file='/t/one.jsonl',
                                         turns=_transcript(*texts))


WRITES = {
    "an import adds a row": lambda aid: _imported(aid, 'a prompt', 'a reply', 'another prompt'),
    # The property the old key's updated_at was there for: the same rows, one
    # of them with new words. It is not the newest row that changes.
    "an import rewrites an old row in place": lambda aid: _imported(aid, 'a prompt, reworded', 'a reply'),
    "an import rewrites the newest row in place": lambda aid: _imported(aid, 'a prompt', 'a reply, reworded'),
    "an import removes the last row": lambda aid: _imported(aid, 'a prompt'),
    "a prompt is sent": lambda aid: message_store.record_user_message(
        agent_id=aid, backend_session_id='bs', client_msg_id='sent', text='sent now'),
    "a prompt is sent to a conversation of its own": lambda aid: message_store.record_user_message(
        agent_id=aid, backend_session_id='another', client_msg_id='sent', text='sent there'),
    "a reply streams in": lambda aid: message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id='bs', text='Streaming'),
    "a notice is written": lambda aid: message_store.record_system_notice(
        agent_id=aid, backend_session_id='bs', notice_id='n1', text='A notice'),
}


@pytest.mark.parametrize("write", list(WRITES))
def test_a_write_by_a_real_writer_moves_the_key_and_refreshes_the_preview(write):
    aid = agents.create_agent(persona='Written', voice_id='', cwd='/tmp', session='written')
    _imported(aid, 'a prompt', 'a reply')
    before, walked = _key(aid), _what_the_walk_saw(aid)
    cached = message_store.dashboard_messages()[aid]
    assert message_previews._PREVIEW_CACHE.peek(aid) is not None

    WRITES[write](aid)

    assert _what_the_walk_saw(aid) != walked            # the old key saw this write
    assert _key(aid) != before                          # and so does the one read from the heads
    assert _heads_cover_messages(aid)
    fresh = message_store.dashboard_messages()[aid]
    assert fresh != cached
    routine = tuple(sorted(message_previews.origins.ROUTINE_AUTOMATION_ORIGINS))
    computed = message_previews._agent_preview(aid, 80, routine)
    assert {name: fresh[name] for name in computed} == computed


def test_a_live_row_that_grows_and_ends_moves_the_key_every_time():
    aid = agents.create_agent(persona='Streamed', voice_id='', cwd='/tmp', session='streamed')
    _imported(aid, 'a prompt', 'a reply')
    seen = [_key(aid)]
    steps = (
        lambda: message_store.upsert_live_assistant_message(agent_id=aid, backend_session_id='bs', text='One'),
        lambda: message_store.upsert_live_assistant_message(agent_id=aid, backend_session_id='bs', text='One, two'),
        lambda: message_store.mark_live_message_final(agent_id=aid, backend_session_id='bs'),
        lambda: message_store.delete_live_assistant_message(agent_id=aid, backend_session_id='bs'),
    )
    for step in steps:
        step()
        seen.append(_key(aid))
        assert _heads_cover_messages(aid)
        message_store.dashboard_messages()
    assert len(set(seen[:4])) == 4, seen                 # each write a key of its own


def test_an_import_that_changes_nothing_moves_nothing_and_reads_no_message_for_the_map():
    aid = agents.create_agent(persona='Quiet', voice_id='', cwd='/tmp', session='quiet')
    _imported(aid, 'a prompt', 'a reply')
    message_store.dashboard_messages()
    before, walked = _key(aid), _what_the_walk_saw(aid)
    _imported(aid, 'a prompt', 'a reply')
    assert (_key(aid), _what_the_walk_saw(aid)) == (before, walked)
