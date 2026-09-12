"""Deterministic, source-owned artifact attention read model.
No inferred completion or age-based retention. Cursors restart on source changes.
Decisions and Janitor failures retain their dedicated authority endpoints.
"""
import base64,hashlib,json,time
from . import db,artifacts,countdown_attention
TYPES={'countdown','document','research','file','audio','video','code_change','data','release','directory','workflow_run','html_form'}
FAILURES={'failure','timed_out','action_required','startup_failure'}
class StaleCursor(ValueError): pass

def bucket(row, now_ms):
    if row['type'] not in TYPES or row['deleted_at'] is not None:return None
    try: payload=json.loads(row['payload_json'] or '{}')
    except (ValueError,TypeError): return 'blocking'
    if not isinstance(payload,dict):return 'blocking'
    if payload.get('attention_kind')=='janitor_failure':return None
    if row['type']=='countdown':
        # No retention/default is selected here. Explicit future settings must
        # remain server-authoritative; payload text cannot grant a policy.
        from .settings_store import get_text
        projection=countdown_attention.project({**payload,**dict(row),'server_id':get_text('server_instance_id') or 'local-host'},now_ms=now_ms)
        return {'correction':'blocking','review':'review','working':'working'}.get(projection['bucket'])
    status=row['status']
    if status in {'draft','cancelled','expired'}:return None
    if status=='failed' or (row['type']=='workflow_run' and payload.get('conclusion') in FAILURES):return 'blocking'
    if status=='active':return 'working'
    if status in {'ready','completed'}:return 'review'
    return None

def page(*,limit=100,cursor=''):
    limit=max(1,min(int(limit),200))
    if len(cursor)>2048: raise ValueError("invalid attention cursor")
    # One SQLite read snapshot includes serialization, not only the ID query.
    now_ms=int(time.time()*1000)
    c=db.conn();c.execute('SAVEPOINT attention_read')
    try:
        marks=','.join('?' for _ in TYPES)
        rows=c.execute(f'''SELECT a.*,g.persona AS agent_name FROM artifacts a
            JOIN agents g ON a.agent_id=g.agent_id WHERE a.deleted_at IS NULL
            AND a.type IN ({marks}) ORDER BY a.created_at,a.artifact_id''',tuple(sorted(TYPES))).fetchall()
        # Select canonical workflow attempt from source updates before eligibility.
        # Distinct attempts never resolve each other; historical records stay intact.
        canonical={}
        for r in rows:
            key=('artifact',r['artifact_id'])
            if r['type']=='workflow_run':
                try:
                    p=json.loads(r['payload_json'] or '{}')
                    if p.get('provider') and p.get('repository') and p.get('run_id'):
                        key=('workflow',p['provider'],p['repository'],str(p['run_id']),str(p.get('run_attempt',1)))
                except (ValueError,TypeError,AttributeError):pass
            old=canonical.get(key)
            if old is None or (r['updated_at'],r['artifact_id'])>(old['updated_at'],old['artifact_id']):canonical[key]=r
        selected=[(r,bucket(r,now_ms)) for r in canonical.values()];selected=[(r,b) for r,b in selected if b]
        ranks={'blocking':0,'review':1,'working':2}
        selected.sort(key=lambda rb:(ranks[rb[1]],rb[0]['created_at'],rb[0]['artifact_id']))
        # Content, archive and source outcome changes invalidate pagination.
        revision=hashlib.sha256(json.dumps([dict(r) for r,_ in selected],sort_keys=True,default=str).encode()).hexdigest()
        offset=0
        if cursor:
            try:
                raw=json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if set(raw)!={'revision','offset'} or type(raw['offset']) is not int or raw['offset']<0:raise ValueError()
            except Exception as e:raise ValueError('invalid attention cursor') from e
            if raw['revision']!=revision:raise StaleCursor('attention snapshot changed; restart pagination')
            offset=raw['offset']
        result=[]
        for r,b in selected[offset:offset+limit]:
            public=artifacts._public(r);public['attention_bucket']=b;result.append(public)
        end=offset+len(result)
        next_cursor=base64.urlsafe_b64encode(json.dumps({'revision':revision,'offset':end}).encode()).decode() if end<len(selected) else None
        return {'artifacts':result,'next_cursor':next_cursor,'snapshot_revision':revision,'total':len(selected),'index_version':1}
    finally:c.execute('RELEASE SAVEPOINT attention_read')
