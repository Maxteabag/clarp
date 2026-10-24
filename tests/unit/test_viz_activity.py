import json
import time
from lib import db,viz_activity


def test_activity_cursors_keep_every_kind_and_filter_future_records():
    con=db.conn()
    for i in range(205):con.execute('INSERT INTO state_log(agent_id,ts,kind,detail) VALUES(?,?,?,?)',('a',i+1,'tool' if i%2 else 'thinking',json.dumps({'tool':'Bash','input':{'cmd':'echo hello'}})))
    first=viz_activity.build(con,205)
    assert len(first['items'])==200 and first['more']
    earlier=viz_activity.build(con,205,before=first['items'][0]['id'])
    assert len(earlier['items'])==5 and not earlier['more']
    assert len(viz_activity.build(con,205,after=200)['items'])==5
    assert all(r['ts']<=10 for r in viz_activity.build(con,10)['items'])
    assert {r['kind'] for r in first['items']}=={'thinking','tool'}


def test_activity_reuses_exact_cached_explanations_without_generating_or_leaking_secrets():
    con=db.conn();now=int(time.time()*1000)
    activity={'kind':'tool','name':'Bash','input':{'cmd':'echo hello'}}
    con.execute("INSERT INTO tool_explanation_jobs(cache_key,detail_level,activity_json,status,created_at,available_at) VALUES('key',2,?,'ready',?,?)",(json.dumps({'activity':activity}),now-20,now-20))
    con.execute("INSERT INTO tool_explanation_cache VALUES('key','Print a greeting',?,?)",(now-10,now+10000))
    con.execute("INSERT INTO state_log(agent_id,ts,kind,detail) VALUES('a',?,'tool',?)",(now,json.dumps({'tool':'Bash','input':{'cmd':'echo hello'}})))
    assert viz_activity.build(con,now)['items'][0]['explanation']=='Print a greeting'
    con.execute("INSERT INTO state_log(agent_id,ts,kind,detail) VALUES('a',?,'tool',?)",(now,json.dumps({'tool':'Bash','input':{'cmd':'curl token=TOPSECRET'}})))
    row=viz_activity.build(con,now)['items'][-1]
    assert row['explanation'] is None
    assert 'TOPSECRET' not in json.dumps(row)
    con.execute("UPDATE tool_explanation_cache SET created_at=?",(now+1,))
    assert viz_activity.build(con,now)['items'][0]['explanation'] is None
