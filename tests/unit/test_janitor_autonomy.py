import pytest
from lib import agents,db,janitors,janitor_builtins,task_plans,heartbeat
from lib import janitor_autonomy as service

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(service.backends,'active_handles',lambda *a:[])
    owners=janitor_builtins.ensure_builtins(initial={'heartbeat-decider':{'enabled':True},'quota-monitor':{'enabled':True}})
    aid=agents.create_agent(persona='Task agent',voice_id='v',cwd='/tmp',session='task',backend='codex')
    agents.update_agent(aid,heartbeat_enabled=True)
    agents.record_state(aid,'done',{'source':'user'})
    task_plans.create(session='task',title='Finish current task',items=[{'id':'do','title':'Implement result'}])
    service.setup()
    return aid,owners

def test_model_chooses_message_and_cadence_and_noop(env):
    aid,owners=env;calls=[];sent=[]
    def decide(packet,run):
        calls.append(packet);return {'action':'wake','delay_seconds':777,'message':'Inspect the retained build result and continue the current task.','reason':'Current task has pending work'}
    worker=service.AutonomyJanitors(lambda *args:sent.append(args)or True,lambda _:None,decide,lambda:{'providers':{}})
    worker.heartbeat_once()
    assert len(sent)==1 and sent[0][0]=='task' and 'retained build' in sent[0][1]
    assert calls[0]['plan']['title']=='Finish current task'
    row=db.conn().execute('SELECT * FROM janitor_continuity WHERE target_id=?',(aid,)).fetchone()
    assert row['status']=='delivered' and row['due_at']>db.now_ms()+700000
    worker.heartbeat_once();assert len(sent)==1
    assert heartbeat.HeartbeatScheduler(send_heartbeat=lambda *_:pytest.fail('Rigid scheduler still active')).run_once()==0

def test_stop_during_model_call_cannot_dispatch(env):
    aid,_=env
    def decide(packet,run):
        agents.record_state(aid,'interrupted',{'source':'user_stop'})
        return {'action':'wake','delay_seconds':60,'message':'Continue','reason':'Proposed before stop'}
    worker=service.AutonomyJanitors(lambda *_:pytest.fail('Stopped agent woken'),lambda _:None,decide)
    worker.heartbeat_once()
    assert janitors.list_runs('clarp-heartbeat-decider')[0]['outcome']=='cancelled'

def test_pause_janitor_during_model_call_cannot_dispatch(env):
    _,owners=env
    def decide(packet,run):
        c=janitors.get(owners['heartbeat-decider']['session']);janitors.set_enabled(c['session'],c['revision'],False)
        return {'action':'wake','delay_seconds':60,'message':'Continue','reason':'Proposed'}
    service.AutonomyJanitors(lambda *_:pytest.fail('Paused janitor dispatched'),lambda _:None,decide).heartbeat_once()

def test_quota_identity_produces_one_real_delivery_receipt(env):
    _,owners=env;notifications=[]
    import datetime
    stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
    usage={'providers':{'codex':{'provider_instance_id':'opaque-account','windows':[{'window_id':'w','used_percentage':80,'observed_at':stamp,'freshness':'fresh'}]}}}
    w=service.AutonomyJanitors(lambda *_:True,lambda p:notifications.append(p)or {'sent':1},usage_read=lambda:usage)
    w.quota_once();w.quota_once();assert len(notifications)==1
    assert db.conn().execute('SELECT delivery_json FROM janitor_quota_receipts').fetchone()[0]=='{"sent": 1}'

def test_current_global_primary_and_unbounded_fallback_chain(env):
    _,owners=env
    from lib import janitor_design_policy,model_fallbacks
    owner=owners['heartbeat-decider'];chain=[{'provider':'codex','model':f'gpt-fixture-{i}'}for i in range(12)]
    value=janitor_design_policy.configure({'model_chain':chain,'inherit_sessions':[owner['session']]},0)
    assert janitor_design_policy.effective_chain(owner['session'])['source']=='global'
    assert len(model_fallbacks.get(owner['agent_id'])['models'])==11
    run=janitor_builtins.begin_run('heartbeat-decider','test-global',context={})
    assert run['configuration']['effective_chain']['chain']==chain
    janitor_design_policy.configure({'model_chain':list(reversed(chain))},value['revision'])
    assert not janitor_builtins.is_current(run['run_id'])

def test_runtime_recovery_preserves_authorization_boundary(env):
    _,owners=env;owner=owners['quota-monitor']
    assert service.runtime_recover('claude',owner['agent_id'],owner['generation'])['status']=='not_authorized'
    c=janitors.configure(owner['session'],owner['revision'],options={'recovery_mode':'ask'})
    c=janitors.set_enabled(c['session'],c['revision'],True)
    assert service.runtime_recover('claude',owner['agent_id'],c['generation'],'invented')['status']=='approval_required'
    assert service.runtime_recover('claude',owner['agent_id'],owner['generation'])['status']=='cancelled'

