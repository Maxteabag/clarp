import json
import threading
from types import SimpleNamespace

import pytest

from lib import agents, oracle_contact, oracle_strategy, oracle_live, oracle_live_stable, oracle_realtime


@pytest.mark.parametrize('value', ['', 'bad', ['direct_contact','operator'], [], 2])
def test_invalid_strategy_never_falls_back(value):
    with pytest.raises(ValueError):oracle_strategy.select(value)


def test_strategy_defaults_and_explicit_override():
    assert oracle_strategy.select() == 'operator'
    assert oracle_strategy.select(None,default='direct_contact') == 'direct_contact'
    assert oracle_strategy.select(['operator'],default='direct_contact') == 'operator'


def test_direct_contact_invalid_does_not_substitute_saved_primary(tmp_path):
    agents.create_agent(persona='Main',session='main',voice_id='fixture',cwd=str(tmp_path))
    oracle_contact.set('main')
    with pytest.raises(ValueError):oracle_strategy.contact('missing',strategy='direct_contact',source='test')
    assert oracle_strategy.contact('',strategy='direct_contact',source='test') == 'main'


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
def test_direct_missing_contact_does_not_call_operator(module,monkeypatch):
    calls=[];events=[]
    def execute(name,args,call_id):
        calls.append(name);return {'agents':[]}
    tools=SimpleNamespace(lock=threading.Lock(),delegations=set(),execute=execute,fallback='',results=lambda:[])
    def forbidden(*a,**k):pytest.fail('operator/network must not run')
    kwargs={'route_request':forbidden} if module is oracle_live else {}
    if module is oracle_live_stable:monkeypatch.setattr(module,'urlopen',forbidden)
    c=module.Conversation(SimpleNamespace(send=lambda x:None),events.append,tools,'',clock=lambda:100,
                          delegation_strategy='direct_contact',**kwargs)
    try:
        c.fragments=[{'role':'user','text':'Ask SUB4 to check tomorrow for15 minutes.'}]
        c.routing=1;c.route('voice')
        assert calls == ['list_agents']
        assert events and any(e['type']=='oracle_v2.notice' for e in events)
    finally:c.stop.set();c.pool.shutdown()


def test_stable_direct_named_worker_goes_to_primary_verbatim_and_result_to_voice(tmp_path,monkeypatch):
    calls=[];sent=[];results=[]
    def execute(name,args,call_id):
        calls.append((name,args,call_id))
        return {'agents':[]} if name=='list_agents' else {'status':'accepted'}
    tools=SimpleNamespace(lock=threading.Lock(),delegations=set(),execute=execute,fallback='main',
                          resolve=lambda session: {'session':session},ctx=SimpleNamespace(media_dir=tmp_path),
                          results=lambda:results)
    monkeypatch.setattr(oracle_live_stable,'urlopen',lambda *a,**k:pytest.fail('Luna must not run'))
    c=oracle_live_stable.Conversation(SimpleNamespace(send=sent.append),lambda e:None,tools,'',clock=lambda:100,
                                     delegation_strategy='direct_contact')
    try:
        utterance='Ask SUB4: tomorrow,15 minutes, including Morten; do not book.'
        c.fragments=[{'role':'user','text':utterance}];c.revision=1
        c.routing=1;c.route('voice')
        assert calls[-1][0]=='investigate_with_oracle' and utterance in calls[-1][1]['request']
        assert 'agent' not in calls[-1][1]
        data=json.loads(next((tmp_path/'oracle-handoffs').glob('*.json')).read_text())
        assert data['original_user_messages']==[utterance]
        c.routing=1;c.route('duplicate-trigger')
        assert sum(x[0]=='investigate_with_oracle' for x in calls)==1
        results.append({'delegation_id':'one','session':'main','status':'completed','request_text':utterance,
                        'result_text':'Three options found. Nothing booked.'})
        c.tick()
        assert any('Three options found' in line for line in sent)
        before=len(sent);c.tick();assert len(sent)==before
    finally:c.stop.set();c.pool.shutdown()


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
def test_direct_voice_contract_routes_all_work_to_selected_primary(module):
    direct=module.live_config(delegation_strategy='direct_contact')
    default=module.live_config()
    assert 'Direct-to-primary mode is active' in direct['instructions']
    assert 'including any worker the user names' in direct['instructions']
    assert 'every substantive question' in direct['instructions']
    assert 'Do not independently answer substantive questions' in direct['instructions']
    assert 'Direct-to-primary mode is active' not in default['instructions']


