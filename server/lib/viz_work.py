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
    'validation':'tests, builds and lint runs are recognized from the recorded command; a single command reports its own result, an && chain proves success of every segment but not which segment failed, a script using ; or || proves neither; recovery requires later exact success of the same checks.',
    'outcome':'artifacts published by the same agent session inside the plan window; previews are host-loaded thumbnails of the recorded media asset, never fetched by generated source.',
    'handoff':'agent-origin prompt admissions that name a plan ID are references to that object, drawn as a knot on the thread; transfer is shown only for an explicit handoff record, which is not yet recorded.',
    'unknown':'a finished plan without a recorded artifact is shown closed without outcome; an event outside any plan window is ordinary activity.',
    'waiting':'background_jobs are recorded dependencies on an external boundary (GitHub Actions, TestFlight, a test lane, a Host update, a service); a running job is waiting, a terminal record is its result, an expired heartbeat is a wait that ended without evidence. Pending decisions are waits on the owner. Quiet time is never drawn as waiting.',
    'routes':'repeated observed interactions may leave a browser-local historical route; a route is a pattern, never a dependency or a cause.'}

BOUNDARIES={'github-workflow':('GitHub Actions','github'),'release':('TestFlight','apple'),'external_test_watch':('Test lane','lane'),
            'server-update':('Host update','host'),'service':('Service','service'),'build':('Build lane','lane'),'other':('External wait','unknown')}


def _json_field(text,limit=2000):
    if not isinstance(text,str) or len(text)>limit:return {}
    try:data=json.loads(text)
    except (ValueError,TypeError):return {}
    return data if isinstance(data,dict) else {}


def jobs(con,since,until,names,limit=60):
    """Recorded waits: each background job names the boundary it depends on."""
    try:
        rows=con.execute('SELECT job_id,agent_id,session,kind,title,detail,status,started_at,updated_at,terminal_at,terminal_reason,heartbeat_at,heartbeat_timeout_ms,metadata_json '
            'FROM background_jobs WHERE started_at<=? AND (terminal_at IS NULL OR terminal_at>=?) AND updated_at>=? ORDER BY started_at DESC LIMIT ?',
            (until,since,since-86400000,limit)).fetchall()
    except Exception:return []
    result=[]
    for r in rows:
        label,boundary=BOUNDARIES.get(r['kind'],('External wait','unknown'))
        meta=_json_field(r['metadata_json'])
        link=meta.get('run_url') or meta.get('url')
        result.append({'id':r['job_id'],'agent_id':r['agent_id'],'agent':names.get(r['agent_id'],r['agent_id'][:8]),'session':r['session'],'kind':r['kind'],
            'boundary':boundary,'boundary_label':label,'title':(r['title'] or '')[:90],'detail':(r['detail'] or '')[:160],'status':r['status'],
            'started_at':r['started_at'],'updated_at':r['updated_at'],'terminal_at':r['terminal_at'],'terminal_reason':(r['terminal_reason'] or '')[:80],
            'heartbeat_at':r['heartbeat_at'],'heartbeat_timeout_ms':r['heartbeat_timeout_ms'],
            **({'link':str(link)[:200]} if isinstance(link,str) and link.startswith('https://') else {})})
    return result[::-1]


def decisions(con,since,until,names,limit=30):
    """Waits on the owner: pending or recently resolved decision artifacts."""
    try:
        rows=con.execute('SELECT d.decision_id,d.status,d.resolved_at,d.blocks_progress,d.urgency,d.deadline_at,d.expires_at,a.agent_id,a.session,a.title,a.created_at '
            'FROM artifact_decisions d JOIN artifacts a USING(artifact_id) WHERE a.created_at<=? AND (d.resolved_at IS NULL OR d.resolved_at>=?) AND a.created_at>=? AND a.deleted_at IS NULL '
            'ORDER BY a.created_at DESC LIMIT ?',(until,since,since-86400000,limit)).fetchall()
    except Exception:return []
    return [{'id':r['decision_id'],'agent_id':r['agent_id'],'agent':names.get(r['agent_id'],r['agent_id'][:8]),'session':r['session'],'title':(r['title'] or '')[:90],
             'status':r['status'],'created_at':r['created_at'],'resolved_at':r['resolved_at'],'blocks_progress':bool(r['blocks_progress']),'urgency':r['urgency'],
             'deadline_at':r['deadline_at'],'expires_at':r['expires_at']} for r in rows][::-1]


def _payload(text):
    if not isinstance(text,str) or len(text)>400000:return {}
    try:data=json.loads(text)
    except (ValueError,TypeError):return {}
    return data if isinstance(data,dict) else {}


