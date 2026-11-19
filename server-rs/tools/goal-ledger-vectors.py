#!/usr/bin/env python3
"""Attributed event, cursor and page oracles from the reference Host."""
import copy
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'server-rs/crates/clarp-kernel/tests/fixtures/goal-ledger-vectors.json'


def peer_calls(con, ledger):
    from lib import helper_agents, peer_requests, task_plans, turn_identity
    plan = {"plan_id":"a:peer","agent_id":"a","revision":1,
            "goal":{"continuation":{"state":"waiting","dependency_key":"peer:fixture"}}}
    con.execute("INSERT INTO task_plans VALUES ('a:peer','a')")
    rows = []
    for door in ("arm", "abandon", "reply"):
        for caller in ("a", "b", ""):
            def mutate(*args, **kwargs):
                # Only the persistence call is stood in for. Record its actual caller
                # through the reference event writer while the door's context is active.
                n = len(rows) + 1
                old = {"owner_agent_id":"a","history_offset":n,"history":[]}
                new = dict(old, history=[{"kind":kwargs["action"],"at":n,"revision":n,"detail":{}}])
                ledger.record_goal_save(con,plan["plan_id"],old,new)
                event = ledger.events(plan["plan_id"])["events"][-1]
                rows.append({"door":door,"caller":caller,"actor_kind":event["actor_kind"],
                             "actor_agent_id":event["actor_agent_id"],"verified":event["new"]["actor_verified"]})
                return plan
            with patch.object(turn_identity,"caller_agent_id",return_value=caller), \
                 patch.object(task_plans,"_goal_mutate",side_effect=mutate), \
                 patch.object(task_plans,"get",return_value=plan), \
                 patch.object(helper_agents,"note_agent_message"):
                if door == "arm":
                    peer_requests._arm(plan,{"agent_id":"a"},"fixture",{"persona":"Other"},60,{},[],"read")
                elif door == "abandon":
                    assert peer_requests.abandon_wait(plan["plan_id"],"fixture","refused")
                else:
                    peer_requests.record_result(plan,{},replier={"agent_id":"b"})
    return rows


def stop_calls(root):
    """Use the Stop handler and read its persisted pause. Only the runtime is a stand-in."""
    import server
    from lib import agents, db, goal_ledger, task_plans

    assert db.DB_PATH.resolve().is_relative_to(root)
    runtime = Mock()
    runtime.begin_stop.return_value = SimpleNamespace(
        terminated=0, dropped=0, trace_id=None, lease_id="fixture")
    handler = SimpleNamespace(ctx=SimpleNamespace(runtime_client=runtime, stream=None))
    rows = []
    for actor, verified in [("user", False), ("agent:peer", True), ("agent:peer", False)]:
        for reason in ["", "review in progress"]:
            session = f"fixture-{len(rows)}"
            agents.create_agent(persona="Fixture", voice_id="", cwd=str(root), session=session)
            plan = task_plans.create(session=session, title="Fixture", items=[],
                                     goal={"outcome":"deliver","limits":"local","criteria":["done"]})
            effects = {}
            server.Handler._stop_agent_session(handler, session, strict=True, actor=actor,
                                               actor_verified=verified, reason=reason, effects=effects)
            assert effects["goals_paused"] == 1
            event = goal_ledger.events(plan["plan_id"])["events"][-1]
            assert event["kind"] == "pause"
            rows.append(dict(actor=actor, verified=verified, reason=reason,
                             event={key:event[key] for key in ("actor_kind","actor_agent_id","reason","new")}))
    return rows


def reference(root):
    harness = runpy.run_path(str(ROOT/'tests/replay/host.py'))
    harness['isolate'](root)
    harness['loopback_only']()
    sys.path.insert(0, str(ROOT/'server'))
    from lib import db, goal_ledger as lib
    stops = stop_calls(root)
    con = sqlite3.connect(':memory:', isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(lib.SCHEMA)
    con.executescript("""
        CREATE TABLE agents(agent_id TEXT PRIMARY KEY,session TEXT, archived_at INTEGER,deleted_at INTEGER,parent_agent_id TEXT);
        CREATE TABLE messages(agent_id TEXT,revision INTEGER,timestamp TEXT,sender_agent_id TEXT);
        CREATE TABLE task_plans(plan_id TEXT PRIMARY KEY,agent_id TEXT);
        INSERT INTO agents(agent_id,session,archived_at,deleted_at) VALUES ('a','one',NULL,NULL),('b','two',NULL,NULL),('d','helper',NULL,NULL);
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
        doors = peer_calls(con,lib)
        # record() itself accepts an empty batch with a cursor past A's events.
        # Authorization is real: an active delegate with its matching credential.
        con.execute("INSERT INTO task_plans VALUES ('b:other','b')")
        last_a=con.execute('SELECT MAX(event_id) FROM goal_events').fetchone()[0]
        high=lib.append(con,'b:other',kind='checkpoint',key='only',actor={'kind':'owner','agent_id':'b'})
        con.execute("INSERT INTO goal_delegations(delegation_id,principal_agent_id,delegate_agent_id,status,token_hash,created_at,created_reason) VALUES('fixture','a','d','active',?,1,'fixture')",(lib._hash('synthetic'),))
        result=lib.record('fixture','synthetic','',caller='d',wake_id='empty',entries=[],goal_event_through=high+100)
        cap=dict(requested=high+100,principal_high=last_a,other_high=high,reference=result['delegation']['goal_event_through'])
    return dict(stop_calls=stops,peer_calls=doors,written_by=f'goal-ledger-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}',stories=stories,pages=pages,cap=cap)


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as root:
        OUT.write_text(json.dumps(reference(Path(root)),ensure_ascii=True,separators=(',',':'))+'\n')
