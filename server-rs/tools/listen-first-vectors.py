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
import tempfile

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
    return scripts

if __name__=='__main__':
    if '--record' in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(record(Path(tmp))))
    else:
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
        output=subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=env)
        OUT.write_text('[\n'+',\n'.join(json.dumps(row,ensure_ascii=True) for row in json.loads(output))+'\n]\n',encoding='ascii')