def test_model_defer_is_persisted_without_waking(env):
    calls=[]
    w=service.AutonomyJanitors(lambda *_:pytest.fail('Deferred target woken'),lambda _:None,lambda *args:calls.append(args)or {'action':'defer','delay_seconds':1200,'message':'','reason':'Waiting for a current worker'})
    w.heartbeat_once();w.heartbeat_once()
    assert len(calls)==1
    row=db.conn().execute('SELECT * FROM janitor_continuity').fetchone()
    assert row['status']=='defer'

def test_empty_or_invalid_model_output_cannot_dispatch(env):
    w=service.AutonomyJanitors(lambda *_:pytest.fail('Invalid proposal dispatched'),lambda _:None,lambda *_:{'action':'wake','delay_seconds':60,'message':''})
    w.heartbeat_once()
    assert db.conn().execute('SELECT status FROM janitor_continuity').fetchone()[0]=='failed'

def test_codex_and_claude_recovery_use_distinct_owned_coordinators(env,monkeypatch):
    from lib import turn_dispatch,config
    from types import SimpleNamespace
    monkeypatch.setattr(config,'load',lambda:SimpleNamespace(codex_account_switch_command=('codex-selector',),claude_account_switch_command=('claude-selector',)))
    assert turn_dispatch.account_failover('codex') is not turn_dispatch.account_failover('claude')
    assert turn_dispatch.account_selector('codex')==('codex-selector',)
    assert turn_dispatch.account_selector('claude')==('claude-selector',)

def test_pausing_adopted_keeper_does_not_revive_rigid_scheduler(env):
    _,owners=env;c=owners['heartbeat-decider']
    c=janitors.set_enabled(c['session'],c['revision'],False)
    c=janitors.set_enabled(c['session'],c['revision'],True)
    janitors.set_enabled(c['session'],c['revision'],False)
    assert heartbeat.HeartbeatScheduler(send_heartbeat=lambda *_:pytest.fail('Rigid loop revived')).run_once()==0

def test_quota_pending_delivery_retries_same_id_without_new_model(env):
    _,owners=env;owner=owners['quota-monitor'];import json
    payload={'agent_id':owner['agent_id'],'owner_generation':owner['generation'],'notification_id':'stable'}
    db.conn().execute('INSERT INTO janitor_quota_receipts VALUES (?,?,?,NULL,?)',('stable','run',json.dumps(payload),db.now_ms()))
    calls=[]
    def fail(value):calls.append(value['notification_id']);raise RuntimeError('Transport offline')
    w=service.AutonomyJanitors(lambda *_:True,fail)
    with pytest.raises(RuntimeError):w.deliver_notifications(owner)
    assert db.conn().execute('SELECT delivery_json FROM janitor_quota_receipts').fetchone()[0]is None
    w.notify=lambda v:calls.append(v['notification_id'])or {'sent':1}
    w.deliver_notifications(owner);assert calls==['stable','stable']
    w.deliver_notifications(owner);assert len(calls)==2

def test_dispatch_guard_rechecks_stop_after_persisted_decision(env):
    aid,_=env
    def dispatch(session,message,request):
        assert service.validate_dispatch(session,request)
        agents.record_state(aid,'interrupted',{'source':'user_stop'})
        assert not service.validate_dispatch(session,request)
        return False
    w=service.AutonomyJanitors(dispatch,lambda _:None,lambda *_:{'action':'wake','delay_seconds':60,'message':'Continue current work','reason':'Pending task'})
    w.heartbeat_once()
    assert db.conn().execute('SELECT status FROM janitor_continuity').fetchone()[0]=='cancelled'

def test_empty_model_inference_does_not_auto_inherit_global_chain(env):
    _,owners=env
    from lib import janitor_design_policy,model_fallbacks
    owner=owners['heartbeat-decider']
    janitor_design_policy.configure({'model_chain':[{'provider':'codex','model':'global'}]},0)
    assert janitor_design_policy.effective_chain(owner['session'])['source']=='explicit'
    assert model_fallbacks.get(owner['agent_id'])['revision']==0

def test_unbounded_fallback_validation_keeps_each_distinct_model(env):
    from lib import model_fallbacks
    chain=[{'backend':'codex','model':f'gpt-model-{i}','effort':'low'}for i in range(10)]
    assert len(model_fallbacks.validate(chain))==10

def test_exhaustion_is_a_new_event_after_low_quota_warning(env):
    _,owners=env;owner=owners['quota-monitor'];window={'window_id':'one-window'}
    assert service.quota_crossing('codex','a',window,80,owner,100)['remaining']==20
    assert service.quota_crossing('codex','a',window,100,owner,101)['remaining']==0
    assert service.quota_crossing('codex','a',window,100,owner,102)is None

def test_runtime_dispatch_rejects_forged_heartbeat_request(env):
    from lib.turn_dispatch import TurnDispatchService,DispatchError
    from types import SimpleNamespace
    with pytest.raises(DispatchError) as e:
        TurnDispatchService(SimpleNamespace()).dispatch(text='wake',requested_session='task',trace_id='janitor-demand-forged',client_msg_id='janitor-demand-forged',origin='heartbeat')
    assert e.value.status==409
