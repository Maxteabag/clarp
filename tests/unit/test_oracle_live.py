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


def test_closing_connection_reconnect_waits_for_release():
    import threading
    principal = 'closing-reconnect-test'
    token = oracle_live.claim_connection(principal)
    assert token
    oracle_live.mark_closing(principal)
    result = []
    worker = threading.Thread(target=lambda: result.append(oracle_live.claim_connection(principal, timeout=1)))
    worker.start()
    oracle_live.release_connection(principal, token)
    worker.join(2)
    try:
        assert len(result) == 1 and result[0] and result[0] != token
    finally:
        oracle_live.release_connection(principal, result[0])


def test_classic_session_keeps_ownership_and_closing_wait_is_bounded():
    from lib import oracle_realtime
    principal = 'active-reconnect-test'
    assert oracle_realtime._claim(principal)
    try:
        assert oracle_live.claim_connection(principal, timeout=.01) is None
        oracle_live.mark_closing(principal)
        assert oracle_live.claim_connection(principal, timeout=.01) is None
    finally:
        oracle_realtime._release(principal)
        with oracle_live._CLOSING_CONDITION:
            oracle_live._CLOSING.discard(principal)
    token = oracle_live.claim_connection(principal)
    assert token
    oracle_live.release_connection(principal, token)


def test_same_device_reconnect_supersedes_stale_live_session():
    import threading
    from lib import oracle_realtime
    principal = 'takeover-test'
    stale = oracle_live.claim_connection(principal)
    assert stale
    stopped = []

    def stop_session():
        stopped.append(True)
        # The stale handler unblocks and releases shortly after being told.
        threading.Timer(.05, oracle_live.release_connection, args=(principal, stale)).start()

    oracle_live.register_stop(principal, stale, stop_session)
    fresh = oracle_live.claim_connection(principal, timeout=2)
    try:
        assert fresh and fresh != stale
        assert stopped == [True]
        # A late release from the stale session must not free the new owner.
        oracle_live.release_connection(principal, stale)
        assert oracle_realtime._ACTIVE_PRINCIPALS.get(principal) == fresh
        assert principal not in oracle_live._CLOSING
    finally:
        oracle_live.release_connection(principal, fresh)
    assert principal not in oracle_realtime._ACTIVE_PRINCIPALS
    assert principal not in oracle_live._STOP_HOOKS


def test_stale_session_that_never_releases_is_bounded():
    principal = 'stuck-takeover-test'
    stuck = oracle_live.claim_connection(principal)
    oracle_live.register_stop(principal, stuck, lambda: None)
    try:
        assert oracle_live.claim_connection(principal, timeout=.05) is None
    finally:
        oracle_live.release_connection(principal, stuck)


def test_pauses_inside_a_reply_are_forwarded_but_sustained_silence_is_not():
    now=[100.0];down=[]
    tools=SimpleNamespace(results=lambda:[])
    c=oracle_live.Conversation(SimpleNamespace(send=lambda _e:None),down.append,tools,'unused',lambda:now[0])
    quiet={'type':'session.output_audio.delta','delta':base64.b64encode(b'\x00\x00'*100).decode()}
    loud={'type':'session.output_audio.delta','delta':base64.b64encode(b'\x00\x20'*100).decode()}
    try:
        c.receive(quiet)
        assert down==[], 'Silence before any speech is not forwarded'
        c.receive(loud)
        now[0]+=1.0;c.receive(quiet)
        assert len(down)==2, 'A one-second pause between sentences keeps the playback buffer fed'
        now[0]+=.3;c.receive(quiet)
        assert len(down)==2, 'Sustained silence after a reply is dropped'
        c.tick()
        assert down[-1]=={'type':'oracle_v2.quiet'}
    finally:
        c.stop.set();c.pool.shutdown()


def _masked_frame(opcode, payload):
    import struct
    mask=b'test'
    header=bytes([0x80|opcode])
    if len(payload)<126:
        header+=bytes([0x80|len(payload)])
    else:
        header+=bytes([0x80|126])+struct.pack('!H',len(payload))
    return header+mask+bytes(v^mask[i&3] for i,v in enumerate(payload))


def test_serve_bounds_idle_clients_and_clears_its_takeover_hook(monkeypatch):
    import io, queue, threading, websocket
    from lib import oracle_realtime
    class Upstream:
        def __init__(self): self.events=queue.Queue(); self.sent=[]
        def settimeout(self, value): pass
        def send(self, raw):
            event=json.loads(raw); self.sent.append(event)
            if event['type']=='session.start':
                self.events.put(json.dumps({'type':'session.started'}))
            elif event['type']=='session.close':
                self.events.put(json.dumps({'type':'session.closed'}))
        def recv(self):
            try: return self.events.get(timeout=.2)
            except queue.Empty: raise websocket.WebSocketTimeoutException()
        def close(self): self.events.put('')
    class Connection:
        def __init__(self): self.timeouts=[]; self.shutdowns=0
        def settimeout(self, value): self.timeouts.append(value)
        def shutdown(self, _how): self.shutdowns+=1
    upstream=Upstream()
    monkeypatch.setattr(websocket,'create_connection',lambda url,**kw:upstream)
    monkeypatch.setattr(oracle_live.config,'load',lambda:SimpleNamespace(openai_key=lambda:'fixture-key'))
    monkeypatch.setattr(oracle_live,'AgentTools',lambda *a,**k:SimpleNamespace(
        lock=threading.Lock(),delegations=set(),results=lambda:[],execute=lambda *a:{}))
    handler=SimpleNamespace(
        headers={'Upgrade':'websocket','Connection':'Upgrade','Sec-WebSocket-Key':'dGhlIHNhbXBsZSBub25jZQ=='},
        path='/oracle/v2',rfile=io.BytesIO(_masked_frame(0x8,b'')),wfile=io.BytesIO(),connection=Connection(),
        ctx=SimpleNamespace(),_request_auth_validated=True,_request_device_scope='full',
        _request_principal='idle-device',_stop_agent_session=lambda *a,**k:(None,None))
    oracle_live.serve(handler)
    assert handler.wfile.getvalue().startswith(b'HTTP/1.1 101')
    assert handler.connection.timeouts==[oracle_live.CLIENT_IDLE_TIMEOUT]
    assert [e['type'] for e in upstream.sent]==['session.start','session.close']
    assert 'idle-device' not in oracle_realtime._ACTIVE_PRINCIPALS
    assert 'idle-device' not in oracle_live._STOP_HOOKS
    assert 'idle-device' not in oracle_live._CLOSING
