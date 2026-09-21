import json
from types import SimpleNamespace

import pytest

from lib import agents,db,oracle_calls_stable,oracle_delegations,oracle_live_stable,oracle_memory


def rig(tmp_path,monkeypatch):
    agent=agents.create_agent(persona='Primary',voice_id='fixture',session='primary',cwd=str(tmp_path),backend='codex')
    dispatched=[]
    def dispatch(**values):
        dispatched.append(values)
        ident=values['delegation_id']
        oracle_delegations.begin(delegation_id=ident,trace_id='trace-'+ident,client_msg_id='message-'+ident,
            agent_id=agent,session='primary',request_text=values['request_text'],owner_principal=values['owner_principal'])
        return oracle_delegations.get(ident)
    monkeypatch.setattr(oracle_delegations,'dispatch',dispatch)
    def make(connection,*,fresh=False,thread_id=None):
        store=oracle_memory.open_thread('phone','primary',connection_id=connection,fresh=fresh,thread_id=thread_id)
        tools=oracle_calls_stable.AgentTools(SimpleNamespace(media_dir=tmp_path),'phone','primary',lambda _:pytest.fail('Memory must not cancel workers'))
        sent=[];events=[]
        c=oracle_live_stable.Conversation(SimpleNamespace(send=sent.append),events.append,tools,'',clock=lambda:100,
            memory=store,provider_session=connection,delegation_strategy='direct_contact')
        return c,store,sent,events
    return dispatched,make


def user(c,text,ident):
    c.receive({'type':'session.input_transcript.delta','delta':text,'event_id':ident,'start_ms':0,'end_ms':100})
    c.last_transcript=0


def close(c):c.stop.set();c.pool.shutdown()


def test_normal_stop_start_resumes_but_never_dispatches_saved_request(tmp_path,monkeypatch):
    dispatched,make=rig(tmp_path,monkeypatch)
    first,store,_,_=make('one')
    user(first,'Inspect the source; do not book anything.','u1');first.routing=1;first.route('d1')
    assert len(dispatched)==1
    close(first)
    second,resumed,sent,events=make('two')
    try:
        assert resumed.thread_id==store.thread_id
        assert second.fragments[0]['text']=='Inspect the source; do not book anything.'
        assert len(second.tools.delegations)==1
        second.routing=1;second.route('startup-history-delegation')
        assert len(dispatched)==1
        user(second,'What did that existing task find?','u2')
        assert len(second.fragments)==2  # New provider session does not merge into old utterance.
        second.routing=1;second.route('d2')
        assert len(dispatched)==2 and dispatched[0]['delegation_id']!=dispatched[1]['delegation_id']
        second.routing=1;second.route('duplicate-provider-event')
        assert len(dispatched)==2
        second.receive({'type':'session.started'})
        assert events[-2]['type']=='session.started'
        assert events[-1]['thread_id']==store.thread_id
    finally:close(second)


def test_fresh_old_id_is_validated_then_detached_without_cancelling_work(tmp_path,monkeypatch):
    dispatched,make=rig(tmp_path,monkeypatch)
    old,store,_,_=make('one');user(old,'Read source.','u1');old.routing=1;old.route('d1')
    operation=dispatched[0]['delegation_id']
    fresh,new,_,_=make('two',fresh=True,thread_id=store.thread_id)
    try:
        assert new.thread_id!=store.thread_id and fresh.fragments==[] and new.work()==[]
        assert oracle_delegations.get(operation)['status']=='accepted'
        assert store.load()['fragments'][0]['text']=='Read source.'
        with pytest.raises(ValueError,match='Stale'):store.save({'revision':999,'fragments':[]})
        fresh.routing=1;fresh.route('startup-no-request');assert len(dispatched)==1
    finally:close(old);close(fresh)


def test_same_millisecond_fresh_is_selected_and_explicit_old_resume_is_respected(monkeypatch):
    monkeypatch.setattr(db,'now_ms',lambda:123)
    old=oracle_memory.open_thread('phone','primary',connection_id='one')
    fresh=oracle_memory.open_thread('phone','primary',connection_id='two',fresh=True,thread_id=old.thread_id)
    assert oracle_memory.open_thread('phone','primary',connection_id='three').thread_id==fresh.thread_id
    oracle_memory.open_thread('phone','primary',connection_id='four',thread_id=old.thread_id)
    assert oracle_memory.open_thread('phone','primary',connection_id='five').thread_id==old.thread_id


