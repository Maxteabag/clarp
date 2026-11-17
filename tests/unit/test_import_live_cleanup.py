"""Leftover-live cleanup is scoped by the turns this import can delete."""
from lib import db, message_writes
from lib.message_import_writer import _ImportWriter


def _row(ident, trace, *, agent='a', session='b', role='assistant',
         source='live:fixture', text='answer', seq=0):
    db.conn().execute(
        'INSERT INTO messages(message_id,agent_id,backend_session_id,seq,role,source_file,'
        'text,trace_id,updated_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (ident,agent,session,seq,role,source,text,trace,1,1))


def _history(size):
    con=db.conn();con.execute('BEGIN')
    con.executemany(
        'INSERT INTO messages(message_id,agent_id,backend_session_id,seq,role,source_file,'
        'text,trace_id,updated_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?)',
        [(f'h{i}','a','b',i,'user' if i%2==0 else 'assistant','/history.jsonl',
          'history',f'old-{i//2}',1,1) for i in range(size)])
    con.execute('COMMIT')


def _cleanup(finals, total=1000):
    con=db.conn();con.execute('UPDATE message_clock SET revision=100000')
    con.execute("INSERT INTO conversation_heads VALUES('a','b',100000,0)")
    statements=[];con.set_trace_callback(statements.append)
    writer=_ImportWriter(con,locked=True)
    try:
        result=message_writes._finish_import(
            writer,'a','b',source_file='/fixture.jsonl',batch_finals=finals,
            total_turns=total,skipped_slot_removed=False,latest_revision=100000,
            authority_tools={})
        writer.finish()
    finally:
        con.set_trace_callback(None)
        if con.in_transaction:con.execute('ROLLBACK')
    selects=[sql for sql in statements if 'SELECT message_id, text, trace_id FROM messages'
             in ' '.join(sql.split())]
    return result,selects


def _query_work(selects):
    con=db.conn();steps=[0];plans=[]
    def tick():steps[0]+=1;return 0
    for sql in selects:
        con.set_progress_handler(tick,1)
        try:con.execute(sql).fetchall()
        finally:con.set_progress_handler(None,0)
        plans.extend(row[3] for row in con.execute('EXPLAIN QUERY PLAN '+sql))
    return steps[0],plans


def test_cleanup_deletes_only_related_live_assistants_of_its_batch_turn():
    _row('exact','t',seq=-1)
    _row('prefix','t',text='ans',seq=-2)
    _row('different-text','t',text='unrelated',seq=-3)
    _row('other-turn','elsewhere',seq=-4)
    _row('null',None,seq=-5)
    _row('empty','',seq=-6)
    _row('other-agent','t',agent='other',seq=-7)
    _row('other-session','t',session='other',seq=-8)
    _row('user','t',role='user',seq=-9)
    _row('not-live','t',source='final:t',seq=-10)
    before={r[0] for r in db.conn().execute('SELECT message_id FROM messages')}
    result,_=_cleanup([('t','answer')] * 33)
    after={r[0] for r in db.conn().execute('SELECT message_id FROM messages')}
    assert result is None and before-after=={'exact','prefix'}
    head=db.conn().execute("SELECT revision,replace_revision FROM conversation_heads WHERE agent_id='a' AND backend_session_id='b'").fetchone()
    assert tuple(head)==(100002,100002)


def test_cleanup_without_a_reply_trace_does_not_read_live_candidates():
    _row('keep','t',seq=-1)
    result,selects=_cleanup([('', 'answer'),(None,'answer')])
    assert result is None and selects==[]
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id='keep'").fetchone()


def test_cleanup_point_work_is_independent_of_13000_history_rows():
    _history(13000)
    _row('keep-unrelated','selected',text='other words',seq=-1)
    _,selects=_cleanup([('selected','answer')],total=13002)
    steps,plans=_query_work(selects)
    assert steps<100,steps
    assert any('idx_messages_trace' in plan for plan in plans),plans


def test_cleanup_chunks_traces_without_skipping_a_chunk_or_deleting_foreign_rows():
    _history(1000)
    traces=[f't{i}' for i in range(65)]
    for i in (0,31,32,63,64):_row(f'delete-{i}',traces[i],seq=-i-1)
    _row('keep-other-trace','not-in-batch',seq=-100)
    _,selects=_cleanup([(trace,'answer') for trace in traces],total=1000)
    assert len(selects)>1
    assert not db.conn().execute("SELECT 1 FROM messages WHERE message_id LIKE 'delete-%'").fetchone()
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id='keep-other-trace'").fetchone()
    assert all('idx_messages_trace' in p for p in _query_work(selects)[1])


def test_full_import_with_hundreds_of_traces_keeps_the_cheaper_single_walk():
    _history(1000)
    _row('keep-unrelated','selected',text='other words',seq=-1)
    _,selects=_cleanup([('selected','answer')]+[(f'old-{i}','answer') for i in range(500)],total=1002)
    assert len(selects)==1
    steps,plans=_query_work(selects)
    assert steps<6500,steps
    assert any('idx_messages_agent_revision' in plan for plan in plans),plans
