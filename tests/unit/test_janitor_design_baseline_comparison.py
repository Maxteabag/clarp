"""Actual existing scheduler vs candidate gate against the same isolated durable state."""
from lib import agents,db,heartbeat,task_plans
from lib import janitor_design_policy as policy
from lib.protocol import AgentState

def test_existing_and_gated_scheduler_decisions(monkeypatch):
    monkeypatch.setattr(heartbeat,'_wake_from_external_signal',lambda *a:None)
    monkeypatch.setattr(heartbeat,'_recent_real_activity_reason',lambda *a:'')
    monkeypatch.setattr(heartbeat.backends,'active_handles',lambda *a:[])
    aid=agents.create_agent(persona='Review fixture',voice_id='v',cwd='/tmp',session='fixture',backend='codex')
    agents.record_state(aid,AgentState.DONE)
    row=agents.get_by_agent_id(aid)
    assert heartbeat._skip_reason(agent=row,state=heartbeat._HeartbeatState(),now=2000000)==''
    policy.configure({'heartbeat_gate':True},0)
    assert heartbeat._skip_reason(agent=row,state=heartbeat._HeartbeatState(),now=2000000)=='no-actionable-commitment'
    plan=task_plans.create(session='fixture',title='Current commitment',items=[{'id':'work','title':'Advance'}])
    assert heartbeat._skip_reason(agent=row,state=heartbeat._HeartbeatState(),now=2000000)==''
    task_plans.update_item(plan['items'][0]['item_id'],'blocked')
    assert heartbeat._skip_reason(agent=row,state=heartbeat._HeartbeatState(),now=2000000)=='no-actionable-commitment'
    # No effect or queued turn is created by decision inspection.
    assert db.conn().execute('SELECT count(*) FROM queued_turns').fetchone()[0]==0
