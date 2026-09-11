import json
import pytest
from lib import db, viz_recap


def seed():
    con = db.conn()
    con.execute("INSERT INTO agents(agent_id,persona,voice_id,cwd,session,created_at) VALUES('agent-a','Ada','v','/work','ada',1)")
    def output(identity, kind='document', created=150, updated=150, payload=None):
        con.execute("INSERT INTO artifacts(artifact_id,agent_id,session,type,title,summary,status,payload_json,created_at,updated_at) VALUES(?,'agent-a','ada',?,'Report','Useful result','ready',?,?,?)", (identity, kind, json.dumps(payload or {'content':'Report text'}), created, updated))
    return con, output


def test_all_outputs_updates_and_open_tasks_are_retained():
    con, output = seed()
    output('first');output('second');output('updated',created=20,updated=155)
    output('old',created=20,updated=20);output('future',created=250,updated=250)
    output('deleted');con.execute("UPDATE artifacts SET deleted_at=160 WHERE artifact_id='deleted'")
    con.execute("INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at) VALUES('plan-a','agent-a','ada','Finish report','active',10,20)")
    result=viz_recap.build(con,100,200,now=300)
    group=result['groups'][0]
    assert {a['id'] for a in group['artifacts']} == {'first','second','updated'}
    assert group['plans'][0]['title']=='Finish report'
    assert result['counts']['artifacts']==3
    assert 'published' in result['coverage'].lower()


def test_deleted_and_unsafe_content_cannot_be_opened():
    con, output=seed()
    output('unsafe',payload={'url':'javascript:alert(1)','content':'<script>bad</script>'})
    record=viz_recap.build(con,100,200,now=300)['groups'][0]['artifacts'][0]
    assert record['link'] is None and record['readable']
    assert viz_recap.content(con,'unsafe')['content']=='<script>bad</script>' # browser must render inertly
    con.execute("UPDATE artifacts SET deleted_at=160 WHERE artifact_id='unsafe'")
    assert viz_recap.content(con,'unsafe') is None


def test_open_work_survives_a_review_boundary_without_new_activity():
    con, output=seed()
    output('pending',kind='decision')
    con.execute("INSERT INTO artifact_decisions(decision_id,artifact_id,question,status) VALUES('pending','pending','Approve?','pending')")
    con.execute("INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at) VALUES('open','agent-a','ada','Still working','active',10,20)")
    result=viz_recap.build(con,200,300,now=300)
    assert result['counts']['artifacts']==0
    assert result['groups'][0]['plans'][0]['id']=='open'
    assert result['groups'][0]['attention'][0]['id']=='pending'


def test_limits_are_disclosed_and_dates_are_validated(monkeypatch):
    con, output=seed();output('a');output('b')
    monkeypatch.setattr(viz_recap,'LIMIT',1)
    result=viz_recap.build(con,100,200,now=300)
    assert result['truncated']==['artifacts']
    for since,until in [(200,100),(-1,200),(0,91*viz_recap.DAY),(100,100000)]:
        with pytest.raises(ValueError):viz_recap.build(con,since,until,now=300)


def test_archived_agent_is_named_and_future_links_are_safe():
    con, output=seed();output('a',payload={'content':'x','url':'https://example.com/report'})
    con.execute("UPDATE agents SET archived_at=180 WHERE agent_id='agent-a'")
    g=viz_recap.build(con,100,200,now=300)['groups'][0]
    assert g['name']=='Ada' and g['archived']
    assert g['artifacts'][0]['link']=='https://example.com/report'


def test_last_user_request_is_per_agent_and_attention_is_current(monkeypatch):
    con, output=seed()
    for i in range(5):
        con.execute("INSERT INTO prompt_admissions(admission_id,admission_version,authenticated_at_admission,cooperative_principal,origin,channel,observed_at,client_admission_id,trace_id,agent_id,session,message_id,original_text) VALUES(?,1,1,'user','user','pwa',?,?,'t','agent-a','ada','m',?)", (str(i),150+i,str(i),'Request '+str(i)))
    monkeypatch.setattr(viz_recap,'LIMIT',2)
    output('pending',kind='decision');output('expired',kind='decision')
    con.execute("INSERT INTO artifact_decisions(decision_id,artifact_id,question,status,expires_at) VALUES('pending','pending','Approve?','pending',400)")
    con.execute("INSERT INTO artifact_decisions(decision_id,artifact_id,question,status,expires_at) VALUES('expired','expired','Old?','pending',250)")
    result=viz_recap.build(con,100,200,now=300)
    assert result['truncated']==[]
    group=result['groups'][0]
    assert group['request']['text']=='Request 4'
    assert [a['id'] for a in group['attention']]==['pending']
    assert group['artifacts']==[]


def test_handler_rejects_invalid_window_and_missing_artifact(monkeypatch):
    import importlib.util
    import pathlib
    import sys
    import types
    # Other suites install a small fake module under the generic name server.
    # Exercise this checkout's HTTP handler even with that cache entry present.
    monkeypatch.setitem(sys.modules, 'server', types.SimpleNamespace(Handler=object))
    spec=importlib.util.spec_from_file_location('fleet_recap_http_test', pathlib.Path(__file__).resolve().parents[2] / 'server/server.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Handler=module.Handler
    class Request:
        path='/viz/recap?since=bad'
        def _send(self, status, body, content_type):
            self.response=(status,json.loads(body))
    req=Request();Handler._handle_viz_recap(req)
    assert req.response[0]==400
    req.path='/viz/recap/artifact?id=missing';Handler._handle_viz_recap_artifact(req)
    assert req.response[0]==404