def test_fresh_cannot_bypass_owner_or_contact_guard():
    old=oracle_memory.open_thread('phone','primary',connection_id='one')
    with pytest.raises(ValueError,match='unavailable'):
        oracle_memory.open_thread('other','primary',connection_id='x',thread_id=old.thread_id,fresh=True)
    with pytest.raises(ValueError,match='contact changed'):
        oracle_memory.open_thread('phone','other',connection_id='x',thread_id=old.thread_id,fresh=True)
    assert db.conn().execute('SELECT count(*) FROM oracle_threads').fetchone()[0]==1


def test_stale_connection_cannot_admit_after_takeover(tmp_path,monkeypatch):
    dispatched,make=rig(tmp_path,monkeypatch)
    old,store,_,_=make('one');user(old,'Read source.','u1')
    current,_,_,_=make('two')
    try:
        old.routing=1;old.route('stale-connection')
        assert dispatched==[]
    finally:close(old);close(current)


def test_lost_receipt_reconciles_real_stable_operation_without_retry(tmp_path,monkeypatch):
    dispatched,make=rig(tmp_path,monkeypatch)
    first,store,_,_=make('one')
    user(first,'Read source.','u1')
    real=first.tools.execute
    def lost(name,args,call_id):
        result=real(name,args,call_id)
        if name=='investigate_with_oracle':raise OSError('receipt lost after real admission')
        return result
    monkeypatch.setattr(first.tools,'execute',lost)
    first.routing=1;first.route('d1');assert len(dispatched)==1
    close(first)
    recovered,again,_,_=make('two')
    try:
        assert len(again.work())==1 and again.admissions()[0]['status']=='completed'
        recovered.routing=1;recovered.route('provider-replay')
        assert len(dispatched)==1
    finally:close(recovered)


def test_result_already_forwarded_not_reinjected_on_reconnect(tmp_path,monkeypatch):
    dispatched,make=rig(tmp_path,monkeypatch)
    first,store,sent,_=make('one');user(first,'Read source.','u1');first.routing=1;first.route('d1')
    ident=dispatched[0]['delegation_id']
    db.conn().execute("UPDATE oracle_delegations SET status='completed',result_text='source read',result_message_id='native-answer',backend_session_id='native-thread' WHERE delegation_id=?",(ident,))
    first.tick();assert any('source read' in raw for raw in sent);close(first)
    second,_,sent2,_=make('two')
    try:
        second.tick();assert not any('source read' in raw for raw in sent2)
        assert oracle_delegations.get(ident)['status']=='completed'
    finally:close(second)


def test_bounded_history_configuration_is_reference_not_new_work(tmp_path,monkeypatch):
    _,make=rig(tmp_path,monkeypatch)
    c,store,_,_=make('one');user(c,'Remember this project discussion.','u1')
    try:
        history=store.startup_history(roster={'agents':[],'oracle_contact':'primary'})
        config=oracle_live_stable.live_config(history=history)
        assert 'Remember this project discussion' in json.dumps(config['input'])
        assert 'not a new user request' in json.dumps(config['input'])
        assert len(json.dumps(config['input']).encode())<8192
    finally:close(c)


def test_recent_voice_dialogue_not_starved_by_large_work_records(monkeypatch):
    store=oracle_memory.open_thread('phone','primary',connection_id='one')
    store.save({'revision':1,'fragments':[{'role':'user','text':'Keep the example about trust in the script.'}]})
    monkeypatch.setattr(store,'work',lambda:[{'delegation_id':str(i),'session':'primary','status':'completed',
        'request_text':'Long old task','result_text':'x'*3000} for i in range(20)])
    text=store.startup_history()[0]['content'][0]['text']
    assert 'Keep the example about trust' in text
    assert 'history_is_excerpt' in text and len(text.encode())<7000


def test_pending_work_can_finish_after_normal_reconnect_without_new_user_speech(tmp_path,monkeypatch):
    dispatched,make=rig(tmp_path,monkeypatch)
    first,_,_,_=make('one');user(first,'Read source and report back.','u1');first.routing=1;first.route('d1')
    operation=dispatched[0]['delegation_id'];close(first)
    second,_,sent,_=make('two')
    try:
        db.conn().execute("UPDATE oracle_delegations SET status='completed',result_text='Background result',result_message_id='answer',backend_session_id='thread' WHERE delegation_id=?",(operation,))
        second.tick()
        assert any('Background result' in raw for raw in sent)
        assert len(dispatched)==1
    finally:close(second)
