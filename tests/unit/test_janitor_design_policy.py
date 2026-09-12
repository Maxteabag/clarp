import concurrent.futures
import pytest
from lib import agents,db,janitors,task_plans
from lib import janitor_design_policy as policy

def agent(name='worker'):
    return agents.create_agent(persona=name,voice_id='v',cwd='/tmp',session=name,backend='codex')

def test_gate_uses_current_plan_and_default_is_unchanged():
    aid=agent();assert policy.heartbeat_allowed(aid)
    policy.configure({'heartbeat_gate':True},0);assert not policy.heartbeat_allowed(aid)
    plan=task_plans.create(session='worker',title='Current task',items=[{'id':'act','title':'Advance task'}])
    assert policy.heartbeat_allowed(aid)
    task_plans.update_item(plan['items'][0]['item_id'],'completed')
    assert not policy.heartbeat_allowed(aid)

def test_policy_compare_and_swap_serializes_writers():
    def save(n):
        try:policy.configure({'quota_threshold':n},0);return True
        except janitors.JanitorError:return False
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(save,[20,30]))
    assert sum(results)==1

def test_quota_receipts_dedupe_and_stale_is_unknown(monkeypatch):
    monkeypatch.setattr(db,'now_ms',lambda:1000000)
    value={'provider':'p','account_id':'a','window_id':'w','remaining_percent':25,'observed_at_ms':999900}
    assert policy.observe_quota(value)['notify']
    assert not policy.observe_quota(value)['notify']
    assert not policy.observe_quota({**value,'observed_at_ms':999901,'remaining_percent':20})['notify']
    assert policy.observe_quota({**value,'observed_at_ms':1})['status']=='unknown'
    assert policy.observe_quota({**value,'window_id':'new'})['notify']
    policy.observe_quota({**value,'observed_at_ms':999902,'remaining_percent':60})
    assert policy.observe_quota({**value,'observed_at_ms':999903})['notify']

@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,101,True])
def test_invalid_quota_cannot_create_observation(bad):
    with pytest.raises(ValueError):policy.observe_quota({'provider':'p','account_id':'a','window_id':'w','remaining_percent':bad,'observed_at_ms':db.now_ms()})

def test_long_ordered_model_chain_persists_and_explicit_model_wins():
    aid=agent('janitor');agents.update_agent(aid,model='explicit-model');config=janitors.create('janitor')
    chain=[{'provider':'codex','model':f'model-{n}'} for n in range(30)]
    policy.configure({'model_chain':chain},0)
    assert policy.configuration()['model_chain']==chain
    assert policy.effective_chain('janitor')['source']=='explicit'
    with pytest.raises(ValueError):policy.configure({'model_chain':[chain[0],chain[0]]},1)

def test_receipt_read_after_scope_revocation_does_not_delete_effect(monkeypatch):
    import test_janitor_store as fixture
    monkeypatch.setattr(fixture.backends,'active_handles',lambda *a:[])
    _,target,config=fixture.setup();run=fixture.admit(config);fixture.review(run)
    assert len(policy.inspect_receipt(run['run_id'],'hugo')['results'])==1
    janitors.configure('sam',config['revision'],scope={'exclude_agent_ids':[target]})
    with pytest.raises(janitors.JanitorError) as error:policy.inspect_receipt(run['run_id'],'hugo')
    assert str(error.value)=='Receipt unavailable'
    assert db.conn().execute('SELECT count(*) FROM janitor_effects').fetchone()[0]==1

def test_partition_executor_overlaps_targets_and_replays_without_recompute(monkeypatch):
    from lib import backends
    from lib.janitor_partition_executor import execute_partitions
    import threading
    monkeypatch.setattr(backends,'active_handles',lambda *a:[])
    parts=[]
    for i in range(2):
        target=agent(f'target-{i}');agent(f'explainer-{i}')
        c=janitors.create(f'explainer-{i}',template_id='tool-explainer',scope={'agent_ids':[target]})
        janitors.set_enabled(c['session'],c['revision'],True)
        parts.append({'target_agent_id':target,'requests':[{'request_id':f'request-{i}','context':{'item_count':1}}]})
    barrier=threading.Barrier(2);calls=[]
    def compute(request):
        calls.append(request['request_id']);barrier.wait(timeout=5);return {'summary':'Executed deterministic fixture'}
    results=execute_partitions(parts,compute,workers=2)
    assert [r['status'] for r in results]==['accepted','accepted']
    again=execute_partitions(parts,lambda _:pytest.fail('Duplicate computation'),workers=2)
    assert [r['status'] for r in again]==['receipt','receipt']
    assert len(calls)==2
    with pytest.raises(ValueError):execute_partitions([parts[0],parts[0]],compute)

def test_model_chain_order_and_ambiguous_failure_never_falls_through(monkeypatch):
    from lib import backends
    monkeypatch.setattr(backends,'active_handles',lambda *a:[])
    agent('worker-models');janitors.create('worker-models')
    chain=[{'provider':'codex','model':f'm-{n}'} for n in range(25)]
    policy.configure({'model_chain':chain},0)
    called=[]
    def compute(request, model):
        called.append(model['model'])
        if model['model']!='m-24':raise policy.ModelUnavailableBeforeExecution()
        return {'summary':'Fallback computation'}
    result,trace=policy.compute_with_model_chain('worker-models',{},compute)
    assert called==[x['model'] for x in chain]
    assert result['summary']=='Fallback computation' and len(trace['attempts'])==25
    called.clear()
    def unknown(request,model):
        called.append(model)
        raise RuntimeError('Outcome unknown')
    with pytest.raises(RuntimeError):policy.compute_with_model_chain('worker-models',{},unknown)
    assert len(called)==1

def test_partition_model_fallback_commits_once_and_fences_global_policy_change(monkeypatch):
    from lib import backends
    from lib.janitor_partition_executor import execute_partitions
    monkeypatch.setattr(backends,'active_handles',lambda *a:[])
    target=agent('fallback-target');agent('fallback-worker')
    config=janitors.create('fallback-worker',template_id='tool-explainer',scope={'agent_ids':[target]})
    janitors.set_enabled(config['session'],config['revision'],True)
    chain=[{'provider':'codex','model':'first'},{'provider':'codex','model':'second'}]
    policy.configure({'model_chain':chain},0)
    parts=[{'target_agent_id':target,'requests':[{'request_id':'fallback-once'}]}];calls=[]
    def compute(request,model):
        calls.append(model['model'])
        if model['model']=='first':raise policy.ModelUnavailableBeforeExecution()
        return {'summary':'Accepted fallback'}
    assert execute_partitions(parts,compute,use_model_chain=True)[0]['status']=='accepted'
    assert calls==['first','second']
    assert execute_partitions(parts,lambda *_:pytest.fail('Already committed'),use_model_chain=True)[0]['status']=='receipt'
    parts[0]['requests']=[{'request_id':'policy-changed'}]
    def changed(request,model):
        policy.configure({'model_chain':[chain[1]]},1)
        return {'summary':'Must not publish'}
    assert execute_partitions(parts,changed,use_model_chain=True)[0]['status']=='stale_rejected'
    assert not db.conn().execute("SELECT 1 FROM janitor_demand_results WHERE result_json LIKE '%Must not publish%'").fetchone()
