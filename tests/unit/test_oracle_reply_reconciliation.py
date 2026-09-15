from lib import agents, message_store


def test_finalized_oracle_reply_keeps_one_identity_after_transcript_import(tmp_path):
    aid=agents.create_agent(persona='Vesper',voice_id='',cwd=str(tmp_path),session='vesper')
    message_store.record_user_message(agent_id=aid,backend_session_id='bs',client_msg_id='request',
        trace_id='oracle-request',text='What is happening?',origin='oracle')
    agents.open_turn(agent_id=aid,source='pwa',trace_id='oracle-request')
    final=message_store.finalize_live_assistant_message(agent_id=aid,backend_session_id='bs',
        trace_id='oracle-request',text='I am idle.')
    turns=[{'role':'user','text':'What is happening?'},{'role':'assistant','text':'I am idle.'}]
    for _ in range(2):
        message_store.store_transcript_turns(agent_id=aid,backend_session_id='bs',source_file='rollout',turns=turns)
        answers=[r for r in message_store.list_messages(agent_id=aid,backend_session_id='bs') if r['role']=='assistant']
        assert len(answers)==1
        assert answers[0]['id']==final['id']


def test_equal_answers_to_two_requests_remain_two_messages(tmp_path):
    aid=agents.create_agent(persona='Vesper',voice_id='',cwd=str(tmp_path),session='vesper')
    turns=[];ids=[]
    for index in range(2):
        trace='oracle-'+str(index)
        message_store.record_user_message(agent_id=aid,backend_session_id='bs',client_msg_id=trace,
            trace_id=trace,text='Status?',origin='oracle')
        turn=agents.open_turn(agent_id=aid,source='pwa',trace_id=trace)
        final=message_store.finalize_live_assistant_message(agent_id=aid,backend_session_id='bs',
            trace_id=trace,text='I am idle.')
        ids.append(final['id']);agents.close_turn(turn)
        turns.extend([{'role':'user','text':'Status?'},{'role':'assistant','text':'I am idle.'}])
        message_store.store_transcript_turns(agent_id=aid,backend_session_id='bs',source_file='rollout',turns=turns)
    answers=[r for r in message_store.list_messages(agent_id=aid,backend_session_id='bs') if r['role']=='assistant']
    assert {r['id'] for r in answers}==set(ids)
    assert len(answers)==2