MAX_PREVIEW_BYTES=8*1024*1024
MAX_PREVIEW_SIDE=16384
MAX_PREVIEW_PIXELS=40_000_000


def _asset_row(con,url):
    if not isinstance(url,str) or not url.startswith('/media/'):return None
    asset_id=url[len('/media/'):].split('?')[0].strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,80}',asset_id):return None
    try:row=con.execute('SELECT mime_type,width,height,bytes FROM media_assets WHERE asset_id=? AND deleted_at IS NULL',(asset_id,)).fetchone()
    except Exception:return None
    return (asset_id,row) if row else None


def _asset(con,url):
    """A preview is exposed only for a recorded image asset with validated,
    bounded dimensions and size. Unknown or zero dimensions are not previewed."""
    found=_asset_row(con,url)
    if not found:return None
    asset_id,row=found
    width,height,size=row['width'],row['height'],row['bytes']
    if row['mime_type'] not in PREVIEW_MIME:return None
    if not all(isinstance(v,int) and not isinstance(v,bool) for v in (width,height,size)):return None
    if not (0<width<=MAX_PREVIEW_SIDE and 0<height<=MAX_PREVIEW_SIDE and width*height<=MAX_PREVIEW_PIXELS and 0<size<=MAX_PREVIEW_BYTES):return None
    return {'url':'/media/'+asset_id,'mime':row['mime_type'],'width':width,'height':height}


def _media_url(con,url):
    """The actual recorded media route (any mime) for opening the artifact itself."""
    found=_asset_row(con,url)
    return '/media/'+found[0] if found else None


def artifact_record(con,row,names):
    p=_payload(row['payload_json']);kind=row['type']
    record={'id':row['artifact_id'],'type':kind,'title':row['title'][:120],'summary':(row['summary'] or '')[:160],'status':row['status'],
            'agent_id':row['agent_id'],'agent':names.get(row['agent_id'],row['agent_id'][:8]),'session':row['session'],
            'created_at':row['created_at'],'updated_at':row['updated_at'],'completed_at':row['completed_at']}
    preview=_asset(con,p.get('thumbnail_url')) or (_asset(con,p.get('url')) if kind in {'image','image_gallery'} or str(p.get('mime_type','')).startswith('image/') else None)
    if preview:record['preview']=preview
    media_url=_media_url(con,p.get('url'))
    if media_url:record['media_url']=media_url
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
        return {'plans':[],'artifacts':[],'messages':[],'jobs':[],'decisions':[],'contract':CONTRACT,'available':False}
    plans=[]
    for r in plan_rows:
        items=con.execute('SELECT item_id,title,status,position,started_at,completed_at FROM task_items WHERE plan_id=? AND parent_id IS NULL ORDER BY position LIMIT 12',(r['plan_id'],)).fetchall()
        total=con.execute('SELECT count(*) FROM task_items WHERE plan_id=?',(r['plan_id'],)).fetchone()[0]
        plans.append({'id':r['plan_id'],'title':r['title'][:120],'status':r['status'],'agent_id':r['agent_id'],'agent':names.get(r['agent_id'],r['agent_id'][:8]),
            'session':r['session'],'created_at':r['created_at'],'updated_at':r['updated_at'],'completed_at':r['completed_at'],'item_total':total,
            'items':[{'id':i['item_id'].rsplit(':',1)[-1],'title':i['title'][:90],'status':i['status'],'position':i['position'],'started_at':i['started_at'],'completed_at':i['completed_at']} for i in items]})
    known={p['id'] for p in plans}
    artifact_rows=con.execute('SELECT artifact_id,agent_id,session,type,title,summary,status,reference_id,payload_json,created_at,updated_at,completed_at FROM artifacts '
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
        plan_ids=sorted({m for m in PLAN_ID.findall(text) if m in known})
        # Naming a plan ID proves a reference, not a transfer. Transfer semantics
        # need an explicit handoff record, which Clarp does not yet write.
        messages.append({'id':r['admission_id'],'from_agent_id':r['sender_agent_id'],'from':names.get(r['sender_agent_id'],r['sender_agent_id'][:8]),
            'to_agent_id':r['agent_id'],'to':names.get(r['agent_id'],r['agent_id'][:8]),'session':r['session'],'ts':r['observed_at'],
            'plan_ids':plan_ids,'link':'reference' if plan_ids else None,'excerpt':re.sub(r'\s+',' ',text)[:140]})
    return {'plans':plans[::-1],'artifacts':artifacts[::-1],'messages':messages[::-1],'jobs':jobs(con,since,until,names),'decisions':decisions(con,since,until,names),
            'contract':CONTRACT,'available':True}
