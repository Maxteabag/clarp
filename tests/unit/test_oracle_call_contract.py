import pytest
from lib.oracle_calls import session_config, validate_offer


def test_webrtc_contract_has_no_pcm_pipeline_or_literal_agent_examples():
    cfg = session_config(model='gpt-realtime-2.1', voice='cedar', fallback='sage', transcription='gpt-4o-mini-transcribe')
    assert cfg['model'] == 'gpt-realtime-2.1'
    assert cfg['max_output_tokens'] != 700
    assert 'format' not in cfg['audio']['input']
    assert cfg['audio']['input']['turn_detection']['create_response'] is True
    assert cfg['audio']['input']['turn_detection']['interrupt_response'] is True
    assert 'Marcus' not in cfg['instructions'] and 'Theo' not in cfg['instructions']
    assert 'investigate_with_oracle' in [t['name'] for t in cfg['tools']]


def test_offer_rejects_oversized_and_non_audio_input():
    for value in ['', 'junk', 'v=0\n' + 'x' * 200000]:
        with pytest.raises(ValueError): validate_offer(value)
    assert validate_offer('v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n')


def test_unknown_owner_uses_configured_contact_not_guessed_agent(tmp_path, monkeypatch):
    from lib import agents, oracle_calls, oracle_delegations
    from types import SimpleNamespace
    agents.create_agent(persona='Sage',voice_id='v',cwd=str(tmp_path),session='sage')
    captured=[]
    def dispatch(**kwargs):
        captured.append(kwargs)
        return {'status':'accepted'}
    monkeypatch.setattr(oracle_delegations,'dispatch',dispatch)
    tools=oracle_calls.AgentTools(SimpleNamespace(),'owner','sage',lambda _:None)
    out=tools.execute('investigate_with_oracle',{'request':'Who made it blue?'},'one-call')
    assert out['agent']=='Sage'
    assert captured[0]['session']=='sage'
    assert captured[0]['request_text']=='Who made it blue?'
    assert captured[0]['owner_principal']=='owner'


def test_missing_contact_returns_actionable_error_without_work():
    from lib.oracle_calls import AgentTools
    tools=AgentTools(None,'owner','',lambda _:None)
    assert 'No Oracle contact' in tools.execute('investigate_with_oracle',{'request':'Find someone'},'x')['error']


def test_call_results_are_owner_and_attempt_scoped(monkeypatch):
    from lib import oracle_calls
    class Tools:
        from threading import Lock
        lock=Lock()
        delegations={'one'}
    from types import SimpleNamespace
    monkeypatch.setattr(oracle_calls,'_CALLS',{'owner':{'attempt':'a','sideband':SimpleNamespace(tools=Tools())}})
    monkeypatch.setattr(oracle_calls.oracle_delegations,'get',lambda ident:{'delegation_id':ident})
    assert oracle_calls.call_results('other','a')==[]
    assert oracle_calls.call_results('owner','old')==[]
    assert oracle_calls.call_results('owner','a')==[{'delegation_id':'one'}]


def test_message_reader_uses_real_message_store_contract(tmp_path):
    from lib import agents
    from lib.oracle_calls import AgentTools
    ident = agents.create_agent(persona='Mira', voice_id='v', cwd=str(tmp_path), session='mira')
    agents.record_user_message(agent_id=ident, backend_session_id='fixture-session',
        text='Keep the accent purple.', client_msg_id='fixture-message')
    result = AgentTools(None, 'owner', '', lambda _:None).execute('read_agent_messages', {'agent':'Mira'}, 'read-1')
    assert result['messages'][0]['text'] == 'Keep the accent purple.'
    assert result['messages'][0]['id']
