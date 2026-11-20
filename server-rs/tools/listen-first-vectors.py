#!/usr/bin/env python3
"""Listen-first opening claims, replay, greeting, retirement and identity checks.

Runs the Python opening functions unchanged with synthetic transaction, clock,
row-evidence and admission ports. A case records what was committed before a
refusal as well as the answer; a raised exception inside the transaction rolls
back. Every caller and token in this fixture belongs to this generator.
"""
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'server-rs/crates/clarp-kernel/tests/fixtures/listen-first-vectors.json'


def record(root):
    spec=importlib.util.spec_from_file_location('replay_host',ROOT/'tests/replay/host.py')
    host=importlib.util.module_from_spec(spec); spec.loader.exec_module(host)
    host.isolate(root);host.loopback_only();sys.path.insert(0,str(ROOT/'server'))
    from lib import agent_ring_openings as op, turn_queue

    gen={'boot':'boot-one','instance':'host-one'}
    ring={'ring_id':'ring-one','call_id':'call-one','agent_id':'guard','session':'guard-session',
          'persona':'Guard','reason':'Check the pump'}
    now=[1000]; counter=[0]; changes=[]
    def mint(n):
        counter[0]+=1
        return format(counter[0],'x').zfill(n*2)
    op.secrets.token_hex=mint
    op.now_ms=lambda:now[0]
    op._server_id=lambda:'host-test'
    op._auth_proof=lambda principal:{'token_hash':'fake-hash'} if principal.startswith('device_') else None
    turn_queue.stop_generation=lambda _:7
    turn_queue.is_paused=lambda _:True
    op._announce_opening=lambda rid:changes.append(['announce',rid])
    op._audit=lambda item:changes.append(['audit',dict(item)])
    op._admit_after_resolve=lambda rid:changes.append(['admit',rid])
    op._fence_breach=lambda _:''

    def initial(capable=True):
        counter[0]=0
        seed={'op':None}
        op._open_record(seed,ring,'device_one',capable=capable,host_gen=gen)
        return seed['op']
    def story(name,script):
        now[0]=1000
        saved=initial()
        start=copy.deepcopy(saved)
        world={'gen':gen,'row':None}
        op._row=lambda _:world['row']
        def withdraw(ident):
            row=world['row']
            if row and row['status'] in ('queued','parked') and row.get('claimed_at') is None:
                row['status']='withdrawn';changes.append(['withdraw',ident]);return True
            return False
        op._withdraw_row=withdraw
        @contextlib.contextmanager
        def transaction(rid):
            box={'op':copy.deepcopy(saved) if rid==saved['ring_id'] else None,'gen':world['gen']}
            yield box
            if box.get('dirty'):
                saved.clear();saved.update(box['op']);saved['updated_at']=now[0]
            if box.get('error'):raise box['error']
        op._opening_txn=transaction
        op._openings=lambda:[saved]
        op.opening_state=lambda _:copy.deepcopy(saved)
        steps=[]
        for n,cmd in enumerate(script):
            now[0]=1001+n
            changes.clear()
            if 'state' in cmd:
                saved['state']=cmd['state']
            if 'intent_state' in cmd:
                op._intend(saved,cmd.get('cause','silence'))
                saved['intent']['state']=cmd['intent_state']
            if 'row' in cmd:
                world['row']=None if cmd['row'] is None else {'status':cmd['row'],'claimed_at':cmd.get('claimed_at'),'trace_id':cmd.get('trace',saved['intent']['trace_id'])}
            if 'gen' in cmd:world['gen']=cmd['gen']
            before=copy.deepcopy(saved)
            who=cmd.get('principal','device_one')
            action=cmd.get('do','claim')
            body={'token':saved['token'],'epoch':saved['epoch'],'request_id':f'r{n}',
                  'claim':cmd.get('claim','listening')}
            body.update(cmd.get('body',{}))
            if body['claim']=='renew':
                body.setdefault('expected_epoch',saved['epoch'])
                body.setdefault('owner_nonce',saved['owner_nonce'])
            try:
                if action=='claim':answer=op.opening_claim(who,body)
                elif action=='greet':
                    answer=op.greeting(who,token=body['token'],epoch=body['epoch'],client_msg_id=cmd.get('msg','hi'),
                                       trace_id=cmd.get('trace','trace-hi'),session=cmd.get('session',ring['session']),
                                       call_id=cmd.get('call_id',ring['call_id']))
                elif action=='admitted':
                    op.greeting_admitted(ring['ring_id'],cmd.get('msg','hi'),ok=cmd['ok'])
                    answer=None
                elif action=='retire':answer=op._retire(saved,cmd.get('reason','call_ended'))
                else:raise AssertionError(action)
                status=200
            except op.CallError as exc:
                status=exc.status;answer=exc.body()
            steps.append({'cmd':cmd,'body':body,'at':now[0],'before':before if any(k in cmd for k in ('state','intent_state','row')) else None,
                          'status':status,'answer':answer,'changes':copy.deepcopy(changes),
                          'written':{k:v for k,v in saved.items() if before.get(k)!=v}})
            steps[-1]=copy.deepcopy(steps[-1])
        return {'name':name,'initial':start,'steps':steps}
    scripts=[]
    def add(name,*steps): scripts.append(story(name,steps))
    add('readiness, onset, renewal, greeting and replay',{}, {'claim':'speech_started','body':{'at_ms':5,'capture_gen':2}},
        {'claim':'speech_started'}, {'claim':'renew','body':{'request_id':'renew'}},
        {'claim':'renew','body':{'request_id':'renew','epoch':1,'expected_epoch':1,'owner_nonce':'own_'+format(2,'032x')}},
        {'do':'greet'}, {'do':'admitted','ok':False},{'do':'admitted','ok':True},{'do':'greet','body':{'epoch':1}})
    add('abandoned greeting creates a second initiative generation',
        {'claim':'no_mic'},{'claim':'speech_started'},
        {'claim':'speech_abandoned','body':{'reason':'empty_capture'}})
    for claim in ('silence','no_mic','not_ready','speech_abandoned','listening','renew','speech_started'):
        for state in ('pending','listening','speech_latched','greeting','fallback','unknown','initiative_started'):
            add('claim in '+state,{'state':state,'claim':claim,'body':{'at_ms':1250,'watermark_ms':1250,'reason':'empty_capture'}})
    for at in (None,True,'1250',1249,1250,1251):
        for mark in (None,True,'1250',1249,1250,1251):
            add('silence watermark',{'claim':'silence','body':{'at_ms':at,'watermark_ms':mark}})
    for reason in ('capture_aborted','empty_capture','other',None):
        add('abandoned',{'claim':'speech_started'},{'claim':'speech_abandoned','body':{'reason':reason}})
    for intent in ('intended','admitted','started','cancelled','unknown'):
        for q in (None,'queued','parked','claimed','started','done','ended','withdrawn','cancelled','unknown','other'):
            for act in ({'do':'greet'},{'claim':'speech_started'},{'do':'retire'}):
                add('supersede '+intent,dict(act,intent_state=intent,row=q))
    for g in (None,{'boot':'other','instance':'host-one'},{'boot':'boot-one','instance':'other'}):
        for state in ('pending','listening','greeting','fallback'):
            add('identity before replay',{'body':{'request_id':'seen'}},{'state':state,'gen':g,'body':{'request_id':'seen'}})
    for bad in ({'request_id':None},{'request_id':' '},{'request_id':'a'*128},{'request_id':'a'*129},
                {'claim':'other'},{'epoch':True},{'epoch':None},{'epoch':'1'},{'epoch':2},
                {'token':'bad'},{'token':'opn_other'}):
        add('invalid claim',{'body':bad})
    add('other principal',{'principal':'device_other'})
    for key in ('epoch','token','owner_nonce','expected_epoch','claim','reason'):
        value={'epoch':2,'token':'opn_other','owner_nonce':'other','expected_epoch':2,'claim':'speech_started','reason':'new'}[key]
        add('replay key '+key,{'body':{'request_id':'same'}},{'body':{'request_id':'same',key:value}})
    for reason in ('call_ended','stopped_after_answer','auth_revoked'):
        add('retire '+reason,{'do':'retire','reason':reason},{},{'do':'greet'},{'do':'retire','reason':'newer'})
    for key, val in (('epoch',None),('epoch',True),('epoch',2),('token','bad'),('token','opn_wrong')):
        add('bad greeting '+key,{'do':'greet','body':{key:val}})
    add('wrong greeting owner',{'do':'greet','principal':'device_other'})
    add('wrong greeting session',{'do':'greet','session':'other'})
    add('wrong greeting call',{'do':'greet','call_id':'other'})
    add('bounded request set', *[{'body':{'request_id':'request-'+str(n)}} for n in range(66)],
        {'claim':'renew','body':{'request_id':'request-0'}})
    add('renew with changed nonce',{'claim':'renew','body':{'owner_nonce':'other'}})
    add('renew bad epoch',{'claim':'renew','body':{'expected_epoch':True}})
    add('renew stale epoch',{'claim':'renew','body':{'expected_epoch':2}})
    for row in ('queued','parked','claimed','started','done'):
        add('mismatched queue trace',{'do':'greet','intent_state':'admitted','row':row,'trace':'wrong'})
        add('queued but claimed',{'do':'greet','intent_state':'admitted','row':row,'claimed_at':1000})
    # Only generation is read from the Host setting here; reads and effects below
    # are isolated substitutes for the queue's evidence.

    add('replay or conflicting reuse',{'body':{'request_id':'s'}},{'body':{'request_id':'s'}},
        {'body':{'request_id':'s','reason':'different'}})
    return {"stories":scripts,**extras(root,op)}

