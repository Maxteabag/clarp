"""Host-owned candidate policy services. No credentials, inference or process control.

Settings use the existing Host transaction/database. Default behavior is unchanged.
"""
from __future__ import annotations
import hashlib
import json
import math
from . import db, janitors, settings_store

KEY = 'janitors.design_policy.v1'

def configuration():
    return json.loads(settings_store.get_text(KEY, default='{"revision":0,"heartbeat_gate":false,"quota_threshold":25,"quota_ttl_seconds":300}'))

def configure(value, expected_revision):
    if not isinstance(value, dict) or set(value)-{'heartbeat_gate','quota_threshold','quota_ttl_seconds','model_chain'}:
        raise ValueError('Unsupported policy fields')
    if 'heartbeat_gate' in value and not isinstance(value['heartbeat_gate'],bool):
        raise ValueError('heartbeat_gate must be boolean')
    for key,low,high in [('quota_threshold',0,100),('quota_ttl_seconds',1,86400)]:
        if key in value and (type(value[key]) is not int or not low<=value[key]<=high):raise ValueError('Invalid '+key)
    if 'model_chain' in value:
        chain=value['model_chain']
        if not isinstance(chain,list) or not chain:raise ValueError('At least one model is required')
        for model in chain:
            if not isinstance(model,dict) or set(model)!={'provider','model'} or any(not isinstance(v,str) or not v.strip() or len(v)>200 for v in model.values()):raise ValueError('Each model needs provider and model identifiers')
        if len({json.dumps(v,sort_keys=True) for v in chain})!=len(chain):raise ValueError('Duplicate model')
    with janitors._write():
        current=configuration()
        if type(expected_revision) is not int or current['revision']!=expected_revision:raise janitors.JanitorError('Policy changed; refresh before saving',409,'revision_conflict')
        current.update(value);current['revision']+=1
        settings_store.set_text(KEY,json.dumps(current,sort_keys=True))
    return current

def commitment_for(agent_id):
    """Read actual durable plan, never infer a commitment from a failure message."""
    row=db.conn().execute('SELECT p.plan_id,p.updated_at FROM task_plans p WHERE p.agent_id=? AND p.status=\'active\' AND EXISTS (SELECT 1 FROM task_items i WHERE i.plan_id=p.plan_id AND i.status IN (\'pending\',\'in_progress\'))',(agent_id,)).fetchone()
    return dict(row) if row else None

def heartbeat_allowed(agent_id):
    return not configuration().get('heartbeat_gate',False) or commitment_for(agent_id) is not None

def observe_quota(value):
    """Account/window attributed observations produce persisted threshold receipts.
    Trusted adapter must supply observations. This never switches accounts.
    """
    required={'provider','account_id','window_id','remaining_percent','observed_at_ms'}
    if not isinstance(value,dict) or set(value)!=required:raise ValueError('Expected attributed quota observation')
    for key in ['provider','account_id','window_id']:
        if not isinstance(value[key],str) or not value[key] or len(value[key])>200:raise ValueError('Invalid identity')
    remaining=value['remaining_percent'];stamp=value['observed_at_ms']
    if isinstance(remaining,bool) or not isinstance(remaining,(int,float)) or not math.isfinite(remaining) or not 0<=remaining<=100 or type(stamp) is not int:raise ValueError('Invalid quota value')
    key='janitors.quota.'+hashlib.sha256(json.dumps([value[k] for k in ['provider','account_id','window_id']],separators=(',',':')).encode()).hexdigest()
    with janitors._write():
        config=configuration();now=db.now_ms()
        if not 0<=now-stamp<=config['quota_ttl_seconds']*1000:return {'status':'unknown','reason':'stale_or_future','notify':False}
        prior=json.loads(settings_store.get_text(key,default='null'))
        if prior and stamp<=prior['observed_at_ms']:return {'status':'ignored','reason':'duplicate_or_out_of_order','notify':False}
        notify=remaining<=config['quota_threshold'] and (prior is None or prior['remaining_percent']>config['quota_threshold'])
        receipt={**value,'notify':notify,'status':'known','policy_revision':config['revision']}
        settings_store.set_text(key,json.dumps(receipt,sort_keys=True))
    return receipt

def effective_chain(session):
    current=janitors.get(session)
    if not current:raise janitors.JanitorError('Janitor unavailable',404,'not_found')
    # Explicit existing model is preserved, never silently migrated to inheritance.
    if current.get('model'):
        return {'source':'explicit','chain':[{'provider':current.get('backend'),'model':current['model']}], 'revision':current['revision']}
    config=configuration()
    return {'source':'global','chain':config.get('model_chain',[]),'revision':config['revision']}

def inspect_receipt(run_id, target_session):
    """Current Host ownership/scope is checked before disclosing retained effects."""
    run=janitors.get_run(run_id)
    target=db.conn().execute('SELECT * FROM agents WHERE session=? AND archived_at IS NULL',(target_session,)).fetchone()
    if not run or not target:raise janitors.JanitorError('Receipt unavailable',403,'scope_denied')
    config=janitors.get(run['session'])
    if not config or not janitors._in_scope(config['scope'],dict(target)):
        raise janitors.JanitorError('Receipt unavailable',403,'scope_denied')
    return {'run_id':run_id,'results':[r for r in run['results'] if r['target_agent_id']==target['agent_id']]}

class ModelUnavailableBeforeExecution(Exception):
    """Adapter certifies no model execution/effect began; a next model is safe."""

def compute_with_model_chain(session, request, compute):
    """Ordered model fallback. Unknown failures never authorize another attempt.

    The outer demand run owns the single claim and publication transaction;
    this function only selects computation adapters under that frozen claim.
    """
    chain=effective_chain(session)
    if not chain['chain']:raise ValueError('No effective model configured')
    attempts=[]
    for model in chain['chain']:
        try:
            result=compute(request,dict(model))
            return result, {'source':chain['source'],'revision':chain['revision'],'attempts':attempts+[{'model':model,'outcome':'completed'}]}
        except ModelUnavailableBeforeExecution:
            attempts.append({'model':model,'outcome':'unavailable_before_execution'})
    raise ModelUnavailableBeforeExecution('Every eligible model unavailable before execution')
