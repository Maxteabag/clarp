import base64
import json
from types import SimpleNamespace

from lib import oracle_live


def test_router_failure_before_dispatch_does_not_claim_agent_unreachable():
    import threading
    from lib import oracle_router
    sent=[];calls=[]
    def execute(name,arguments,call_id):
        calls.append(name)
        assert name=='list_agents'
        return {'agents':[{'name':'Rowan','session':'rowan'}]}
    def reject(*args,**kwargs):raise oracle_router.RouterError('empty_router_result')
    tools=SimpleNamespace(lock=threading.Lock(),delegations=set(),execute=execute)
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),lambda _:None,tools,'unused',
        clock=lambda:100,route_request=reject)
    try:
        c.fragments=[{'role':'user','text':'Ask Rowan to inspect stock; keep Mira working.'}]
        c.routing=1;c.route('voice-request')
        text=''.join(json.loads(row).get('content','') for row in sent)
        assert 'before a new agent action was attempted' in text
        assert 'not evidence that the requested agent is unreachable' in text
        assert calls==['list_agents']
    finally:c.stop.set();c.pool.shutdown()


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


def test_completed_work_facts_reach_voice_while_audible_delivery_is_held(monkeypatch):
    import threading
    rows = {
        'missing': {'delegation_id': 'missing', 'session': 'mira', 'status': 'completed',
                    'request_text': 'Inspect release notes', 'result_text': 'The release document is missing.'},
        'health': {'delegation_id': 'health', 'session': 'rowan', 'status': 'completed',
                   'request_text': 'Inspect staging health', 'result_text': 'The health file exists. Readiness fails.'},
    }
    monkeypatch.setattr(oracle_live.oracle_delegations, 'get', rows.get)
    sent = []; downstream = []
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(rows), results=lambda: list(rows.values()))
    c = oracle_live.Conversation(SimpleNamespace(send=sent.append), downstream.append, tools, 'unused', lambda: 100)
    try:
        c.last_input = c.last_output = 100
        c.tick()
        events = [json.loads(raw) for raw in sent]
        assert events and all(event['type'] == 'session.thinking.append' for event in events)
        payload = ''.join(event['content'] for event in events)
        facts = json.loads(payload.split(': ', 1)[1])
        by_agent = {item['agent']: item for item in facts}
        assert by_agent['mira']['finding'] == rows['missing']['result_text']
        assert by_agent['rowan']['finding'] == rows['health']['result_text']
        assert not c.results_sent, 'Available knowledge is not an audible delivery acknowledgement'
        assert not any(event['type'] == 'oracle_v2.result_context' for event in downstream)
        count = len(sent)
        c.tick()
        assert len(sent) == count, 'Unchanged silent facts are not repeatedly appended'
    finally:
        c.stop.set(); c.pool.shutdown()


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


class _FakeJournal:
    def __init__(self):
        import threading
        self.lock = threading.Lock(); self.audio_bytes = {"client": 0, "server": 0}; self.records = []; self.session_id = "journal-1"
    def record(self, kind, fields=None): self.records.append((kind, fields or {}))
    def close(self): self.records.append(("session.close", {}))


def test_v2_journal_keeps_transcripts_and_timing_but_never_audio():
    now=[100.0];down=[];sent=[]
    tools=SimpleNamespace(results=lambda:[])
    c=oracle_live.Conversation(SimpleNamespace(send=sent.append),down.append,tools,'unused',lambda:now[0])
    c.journal=_FakeJournal()
    loud_audio=base64.b64encode(b'\x00\x20'*1200).decode()
    try:
        c.input({'type':'session.input_audio.append','audio':loud_audio})
        c.receive({'type':'session.output_audio.delta','delta':loud_audio})
        c.receive({'type':'session.output_transcript.delta','delta':'Hello driver','start_ms':1000,'end_ms':1600})
        c.receive({'type':'session.input_transcript.delta','delta':'hey','start_ms':200,'end_ms':500})
        c.input({'type':'oracle_v2.interrupt'})
        now[0]+=1;c.tick()
        kinds=[k for k,_ in c.journal.records]
        assert 'session.output_transcript.delta' in kinds and 'session.input_transcript.delta' in kinds
        assert 'oracle_v2.interrupt' in kinds and 'oracle_v2.quiet' in kinds and 'session.instructions.append' in kinds
        assert dict(c.journal.records[kinds.index('session.output_transcript.delta')][1])['delta']=='Hello driver'
        assert c.journal.audio_bytes=={'client':2400,'server':2400}
        assert all(loud_audio not in json.dumps(f) for _,f in c.journal.records), 'audio payloads never reach the journal'
        assert c.counts['in_chunks']==1 and c.counts['out_audible']==1
    finally:
        c.stop.set();c.pool.shutdown()


def test_serve_logs_v2_sessions_and_journals_when_enabled(monkeypatch):
    import io, queue, threading, websocket
    from lib import oracle_diagnostics
    class Upstream:
        def __init__(self): self.events=queue.Queue(); self.sent=[]
        def settimeout(self, value): pass
        def send(self, raw):
            event=json.loads(raw); self.sent.append(event)
            if event['type']=='session.start': self.events.put(json.dumps({'type':'session.started'}))
            elif event['type']=='session.close': self.events.put(json.dumps({'type':'session.closed'}))
        def recv(self):
            try: return self.events.get(timeout=.2)
            except queue.Empty: raise websocket.WebSocketTimeoutException()
        def close(self): self.events.put('')
    class Connection:
        def settimeout(self, value): pass
        def shutdown(self, _how): pass
    logged=[]
    journals=[]
    class Journal(_FakeJournal):
        def __init__(self, *a, **k): super().__init__(); journals.append(self)
    monkeypatch.setattr(oracle_diagnostics,'OracleJournal',Journal)
    monkeypatch.setattr(oracle_live,'log',lambda event,detail='':logged.append((event,detail)))
    monkeypatch.setattr(websocket,'create_connection',lambda url,**kw:Upstream())
    monkeypatch.setattr(oracle_live.config,'load',lambda:SimpleNamespace(openai_key=lambda:'fixture-key',oracle_diagnostics=True))
    monkeypatch.setattr(oracle_live,'AgentTools',lambda *a,**k:SimpleNamespace(
        lock=threading.Lock(),delegations=set(),results=lambda:[],execute=lambda *a:{}))
    handler=SimpleNamespace(
        headers={'Upgrade':'websocket','Connection':'Upgrade','Sec-WebSocket-Key':'dGhlIHNhbXBsZSBub25jZQ=='},
        path='/oracle/v2',rfile=io.BytesIO(_masked_frame(0x8,b'')),wfile=io.BytesIO(),connection=Connection(),
        ctx=SimpleNamespace(),_request_auth_validated=True,_request_device_scope='full',
        _request_principal='journal-device',_stop_agent_session=lambda *a,**k:(None,None))
    oracle_live.serve(handler)
    assert [e for e,_ in logged]==['oracleV2Open','oracleV2Close']
    assert 'journal=journal-1' in logged[0][1] and 'seconds=' in logged[1][1] and 'out_audible=0' in logged[1][1]
    kinds=[k for k,_ in journals[0].records]
    assert kinds[0]=='session.open' and 'session.start' in kinds and kinds[-2:]==['session.summary','session.close']