def extras(root, op):
    from lib import group_calls, host_generation as identity
    headers=[]
    clips=[]
    for cause in ('silence','no_mic','not_ready','speech_abandoned'):
        for ms in (1000,1250,2000):
            headers.append(dict(reason='Pump offline',cause=cause,ms=ms,text=group_calls.opening_header('Pump offline',cause,ms)))
    for state in ('intended','admitted','started','cancelled','unknown'):
        for ended in (True,False):
            for late in (True,False):
                opening=dict(ring_id='ring',call_id='call',token='opn_test',epoch=2,retired={'reason':'ended'} if ended else None,
                             late_greeting={'client_msg_id':'late'} if late else None,
                             intent={'state':state,'trace_id':'intent-trace','admission_id':'call-open:ring:1'},
                             greeting={'client_msg_id':'greet','trace_id':'greet-trace'})
                op._openings=lambda:[opening]
                for trace in ('','other','intent-trace','greet-trace'):
                    clips.append(dict(op=opening,trace=trace,tag=op.opening_audio(trace),skip=op.skip_opening_synthesis(trace)))
    uuids=[]
    value='f46bf8e2-4219-4ee5-8d9c-874d36c116ef'
    for raw in (value,value+'\n',value.upper(),value+'\n\n',' '+value,value+' ',value.replace('-',''),'.'*36,'x'*65,'',value+'x',value+'a',value[:-1]):
        try: canonical=identity._canonical_uuid(raw.encode())
        except ValueError: canonical=None
        except identity._Invalid: canonical=None
        uuids.append(dict(raw=raw,canonical=canonical))
    gates=[]
    from lib import turn_queue
    import contextlib,copy
    row_id='call-open:ring:1'
    trace='trace'
    for action in ('claim','release','parked','failed'):
        for state in ('intended','admitted','started','cancelled','unknown'):
            for status in ('queued','parked','claimed','started','done',None):
                worlds=['','stopped_after_answer','reboot']
                if state in ('started','admitted') and status in ('queued','claimed'):
                    worlds+=['retired','foreign','wrong-id','wrong-trace']
                for breach in worlds:
                    now=3000
                    generation={'boot':'boot','instance':'instance'}
                    opening={'ring_id':'ring','state':'initiative_started' if state=='started' else 'initiative_queued',
                             'server_id':'test','retired':None,'fence':{'host_gen':generation},
                             'intent':{'admission_id':row_id,'trace_id':trace,'state':state,'kind':'initiative'}}
                    if breach=='retired': opening['retired']={'reason':'call_ended','at':2000};opening['state']='retired'
                    if breach=='wrong-id':opening['intent']['admission_id']='call-open:ring:2'
                    if breach=='wrong-trace':opening['intent']['trace_id']='other-trace'
                    before=copy.deepcopy(opening)
                    row={'status':status,'trace_id':trace,'claimed_at':100 if status=='claimed' else None} if status else None
                    effects=[]
                    box={'op':opening,'gen':dict(generation,boot='other') if breach=='reboot' else generation}
                    @contextlib.contextmanager
                    def transaction(_):
                        yield box
                    op._opening_txn=transaction
                    op.now_ms=lambda:now
                    op._row=lambda _:row
                    op._fence_breach=lambda _: 'call_ended' if breach=='retired' else breach if breach=='stopped_after_answer' else ''
                    op._announce_opening=lambda _:None
                    op.is_initiative=lambda *args:breach not in ('foreign','wrong-trace','wrong-id')
                    op.is_foreign_message=lambda _:breach=='foreign'
                    op._row_admission=lambda _:row_id
                    def withdraw(_):
                        if status in ('queued','parked'):
                            effects.append('withdraw');return True
                        return False
                    op._withdraw_row=withdraw
                    turn_queue.mark_unknown=lambda _:effects.append('unknown')
                    turn_queue.withdraw_claimed=lambda _:effects.append('withdraw_claimed')
                    turn_queue.release_claim=lambda _:effects.append('release')
                    turn_queue.mark_launched_call_open_unknown=lambda _:effects.append('launched_unknown')
                    def take(_):
                        if status=='queued':effects.append('claim');return True
                        return False
                    turn_queue.claim_for_dispatch=take
                    if action=='claim':result=op.claim_initiative(row_id)
                    elif action=='release':result=op.release_unsent(row_id,trace)
                    elif action=='parked':result=op.authorize_parked_launch(row_id,trace)
                    else:result=op.resolve_launch_failure(row_id,trace)
                    gates.append(dict(action=action,before=before,status=status,breach=breach,result=result,effects=effects,after=opening))
    return dict(headers=headers,clips=clips,uuids=uuids,gates=gates,admissions=admissions(op))


