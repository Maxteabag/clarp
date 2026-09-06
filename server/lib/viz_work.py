"""Work objects: recorded intent, attributed evidence and published outcomes.

Three separate facts feed one persistent visual object. A task plan is the
agent's own declaration of intent. Tool events are attributed to a plan by the
same agent identity and time overlap; that is an honest basis, not causal
proof, and the map says so. Artifacts are recorded outcomes with a media asset
the host may preview. Agent-origin prompt admissions that name a plan ID are
verified handoffs of that object. Nothing here counts tool calls as progress.
"""
from __future__ import annotations
import json
import re

PLAN_ID=re.compile(r'[0-9a-f]{16}:[A-Za-z0-9._-]{1,80}:[0-9a-f]{8}')
PREVIEW_MIME={'image/png','image/jpeg','image/webp','image/gif'}
CONTRACT={
    'intent':'task_plans and task_items are the agent\'s declared intent and status; titles are not measured progress.',
    'evidence':'tool events by the same agent between plan creation and completion (plus a short publishing tail) are attributed by identity and time overlap only.',
    'validation':'tests, builds and lint runs are recognized from the recorded command; a chain joined by && reports one exact outcome, a script using ; or || does not.',
    'outcome':'artifacts published by the same agent session inside the plan window; previews are host-loaded thumbnails of the recorded media asset, never fetched by generated source.',
    'handoff':'agent-origin prompt admissions that name a plan ID transfer that object; other agent messages are collaboration threads without transfer.',
    'unknown':'a finished plan without a recorded artifact is shown closed without outcome; an event outside any plan window is ordinary activity.'}


def _payload(text):
    if not isinstance(text,str) or len(text)>400000:return {}
    try:data=json.loads(text)
    except (ValueError,TypeError):return {}
    return data if isinstance(data,dict) else {}


def _asset(con,url):
    if not isinstance(url,str) or not url.startswith('/media/'):return None
    asset_id=url[len('/media/'):].split('?')[0].strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,80}',asset_id):return None
    try:row=con.execute('SELECT mime_type,width,height,bytes FROM media_assets WHERE asset_id=? AND deleted_at IS NULL',(asset_id,)).fetchone()
    except Exception:return None
    if not row or row['mime_type'] not in PREVIEW_MIME:return None
    return {'url':'/media/'+asset_id,'mime':row['mime_type'],'width':row['width'],'height':row['height']}


def artifact_record(con,row,names):
    p=_payload(row['payload_json']);kind=row['type']
    record={'id':row['artifact_id'],'type':kind,'title':row['title'][:120],'summary':(row['summary'] or '')[:160],'status':row['status'],
            'agent_id':row['agent_id'],'agent':names.get(row['agent_id'],row['agent_id'][:8]),'session':row['session'],
            'created_at':row['created_at'],'completed_at':row['completed_at']}
    preview=_asset(con,p.get('thumbnail_url')) or (_asset(con,p.get('url')) if kind in {'image','image_gallery'} or str(p.get('mime_type','')).startswith('image/') else None)
    if preview:record['preview']=preview
    media={k:p[k] for k in ('mime_type','file_name','duration_ms','size_bytes') if k in p and isinstance(p[k],(str,int,float))}
    if media:record['media']=media
    if kind=='workflow_run':
        record['run']={k:str(p.get(k,''))[:120] for k in ('workflow_name','repository','branch','commit','conclusion','github_status','run_url')}
        repo=record['run']['repository']
        if re.fullmatch(r'[^/\s]+/[^/\s]+',repo):record['remote_target']='github:'+repo
    if kind in {'research','document'} and isinstance(p.get('sources'),list):
        record['sources']=[{'title':str(s.get('title',''))[:80],'url':str(s.get('url',''))[:200]} for s in p['sources'][:8] if isinstance(s,dict)]
        record['source_count']=len(p['sources'])
    if kind=='plan' and isinstance(row['reference_id'],str):record['plan_id']=row['reference_id']
    return record


def build(con,since,until,names,limit_plans=40,limit_artifacts=60,limit_messages=40):
    until=until if until<(1<<61) else 1<<62
    try:
        plan_rows=con.execute('SELECT plan_id,agent_id,session,title,status,created_at,updated_at,completed_at FROM task_plans '
            'WHERE created_at<=? AND ((completed_at IS NULL AND updated_at>=?) OR completed_at>=?) ORDER BY created_at DESC LIMIT ?',
            (until,since-86400000,since,limit_plans)).fetchall()
    except Exception:
        return {'plans':[],'artifacts':[],'messages':[],'contract':CONTRACT,'available':False}
    plans=[]
    for r in plan_rows:
        items=con.execute('SELECT item_id,title,status,position,started_at,completed_at FROM task_items WHERE plan_id=? AND parent_id IS NULL ORDER BY position LIMIT 12',(r['plan_id'],)).fetchall()
        total=con.execute('SELECT count(*) FROM task_items WHERE plan_id=?',(r['plan_id'],)).fetchone()[0]
        plans.append({'id':r['plan_id'],'title':r['title'][:120],'status':r['status'],'agent_id':r['agent_id'],'agent':names.get(r['agent_id'],r['agent_id'][:8]),
            'session':r['session'],'created_at':r['created_at'],'updated_at':r['updated_at'],'completed_at':r['completed_at'],'item_total':total,
            'items':[{'id':i['item_id'].rsplit(':',1)[-1],'title':i['title'][:90],'status':i['status'],'position':i['position'],'started_at':i['started_at'],'completed_at':i['completed_at']} for i in items]})
    known={p['id'] for p in plans}
    artifact_rows=con.execute('SELECT artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,completed_at FROM artifacts '
        "WHERE created_at BETWEEN ? AND ? AND deleted_at IS NULL AND type NOT IN ('plan','live_task','question','decision') ORDER BY created_at DESC LIMIT ?",
        (since,until,limit_artifacts)).fetchall()
    artifacts=[artifact_record(con,r,names) for r in artifact_rows]
    messages=[]
    try:
        message_rows=con.execute("SELECT admission_id,sender_agent_id,agent_id,session,observed_at,original_text,trace_id FROM prompt_admissions "
            "WHERE origin='agent' AND sender_agent_id!='' AND observed_at BETWEEN ? AND ? ORDER BY observed_at DESC LIMIT ?",(since,until,limit_messages)).fetchall()
    except Exception:message_rows=[]
    for r in message_rows:
        text=r['original_text'] or ''
        messages.append({'id':r['admission_id'],'from_agent_id':r['sender_agent_id'],'from':names.get(r['sender_agent_id'],r['sender_agent_id'][:8]),
            'to_agent_id':r['agent_id'],'to':names.get(r['agent_id'],r['agent_id'][:8]),'session':r['session'],'ts':r['observed_at'],
            'plan_ids':sorted({m for m in PLAN_ID.findall(text) if m in known}),'excerpt':re.sub(r'\s+',' ',text)[:140]})
    return {'plans':plans[::-1],'artifacts':artifacts[::-1],'messages':messages[::-1],'contract':CONTRACT,'available':True}
