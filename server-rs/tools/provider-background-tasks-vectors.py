#!/usr/bin/env python3
"""Record native background Bash evidence from the isolated Python Host."""
import importlib.util
import itertools
import json
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'server-rs/crates/clarp-kernel/tests/fixtures/provider-background-tasks.json'
NOW=1800000000000
DAY=86400000
NATIVE='fixture-session'
TOOL='fixture-tool'
TOKEN='a'*24

def generate(root):
    spec=importlib.util.spec_from_file_location('isolated_host',ROOT/'tests/replay/host.py')
    isolated=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.isolate(root)
    isolated.loopback_only()
    sys.path.insert(0,str(ROOT/'server'))
    from lib import db,agents,background_jobs as jobs,provider_background_jobs as p
    from server import Handler
    db.now_ms=lambda:NOW
    aid=agents.create_agent(persona='Fixture',voice_id='',cwd=str(root),session='fixture')
    owner=dict(agent_id=aid,session='fixture',bound_at=NOW-1)
    ident=p.job_id(aid,NATIVE,TOOL)
    c=db.conn()
    cases=[]
    def record(r):
        cases.append([r,p.parse_actions(NATIVE,'project',r)])
    def block(typ,part,**extra):
        return dict(type=typ,message=dict(content=[part]),timestamp='2027-01-15T08:00:00Z',**extra)
    def launch(**kw):
        return block('assistant',dict(type='tool_use',name='Bash',id=TOOL,input=dict(run_in_background=True,description='Check',**kw)))
    def receipt(meta,error=False):
        return block('user',dict(type='tool_result',tool_use_id=TOOL,is_error=error),toolUseResult=meta)
    for k,vals in {'sessionId':[NATIVE,'other','',None],'isSidechain':[False,True,1,'false',[]],'timestamp':[None,0,'','invalid','2027-01-15T08:00:00','2027-01-15T10:00:00+02:00','2027-01-15T08:00:00.123456Z']}.items():
        for v in vals:record({**launch(),k:v})
    for d in [-1,0,1,59999,60000,60001]:
        record({**launch(),'timestamp':datetime.fromtimestamp((NOW+d)/1000,timezone.utc).isoformat()})
    for name,flag in itertools.product(['Bash','bash','Agent'],[True,False,'true',1,None]):
        r=launch();r['message']['content'][0].update(name=name,input={'run_in_background':flag});record(r)
    for v in ['x'*119,'λ'*120,'λ'*121,'api_key=synthetic',False,0,17,True,[],{'kind':'check'}]:
        r=launch();r['message']['content'][0]['input']['description']=v;record(r)
    for content in [None,42,{},'hello',[None,42,{'type':'other'}]]:record({**launch(),'message':{'content':content}})
    for meta,error in itertools.product([None,[],42,{'exitCode':True},{'exitCode':0},{'exitCode':-1},{'exitCode':'0'},{'backgroundTaskId':'child','exitCode':0}],[False,True]):
        record(receipt(meta,error))
    ids=['','a','a'*159,'a'*160,'a'*161,'_A-9','α','a/b','a.b','a\n']
    for id_ in ids:record(receipt({'backgroundTaskId':id_}))
    def notice(state='completed',task='child',tool=TOOL):
        return f'<task-notification><status>{state}</status><task-id>{task}</task-id><tool-use-id>{tool}</tool-use-id></task-notification>'
    for state,operation,typ in itertools.product(['completed','failed','killed','stopped','running','Completed'],[None,'enqueue','dequeue'],['queue-operation','user']):
        record({**launch(),'type':typ,'operation':operation,'content':notice(state)})
    for v in [None,[],notice()[:-20],notice()+notice('failed'),notice(task='a/b'),notice(tool=''),notice(task=' child ')]:
        record({**launch(),'type':'queue-operation','content':v})
    texts=['plain','sk-'+'synthetic','ask-test','Authorization: Bearer placeholder','AUTHORIZATION="Bearer fake"','authorization: Bearer [REDACTED TOKEN]','TOKEN=value; PASSWORD: example,','client-secret="thing"','api-key=abc\nnext','-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----','-----BEGIN TEST PRIVATE KEY-----\npartial']
    texts += [a+sep+b for a in ['Authorization','auth_token','API_KEY','password','nontokenish'] for sep in ['=',"': ",':\x1c'] for b in ['Bearer test','x;two']]
    stories=[]
    snapshots=[]
    state_ids={}
    def act(state,delta=0,**kw):
        return dict(op='action',action={'tool':TOOL,'at':NOW+delta,'state':state,**kw},now=NOW+max(0,delta))
    L=act('launching',title='Check token=fixture',project='project')
    R=act('running',task='child')
    def step(req):
        db.now_ms=lambda:req.get('now',NOW)
        op=req['op']
        if op=='action':p.apply_actions(c,owner,NATIVE,[req['action']])
        elif op=='reconcile':p.reconcile(c,owner,NATIVE,ended=req['ended'])
        elif op=='exit':jobs.project_native_task_turn_exit(c,ident,turn=TOKEN,exited_at=req['now'])
        elif op=='expire':
            if req['protected']:
                c.execute('insert or replace into provider_turns(turn_token,agent_id,provider,start_token,started_at) values(?,?,?,?,?)',(TOKEN,aid,'claude','fixture',NOW))
                p.record_task_turn(aid,NATIVE,TOOL,TOKEN)
            p.expire_lost_evidence(c,now=req['now'])
        row=c.execute('select title,status,started_at,updated_at,terminal_at,terminal_reason,progress_text,metadata_json from background_jobs where job_id=?',(ident,)).fetchone()
        out=dict(row) if row else None
        if out:out['metadata']=json.loads(out.pop('metadata_json'))
        return out
    def story(requests):
        for table in ['provider_task_turns','provider_turns','background_job_events','background_jobs']:c.execute('delete from '+table)
        states=[]
        for req in requests:
            n=c.execute('select count(*) from background_job_events').fetchone()[0]
            state=step(req)
            changed=c.execute('select count(*) from background_job_events').fetchone()[0]!=n
            if state is not None:
                public=jobs._public(c.execute('select * from background_jobs where job_id=?',(ident,)).fetchone(),observed_at=db.now_ms())
                state['display']={k:public[k] for k in ['can_cancel','outcome_state','worker_freshness','elapsed_ms','heartbeat_at','worker_pid']}
            key=json.dumps(state,sort_keys=True)
            if key not in state_ids:
                state_ids[key]=len(snapshots)
                snapshots.append(state)
            states.append([req,state_ids[key],changed])
        stories.append(states)
    exit_=dict(op='exit',now=NOW+7)
    stale=dict(op='reconcile',now=NOW+600001,ended=False)
    for prefix in [[],[L],[L,R],[L,R,stale],[L,R,exit_],[L,exit_],[L,R,act('failed',3)]]:
        for state,task,dt in itertools.product(['launching','running','completed','failed','stopped'],['','child','other'],[-2,0,5]):
            story(prefix+[act(state,dt,task=task,**({'exit_code':0} if state=='completed' else {}))])
    for age in [599999,600000,600001,DAY-1,DAY,DAY+1]:
        for ended,protected in itertools.product([True,False],repeat=2):
            story([L,R,dict(op='reconcile',ended=ended,now=NOW+age),dict(op='expire',protected=protected,now=NOW+age),act('completed',3,task='child',require_receipt=True)])
        story([{**L,'now':NOW+age}, {**R,'now':NOW+age},R])
    for state,task in itertools.product(['completed','failed','stopped'],['child','wrong']):
        story([L,act(state,task=task,require_receipt=True),R,act(state,task=task,require_receipt=True),exit_])
    story([L,exit_,R,act('completed',1,task='child',require_receipt=True)])
    story([act('launching',-1),act('failed',1,exit_code=23)])
    for title in ['', 'n'*121, 'test '+('λ'*119), 'token=test']:
        story([act('launching',title=title,project='fixture'),act('running',1,task='child'),act('running',2,task='child'),act('running',2,task='child')])
    for id_ in [None,False,'a/b','x'*161]:
        story([act('launching',tool=id_)])
    story([act('launching',-2)])
    mirrors=[]
    story([L,R])
    job={'job_id':ident,'agent_id':aid,'status':'running','updated_at':NOW,'heartbeat_source':'provider_event','metadata':dict(provider='claude',native_session_id=NATIVE,tool_use_id=TOOL)}
    for provider,native,tool,agent,at in itertools.product(['claude','other'],[NATIVE,'other'],[TOOL,'other'],[aid,'other'],[NOW-1,NOW+1]):
        # registered_mirror searches real rows for the native owner.
        meta=dict(provider=provider,native_session_id=native,tool_use_id=tool)
        other=dict(job_id='registered',agent_id=agent,metadata=meta,heartbeat_source='worker_registration')
        c.execute('delete from background_jobs where job_id=?',('registered',))
        if agent==aid:
            jobs.upsert(session='fixture',job_id='registered',kind='fixture',title='Registered',metadata=meta)
        mirrors.append([job,[other],at,jobs.event_projection(job,at)])
    c.execute('delete from background_jobs where job_id=?',('registered',))
    other=dict(job_id='registered',agent_id=aid,metadata=job['metadata'],heartbeat_source='worker_registration')
    jobs.upsert(session='fixture',job_id='registered',kind='fixture',title='Registered',metadata=job['metadata'])
    for variant in [{**job,'heartbeat_source':'worker_registration'},{**job,'metadata':{}},{**job,'metadata':{'provider':'claude'}},job]:
        mirrors.append([variant,[other],NOW+1,jobs.event_projection(variant,NOW+1)])
    # Normalize the synthetic owner and derived job ID; no generated identity leaks.
    class Reply:
        def _expected_generation(self):return True,None
        def _json(self,status,body):return [status,body]
    cancel=Handler._handle_background_job_cancel(Reply(),ident)
    cutoff=[]
    for age in [7*DAY-1,7*DAY,7*DAY+1]:
        c.execute('delete from provider_turns')
        c.execute('insert into provider_turns(turn_token,agent_id,provider,started_at,exited_at) values(?,?,?,?,?)',(TOKEN,aid,'claude',NOW-1,NOW))
        db.now_ms=lambda:NOW+age
        p.reap_exited_turns()
        cutoff.append([age,not bool(c.execute('select 1 from provider_turns').fetchone())])
    return json.loads(json.dumps(dict(parser=cases,ids=[[s,bool(p._ID.fullmatch(s)),p.job_id('agent',NATIVE,s)] for s in ids],redaction=[[v,p.safe_text(v)] for v in texts],stories=stories,snapshots=snapshots,mirrors=mirrors,cancel=cancel,retention=cutoff)).replace(ident,'native-fixture').replace(aid,'agent'))
if __name__=='__main__':
    if '--record' in sys.argv:
        with tool_scratch.directory() as td:print(json.dumps(generate(Path(td))))
    else:
        data=json.loads(subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'UTC'})))
        OUT.write_text('{\n'+',\n'.join(json.dumps(k)+':['+',\n'.join(json.dumps(row) for row in rows)+']' for k,rows in data.items())+'\n}\n',encoding='ascii')
        print({k:len(v) for k,v in data.items()})