def admissions(op):
    import sqlite3
    from lib import prompt_admissions, turn_queue
    con=sqlite3.connect(':memory:')
    con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE prompt_admissions (agent_id, client_admission_id, admission_id, channel, trace_id)')
    op.db.conn=lambda:con
    cases=[]
    for state in ('intended','admitted','cancelled','started',None):
        for admission in (None,'call','foreign','other-trace'):
            for queued in (None,'same','other'):
                for guard in ('ok','stopped_after_answer','retired','restart'):
                    gen={'boot':'boot','instance':'instance'}
                    opening={'ring_id':'ring','call_id':'call','server_id':'test','session':'service','agent_id':'agent',
                             'reason':'Pump','silence_ms':1250,'state':'initiative_queued','retired':None,
                             'fence':{'host_gen':gen},'intent':{'state':state,'trace_id':'trace','admission_id':'call-open:ring:1','cause':'no_mic'}}
                    if state is None: opening['intent']=None
                    if guard=='retired':opening['retired']={'reason':'call_ended','at':100}
                    box={'op':opening,'gen':gen if guard!='restart' else dict(gen,boot='new-boot')}
                    before=copy.deepcopy(opening)
                    row=None if queued is None else {'status':'queued','claimed_at':None,'trace_id':'trace' if queued=='same' else 'other'}
                    op._row=lambda _:row
                    op._fence_breach=lambda _: 'stopped_after_answer' if guard=='stopped_after_answer' else ''
                    op._withdraw_row=lambda _: row is not None
                    effects=[]
                    turn_queue.mark_unknown=lambda _:None
                    @contextlib.contextmanager
                    def transaction(_): yield box
                    op._opening_txn=transaction
                    con.execute('DELETE FROM prompt_admissions')
                    existing=None if admission is None else {'channel':'message' if admission=='foreign' else 'call','trace_id':'other' if admission=='other-trace' else 'trace'}
                    if existing:con.execute('INSERT INTO prompt_admissions VALUES(?,?,?,?,?)',('agent','call-open:ring:1','owned',existing['channel'],existing['trace_id']))
                    prompt_admissions.create=lambda **kw:effects.append('create') or kw
                    prompt_admissions.record=lambda *_a,**_kw:effects.append('record') or 'admission'
                    turn_queue.enqueue=lambda **_kw:effects.append('enqueue')
                    turn_queue.allow_paused_for=lambda _:effects.append('allow_paused')
                    result=op.admit_initiative('ring')
                    cases.append(dict(before=before,generation=box['gen'],guard=guard,existing=existing,row=row,result=result,after=opening,effects=effects))
    con.close()
    return cases

if __name__=='__main__':
    if '--record' in sys.argv:
        with tool_scratch.directory() as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        output=subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=env)
        data=json.loads(output)
        fields=[json.dumps(k)+':['+',\n'.join(json.dumps(row,ensure_ascii=True) for row in rows)+']' for k,rows in data.items()]
        OUT.write_text('{\n'+',\n'.join(fields)+'\n}\n',encoding='ascii')
