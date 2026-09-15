from lib.oracle_progress import ProgressCadence, valid_interval
from lib import oracle_live


def test_preferences_are_bounded_and_cannot_smuggle_model_or_tool_commands():
    import json
    for value in [True,-1,1,9,121,20.0,'20']:
        assert not valid_interval(value)
        assert oracle_live.client_event(json.dumps({'type':'oracle_v2.preferences','progress_interval_seconds':value})) is None
    event=oracle_live.client_event(json.dumps({'type':'oracle_v2.preferences','progress_interval_seconds':20,'model':'other','tool':'write'}))
    assert event=={'type':'oracle_v2.preferences','progress_interval_seconds':20}


def test_no_chatter_without_work_and_one_consolidated_update_for_many_agents():
    cadence=ProgressCadence();cadence.configure(20,100)
    state=dict(items=[],routing=0,last_user=0,last_output=0,last_append=0,pending_result=False)
    assert cadence.opportunity(now=150,**state) is None
    state['items']=[{'session':f'worker-{n}','status':'accepted','request':'Investigate fixture'} for n in range(10)]
    assert cadence.opportunity(now=150,**state) is None
    assert cadence.opportunity(now=169,**state) is None
    facts=cadence.opportunity(now=170,**state)
    assert len(facts['active'])==10 and facts['completed_count']==0
    cadence.offered(170)
    assert cadence.opportunity(now=171,**state) is None


def test_user_speech_and_available_findings_take_precedence_over_periodic_status():
    cadence=ProgressCadence();cadence.configure(10,100)
    state=dict(items=[{'session':'sage','status':'accepted','request':'Check invoice'}],routing=0,
               last_user=0,last_output=0,last_append=0,pending_result=False)
    cadence.opportunity(now=100,**state)
    assert cadence.opportunity(now=111,**{**state,'last_user':110.5}) is None
    assert cadence.opportunity(now=111,**{**state,'pending_result':True}) is None
    assert cadence.opportunity(now=111,**{**state,'last_output':108}) is None
    assert cadence.opportunity(now=121,**state) is not None
    cadence.configure(0,121)
    assert cadence.opportunity(now=200,**state) is None
