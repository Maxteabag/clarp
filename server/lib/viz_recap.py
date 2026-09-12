"""Bounded morning recap of durable records; no model calls or transcript replay."""
import json
import time
from urllib.parse import urlsplit
from . import viz_work

LIMIT = 200
DAY = 86400000


def safe_link(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        p = urlsplit(value)
        if p.scheme == 'https' and p.hostname and not p.username and not p.password:
            return value
    except ValueError:
        pass
    return None


def artifact(con, row, names):
    result = viz_work.artifact_record(con, row, names)
    payload = viz_work._payload(row['payload_json'])
    result['link'] = result.get('media_url') or safe_link(payload.get('run_url')) or safe_link(payload.get('url'))
    result['readable'] = isinstance(payload.get('content'), str) and bool(payload['content'])
    return result


def content(con, artifact_id):
    row = con.execute('SELECT type,title,payload_json FROM artifacts WHERE artifact_id=? AND deleted_at IS NULL', (artifact_id,)).fetchone()
    if not row:
        return None
    payload = viz_work._payload(row['payload_json'])
    return {'type': row['type'], 'title': row['title'], 'content': str(payload.get('content', '')),
            'sources': [{'title': str(s.get('title', 'Source'))[:160], 'url': safe_link(s.get('url'))}
                        for s in payload.get('sources', [])[:30] if isinstance(s, dict)] if isinstance(payload.get('sources'), list) else []}


def build(con, since, until, now=None):
    now = int(time.time()*1000) if now is None else now
    if since < 0 or until <= since or until > now+60000 or until-since > 90*DAY:
        raise ValueError('Choose a time window of up to 90 days, ending no later than now.')
    people = {r['agent_id']: dict(r) for r in con.execute('SELECT agent_id,persona,session,avatar_path,archived_at,deleted_at FROM agents')}
    names = {k: v['persona'] for k, v in people.items()}
    groups = {}
    def group(agent_id, session=''):
        key = agent_id or 'session:'+session
        if key not in groups:
            person = people.get(agent_id, {})
            groups[key] = {'id': key, 'agent_id': agent_id, 'name': person.get('persona') or session or 'Unknown agent',
                           'session': session or person.get('session', ''), 'archived': bool(person.get('archived_at') or person.get('deleted_at')),
                           'avatar_url': '/avatars/'+agent_id if person.get('avatar_path') else '',
                           'artifacts': [], 'plans': [], 'attention': [], 'request': None, 'latest': 0}
        return groups[key]
    truncated = []
    def rows(kind, sql, params):
        values = con.execute(sql+' LIMIT ?', (*params, LIMIT+1)).fetchall()
        if len(values)>LIMIT:
            truncated.append(kind)
        return values[:LIMIT]
    outputs = rows('artifacts', "SELECT * FROM artifacts WHERE deleted_at IS NULL AND type NOT IN ('plan','live_task','question','decision') AND max(created_at,updated_at) BETWEEN ? AND ? ORDER BY max(created_at,updated_at) DESC", (since, until))
    for row in outputs:
        g = group(row['agent_id'], row['session'])
        g['artifacts'].append(artifact(con, row, names))
        g['latest'] = max(g['latest'], row['created_at'], row['updated_at'])
    requests = rows('agents with requests', "SELECT agent_id,session,observed_at,original_text FROM (SELECT agent_id,session,observed_at,original_text,ROW_NUMBER() OVER (PARTITION BY agent_id ORDER BY observed_at DESC,admission_id DESC) AS rank FROM prompt_admissions WHERE origin IN ('user','oracle') AND sender_agent_id='' AND observed_at BETWEEN ? AND ?) WHERE rank=1 ORDER BY observed_at DESC", (since, until))
    for row in requests:
        g = group(row['agent_id'], row['session'])
        if not g['request']:
            g['request'] = {'text': ' '.join((row['original_text'] or '').split())[:280], 'at': row['observed_at']}
        g['latest'] = max(g['latest'], row['observed_at'])
    plans = rows('plans', 'SELECT * FROM task_plans WHERE max(created_at,updated_at) BETWEEN ? AND ? ORDER BY updated_at DESC', (since, until))
    # Reviewing yesterday's outputs must not hide still-open work next morning.
    unfinished = rows('unfinished plans', "SELECT * FROM task_plans WHERE status NOT IN ('completed','cancelled','failed') ORDER BY updated_at DESC", ())
    known = {r['plan_id'] for r in plans}
    for row in [*plans, *(r for r in unfinished if r['plan_id'] not in known)]:
        g = group(row['agent_id'], row['session'])
        items = con.execute("SELECT title FROM task_items WHERE plan_id=? AND status='in_progress' ORDER BY position LIMIT 1", (row['plan_id'],)).fetchone()
        g['plans'].append({'id': row['plan_id'], 'title': row['title'], 'status': row['status'], 'updated_at': row['updated_at'], 'current': items['title'] if items else None})
        g['latest'] = max(g['latest'], row['updated_at'])
    attention = rows('decisions', "SELECT a.agent_id,a.session,a.title,d.decision_id,d.blocks_progress,a.created_at FROM artifact_decisions d JOIN artifacts a USING(artifact_id) WHERE d.status='pending' AND a.deleted_at IS NULL AND (d.expires_at IS NULL OR d.expires_at>?) ORDER BY a.created_at DESC", (now,))
    for row in attention:
        g = group(row['agent_id'], row['session'])
        g['attention'].append({'id': row['decision_id'], 'title': row['title'], 'blocks_progress': bool(row['blocks_progress'])})
    result = sorted(groups.values(), key=lambda g: (any(a['type']!='workflow_run' for a in g['artifacts']), bool(g['artifacts']), g['latest']), reverse=True)
    return {'since': since, 'until': until, 'generated_at': now, 'groups': result, 'truncated': truncated,
            'counts': {'agents': len(result), 'artifacts': len(outputs), 'attention': len(attention)},
            'coverage': 'Published artifacts, recorded requests and declared task status. Files not published as artifacts may be missing. Open tasks and decisions show their current status.'}
