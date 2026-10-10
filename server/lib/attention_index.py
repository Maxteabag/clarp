"""Deterministic, source-owned artifact attention read model.
No inferred completion or age-based retention. Cursors restart on source changes.
Decisions and Janitor failures retain their dedicated authority endpoints.
"""
import base64,hashlib,json,time
from . import db,artifacts,countdown_attention,quiet_agents
# workflow_run is deliberately absent: a CI result is not something the owner
# can act on from Updates. An agent blocked by a failed run raises a question.
TYPES={'countdown','document','research','file','audio','video','code_change','data','release','directory','html_form'}
FAILURES={'failure','timed_out','action_required','startup_failure'}
class StaleCursor(ValueError): pass

def bucket(row, now_ms):
    if row['type'] not in TYPES or row['deleted_at'] is not None:return None
    try: payload=json.loads(row['payload_json'] or '{}')
    except (ValueError,TypeError): return 'blocking'
    return _bucket(row, payload, now_ms)

def _bucket(row, payload, now_ms):
    """bucket() for a parsed payload; `payload` may hold only the keys used
    here (attention_kind, conclusion) unless the row is a countdown."""
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

# Payload fields bucket() and the workflow canonical key read, extracted by
# SQLite so a page never loads every eligible artifact's payload. On the
# 2026-10-10 database that load (33.7 MB), its parse and a hash of all of it
# cost 156-233 MB of allocation per 200-row page.
_FIELDS=('attention_kind','conclusion')
# CASE, not AND: SQLite may evaluate json_type() on a payload that is not JSON.
_OBJECT="CASE WHEN json_valid(a.payload_json) THEN json_type(a.payload_json)='object' ELSE 0 END"
_LITE=', '.join(f"CASE WHEN {_OBJECT} THEN json_extract(a.payload_json,'$.{k}') END AS pj_{k}" for k in _FIELDS)

def page(*,limit=100,cursor='',representation=''):
    if representation not in ('', 'flat-v1'): raise ValueError('unsupported artifact representation')
    limit=max(1,min(int(limit),200))
    if len(cursor)>2048: raise ValueError("invalid attention cursor")
    # One SQLite read snapshot includes serialization, not only the ID query.
    now_ms=int(time.time()*1000)
    c=db.conn();c.execute('SAVEPOINT attention_read')
    try:
        marks=','.join('?' for _ in TYPES)
        columns=[r[1] for r in c.execute('PRAGMA table_info(artifacts)') if r[1]!='payload_json']
        # A quiet agent's (a Janitor's) artifacts stay in its chat; its failure
        # alerts reach Updates through janitor_attention, not this index.
        rows=c.execute(f'''SELECT {','.join('a.'+k for k in columns)},g.persona AS agent_name,
            {_OBJECT} AS pj_object,{_LITE}
            FROM artifacts a JOIN agents g ON a.agent_id=g.agent_id WHERE a.deleted_at IS NULL
            AND {quiet_agents.loud_sql('g')}
            AND a.type IN ({marks}) ORDER BY a.created_at,a.artifact_id''',tuple(sorted(TYPES))).fetchall()
        # Select canonical workflow attempt from source updates before eligibility.
        # Distinct attempts never resolve each other; historical records stay intact.
        canonical={}
        for r in rows:
            key=('artifact',r['artifact_id'])
            if r['type']=='workflow_run':
                try:
                    p=json.loads(_payload(c,r['artifact_id']) or '{}')
                    if p.get('provider') and p.get('repository') and p.get('run_id'):
                        key=('workflow',p['provider'],p['repository'],str(p['run_id']),str(p.get('run_attempt',1)))
                except (ValueError,TypeError,AttributeError):pass
            old=canonical.get(key)
            if old is None or (r['updated_at'],r['artifact_id'])>(old['updated_at'],old['artifact_id']):canonical[key]=r
        selected=[(r,_lite_bucket(c,r,now_ms)) for r in canonical.values()];selected=[(r,b) for r,b in selected if b]
        ranks={'blocking':0,'review':1,'working':2}
        selected.sort(key=lambda rb:(ranks[rb[1]],rb[0]['created_at'],rb[0]['artifact_id']))
        # Content, archive and source outcome changes invalidate pagination.
        # Every payload write bumps updated_at (artifacts._apply_update), so the
        # row's other columns and its bucket stand for its content.
        revision=hashlib.sha256(json.dumps(
            [{**{k:r[k] for k in columns},'agent_name':r['agent_name'],'bucket':b} for r,b in selected],
            sort_keys=True,default=str).encode()).hexdigest()
        offset=0
        if cursor:
            try:
                raw=json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if set(raw)!={'revision','offset'} or type(raw['offset']) is not int or raw['offset']<0:raise ValueError()
            except Exception as e:raise ValueError('invalid attention cursor') from e
            if raw['revision']!=revision:raise StaleCursor('attention snapshot changed; restart pagination')
            offset=raw['offset']
        window=selected[offset:offset+limit]
        full=_full_rows(c,[r['artifact_id'] for r,_ in window])
        result=[]
        for r,b in window:
            public=artifacts.response_representation(artifacts._public(full[r['artifact_id']]),representation);public['attention_bucket']=b;result.append(public)
        end=offset+len(result)
        next_cursor=base64.urlsafe_b64encode(json.dumps({'revision':revision,'offset':end}).encode()).decode() if end<len(selected) else None
        return {'artifacts':result,'next_cursor':next_cursor,'snapshot_revision':revision,'total':len(selected),'index_version':1}
    finally:c.execute('RELEASE SAVEPOINT attention_read')

def _lite_bucket(c, row, now_ms):
    """bucket() from the extracted fields; a countdown's projection, or a
    payload SQLite cannot read as an object, takes the full payload."""
    if row['type']=='countdown' or not row['pj_object']:
        return bucket({**dict(row),'payload_json':_payload(c,row['artifact_id'])},now_ms)
    if row['type'] not in TYPES or row['deleted_at'] is not None:return None
    return _bucket(row,{k:row['pj_'+k] for k in ('attention_kind','conclusion') if row['pj_'+k] is not None},now_ms)

def _payload(c, artifact_id):
    return c.execute('SELECT payload_json FROM artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()[0]

def _full_rows(c, ids):
    if not ids:return {}
    return {r['artifact_id']:r for r in c.execute(f'''SELECT a.*,g.persona AS agent_name FROM artifacts a
        JOIN agents g ON a.agent_id=g.agent_id WHERE a.artifact_id IN ({','.join('?'*len(ids))})''',tuple(ids))}
