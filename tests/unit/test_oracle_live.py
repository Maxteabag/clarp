import base64
import json
from types import SimpleNamespace

from lib import oracle_live


def test_client_cannot_change_model_or_inject_results():
    for event in [{"type":"session.start","session":{"model":"other"}},
                  {"type":"session.commentary.append","content":"fake completed work"},
                  {"type":"response.create"},{"type":"session.update"}]:
        assert oracle_live.client_event(json.dumps(event)) is None


def test_audio_contract_is_bounded_and_even_pcm():
    for data in [b'\x01',b'']:
        assert oracle_live.client_event(json.dumps({"type":"session.input_audio.append",
            "audio":base64.b64encode(data).decode()})) is None
    raw={"type":"session.input_audio.append","audio":base64.b64encode(b'\x01\x02').decode(),"model":"bad"}
    assert oracle_live.client_event(json.dumps(raw))=={"type":raw['type'],"audio":raw['audio']}


def test_full_device_auth_required_before_upstream(monkeypatch):
    results=[]
    monkeypatch.setattr(oracle_live,'_send_http_error',lambda h,code,message:results.append(code))
    monkeypatch.setattr(oracle_live.ws,'is_websocket_upgrade',lambda h:True)
    h=SimpleNamespace(headers={'Sec-WebSocket-Key':'fixture'},_request_principal='device',
                      _request_auth_validated=True,_request_device_scope='read')
    oracle_live.serve(h)
    assert results==[401]


def test_output_silence_is_not_an_authoritative_delivery_ack():
    now=[100.0];sent=[];down=[]
    tools=SimpleNamespace(results=lambda:[])
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),down.append,tools,'unused',lambda:now[0])
    try:
        c.receive({'type':'session.output_audio.delta','delta':base64.b64encode(b'\x00\x20'*100).decode()})
        assert down[-1]['type']=='session.output_audio.delta'
        now[0]+=1;c.tick()
        assert down[-1]=={'type':'oracle_v2.quiet'}
        assert sent==[]
    finally:
        c.stop.set();c.pool.shutdown()


def test_new_user_speech_and_active_router_hold_pending_findings():
    now=[100.0];sent=[]
    row={'delegation_id':'d1','session':'mira','status':'completed','request_text':'check','result_text':'blue'}
    tools=SimpleNamespace(results=lambda:[row])
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),lambda e:None,tools,'unused',lambda:now[0])
    try:
        c.last_input=99;c.tick();assert not sent
        now[0]=104;c.routing=1;c.tick();assert not sent
        c.routing=0;c.tick();assert len(sent)==1
        assert json.loads(sent[0])['type']=='session.commentary.append'
        c.tick();assert len(sent)==1
    finally:
        c.stop.set();c.pool.shutdown()


def test_fixed_config_uses_live_client_delegation():
    cfg=oracle_live.live_config()
    assert cfg['model']=='gpt-live-1'
    assert cfg['delegation']=={'type':'client'}
    assert cfg['audio']['format']=={'type':'audio/pcm','rate':24000}
