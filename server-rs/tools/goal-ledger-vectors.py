#!/usr/bin/env python3
"""Attributed event, cursor and page oracles from the reference Host."""
import copy
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'server-rs/crates/clarp-kernel/tests/fixtures/goal-ledger-vectors.json'


def reference(root):
    harness = runpy.run_path(str(ROOT/'tests/replay/host.py'))
    harness['isolate'](root)
    harness['loopback_only']()
    sys.path.insert(0, str(ROOT/'server'))
    from lib import db, goal_ledger as lib
    con = sqlite3.connect(':memory:', isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(lib.SCHEMA)
    con.executescript("""
        CREATE TABLE agents(agent_id TEXT PRIMARY KEY,session TEXT, archived_at INTEGER,deleted_at INTEGER);
        CREATE TABLE messages(agent_id TEXT,revision INTEGER,timestamp TEXT,sender_agent_id TEXT);
        CREATE TABLE task_plans(plan_id TEXT PRIMARY KEY,agent_id TEXT);
        INSERT INTO agents VALUES ('a','one',NULL,NULL),('b','two',NULL,NULL),('d','helper',NULL,NULL);
    """)
    stories = []
    with patch.object(db, 'conn', return_value=con), patch.object(db,'now_ms',return_value=700):
        for index,(author,identity,verified) in enumerate([
                ('owner','a',True),('owner','b',True),('owner','',False),
                ('user','',False),('unattributed','',False),('system','k',False),('peer','b',False)]):
            plan = f'a:goal:{index}'
            con.execute('INSERT INTO task_plans VALUES (?,?)',(plan,'a'))
            old = {}
            steps = []
            status = 'active'
            for n,kind in enumerate(['create','checkpoint','step','pause','resume','dependency_result',
                                     'wake_claim','dispatch_queued','dispatch_admitted','dispatch_retry',
                                     'execution_observed','wake_undelivered','wake_failed','block','cancel']):
                new = copy.deepcopy(old)
                new['owner_agent_id']='a'
                new['outcome']='first' if n==0 else 'revised'
                if n==1:
                    new.update(criteria=[{'id':'c','text':'example'}],limits='only here',checkpoint={'progress':'one'},native_session_id='session',superseded_by=None)
                if n==2:
                    new.pop('limits')
                detail={'reason':'reason','source_refs':[' ',23,'x'*202]+[f'ref-{i}' for i in range(23)]}
                if kind=='step':
                    detail['item_id']='step1'
                new['history']=new.get('history',[])+[dict(kind=kind,at=n+1,revision=n,detail=detail)]
                with lib.acting_as(author,identity,delegation_id='goal-keeper' if author=='system' else '', verified=verified):
                    lib.record_goal_save(con,plan,old,new)
                    lib.record_goal_save(con,plan,old,new)
                event=lib.events(plan,after=0)['events'][-1].copy()
                event.pop('actor_session')
                steps.append(dict(fields={k:v for k,v in new.items() if k!='history'},entry=new['history'][-1],prior_status=status,event=event))
                status=event['new'].get('status',status)
                old=new
            stories.append(dict(plan=plan,author=author,identity=identity,verified=verified,steps=steps))
        pages=[]
        plan=stories[0]['plan']
        for pid,after,limit in [(plan,-1,0),(plan,0,4),(plan,1,4),(plan,4,4),(plan,99,4000),('unknown',17,3)]:
            pages.append(dict(plan=pid,after=after,limit=limit,page=lib.events(pid,after=after,limit=limit)))
        # record() itself accepts an empty batch with a cursor past A's events.
        # Authorization is real: an active delegate with its matching credential.
        con.execute("INSERT INTO task_plans VALUES ('b:other','b')")
        last_a=con.execute('SELECT MAX(event_id) FROM goal_events').fetchone()[0]
        high=lib.append(con,'b:other',kind='checkpoint',key='only',actor={'kind':'owner','agent_id':'b'})
        con.execute("INSERT INTO goal_delegations(delegation_id,principal_agent_id,delegate_agent_id,status,token_hash,created_at,created_reason) VALUES('fixture','a','d','active',?,1,'fixture')",(lib._hash('synthetic'),))
        result=lib.record('fixture','synthetic','',caller='d',wake_id='empty',entries=[],goal_event_through=high+100)
        cap=dict(requested=high+100,principal_high=last_a,other_high=high,reference=result['delegation']['goal_event_through'])
    return dict(written_by=f'goal-ledger-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}',stories=stories,pages=pages,cap=cap)


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as root:
        OUT.write_text(json.dumps(reference(Path(root)),ensure_ascii=True,separators=(',',':'))+'\n')
