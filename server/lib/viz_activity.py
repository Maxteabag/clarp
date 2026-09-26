"""Read the same durable state stream Clarp records, without generating text."""
import json
import time
from .tool_explanations import normalize_activity, snippet


def build(con, until, after=0, before=0):
    until=min(until,int(time.time()*1000));limit=200
    clauses=['s.ts<=?'];params=[until]
    if after:clauses.append('s.state_id>?');params.append(after)
    if before:clauses.append('s.state_id<?');params.append(before)
    order='ASC' if after else 'DESC'
    rows=con.execute('SELECT s.*,a.persona FROM state_log s LEFT JOIN agents a USING(agent_id) WHERE '+' AND '.join(clauses)+f' ORDER BY s.state_id {order} LIMIT ?',(*params,limit+1)).fetchall()
    more=len(rows)>limit;rows=rows[:limit]
    if not after:rows=rows[::-1]
    cached={}
    cache_rows=con.execute('SELECT j.activity_json,c.explanation FROM tool_explanation_jobs j JOIN tool_explanation_cache c USING(cache_key) WHERE c.created_at<=? AND c.expires_at>? ORDER BY c.created_at DESC LIMIT 500',(until,int(time.time()*1000))).fetchall()
    for row in cache_rows:
        try:activity=json.loads(row['activity_json']).get('activity',{})
        except (ValueError,AttributeError):continue
        key=json.dumps(activity,sort_keys=True)
        if key not in cached:cached[key]=row['explanation']
    result=[]
    for row in rows:
        try:detail=json.loads(row['detail'] or '{}')
        except ValueError:detail={}
        if not isinstance(detail,dict):detail={}
        inputs=detail.get('input') if isinstance(detail.get('input'),dict) else {}
        tool=detail.get('tool') or detail.get('name') or row['kind']
        raw=inputs.get('command') or inputs.get('cmd') or detail.get('command') or detail.get('message') or detail.get('text') or detail.get('summary') or tool
        explanation=detail.get('explanation')
        if not isinstance(explanation,str):
            candidates=[{'kind':'tool','name':tool,'input':inputs},{'kind':'tool','name':tool,'command':raw}]
            explanation=next((cached.get(json.dumps(normalize_activity(a),sort_keys=True)) for a in candidates if json.dumps(normalize_activity(a),sort_keys=True) in cached),None)
        result.append({'id':row['state_id'],'agent_id':row['agent_id'],'agent':row['persona'] or row['agent_id'][:8],'ts':row['ts'],'kind':row['kind'],
                       'tool':snippet(tool,120),'text':snippet(raw,1200),'explanation':snippet(explanation,500) if explanation else None,
                       'status':snippet(detail.get('status') or detail.get('phase') or '',80),'details':snippet(detail,2200)})
    return {'items':result,'more':more,'cursor':result[-1]['id'] if result else after,'transport':'Recorded Clarp state stream; polled once a second'}
