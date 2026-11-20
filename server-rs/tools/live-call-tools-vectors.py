#!/usr/bin/env python3
"""Capture the live call director's receipts and the notes the voice hears."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/live-call-tools-vectors.json'


def record(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agent_live_tools as tools

    base = dict(sesjon='test', intent='one', intent_status='ok', sent=True)
    outcomes = []
    def receive(status, answer):
        outcomes.append(dict(status=status, answer=answer, receipt=tools.regi_outcome(answer, status)))
    for code in (None, 199, 200, 201, 399, 400, 401, 408, 409, 410, 412, 413, 414, 500):
        for answer in (None, [], '', {}, dict(result='refused', reason='why'), dict(base, result='forwarded')):
            receive(code, answer)
    for result in ('forwarded','queued','already_present','not_forwarded','unknown'):
        for sent in (True,False,None,0,'yes'):
            receive(200,dict(base,result=result,sent=sent))
    for field in base:
        row = dict(base, result='forwarded')
        del row[field]
        receive(200,row)
    for reason in ('cap_reached','base_task_failed','send_failed','other',None):
        for status in ('ukjent','ok',None):
            receive(200,dict(base,result='not_forwarded',reason=reason,intent_status=status))
    receive(200,dict(base,result='forwarded',base_task='started_now',unknown='omit',
                     additions_used=2, additions_max=3, max=160, len=9, max_bytes=4096))
    notes = []
    def hear(receipts):
        notes.append(dict(receipts=receipts, notes=tools.host_note(receipts)))
    hear([])
    hear([{}])
    for name in ('delegate_to_agent','schedule_callback','cancel_callback','get_agent_status',
                 'read_agent_transcript','ask_my_agent','read_result','other',''):
        for extra in ({},dict(minutes=5,at='12:34:56 UTC',replaced={'minutes':1},regi_status='venter'),
                      dict(regi_status='ukjent',reason='why'),dict(status='asked')):
            hear([dict({'tool':name,'agent':'Theo','status':'forwarded'},**extra)])
    for data in ({}, {'messages':[]},{'messages':[{'role':'assistant','text':'old'},{'role':'user','text':'new'}]},
                 {'messages':[{'role':'assistant','text':'old'},{'role':'assistant','text':'last'}]},
                 {'state':'working','current_step':'checking','last_said':'answer'},
                 {'error':'away','current_step':'checking'},{'messages':[{'role':'assistant'}]}):
        hear([dict(tool='read_agent_transcript',agent='A',untrusted_data=data)])
    for size in (7,8,9,23,24,25,39,40,41,59,60,61,79,80,81,119,120,121):
        for unit in ('A',chr(0xe9),chr(0x1f4f1)):
            text = unit * size
            hear([dict(agent=text,status=text,minutes=2,at=text,regi_status=text,reason=text,
                       untrusted_data={'last_said':text})]*3)
    for size in (449,450,451,900):
        hear([dict(tool='X'*(size-len(tools._NOTE_HEAD)-len(': ok')),status='ok')])
    return {'outcomes':outcomes,'notes':notes,'calls':calls(root, tools),'callbacks':callbacks(root,tools),'transcripts':transcripts(root,tools),'routes':[routing(root,tools)],'answers':answers(root,tools),'asks':asks(root,tools)}


def calls(root, tools):
    from unittest.mock import patch
    from itertools import count
    from types import SimpleNamespace
    scenarios = []
    member = {'session':'test-member','name':'Theo','role':'fixture'}
    for delivery in ('sent','refused','uncertain','closed'):
        effects = []
        def prompt(to, sender, message):
            effects.append(['prompt', to, sender, message])
            if delivery == 'refused':
                raise tools.Refused('stand-in refusal')
            if delivery == 'uncertain':
                raise tools.Unconfirmed('stand-in uncertain')
        def status(to):
            effects.append(['status', to])
            return {'state':'idle','last_said':'fixture'}
        def transcript(to, limit):
            effects.append(['transcript',to,limit])
            return {'messages':[{'role':'assistant','text':'fixture'}]}
        cfg = tools.frozen_config({'agent_session':'guard','live_tools':{'enabled':True,
                                'agents':[member]}},'user_call')
        snapshot = {'live_tools':cfg,'agent':{'session':'guard'},'profile':{'display_name':'Guard'},'host_id':'host-test'}
        ns = count(100)
        with patch.object(tools.time,'time_ns',side_effect=lambda:next(ns)), patch.object(tools.time,'time',return_value=10), patch.object(tools,'log'), patch.object(tools,'log_exception'):
            ledger=tools.Ledger(root / delivery / 'ledger.json')
            call=tools.CallTools(snapshot,'call-one',lambda *_:False,
                                 effects=SimpleNamespace(status=status,transcript=transcript,prompt=prompt),
                                 principal='phone-one',current=lambda:delivery!='closed',ledger=ledger)
            steps=[]
            def execute(name,args):
                effects.clear()
                result=call.execute(name,args)
                steps.append(dict(name=name,args=args,result=result,effects=effects.copy()))
            for agent in (None, '', 'outsider', 'test-member'):
                execute('get_agent_status',{'agent':agent})
                execute('unknown',{'agent':agent})
            for limit in (None, False, True, 0, 1, 2, 5, 6, 2.0, '4'):
                execute('read_agent_transcript',{'agent':'test-member','limit':limit})
            for request in ('','   ','line one  line two','a'*tools.REQUEST_CHARS,'a'*(tools.REQUEST_CHARS+1)):
                execute('delegate_to_agent',{'agent':'test-member','request':request})
            for n in range(12):
                execute('delegate_to_agent',{'agent':'test-member','request':'one more '+str(n)})
            scenarios.append(dict(delivery=delivery,steps=steps))
            call.close()
    return scenarios


def transcripts(root,tools):
    from unittest.mock import patch
    meta={'call_id':'one','principal':'device-a','agent_session':'guard','agent':'Guard'}
    samples=[('text',{'role':'user','text':'One '}),('receipt',{'receipt':{'status':'forwarded'}}),
             ('text',{'role':'user','text':'question'}),('pacing',{'event':'part','detail':'1 of 2'}),
             ('text',{'role':'assistant','text':'Answer'}),('other',{'extra':1}),
             ('text',{'role':'user','text':'Again'})]
    rows=[]
    for i,ident in enumerate(('call-1','a','a.b-C_1','x'*120,'x'*121,'.','..','../other','',chr(0xe9))):
        now=[0]
        with patch.object(tools,'_iso',side_effect=lambda:'T'+str(now[0])):
            t=tools.Transcript(root/('case'+str(i)),ident,meta)
            events=[]
            for n,(kind,fields) in enumerate(samples[:i]):
                now[0]=n+1
                t.add(kind,**fields)
                events.append(json.loads(t.partial.read_text().splitlines()[-1]))
            now[0]=20
            t.finish()
            rows.append(dict(id=ident,name=t.final.stem,entries=events,document=json.loads(t.final.read_bytes())))
    return rows


def routing(root, tools):
    from types import SimpleNamespace
    from unittest.mock import patch
    from concurrent.futures import Future
    bodies,spoken,steps=[],[],[]
    class Inline:
        def submit(self, fn):
            fn()
            return Future()
        def shutdown(self, **_): pass
    def route(body):
        bodies.append(body)
        return {'output':[]}
    cfg=tools.frozen_config({'agent_session':'guard','live_tools':{'enabled':True,'agents':[{'session':'member','name':'Theo','role':'engineer'}]}},'user_call')
    snapshot={'live_tools':cfg,'agent':{'session':'guard'},'profile':{'display_name':'Guard'},'host_id':'host-test'}
    now=[0.0]
    with patch.object(tools,'log'),patch.object(tools,'log_exception'),patch.object(tools.time,'strftime',return_value='12:13'):
        call=tools.CallTools(snapshot,'call-one',lambda kind,text:spoken.append([kind,text]),
                            effects=SimpleNamespace(route=route),clock=lambda:now[0],executor=Inline(),
                            principal='phone-one',ledger=tools.Ledger(root/'routing'/'ledger.json'))
        def step(at,kind,delta=''):
            now[0]=at
            event={'type':kind,'delta':delta}
            bodies.clear();spoken.clear()
            if kind=='tick': call.tick()
            else: call.observe(event)
            steps.append(dict(at=at,event=event,bodies=bodies.copy(),spoken=spoken.copy()))
        step(0,'session.output_transcript.delta','hello')
        step(1,'session.input_transcript.delta','Check ')
        step(2,'session.input_transcript.delta','this')
        step(2.2,'session.output_transcript.delta','yes')
        step(2.3,'session.input_transcript.delta',' ')
        step(2+tools.SETTLE_S-0.001,'tick')
        step(2+tools.SETTLE_S,'tick')
        step(7,'session.delegation.created')
        for i in range(44):
            step(10+i,'session.'+('input' if i%2 else 'output')+'_transcript.delta','x'+str(i))
        step(58,'session.delegation.created')
        step(60,'session.input_transcript.delta','a'*4001)
        for i in range(31): step(61+i,'session.delegation.created')
        call.close()
    recorded=[]
    for step in steps:
        ids=[]
        for body in step.pop('bodies'):
            if body not in recorded: recorded.append(body)
            ids.append(recorded.index(body))
        step['body_ids']=ids
    return dict(config=cfg,steps=steps,bodies=recorded)


def answers(root,tools):
    from unittest.mock import patch
    from types import SimpleNamespace
    cases=[]
    for choice in ('accept','drop','uncertain','interrupted','short'):
        now=[100.0]
        sent=[];acks=[]; result=[True]
        key='answer-one'
        def speak(channel,text):
            sent.append([channel,text])
            if result[0] is None: raise OSError('uncertain')
            return result[0]
        cfg={'from':'guard','agents':[]}
        snap={'agent':{'session':'guard'},'profile':{'display_name':'Guard'},'live_tools':cfg}
        with patch.object(tools,'log'),patch.object(tools,'log_exception'):
            call=tools.CallTools(snap,'test',speak,SimpleNamespace(acknowledge=lambda ident,principal:acks.append(ident)),
                                 clock=lambda:now[0],principal='phone',ledger=tools.Ledger(root/'answers'/'ledger.json'))
            text=('Short answer' if choice=='short' else ('A sentence. '+chr(0xe9)+' '*2)*180)
            relay=tools._relay(key,'A sender','a source',text,'  a   question?  ',450)
            relay.ack=key
            call.pending_relays.append(relay)
            steps=[]
            def step(at,*,read=None,delivery=True,user=None,voice=None):
                now[0]=at
                sent.clear();acks.clear()
                if voice is not None:call.last_output=voice
                if user is not None:call.last_user=user
                result[0]=delivery
                receipt=call._read_result(read) if read is not None else None
                if read is None:call._pace_relays()
                steps.append(dict(at=at,user=call.last_user,voice=call.last_output,delivery=delivery,read=read,
                                  receipt=receipt,spoken=sent.copy(),ack=acks.copy()))
            step(100,read={'operation_id':'missing'})
            step(100,delivery=False if choice=='drop' else None if choice=='uncertain' else True)
            step(103.99)
            step(104)
            step(115)
            step(115.01,user=114.5 if choice=='interrupted' else None)
            step(116,read={'operation_id':key,'from_part':1,'verbatim':True})
            for n in range(1,11):step(116+2*n,voice=116+2*n-1.5)
            step(140,read={'operation_id':key})
            step(141,read={'from_part':10000,'verbatim':False})
            step(142,read={})
            cases.append(dict(text=text,steps=steps))
            call.close()
    return cases


def asks(root, tools):
    from types import SimpleNamespace
    from unittest.mock import patch
    stories=[]
    for outcome in ('accepted','failed-row','lookup-found','lookup-empty','lookup-failed','ended'):
        effects=[]
        def ask(id,session,question,owner):
            effects.append(['ask',id,session,question,owner])
            if outcome.startswith('lookup-'): raise RuntimeError('uncertain')
            return {'status':'failed','error':'unavailable'} if outcome=='failed-row' else {'status':'accepted'}
        def delegation(id):
            effects.append(['get',id])
            if outcome=='lookup-failed': raise OSError('unavailable')
            return {'status':'queued'} if outcome=='lookup-found' else None
        cfg={'from':'guard','contact':{'session':'contact','name':'Contact'}}
        snapshot={'live_tools':cfg,'profile':{'display_name':'Guard'},'agent':{'session':'guard'},'host_id':'host-test'}
        with patch.object(tools,'log'),patch.object(tools,'log_exception'):
            call=tools.CallTools(snapshot,'call',lambda *_:False,SimpleNamespace(ask=ask,delegation=delegation),
                                 current=lambda:outcome!='ended',principal='device-one',ledger=tools.Ledger(root/'asks'/'ledger.json'))
            steps=[]
            for question in (None,'','  ','What  happened?',' WHAT happened?','Q'*4000,'Q'*4001):
                effects.clear()
                receipt=call.execute('ask_my_agent',{'question':question})
                # Repeated long inputs are expanded by the test. Their complete
                # operation IDs and the receipt are from the reference.
                seen=[e.copy() for e in effects]
                for e in seen:
                    if len(e)>3 and question=='Q'*4000:
                        assert e[3].endswith(question)
                        e[3]=e[3][:-4000]+'{question}'
                steps.append(dict(question=question if not isinstance(question,str) or len(question)<4000 else {'Q':len(question)},receipt=receipt,effects=seen))
            stories.append(dict(outcome=outcome,steps=steps))
            call.close()
    return stories


def callbacks(root, tools):
    from unittest.mock import patch
    from types import SimpleNamespace

    scenarios = []
    member = {'session':'member','name':'Theo'}
    cfg = tools.frozen_config({'agent_session':'guard','live_tools':{'enabled':True,'agents':[member],
                              'callback':{'kind':'http','url':'http://127.0.0.1:17001/callback'}}},'user_call')
    snapshot = {'live_tools':cfg,'agent':{'session':'guard'},'profile':{'display_name':'Guard'},'host_id':'host-test'}
    kinds = ('scheduled','already_scheduled','not-eligible','cancelled','nothing_pending','off','uncertain',
             'uncertain-once','mismatch','refused','invalid','unexpected-status','legacy-replace','superseded','fired','firing','unclear-fired','cancelled-before','not-placed','failed','unknown','server-error')
    for i,kind in enumerate(kinds):
        path=root / ('cb-'+str(i)) / 'ledger.json'
        writes=[]
        record_cfg={**cfg,'callback':{'kind':'none'} if kind=='off' else cfg['callback']}
        def post(url, body, timeout):
            writes.append(dict(body))
            if kind=='uncertain' or (kind=='uncertain-once' and len(writes)==1):
                raise tools.Unconfirmed('unknown')
            if kind=='mismatch' and len(writes)==1:
                raise tools.Refused('mismatch',409,{'error':'supersedes_mismatch','pending':'other-call'})
            if kind=='server-error': raise tools.Refused('uncertain',500,{'error':'other'})
            if kind=='refused': raise tools.Refused('no',400,{'reason':'disabled'})
            if kind=='invalid': return {'ok':False,'result':'scheduled'}
            result='cancelled' if body['action']=='cancel' else 'scheduled'
            if kind in ('already_scheduled','nothing_pending','unexpected-status'): result=kind
            if kind=='legacy-replace' and body.get('explicit'):
                result='already_scheduled'
            status={'fired':'fyrt','firing':'fyrer','unclear-fired':'fyrt_usikker','cancelled-before':'avlyst',
                    'not-placed':'ikke_plassert','failed':'feil','unknown':None}.get(kind,'venter')
            return {'ok':True,'result':result,'operation_id':body['operation_id'],'due':3600,'status':status,
                    **({'superseded':'earlier'} if kind=='superseded' and body.get('explicit') else {})}
        effects=SimpleNamespace(post=post,recipient_ok=lambda _:kind!='not-eligible')
        with patch.object(tools.time,'time',return_value=100), patch.object(tools,'log'), patch.object(tools,'log_exception'):
            call=tools.CallTools(dict(snapshot,live_tools=record_cfg),'call-one',lambda *_:False,
                                 effects=effects,principal='phone-one',ledger=tools.Ledger(path))
            steps=[]
            for args in ({},{'minutes':False,'reason':'  '},{'minutes':0},{'minutes':1},{'minutes':30},
                         {'minutes':31,'reason':' one    two '},{'minutes':2.0},{'minutes':'3'},{'minutes':5}):
                writes.clear()
                receipt=call.execute('schedule_callback',args)
                steps.append(dict(tool='schedule_callback',args=args,receipt=receipt,effects=writes.copy()))
            writes.clear()
            receipt=call.execute('cancel_callback',{})
            steps.append(dict(tool='cancel_callback',args={},receipt=receipt,effects=writes.copy()))
            scenarios.append(dict(kind=kind,steps=steps))
            call.close()
    return scenarios


if __name__ == '__main__':
    if '--record' in sys.argv:
        with tool_scratch.directory() as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        raw = subprocess.check_output([sys.executable,'-I',__file__,'--record'],
                                     env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'UTC'}))
        rows=json.loads(raw)
        written = ',\n'.join(' "'+name+'":[\n'+',\n'.join(json.dumps(r, ensure_ascii=True)
                            for r in values)+'\n]' for name,values in rows.items())
        OUT.write_text('{\n'+written+'\n}\n',encoding='ascii')