def test_capability_advertises_both_engines():
    capability=oracle_realtime.capability()
    assert capability['v2']['direct_contact'] is True
    assert capability['v2_tinkered']['direct_contact'] is True


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
@pytest.mark.parametrize('suffix', ['delegation_strategy=bad',
    'delegation_strategy=direct_contact&delegation_strategy=operator',
    'delegation_strategy=', 'delegation_strategy=direct_contact&oracle_session=missing'])
def test_ws_rejects_invalid_strategy_or_primary_before_upstream(module,suffix,monkeypatch):
    from lib import config
    errors=[]
    monkeypatch.setattr(module,'_send_http_error',lambda h,code,message:errors.append((code,message)))
    monkeypatch.setattr(module.ws,'is_websocket_upgrade',lambda h:True)
    monkeypatch.setattr(module.config,'load',lambda:config.Config(openai_api_key='fixture'))
    monkeypatch.setattr(module,'claim_connection',lambda *a:pytest.fail('must reject before claim/upstream'))
    h=SimpleNamespace(headers={'Sec-WebSocket-Key':'fixture'},_request_principal='device',
        _request_auth_validated=True,_request_device_scope='full',path='/oracle/v2?'+suffix)
    module.serve(h)
    assert errors and errors[0][0]==400


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
def test_ws_missing_direct_primary_fails_before_upstream(module,monkeypatch):
    from lib import config
    errors=[]
    monkeypatch.setattr(module,'_send_http_error',lambda h,code,message:errors.append((code,message)))
    monkeypatch.setattr(module.ws,'is_websocket_upgrade',lambda h:True)
    monkeypatch.setattr(module.config,'load',lambda:config.Config(openai_api_key='fixture'))
    monkeypatch.setattr(module,'claim_connection',lambda *a:pytest.fail('must reject before claim/upstream'))
    h=SimpleNamespace(headers={'Sec-WebSocket-Key':'fixture'},_request_principal='device',
        _request_auth_validated=True,_request_device_scope='full',path='/oracle/v2?delegation_strategy=direct_contact')
    module.serve(h)
    assert errors and errors[0][0]==400 and 'Select a primary' in errors[0][1]


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
def test_ws_direct_podcast_fails_before_upstream(module,monkeypatch,tmp_path):
    from lib import config
    agents.create_agent(persona='Main',session='main',voice_id='fixture',cwd=str(tmp_path))
    errors=[]
    monkeypatch.setattr(module,'_send_http_error',lambda h,code,message:errors.append((code,message)))
    monkeypatch.setattr(module.ws,'is_websocket_upgrade',lambda h:True)
    monkeypatch.setattr(module.config,'load',lambda:config.Config(openai_api_key='fixture'))
    monkeypatch.setattr(module,'claim_connection',lambda *a:pytest.fail('must reject before claim/upstream'))
    h=SimpleNamespace(headers={'Sec-WebSocket-Key':'fixture'},_request_principal='device',
        _request_auth_validated=True,_request_device_scope='full',
        path='/oracle/v2?delegation_strategy=direct_contact&oracle_session=main&podcast_artifact=test')
    module.serve(h)
    assert errors and errors[0][0]==400 and 'podcast detours' in errors[0][1]


