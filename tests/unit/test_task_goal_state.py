"""Outcome commitment semantics use the actual isolated SQLite store."""
import json
import pytest
from lib import agents,db,task_plans,task_goal_state as goals,task_goal_recovery as recovery


def make_goal(tmp_path, **options):
    agent=agents.create_agent(persona='Goal',voice_id='v',cwd=str(tmp_path),session='goal-owner')
    agent=agents.get_by_session('goal-owner')
    agents.start_runtime(agent['agent_id'],agent['session'])
    agents.bind_backend_session(agent['agent_id'],'native-goal-test')
    return task_plans.create(session=agent['session'],title='Ship outcome',items=[{'id':'method','title':'Initial method'}],
        goal={'criteria':['Outcome works','Tests prove it'],'limits':'No deployment','enroll':True,**options})


def act(plan,action,data):
    return goals.mutate(plan['plan_id'],revision=plan['revision'],action=action,data=data)


def test_pending_and_skipped_work_never_claims_completion(tmp_path):
    p=make_goal(tmp_path)
    with pytest.raises(ValueError,match='required work'): task_plans.finish(p['plan_id'],revision=0)
    with pytest.raises(ValueError,match='reason'): task_plans.update_item(p['items'][0]['item_id'],'skipped',revision=0)
    p=task_plans.update_item(p['items'][0]['item_id'],'skipped','Need another approach',revision=0)
    assert p['completed_count']==0 and p['status']=='active'
    assert p['goal']['history'][-1]['detail']['reason']=='Need another approach'
    with pytest.raises(ValueError,match='required work'): task_plans.finish(p['plan_id'],revision=p['revision'])


def test_replanning_discovery_preserves_outcome_and_supersedes_wake(tmp_path):
    p=make_goal(tmp_path)
    claimed=recovery._claim(p['plan_id'],db.now_ms()+130000)
    old_request=claimed[1]['continuation']['request_id']
    p=act(p,'replan',{'reason':'Probe shows a built-in path already exists','steps':[{'id':'reuse','title':'Verify and reuse existing behavior'}]})
    p=act(p,'checkpoint',{'progress':'Existing path discovered','next_work':'Inspect new probe results, choose whether to reuse',
        'continuation':{'kind':'timer','due_at':db.now_ms()+1000}})
    with pytest.raises(ValueError,match='superseded'): recovery.validate_dispatch(agents.get_by_session('goal-owner'),old_request)
    assert p['goal']['outcome']=='Ship outcome'
    assert next(i for i in p['items'] if i['status']=='removed')['detail'].startswith('Probe')
    item=next(i for i in p['items'] if i['status']=='pending')
    p=task_plans.update_item(item['item_id'],'completed','Actual probe passed',revision=p['revision'])
    with pytest.raises(ValueError,match='evidence'): task_plans.finish(p['plan_id'],revision=p['revision'])
    p=act(p,'checkpoint',{'progress':'Verified outcome','next_work':'Close with evidence','evidence':{'criterion-1':'probe.json passed','criterion-2':'test output passed'}})
    p=task_plans.finish(p['plan_id'],revision=p['revision'])
    assert p['status']=='completed' and p['completed_count']==1 and p['counts']['removed']==1


def test_concurrent_edit_rejected_and_checkpoint_atomic(tmp_path):
    p=make_goal(tmp_path)
    updated=act(p,'checkpoint',{'progress':'One result','next_work':'Reassess','continuation':{'kind':'dependency','key':'job-1','reason':'Build running','due_at':db.now_ms()+60000}})
    with pytest.raises(ValueError,match='changed'): act(p,'cancel',{'reason':'Stale request'})
    before=task_plans.get(p['plan_id'])
    with pytest.raises(ValueError,match='dependency'): act(updated,'checkpoint',{'progress':'Invalid','next_work':'Next','evidence':{'criterion-1':'must rollback'},'continuation':{'kind':'dependency'}})
    assert task_plans.get(p['plan_id'])==before


@pytest.mark.parametrize('outcome',['succeeded','failed'])
def test_external_result_is_durable_and_idempotent_by_revision(tmp_path,outcome):
    p=make_goal(tmp_path)
    p=act(p,'checkpoint',{'progress':'Launched job','next_work':'Inspect job result','continuation':{'kind':'dependency','key':'job-1','reason':'Build running','due_at':db.now_ms()+60000}})
    old=p
    p=act(p,'dependency',{'key':'job-1','outcome':outcome,'evidence':'build.log'})
    assert p['goal']['continuation']['dependency_result']['outcome']==outcome
    with pytest.raises(ValueError,match='changed'): act(old,'dependency',{'key':'job-1','outcome':outcome,'evidence':'build.log'})


@pytest.mark.parametrize('action',['pause','cancel','block'])
def test_goal_control_fences_existing_wake(tmp_path,action):
    p=make_goal(tmp_path)
    claim=recovery._claim(p['plan_id'],db.now_ms()+130000)
    request=claim[1]['continuation']['request_id']
    act(p,action,{'reason':'Owner requested this'})
    assert recovery.tick(lambda *args: pytest.fail('must not dispatch'),now=db.now_ms()+999999)==0
    with pytest.raises(ValueError): recovery.validate_dispatch(agents.get_by_session('goal-owner'),request)


def test_duplicate_claim_and_restart_reuse_same_receipt(tmp_path):
    p=make_goal(tmp_path);now=db.now_ms()+130000
    first=recovery._claim(p['plan_id'],now)
    assert recovery._claim(p['plan_id'],now) is None
    # Expired lease models process loss after claiming, before receiving dispatch result.
    second=recovery._claim(p['plan_id'],now+61000)
    assert second[1]['continuation']['request_id']==first[1]['continuation']['request_id']
    request=first[1]['continuation']['request_id']
    recovery._result(p['plan_id'],request,now+61000,'admitted','receipt')
    third=recovery._claim(p['plan_id'],now+999999)
    assert third[1]['continuation']['request_id']!=request


def test_wrong_native_owner_suppresses_wake(tmp_path):
    p=make_goal(tmp_path)
    agents.bind_backend_session(p['agent_id'],'different-native')
    assert recovery.tick(lambda *args: pytest.fail('wrong target'),now=db.now_ms()+999999)==0
    assert task_plans.get(p['plan_id'])['goal']['continuation']['observed_state']=='owner_changed'


def test_native_provider_loop_never_competes(tmp_path):
    from lib import agent_goals
    p=make_goal(tmp_path)
    agent_goals.upsert(p['agent_id'],session=p['session'],backend='codex',goal={'objective':'Native objective','status':'active'})
    assert recovery.tick(lambda *args: pytest.fail('competing scheduler'),now=db.now_ms()+999999)==0
    assert task_plans.get(p['plan_id'])['goal']['continuation']['observed_state']=='native_owned'
