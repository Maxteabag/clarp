#!/usr/bin/env python3
"""Delegate reads, immutable citations and atomic batches from the reference Host."""
from pathlib import Path
import itertools
import json
import runpy
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scratch import case

OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/goal-bookkeeping.json'


@case('clarp-goal-bookkeeping-')
def reference(root):
    harness = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    harness['isolate'](root)
    harness['loopback_only']()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agents, db, goal_ledger as ledger, task_plans, message_store
    assert db.DB_PATH.resolve().is_relative_to(root)
    ids = itertools.count(1)
    clock = [20000]
    with patch.object(db, 'now_ms', side_effect=lambda: clock[0]), \
            patch('secrets.token_hex', side_effect=lambda n: f'{next(ids):0{2*n}x}'):
        for name in ['owner','helper','other']:
            with patch.object(agents,'_new_agent_id',return_value=name):
                agents.create_agent(session=name,persona=name,voice_id='',cwd=str(root))
        agents.set_lineage('helper',parent_agent_id='owner',role='helper')
        def make(who):
            with ledger.acting_as('owner',who,verified=True):
                return task_plans.create(session=who,title='Deliver',items=[{'id':'build','title':'Build'}],
                                         goal={'outcome':'Deliver','limits':'Local','criteria':['Done']})['plan_id']
        pid = make('owner')
        other = make('other')
        con=db.conn()
        def message(name, text, sender=''):
            row=message_store.record_user_message(agent_id='owner',backend_session_id='native',
                client_msg_id=name,text=text,origin='agent' if sender else 'user',sender_agent_id=sender or None)
            con.execute("UPDATE messages SET timestamp='1970-01-01T00:00:20.000Z' WHERE agent_id=? AND message_id=?",
                        ('owner','u-'+name))
            return row['revision']
        rev=message('note','évidence'*46)
        message('self','not a source','helper')
        message('final','last')
        clock[0]=21000
        d=ledger.enable(agents.get_by_agent_id('owner'),agents.get_by_agent_id('helper'),'fixture')['delegation_id']
        token=ledger.read_token(d)
        seed_plan=task_plans.get(pid)
        seed_messages=[dict(r) for r in con.execute("SELECT message_id,revision,role,kind,origin,sender_agent_id,timestamp,text,tool_name,backend_session_id FROM messages WHERE agent_id=? ORDER BY revision",('owner',))]
        records=[]
        def record(wake,entries,plan=pid,caller='helper',credential=token,**opts):
            clock[0]+=1
            before=ledger.events(pid)['next_after']
            item=dict(wake=wake,entries=entries,plan=plan,caller=caller,credential=(credential==token),options=opts,now=clock[0])
            try:
                item['result']=ledger.record(d,credential,plan,caller=caller,wake_id=wake,entries=entries,**opts)
            except (ValueError, TypeError, AttributeError) as e:
                item.update(error=str(e),exit=4 if isinstance(e,ledger.DelegationDenied) else 1)
            item['events']=ledger.events(pid,after=before)['events']
            item['keys']=[r[0] for r in con.execute("SELECT idempotency_key FROM goal_events WHERE plan_id=? AND event_id>? ORDER BY event_id",(pid,before))]
            if 'error' in item:
                assert not item['events']
            records.append(item)
        observed=[]
        def observe(**opts):
            observed.append(dict(options=opts,result=ledger.observe(d,token,caller='helper',**opts)))
        for opts in [{},{'limit':0},{'limit':1},{'limit':2},{'limit':501},{'after_message':rev,'after_goal_event':1},
                     {'after_message':100000,'after_goal_event':100000}]:
            observe(**opts)
        con.execute("INSERT INTO task_goal_documents(plan_id,name,revision,format,content_json,reason,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (pid,'proof',1,'markdown','"fixture"','fixture',20000))
        refs=[f'message:u-note@{rev}',f'message:u-note@{rev-1}',f'message:u-note@{rev+1}',
              'message:u-note','message:u-note@word','message:u-self@1','message:missing',
              'goal_event:1','goal_event:2','goal_event:no','document:notes@1','document:proof@1','document:proof@2','job:work']
        snapshots=[ledger._snapshot(con,ledger.delegation(d),r) for r in refs]
        for wake,entries in [('',[]),(' '*2,[]),('w'*161,[]),('limit',[{}]*51),('wrong','text'),('type',[3])]:
            record(wake,entries)
        record('empty',[],plan='missing',message_through=rev)
        record('foreign',[{}],plan=other)
        record('absent',[{}],plan='missing')
        record('bad-key',[],credential='wrong')
        for i,kind in enumerate([{},[],None,False]):
            record(f'kind-{i}',[dict(type=kind)])
        record('bad-caller',[],caller='other')
        for kind in ['observation','discrepancy','claim','unknown']:
            record('needs-refs-'+kind,[dict(type=kind,text='hi')])
            record(kind,[dict(type=kind,text='  testing é  ',source_refs=refs,observed_at=20001,reason=' detail ')])
        entry=dict(type='observation',text='repeat',source_refs=['goal_event:1'])
        record('replay',[entry,entry])
        record('replay',[dict(reversed(list(entry.items())))])
        record('key',[dict(entry,key='fixed'),dict(entry,key='fixed',text='other')])
        record('rollback',[dict(entry,key='lost'),dict(type='pause')])
        record('rollback',[dict(entry,key='lost')])
        for i,fields in enumerate([dict(source_refs=[' ']),dict(source_refs=['ref']*21),dict(subject='bad'),
                       dict(subject='step:build'),dict(subject='criterion:x'),dict(subject='subgoal:x'),
                       dict(text=''),dict(text='x'*4001),dict(reason='x'*4001),dict(subject='x'*121)]):
            record(str(i),[dict(entry,**fields)])
        record('propose',[dict(type='subgoal_propose',subgoal_id='watch',title='Watch')])
        for change in [dict(status='done'),dict(title='New'),dict(status='blocked'),dict(status='unknown')]:
            revision=ledger.subgoals(pid)[0]['revision']
            record('edit-'+str(change),[dict(type='subgoal_update',subgoal_id='watch',expected_revision=revision,fields=change)])
        record('subgoal-stale',[dict(type='subgoal_update',subgoal_id='watch',expected_revision=1,fields={'status':'unknown'})])
        record('propose-duplicate',[dict(type='subgoal_propose',subgoal_id='watch',title='Duplicate')])
        record('forbidden-evidence',[dict(type='subgoal_update',subgoal_id='watch',expected_revision=3,fields={'evidence':[{}]})])
        record('late-cursor-error',[dict(type='unknown',text='staged then refused')],message_through='nonnumeric')
        record('w'*160,[dict(type='unknown',text='boundary',key='界')]*50)
        record('cap',[],message_through=10**9,goal_event_through=10**9)
        record('lower',[],message_through=-8,goal_event_through=-8)
        observe()
        option_cases=[]
        for opts in [None,False,[],{'no_such':0},{'limit':None},{'limit':'two'},{'limit':False},
                     {'limit':1.75},{'after_message':' -1 ','after_goal_event':None},
                     {'after_message':True,'limit':' 20 '},{'after_goal_event':[]}]:
            item=dict(options=opts)
            try:
                result=ledger.observe(d,token,caller='helper',**opts)
                item['normalized']={k:int(v) for k,v in opts.items() if v is not None}
            except (TypeError,ValueError) as exc:
                item['error']=str(exc)
            option_cases.append(item)
        accounting=ledger.accounting(pid)
        # Exercise each limit with more eligible rows than the largest page,
        # interleaved with evidence from the delegate that must be skipped.
        prior_event=con.execute("SELECT MAX(event_id) FROM goal_events").fetchone()[0]
        recent_plans=[]
        for i in range(12):
            clock[0]+=1
            name=make('owner')
            event=ledger.events(name)['events'][0]['event_id']
            recent_plans.append([name,clock[0],event])
        extra=[]
        for i in range(505):
            message(f'scale-{i}', 'x'*4001 if i==0 else 'row',
                    'helper' if i in (1,128,257) else '')
            own=i%31==0
            target=recent_plans[i%len(recent_plans)][0]
            event=ledger.append(con,target,kind='claim',key=f'page-{i}',
                actor={'kind':'delegate' if own else 'owner','agent_id':'helper' if own else 'owner',
                       'delegation_id':d if own else ''})
            extra.append([target,event,own])
        scale=[]
        for lim in (1,199,200,499,500,501):
            options={'after_message':seed_messages[-1]['revision'],'after_goal_event':prior_event,'limit':lim}
            page=ledger.observe(d,token,caller='helper',**options)
            scale.append(dict(limit=lim,goals=[g['plan_id'] for g in page['goals']],
                              messages=[m['message_id'] for m in page['messages']],
                              events=[e['event_id'] for e in page['goal_events']],
                              excerpt_length=len(page['messages'][0]['text']),more=page['has_more']))
        return dict(scale=dict(plans=recent_plans,extra=extra,pages=scale,after=prior_event),options=option_cases,written_by=f'goal-bookkeeping-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}',
                    plan=pid,seed=seed_plan,messages=seed_messages,delegation=d,message_revision=rev,snapshots=snapshots,record=records,
                    observe=observed,accounting=accounting)


if __name__=='__main__':
    OUT.write_text(json.dumps(reference(),ensure_ascii=True,sort_keys=True,separators=(',',':'))+'\n')