def test_subscription_direct_availability_does_not_require_operator_key(monkeypatch):
    from lib import config,oracle_live_provider
    cfg=config.Config(openai_api_key='',oracle_voice_backend='subscription',oracle_router_backend='api')
    monkeypatch.setattr(oracle_realtime.config,'load',lambda:cfg)
    monkeypatch.setattr(oracle_live_provider,'available',lambda mode,cfg:True)
    c=oracle_realtime.capability()
    assert c['v2_tinkered']['direct_contact_available'] is True
    assert c['v2_tinkered']['available'] is False
    assert c['v2']['direct_contact_available'] is False


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
@pytest.mark.parametrize('later_speech',['', 'Tell me as soon as it finishes.', 'Nå snakker jeg til publikum.'])
def test_direct_result_delivery_needs_no_new_admission_after_later_speech(module,later_speech,tmp_path,monkeypatch):
    from lib import oracle_calls,oracle_calls_stable,oracle_delegations
    agent_id=agents.create_agent(persona='Primary',session='primary',voice_id='fixture',cwd=str(tmp_path))
    def dispatch(**values):
        oracle_delegations.begin(delegation_id=values['delegation_id'],trace_id=values['delegation_id'],client_msg_id=values['delegation_id'],
            agent_id=agent_id,session=values['session'],request_text=values['request_text'],owner_principal=values['owner_principal'])
        return oracle_delegations.get(values['delegation_id'])
    monkeypatch.setattr(oracle_delegations,'dispatch',dispatch)
    tools_module=oracle_calls_stable if module is oracle_live_stable else oracle_calls
    tools=tools_module.AgentTools(SimpleNamespace(media_dir=tmp_path),'phone','primary',lambda _:None)
    sent=[];now=[100.0]
    c=module.Conversation(SimpleNamespace(send=sent.append),lambda e:None,tools,'unused',clock=lambda:now[0],
                         delegation_strategy='direct_contact')
    try:
        c.fragments=[{'role':'user','text':'Bring our four workers to the terrace.','end_ms':100}]
        c.revision=1;c.routing=1;c.route('voice')
        assert len(tools.delegations)==1
        operation_id=next(iter(tools.delegations))
        if later_speech:
            c.receive({'type':'session.input_transcript.delta','delta':later_speech,'start_ms':5000,'end_ms':6000})
        row={**oracle_delegations.get(operation_id),'status':'completed','result_text':'CAST_RESULT_UNIQUE'}
        monkeypatch.setattr(tools,'results',lambda:[row])
        sent.clear()
        if later_speech:
            c.tick()
            assert not [r for r in sent if 'CAST_RESULT_UNIQUE' in r]  # Existing short acoustic pacing remains.
            now[0]+=4
        c.tick()
        messages=[json.loads(raw) for raw in sent if 'CAST_RESULT_UNIQUE' in raw]
        assert messages and {m['type'] for m in messages}=={'session.commentary.append'}
        assert len(tools.delegations)==1  # No new admission was manufactured to release the result.
    finally:c.stop.set();c.pool.shutdown()


@pytest.mark.parametrize('module',[oracle_live_stable,oracle_live])
@pytest.mark.parametrize('later_speech',[False,True])
def test_shared_native_response_injected_once_without_text_dedup(module,later_speech):
    now=[100.0];sent=[]
    def row(ident,message):return {'delegation_id':ident,'agent_id':'a','session':'main','backend_session_id':'native1',
        'result_message_id':message,'status':'completed','request_text':'check','result_text':'SAME_WORDING'}
    rows=[row('op'+str(i),'one-native-answer') for i in range(6)]
    tools=SimpleNamespace(results=lambda:rows)
    c=module.Conversation(SimpleNamespace(send=sent.append),lambda e:None,tools,'',clock=lambda:now[0],
                         delegation_strategy='direct_contact')
    try:
        c.revision=7 if later_speech else 6
        for _ in range(6):c.tick();now[0]+=5
        originals=[json.loads(r) for r in sent if 'SAME_WORDING' in r]
        assert len(originals)==1 and originals[0]['type']=='session.commentary.append'
        assert c.results_seen=={r['delegation_id'] for r in rows}
        rows.append(row('different-op','different-native-answer'))
        c.tick()
        assert len([r for r in sent if 'SAME_WORDING' in r])==2
    finally:c.stop.set();c.pool.shutdown()


def test_direct_progress_keeps_ordinary_commentary_delivery_without_admission(monkeypatch):
    sent=[]
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),lambda e:None,
        SimpleNamespace(results=lambda:[]),'',clock=lambda:100,delegation_strategy='direct_contact')
    try:
        c.revision=99
        c.progress=SimpleNamespace(opportunity=lambda **kw:{'working':1},offered=lambda *args:None)
        monkeypatch.setattr(oracle_live,'progress_context',lambda facts:'ordinary progress')
        c.offer_progress(100)
        assert json.loads(sent[-1])['type']=='session.commentary.append'
    finally:c.stop.set();c.pool.shutdown()


def test_admission_and_unrelated_completed_finding_keep_separate_operation_scope():
    support='Handle current request. Current user message, verbatim:\nRead support sources.\n\n<oracle-reference-data>file</oracle-reference-data>'
    admitted=oracle_strategy.admission_context('primary','support-op',support,'accepted')
    completed=oracle_strategy.direct_result_context({'delegation_id':'cast-op','session':'primary','status':'completed',
        'request_text':'Current user message, verbatim:\nBring workers to the terrace.','result_text':'Workers are on the terrace.'})
    assert 'support-op' in admitted and 'Read support sources.' in admitted
    assert 'no verified result' in admitted and 'accepted' in admitted
    assert 'cast-op' in completed and 'Bring workers to the terrace.' in completed
    assert 'Read support sources' not in completed and 'support-op' not in completed
