#!/usr/bin/env python3
"""Record the bookkeeping listener's actual tick with an isolated Host store."""
from dataclasses import asdict
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scratch import case


@case('clarp-goal-listener-')
def reference(root):
    replay = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    replay['isolate'](root)
    replay['loopback_only']()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agents, bookkeeping_listener as listener, db, goal_ledger
    assert db.DB_PATH.resolve().is_relative_to(root)
    for name in ('principal', 'delegate'):
        with patch.object(agents, '_new_agent_id', return_value=name):
            agents.create_agent(session=name, persona=name, voice_id='', cwd=str(root))
    agents.set_lineage('delegate', parent_agent_id='principal', role='helper')
    with patch.object(db, 'now_ms', return_value=1000), patch('secrets.token_hex', side_effect=lambda n: 'a' * (2*n)):
        grant = goal_ledger.enable(agents.get_by_agent_id('principal'), agents.get_by_agent_id('delegate'), 'fixture')
    did = grant['delegation_id']
    con = db.conn()
    updates = ('last_heartbeat_at', 'last_source_at', 'unapplied_since', 'last_wake_id',
               'last_wake_at', 'last_error', 'dispatched_message_through', 'dispatched_goal_event_through')
    original = dict(listener._row(did))
    cases = []
    def tick(name, now=1000000, source=(10, 5, 900), queued='', failures=0, error='', pending=None, **saved):
        before = dict(original, **saved)
        for key in (*updates, 'message_through', 'goal_event_through'):
            con.execute(f'UPDATE goal_delegations SET {key}=? WHERE delegation_id=?',(before[key], did))
        window = listener.Pending(**(pending or {}))
        window.failures = failures
        started = asdict(window)
        sent = []
        def send(session, text, wake):
            sent.append(dict(session=session, text=text, wake=wake))
            if error:
                raise OSError(error)
        with patch.object(listener, 'source_state', return_value=source), patch.object(listener, '_wake_pending', return_value=queued), patch.object(goal_ledger, 'unapplied_since',return_value=1000):
            outcome = listener.tick(did, window, send, now=now)
        row = listener._row(did)
        cases.append(dict(name=name, now=now, source=source, busy=queued, failure=error, start=started,
                          before={k:before[k] for k in (*updates,'message_through','goal_event_through')},
                          after={k:row[k] for k in updates}, end=asdict(window), sent=sent, outcome=outcome))
    for age in (4999,5000,5001):
        tick(f'idle-beat-{age}',source=(0,0,None),last_heartbeat_at=1000000-age)
    tick('idle-clears-window',source=(0,0,None),pending=dict(marker=(4,9),first_seen=770,last_change=888))
    tick('new-marker')
    tick('backoff',pending=dict(retry_at=1000001))
    for age in (7999,8000,8001):
        tick(f'quiet-{age}',pending=dict(marker=(10,5),first_seen=990000,last_change=1000000-age))
    for age in (59999,60000,60001):
        tick(f'burst-{age}',pending=dict(marker=(9,5),first_seen=1000000-age,last_change=999999))
    for age in (899999,900000,900001):
        for busy in ('','the delegate is still working','a wake is still queued for the delegate'):
            tick(f'inflight-{age}-{busy}',queued=busy,last_wake_at=1000000-age,dispatched_message_through=8)
    ready=dict(marker=(10,5),first_seen=900000,last_change=900000)
    for busy in ('the delegate is still working','a wake is still queued for the delegate'):
        tick('admitted-'+busy,queued=busy,pending=ready)
    for failures in (0,1,5,6,7,8,20):
        tick(f'failed-{failures}',failures=failures,error='dispatch refused',pending=ready)
    tick('long-error',error='x'*501,pending=ready)
    tick('retry-at',pending=dict(ready,retry_at=1000000),failures=4,last_error='previous failure')
    tick('events-in-flight',dispatched_goal_event_through=8,last_wake_at=999999)
    tick('message-already-applied',source=(0,5,None),message_through=10,pending=ready)
    tick('events-already-applied',source=(10,0,1000),goal_event_through=5,pending=ready)
    tick('no-advance',dispatched_message_through=10,dispatched_goal_event_through=5,message_through=10,goal_event_through=5)
    from lib import turn_queue
    busy_cases=[]
    clock=10**9
    def busy(name, queued, started=None):
        why=listener._wake_pending(listener._row(did),clock)
        busy_cases.append(dict(name=name,queued=queued,started=started,now=clock,why=why))
    busy('empty',False)
    key=f'bookkeeping-{did}-m1-e2'
    turn_queue.enqueue(queue_id=key,agent_id='delegate',session='delegate',text='stand-in',trace_id=key,client_msg_id=key,synthesize_audio=False,origin='automation',sender_agent_id='')
    for status in ('queued','claimed','parked','started'):
        con.execute('UPDATE queued_turns SET status=? WHERE client_msg_id=?',(status,key))
        busy(status,status!='started')
    for age in (7199999,7200000,7200001):
        con.execute('INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES(?,?,?,?)',('delegate','pwa',str(age),clock-age))
        busy(f'age-{age}',False,clock-age)
    for key in ('ended_at','settled_at'):
        con.execute(f'INSERT INTO turns(agent_id,source,trace_id,started_at,{key}) VALUES(?,?,?,?,?)',('delegate','pwa',key,clock,clock))
        busy(key,False)
    return dict(delegation_id=did,cases=cases,busy=busy_cases)

if __name__ == '__main__':
    out=ROOT/'server-rs/crates/clarp-kernel/tests/fixtures/goal-listener.json'
    out.write_text(json.dumps(reference(),sort_keys=True,separators=(',',':'))+'\n')
